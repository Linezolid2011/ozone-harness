"""可移植性测试：这套东西拷到别人的机器上还能不能跑。

这些断言看着琐碎，但每一条都对应一个真实会踩的坑：

* **代码里不能有写死的绝对路径** —— 同学的盘符和目录名跟我不一样；
* **启动脚本必须是纯 ASCII** —— Windows PowerShell 5.1 读没有 BOM 的 `.ps1`
  会按 ANSI 解码，中文注释被解坏后会变成语法错误（真踩过）；
  `run.cmd` 同理，cmd.exe 按 OEM 代码页读批处理；
* **`HARNESS_DATA_DIR` 填相对路径时按项目根解析** —— 否则"在项目里跑"
  和"在别处跑"会指向两个不同的数据目录，而用户以为看的是同一份数据。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from harness.config import ROOT, resolve_data_dir

from .helpers import test_settings

# 会被"拷给别人"的文件。刻意**不含** tests/ 的用例文件：测试里可以出现
# `file:///C:/...`、`C:/x.md`、`..\\..\\victim` 这种字面量（那是**被测对象**，
# 不是环境依赖）。但**测试的基础设施**必须能换台机器跑，所以那两个单独列出来。
#
# 用 `**` 而不是 `*`：子目录也要扫。这条**踩过一次** —— 原来 web/ 与 scripts/
# 只写了单层，于是后来新增的 `scripts/ui_check/`（.ps1 / .mjs / .md）整个落在守卫
# 之外，里面恰好就留着一个 `D:\test2\...` 的示例路径，谁也没拦住。
# `test_new_subdirectories_are_covered` 就是给这个缺口上锁的。
SHIPPED_GLOBS = (
    "harness/**/*.py",
    "scripts/**/*.py",
    "scripts/**/*.ps1",
    "scripts/**/*.mjs",
    "scripts/**/*.md",
    "scripts/**/*.js",
    "web/**/*.js",
    "web/**/*.html",
    "web/**/*.css",
    "docs/*.md",
    "*.md",
    "*.cmd",
    "*.ps1",
    "*.txt",
    ".env.example",
    "tests/__init__.py",
    "tests/helpers.py",
)

# 盘符路径、UNC、以及我的用户名。
# 注意第一个正则的 lookbehind：它要求盘符前面**不是**字母/数字/`+.-`，
# 否则 `https://api.example.com` 里的 `s:/` 会被当成盘符路径（第一版就这么误报了一堆）。
ABSOLUTE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/]"),  # D:\... 或 D:/...
    re.compile(r"\\\\[A-Za-z0-9_.-]+\\"),               # UNC \\server\share
)

# 反斜杠在这些文件里是**转义字符**：JS 源码里写一个路径要用 `\\`。
# 不归一化的话，`.tmp-ui-check\\data\\vault`（一个正常的相对路径）会被 UNC 正则
# 当成 `\\data\` 而误报。归一化之后看的是"路径本身"，与它在哪种语言里怎么转义无关。
ESCAPING_SUFFIXES = {".js", ".mjs", ".cjs", ".json", ".ts"}


def normalize_for_scan(path: Path, text: str) -> str:
    """把某几种语言里的转义反斜杠还原成单个，免得误报（也会漏报真 UNC，见测试）。"""
    if path.suffix.lower() in ESCAPING_SUFFIXES:
        return text.replace("\\\\", "\\")
    return text


class DataDirResolutionTests(unittest.TestCase):
    def test_relative_path_resolves_under_the_project_root(self) -> None:
        """相对路径按**项目根**解析，不跟当前工作目录走。"""
        resolved = resolve_data_dir({"HARNESS_DATA_DIR": "data-lab"})
        self.assertEqual(resolved, ROOT / "data-lab")
        self.assertTrue(resolved.is_absolute())

    def test_absolute_path_is_respected(self) -> None:
        # 用 ROOT.parent 造一个**当前平台上真算绝对**的路径。
        # （别用 "/tmp/x"：Windows 上那不是绝对路径，会掉进相对分支。第一版就这么误判过。）
        absolute = ROOT.parent / "elsewhere"
        self.assertTrue(absolute.is_absolute())
        self.assertEqual(
            resolve_data_dir({"HARNESS_DATA_DIR": str(absolute)}), absolute
        )

    def test_default_is_the_projects_data_dir(self) -> None:
        self.assertEqual(resolve_data_dir({}), ROOT / "data")

    def test_blank_value_falls_back_to_default(self) -> None:
        self.assertEqual(resolve_data_dir({"HARNESS_DATA_DIR": "   "}), ROOT / "data")

    def test_load_uses_the_same_rule(self) -> None:
        from harness.config import Settings

        settings = Settings.load()
        self.assertTrue(settings.data_dir.is_absolute(), "数据目录必须是绝对路径")


class NoHardCodedPathsTests(unittest.TestCase):
    """仓库里不该出现写死的绝对路径。

    这条测试是有来历的：`run.ps1` 曾经写死了
    `C:\\Users\\<我>\\.dsh\\dsh-runtimes\\...\\python.exe`，
    换台机器直接就是坏的 —— 而且是那种"看起来能跑"的坏。
    """

    def _shipped_files(self) -> list[Path]:
        files: list[Path] = []
        for pattern in SHIPPED_GLOBS:
            files.extend(p for p in ROOT.glob(pattern) if p.is_file())
        return sorted(set(files))

    def test_no_absolute_paths_in_shipped_files(self) -> None:
        offenders: list[str] = []
        for path in self._shipped_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(
                normalize_for_scan(path, text).splitlines(), 1
            ):
                for pattern in ABSOLUTE_PATTERNS:
                    if pattern.search(line):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{number}  {line.strip()[:90]}"
                        )
                        break
        self.assertEqual(
            offenders,
            [],
            "这些文件里出现了绝对路径，换台机器就不好使了：\n  " + "\n  ".join(offenders),
        )

    def test_shipped_files_were_actually_scanned(self) -> None:
        """防止 glob 写错导致上面那条测试变成"空跑也通过"。"""
        files = self._shipped_files()
        names = {p.name for p in files}
        for expected in ("config.py", "app.js", "run.cmd", "run.ps1", ".env.example"):
            self.assertIn(expected, names, f"没扫到 {expected}，SHIPPED_GLOBS 有问题")
        self.assertGreater(len(files), 30)

    def test_new_subdirectories_are_covered(self) -> None:
        """子目录里的文件也必须被扫到 —— 这个缺口真的漏过一个 `D:\\test2\\...`。

        原来 web/ 与 scripts/ 的 glob 只写一层，于是 `scripts/ui_check/` 整个在守卫之外。
        所以这里逐个点名**具体的文件**，而不是只数个数：数个数那种断言在
        "多了十个文件、少了三个"时照样通过。
        """
        scanned = {str(p.relative_to(ROOT)).replace("\\", "/") for p in self._shipped_files()}
        for expected in (
            "scripts/ui_check/seed-vault.ps1",
            "scripts/ui_check/dom-stub.test.mjs",
            "scripts/ui_check/e2e-live.mjs",
            "scripts/ui_check/README.md",
            "scripts/probe_effort.py",
            "web/app.js",
        ):
            self.assertIn(expected, scanned, f"{expected} 没被扫到 —— 它可以在里面写死绝对路径")

    def test_escaped_backslashes_are_not_reported_as_unc(self) -> None:
        """JS 里的 `\\` 是转义，不该被当成 UNC 而误报（假红会让人开始怀疑正确的行为）。"""
        fake = ROOT / "web" / "_scan_probe.mjs"
        self.assertEqual(
            normalize_for_scan(fake, "path: '.tmp-ui-check\\\\data\\\\vault',"),
            "path: '.tmp-ui-check\\data\\vault',",
        )
        # 归一化之后：相对路径不报，真 UNC 照报
        normalized = normalize_for_scan(fake, "'\\\\\\\\server\\\\share'")
        self.assertTrue(
            any(p.search(normalized) for p in ABSOLUTE_PATTERNS), "真 UNC 应当仍然被报出来"
        )
        self.assertFalse(
            any(p.search(normalize_for_scan(fake, "path: '.tmp-ui-check\\\\data',")) for p in ABSOLUTE_PATTERNS)
        )


class RuntimeArtifactsArePortableTests(unittest.TestCase):
    """**代码里没有绝对路径 ≠ 产物里没有。**

    这条更隐蔽：会话日志、运行记录、轨迹、记忆库……都可能顺手把"数据目录在哪"
    写进去。那样一来，`data/` 拷给别人（或者上传、或者贴进 issue）就带着我这台机器的
    盘符与用户名 —— 而且是在**用户看不见的地方**。

    ADR-042 已经为运行记录定过这条规矩（只存目录名、不存绝对路径），但那是靠人记住的。
    这里让它在每次跑测试时被机械地检查一遍：造一个临时数据目录、跑一轮真流程
    （会话 + 记忆 + 一次**预演**整理，不花钱），然后把 data/ 下所有文本文件扫一遍。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.settings = test_settings(self.root)

    def _write_some_artifacts(self) -> None:
        from harness.dream import DreamRunner
        from harness.events import EventLog
        from harness.memory import open_memory
        from harness.vault import VaultStore

        from .helpers import FakeProvider, text_reply

        session_id = "s-20261012-100000-abcd"
        log = EventLog(self.settings.sessions_dir / f"{session_id}.jsonl", session_id)
        log.append("session_start", provider="deepseek", model="deepseek-flash")
        log.append("user_message", text="我在做一个记忆系统的项目")
        log.append("assistant_message", message={"content": "嗯"})
        log.append("prompt_built", estimated_tokens=100)
        log.close()

        memory = open_memory(self.settings)
        self.addCleanup(memory.close)
        memory.create("用户在做一个记忆系统的项目")

        vault = VaultStore(self.settings.vault_dir)
        vault.write_file("sections/项目.md", "---\ntitle: 项目\n---\n\n记忆系统。\n")
        runner = DreamRunner(
            provider=FakeProvider([text_reply("看过记录，没什么要改的。")]),
            settings=self.settings,
            memory=memory,
            vault=vault,
        )
        runner.run()

    def test_no_absolute_paths_in_runtime_artifacts(self) -> None:
        self._write_some_artifacts()

        machine_path = str(self.root)  # 这个临时目录的绝对路径
        offenders: list[str] = []
        scanned = 0
        for path in sorted(self.settings.data_dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # SQLite 之类不是文本，跳过（它的内容我们也管不着）
            scanned += 1
            rel = path.relative_to(self.settings.data_dir)
            for number, line in enumerate(text.splitlines(), 1):
                if machine_path and machine_path in line:
                    offenders.append(f"{rel}:{number} 出现了数据目录的绝对路径")
                    break
                if any(p.search(line) for p in ABSOLUTE_PATTERNS):
                    offenders.append(f"{rel}:{number}  {line.strip()[:90]}")
                    break

        self.assertGreater(scanned, 2, "没扫到几个文件，这条测试可能空跑了")
        self.assertEqual(
            offenders,
            [],
            "运行时产物里出现了绝对路径 —— 把 data/ 拷给别人就带着这台机器的盘符/用户名：\n  "
            + "\n  ".join(offenders),
        )


class LauncherEncodingTests(unittest.TestCase):
    """启动脚本必须纯 ASCII（两个 shell 的编码坑都踩过）。"""

    def test_launchers_are_pure_ascii(self) -> None:
        for name in ("run.cmd", "run.ps1", "start.cmd"):
            path = ROOT / name
            data = path.read_bytes()
            bad = [i for i, byte in enumerate(data) if byte > 127]
            self.assertEqual(
                bad,
                [],
                f"{name} 里有 {len(bad)} 个非 ASCII 字节 —— "
                "cmd.exe 按 OEM 代码页读 .cmd、PowerShell 5.1 按 ANSI 读无 BOM 的 .ps1，"
                "中文注释会被解坏甚至变成语法错误",
            )

    def test_launchers_do_not_pin_python_by_path(self) -> None:
        """启动脚本只能按名字找 Python，不能写死某个 python.exe 的路径。"""
        for name in ("run.cmd", "run.ps1", "start.cmd"):
            text = (ROOT / name).read_text(encoding="ascii", errors="replace")
            self.assertNotIn(".dsh-runtimes", text, name)
            self.assertRegex(text, r"\bpy\b", f"{name} 应当优先用 py 启动器")


if __name__ == "__main__":
    unittest.main()
