"""双击启动时的可选 venv 检查与事务式安装。

这条路径只由 ``start.cmd`` 的引导模式启用；``run.cmd`` 的稳定行为不变。
HTTP 层只认识这里的状态快照，不直接操作 pip 或文件。

为什么要有事务日志：venv 里的脚本会记住创建路径，不能先在临时目录建完再改名。
因此旧 ``.venv`` 先搬进同级事务目录，新环境始终在最终路径创建；取消或失败时
删掉这次的新环境并恢复旧环境，崩溃后下次启动也按日志完成同一套恢复。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import signal
import site
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .atomic_io import write_text_atomic

OFFICIAL_PYPI = "https://pypi.org/simple"
STAGE_PREFIX = ".venv-download-"
_PROBE_MARKER = "__O3ZONE_VENV_PROBE__"

# 发行名与真正要导入的模块不是同一种名字（Pillow 是最明显的一个）。
# 只查 dist metadata 会漏掉"目录还在、文件已经坏了"的环境，所以两项都查。
_IMPORT_NAMES = {
    "httpx": "httpx",
    "trafilatura": "trafilatura",
    "lxml": "lxml.etree",
    "pillow": "PIL.Image",
    "rapidocr-onnxruntime": "rapidocr_onnxruntime",
}

_ACTIVE_STATES = {"preparing", "downloading", "paused", "verifying"}
_SIZE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class SetupError(RuntimeError):
    """给 Web 层的可预期失败；code 供前端翻译，detail 保留技术原因。"""

    def __init__(
        self, code: str, detail: str = "", *, http_status: int = 400
    ) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.http_status = http_status


class _DownloadCancelled(Exception):
    pass


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    missing: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    site_packages: str = ""
    python_version: tuple[int, int] = ()

    def public(self) -> dict[str, Any]:
        # 本机绝对路径只用于进程内激活，不放进状态接口。
        return {
            "ok": self.ok,
            "missing": list(self.missing),
            "issues": list(self.issues),
            "details": list(self.details),
        }


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_names(path: Path) -> list[str]:
    """读取本项目那份简单的 requirements.txt；不认识的行宁可报错。

    这里不是通用 requirements 解析器。真遇到 URL / editable / include 等新语法时，
    不能把包名猜错再宣布"完整"，而应让维护者把检查规则一起更新。
    """
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:[<>=!~;].*)?$", line)
        if match is None:
            raise ValueError(f"unsupported requirement line: {line[:120]}")
        names.append(_canonical_name(match.group(1)))
    return names


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


# 在**目标 venv 自己的解释器**里跑。父进程只读 JSON，绝不靠系统 Python 上
# 恰好装过的同名包判断目标环境是否完整。
_PROBE_SOURCE = r"""
import importlib
import importlib.metadata
import json
import sys
import sysconfig

required = json.loads(sys.argv[1])
result = {
    "missing": [],
    "details": [],
    "site_packages": sysconfig.get_path("purelib"),
    "python_version": list(sys.version_info[:2]),
    "prefix": sys.prefix,
}
for distribution, module_name in required:
    try:
        importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        result["missing"].append(distribution)
        continue
    except Exception as exc:
        result["details"].append(f"{distribution}: metadata: {type(exc).__name__}: {exc}")
        continue
    if not module_name:
        continue
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        result["details"].append(f"{module_name}: {type(exc).__name__}: {exc}")
print(""" + repr(_PROBE_MARKER) + r""" + json.dumps(result, ensure_ascii=True))
"""


def inspect_venv(venv: Path, requirements: Path, *, timeout: int = 90) -> IntegrityReport:
    """用目标解释器验包、验 import、验依赖闭包，并核对当前进程 ABI。"""
    try:
        names = requirement_names(requirements)
    except (OSError, ValueError) as exc:
        return IntegrityReport(False, issues=("requirements_unreadable",), details=(str(exc),))
    if not venv.is_dir():
        return IntegrityReport(False, tuple(names), ("venv_missing",))
    python = venv_python(venv)
    if not python.is_file():
        return IntegrityReport(False, tuple(names), ("python_missing",))

    pairs = [[name, _IMPORT_NAMES.get(name, "")] for name in names]
    try:
        probe = subprocess.run(
            [str(python), "-I", "-B", "-c", _PROBE_SOURCE, json.dumps(pairs)],
            cwd=venv.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return IntegrityReport(False, tuple(names), ("probe_failed",), (str(exc),))

    payload: dict[str, Any] = {}
    for line in probe.stdout.splitlines():
        if line.startswith(_PROBE_MARKER):
            try:
                candidate = json.loads(line[len(_PROBE_MARKER) :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
    if not payload:
        detail = (probe.stderr or probe.stdout).strip()[-1000:]
        return IntegrityReport(
            False,
            tuple(names),
            ("probe_failed",),
            (detail or f"venv probe exited with {probe.returncode}",),
        )

    missing = tuple(str(item) for item in payload.get("missing") or [])
    issues: list[str] = []
    details = [str(item) for item in payload.get("details") or []]
    if details:
        issues.append("import_failed")
    if probe.returncode:
        issues.append("probe_failed")
    version = payload.get("python_version") or []
    python_version = (0, 0)
    if len(version) == 2:
        try:
            python_version = (int(version[0]), int(version[1]))
        except (TypeError, ValueError):
            pass
    if python_version != tuple(sys.version_info[:2]):
        issues.append("python_mismatch")
        details.append(
            "venv Python {}.{} differs from running Python {}.{}".format(
                *python_version, *sys.version_info[:2]
            )
        )
    try:
        prefix_matches = Path(str(payload.get("prefix") or "")).resolve() == venv.resolve()
    except OSError:
        prefix_matches = False
    if not prefix_matches:
        issues.append("prefix_mismatch")
        details.append("the interpreter does not belong to the project .venv")

    try:
        checked = subprocess.run(
            [str(python), "-I", "-B", "-m", "pip", "check", "--disable-pip-version-check"],
            cwd=venv.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        issues.append("pip_check_failed")
        details.append(str(exc))
    else:
        if checked.returncode:
            issues.append("pip_check_failed")
            details.append((checked.stdout or "").strip()[-1000:])

    # 去重但保持顺序：同一次失败可能同时被 import 与 pip check 看见。
    issues = list(dict.fromkeys(issues))
    return IntegrityReport(
        not missing and not issues,
        missing,
        tuple(issues),
        tuple(details),
        str(payload.get("site_packages") or ""),
        python_version,
    )


def activate_venv(venv: Path, report: IntegrityReport) -> None:
    """把已验证的 site-packages 接入引导进程，不重载任何应用模块。

    所有可选第三方包在本项目里都是延迟 import，且 ``inspect_venv`` 已排除
    Python ABI 不一致；因此不必重启 Web 进程。激活发生在聊天 API 解锁之前，
    不存在"界面说装好了、实际仍从全局环境找包"的窗口。
    """
    if not report.ok or not report.site_packages:
        raise SetupError("activation_failed", http_status=500)
    if report.python_version != tuple(sys.version_info[:2]):
        raise SetupError("activation_failed", "Python ABI mismatch", http_status=500)
    packages = str(Path(report.site_packages).resolve())
    if not Path(packages).is_dir():
        raise SetupError("activation_failed", "site-packages is missing", http_status=500)
    # addsitedir 处理 wheel 附带的 .pth；随后移到最前，避免同名全局包抢占。
    site.addsitedir(packages)
    while packages in sys.path:
        sys.path.remove(packages)
    sys.path.insert(0, packages)
    importlib.invalidate_caches()
    os.environ["VIRTUAL_ENV"] = str(venv.resolve())
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    old_path = os.environ.get("PATH", "")
    if str(scripts) not in old_path.split(os.pathsep):
        os.environ["PATH"] = str(scripts) + (os.pathsep + old_path if old_path else "")


def normalize_mirror(value: Any) -> str:
    """空值 = 官方源；自定义源只接受不带凭据的 HTTP(S) index URL。"""
    text = str(value or "").strip()
    if not text:
        return OFFICIAL_PYPI
    if len(text) > 2048 or any(ch.isspace() for ch in text):
        raise SetupError("invalid_mirror")
    try:
        parts = urlsplit(text)
        _ = parts.port
    except ValueError as exc:
        raise SetupError("invalid_mirror", str(exc)) from exc
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "\\" in text
    ):
        raise SetupError("invalid_mirror")
    return parts.geturl()


def create_venv_command(stage: Path) -> list[str]:
    return [sys.executable, "-m", "venv", str(stage)]


def pip_install_command(stage: Path, requirements: Path, mirror: str) -> list[str]:
    """构造无 shell 的 pip 命令；空镜像已在 normalize_mirror 变成官方源。"""
    return [
        str(venv_python(stage)),
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--progress-bar",
        "on",
        "--index-url",
        mirror,
        "--requirement",
        str(requirements),
    ]


def suspend_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        raise SetupError("not_active", http_status=409)
    if os.name == "nt":
        import ctypes

        handle = int(getattr(process, "_handle", 0) or 0)
        if not handle:
            raise SetupError("pause_unavailable", http_status=500)
        # Popen 自己持有的句柄仍有全部权限；c_void_p 避免 64 位句柄被截断。
        status = ctypes.windll.ntdll.NtSuspendProcess(ctypes.c_void_p(handle))
        if status != 0:
            raise SetupError("pause_failed", f"NTSTATUS {status}", http_status=500)
        return
    process.send_signal(signal.SIGSTOP)


def resume_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        raise SetupError("not_active", http_status=409)
    if os.name == "nt":
        import ctypes

        handle = int(getattr(process, "_handle", 0) or 0)
        if not handle:
            raise SetupError("pause_unavailable", http_status=500)
        status = ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(handle))
        if status != 0:
            raise SetupError("resume_failed", f"NTSTATUS {status}", http_status=500)
        return
    process.send_signal(signal.SIGCONT)


def terminate_process(process: subprocess.Popen[str]) -> None:
    """取消时终止安装进程树；只按这次 Popen 的 PID 操作，绝不按名字杀。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        # pip 自举/build 可能有子进程。taskkill 的 /T 限定在这棵树上，/F 也能结束
        # 已 suspend 的进程；进程刚好先退出时返回非零是正常竞态，后面用 wait 收口。
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class VenvSetupManager:
    """线程安全的启动状态机。拒绝/取消只存在内存里，下次启动必重新检查。"""

    def __init__(self, root: Path, requirements: Path | None = None) -> None:
        self.root = Path(root)
        self.requirements = Path(requirements) if requirements else self.root / "requirements.txt"
        self.venv_path = self.root / ".venv"
        self._condition = threading.Condition(threading.RLock())
        self._state = "checking"
        self._integrity = IntegrityReport(False)
        self._progress: dict[str, Any] = {
            "percent": None,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "message": "",
        }
        self._source = ""
        self._error = ""
        self._error_code = ""
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._worker: threading.Thread | None = None
        self._lock_handle: Any = None
        self._lock_attempted = False

    def allows_chat(self) -> bool:
        with self._condition:
            return self._state in ("ready", "bypassed")

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "enabled": True,
                "state": self._state,
                "can_chat": self._state in ("ready", "bypassed"),
                "full_venv": self._state == "ready",
                "integrity": self._integrity.public(),
                "progress": dict(self._progress),
                "source": self._source,
                "error": self._error,
                "error_code": self._error_code,
            }

    def _spawn(self, target: Any) -> None:
        worker = threading.Thread(target=target, name="o3zone-venv-setup", daemon=True)
        self._worker = worker
        worker.start()

    def begin(self) -> None:
        with self._condition:
            if self._worker is not None:
                return
            self._acquire_project_lock()
            self._spawn(self._initial_check)

    def _acquire_project_lock(self) -> None:
        """OS 级文件锁阻止两个自动端口实例同时改 .venv；进程退出会自动释放。"""
        if self._lock_attempted:
            return
        self._lock_attempted = True
        handle = (self.root / ".venv-setup.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            return
        self._lock_handle = handle

    def _release_project_lock(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def wait_until(self, states: set[str], timeout: float = 10.0) -> dict[str, Any]:
        """测试与关闭路径使用；Web 端走轮询，不长时间占住 HTTP 请求。"""
        with self._condition:
            self._condition.wait_for(lambda: self._state in states, timeout=timeout)
            return self.snapshot()

    def _initial_check(self) -> None:
        try:
            if self._lock_handle is not None:
                self._recover_interrupted()
            report = inspect_venv(self.venv_path, self.requirements)
            if report.ok:
                try:
                    activate_venv(self.venv_path, report)
                except SetupError as exc:
                    report = IntegrityReport(
                        False,
                        report.missing,
                        tuple(dict.fromkeys((*report.issues, "activation_failed"))),
                        (*report.details, exc.detail or exc.code),
                        report.site_packages,
                        report.python_version,
                    )
        except Exception as exc:  # 检查失败也必须回到可选择的页面，不能白屏
            report = IntegrityReport(
                False, issues=("probe_failed",), details=(f"{type(exc).__name__}: {exc}",)
            )
        with self._condition:
            self._integrity = report
            self._state = "ready" if report.ok else "consent"
            self._progress["percent"] = 100 if report.ok else None
            self._condition.notify_all()

    def start_install(self, mirror: Any = "") -> dict[str, Any]:
        index_url = normalize_mirror(mirror)
        with self._condition:
            if self._state not in ("consent", "error"):
                raise SetupError("not_active", http_status=409)
            if self._lock_handle is None:
                raise SetupError("install_busy", http_status=409)
            self._state = "preparing"
            self._source = "official" if index_url == OFFICIAL_PYPI else "mirror"
            self._error = ""
            self._error_code = ""
            self._cancel_requested = False
            self._progress = {
                "percent": 0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "message": "",
            }
            self._spawn(lambda: self._install_worker(index_url))
            self._condition.notify_all()
            return self.snapshot()

    def skip(self) -> dict[str, Any]:
        with self._condition:
            if self._state not in ("consent", "error", "bypassed"):
                raise SetupError("not_active", http_status=409)
            self._state = "bypassed"
            self._error = ""
            self._error_code = ""
            self._condition.notify_all()
            return self.snapshot()

    def pause(self) -> dict[str, Any]:
        with self._condition:
            if self._state != "downloading" or self._process is None:
                raise SetupError("not_active", http_status=409)
            suspend_process(self._process)
            self._state = "paused"
            self._condition.notify_all()
            return self.snapshot()

    def resume(self) -> dict[str, Any]:
        with self._condition:
            if self._state != "paused" or self._process is None:
                raise SetupError("not_active", http_status=409)
            resume_process(self._process)
            self._state = "downloading"
            self._condition.notify_all()
            return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        with self._condition:
            if self._state not in _ACTIVE_STATES:
                raise SetupError("not_active", http_status=409)
            self._cancel_requested = True
            self._state = "cancelling"
            process = self._process
            self._condition.notify_all()
        if process is not None:
            terminate_process(process)
        return self.snapshot()

    def shutdown(self) -> None:
        """关服务器时别留下 pip 与半截暂存目录。"""
        with self._condition:
            active = self._state in _ACTIVE_STATES
            worker = self._worker
        if active:
            try:
                self.cancel()
            except SetupError:
                pass
        if worker is not None and worker.is_alive():
            worker.join(timeout=20)
        self._release_project_lock()

    def _check_cancel(self) -> None:
        with self._condition:
            if self._cancel_requested:
                raise _DownloadCancelled

    def _run_command(self, command: list[str], stage: Path, error_code: str) -> None:
        """跑 pip/venv 并逐行收输出；stdout 合并 stderr，错误不会只剩退出码。"""
        env = os.environ.copy()
        # 连 pip 的临时文件也限定在本次暂存目录里；取消时删除 stage 就覆盖它们。
        temp_dir = stage / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "TMPDIR": str(temp_dir),
                "TEMP": str(temp_dir),
                "TMP": str(temp_dir),
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise SetupError(error_code, str(exc), http_status=500) from exc
        with self._condition:
            self._process = process
            cancelling = self._cancel_requested
        if cancelling:
            terminate_process(process)
        output: list[str] = []
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                line = _ANSI.sub("", raw).replace("\r", "\n").strip()
                if line:
                    output.append(line[-500:])
                    output = output[-12:]
                if error_code == "download_failed":
                    self._record_download_output(line)
                if self._cancel_requested:
                    terminate_process(process)
            returncode = process.wait()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            with self._condition:
                if self._process is process:
                    self._process = None
                    self._condition.notify_all()
        self._check_cancel()
        if returncode:
            detail = "\n".join(output[-4:])[-1500:]
            raise SetupError(error_code, detail or f"process exited with {returncode}", http_status=500)

    def _record_download_output(self, line: str) -> None:
        percent: float | None = None
        downloaded = 0
        total = 0
        direct = re.search(r"(?<!\d)(\d{1,3})(?:\.\d+)?\s*%", line)
        if direct:
            percent = float(min(100, int(direct.group(1))))
        ratio = re.search(
            r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([kmg]?i?b)\b",
            line,
            re.IGNORECASE,
        )
        if ratio:
            unit = ratio.group(3).lower()
            multiplier = _SIZE_UNITS[unit]
            downloaded = int(float(ratio.group(1)) * multiplier)
            total = int(float(ratio.group(2)) * multiplier)
            if total:
                percent = min(100.0, downloaded * 100.0 / total)
        announced = re.search(r"\((\d+(?:\.\d+)?)\s*([kmg]?i?b)\)", line, re.IGNORECASE)
        if announced:
            total = int(float(announced.group(1)) * _SIZE_UNITS[announced.group(2).lower()])
        with self._condition:
            if percent is not None:
                # 100% 只留给最终验证通过；单个 wheel 下完不等于整个环境好了。
                self._progress["percent"] = min(99.0, round(percent, 1))
            if downloaded:
                self._progress["downloaded_bytes"] = downloaded
            if total:
                self._progress["total_bytes"] = total
            if line:
                self._progress["message"] = line[-300:]
            self._condition.notify_all()

    def _create_stage(self, stage: Path) -> None:
        """开事务并把新 venv 建在最终路径，避免 relocate 后脚本仍指向暂存路径。"""
        self._validate_stage(stage)
        stage.mkdir()
        had_old = self.venv_path.exists() or self.venv_path.is_symlink()
        self._write_journal(stage, had_old=had_old, committed=False)
        if had_old:
            os.replace(self.venv_path, stage / "old-venv")
        self._run_command(create_venv_command(self.venv_path), stage, "venv_create_failed")
        (self.venv_path / ".o3zone-installing").write_text(stage.name, encoding="ascii")

    def _install_worker(self, index_url: str) -> None:
        stage = self.root / f"{STAGE_PREFIX}{uuid.uuid4().hex}"
        committed = False
        try:
            self._create_stage(stage)
            self._check_cancel()
            with self._condition:
                if not self._cancel_requested:
                    self._state = "downloading"
                    self._condition.notify_all()
            self._run_command(
                pip_install_command(self.venv_path, self.requirements, index_url),
                stage,
                "download_failed",
            )
            self._check_cancel()
            with self._condition:
                self._state = "verifying"
                self._progress["percent"] = 95
                self._condition.notify_all()
            report = inspect_venv(self.venv_path, self.requirements)
            if not report.ok:
                raise SetupError(
                    "integrity_after_install",
                    "; ".join((*report.missing, *report.issues, *report.details))[-1500:],
                    http_status=500,
                )
            self._check_cancel()
            with self._condition:
                # 提交后不再接受取消：此刻已经不是"下载中"，不能回滚成另一份环境。
                self._state = "finalizing"
                self._progress["percent"] = 98
                self._condition.notify_all()
            self._commit(stage)
            committed = True
            activate_venv(self.venv_path, report)
            with self._condition:
                self._integrity = report
                self._state = "ready"
                self._progress["percent"] = 100
                self._condition.notify_all()
        except _DownloadCancelled:
            try:
                self._rollback(stage)
            except SetupError as exc:
                with self._condition:
                    self._state = "error"
                    self._error_code = exc.code
                    self._error = exc.detail
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._state = "bypassed"
                    self._error = ""
                    self._error_code = ""
                    self._condition.notify_all()
        except Exception as exc:
            if not committed:
                try:
                    self._rollback(stage)
                except SetupError as cleanup:
                    exc = SetupError(
                        "cleanup_failed",
                        f"{exc}; {cleanup.detail or cleanup.code}",
                        http_status=500,
                    )
            with self._condition:
                self._state = "error"
                self._error_code = exc.code if isinstance(exc, SetupError) else "install_failed"
                self._error = exc.detail if isinstance(exc, SetupError) else f"{type(exc).__name__}: {exc}"
                self._condition.notify_all()

    def _validate_stage(self, stage: Path) -> None:
        if stage.parent != self.root or not stage.name.startswith(STAGE_PREFIX):
            raise SetupError("cleanup_failed", "unsafe transaction path", http_status=500)

    def _write_journal(self, stage: Path, *, had_old: bool, committed: bool) -> None:
        self._validate_stage(stage)
        write_text_atomic(
            stage / "transaction.json",
            json.dumps(
                {
                    "version": 1,
                    "id": stage.name,
                    "target": ".venv",
                    "had_old": had_old,
                    "committed": committed,
                },
                ensure_ascii=True,
            ),
        )

    def _read_journal(self, stage: Path) -> dict[str, Any]:
        self._validate_stage(stage)
        try:
            data = json.loads((stage / "transaction.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupError("cleanup_failed", str(exc), http_status=500) from exc
        if (
            data.get("version") != 1
            or data.get("id") != stage.name
            or data.get("target") != ".venv"
            or not isinstance(data.get("had_old"), bool)
            or not isinstance(data.get("committed"), bool)
        ):
            raise SetupError("cleanup_failed", "invalid venv transaction journal", http_status=500)
        return data

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def _rollback(self, stage: Path) -> None:
        """撤掉本次最终路径安装并恢复旧环境；提交后只清事务文件。"""
        if not stage.exists():
            return
        data = self._read_journal(stage)
        backup = stage / "old-venv"
        try:
            if not data["committed"]:
                if data["had_old"] and not backup.exists():
                    # 旧目录的换名根本没成功：目标位置那份仍是旧环境，绝不能删。
                    pass
                else:
                    self._remove_path(self.venv_path)
                    if data["had_old"]:
                        os.replace(backup, self.venv_path)
            self._remove_path(stage)
        except OSError as exc:
            raise SetupError("cleanup_failed", str(exc), http_status=500) from exc

    def _recover_interrupted(self) -> None:
        """上次关窗/崩溃留下的未完成事务，在本次检查前恢复完。"""
        for stage in self.root.glob(f"{STAGE_PREFIX}*"):
            if stage.is_dir() and (stage / "transaction.json").is_file():
                self._rollback(stage)

    def _commit(self, stage: Path) -> None:
        """完整验证后落 committed 标记，再删旧备份与本次临时文件。"""
        data = self._read_journal(stage)
        if not data["committed"]:
            (self.venv_path / ".o3zone-installing").unlink(missing_ok=True)
            self._write_journal(stage, had_old=data["had_old"], committed=True)
        try:
            self._remove_path(stage)
        except OSError as exc:
            # 新环境已完整且 committed；清理失败不能伪装成下载失败或触发回滚。
            print(f"[setup] 新环境已就绪，但事务目录未能删除：{stage.name}（{exc}）", flush=True)
