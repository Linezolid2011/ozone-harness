"""引导式 venv 安装的状态机与 HTTP 合同；全部离线，不运行真 pip。"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from harness import venv_setup
from harness.config import ROOT
from harness.venv_setup import (
    OFFICIAL_PYPI,
    IntegrityReport,
    SetupError,
    VenvSetupManager,
    activate_venv,
    inspect_venv,
    normalize_mirror,
    pip_install_command,
    requirement_names,
)
from harness.web import HarnessHandler, HarnessServer

from .helpers import test_settings


class RequirementAndCommandTests(unittest.TestCase):
    def test_shipped_requirements_have_known_import_probes(self) -> None:
        names = requirement_names(ROOT / "requirements.txt")
        self.assertEqual(
            names,
            ["httpx", "trafilatura", "lxml", "pillow", "rapidocr-onnxruntime"],
        )
        for name in names:
            self.assertIn(name, venv_setup._IMPORT_NAMES)

    def test_blank_source_is_explicit_official_pypi(self) -> None:
        command = pip_install_command(Path("project/.venv"), Path("project/requirements.txt"), normalize_mirror(""))
        self.assertEqual(command[1:4], ["-m", "pip", "install"])
        self.assertEqual(command[command.index("--index-url") + 1], OFFICIAL_PYPI)
        self.assertIn("--isolated", command)
        self.assertIn("--no-cache-dir", command)
        self.assertNotIn("--extra-index-url", command)

    def test_mirror_is_the_only_index(self) -> None:
        mirror = "https://mirror.example/simple"
        command = pip_install_command(Path("project/.venv"), Path("project/requirements.txt"), mirror)
        self.assertEqual(command[command.index("--index-url") + 1], mirror)
        self.assertEqual(command.count("--index-url"), 1)
        self.assertNotIn(OFFICIAL_PYPI, command)

    def test_bad_mirrors_are_rejected_before_any_process(self) -> None:
        for value in (
            "ftp://mirror.example/simple",
            "https://user:secret@mirror.example/simple",
            "not a url",
            "https://mirror.example/simple?next=official",
            "javascript:alert(1)",
        ):
            with self.subTest(value=value), self.assertRaises(SetupError) as caught:
                normalize_mirror(value)
            self.assertEqual(caught.exception.code, "invalid_mirror")


class IntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("demo-package>=1\n", encoding="utf-8")

    def test_absent_venv_is_missing_not_a_subprocess_call(self) -> None:
        with mock.patch.object(venv_setup.subprocess, "run") as run:
            report = inspect_venv(self.root / ".venv", self.requirements)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing, ("demo-package",))
        self.assertEqual(report.issues, ("venv_missing",))
        run.assert_not_called()

    def test_activation_loads_a_new_lazy_package_from_the_verified_venv(self) -> None:
        """成功安装后无需重启的前提必须是真实 import 可用，而不只是改个状态。"""
        packages = self.root / ".venv" / "Lib" / "site-packages"
        packages.mkdir(parents=True)
        module_name = f"o3zone_activation_{int(time.time() * 1000)}"
        (packages / f"{module_name}.py").write_text("VALUE = 'from-venv'\n", encoding="utf-8")
        report = IntegrityReport(
            True,
            site_packages=str(packages),
            python_version=tuple(sys.version_info[:2]),
        )
        old_path = list(sys.path)
        old_virtual = os.environ.get("VIRTUAL_ENV")
        old_env_path = os.environ.get("PATH", "")
        try:
            activate_venv(self.root / ".venv", report)
            module = importlib.import_module(module_name)
            self.assertEqual(module.VALUE, "from-venv")
            self.assertTrue(str(Path(module.__file__ or "")).startswith(str(packages)))
        finally:
            sys.path[:] = old_path
            sys.modules.pop(module_name, None)
            if old_virtual is None:
                os.environ.pop("VIRTUAL_ENV", None)
            else:
                os.environ["VIRTUAL_ENV"] = old_virtual
            os.environ["PATH"] = old_env_path


class ProcessControlTests(unittest.TestCase):
    def test_real_local_process_supports_pause_resume_and_cancel(self) -> None:
        """不只 mock 按钮：平台控制函数必须能操作一个真实、离线的子进程。"""
        process = venv_setup.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=venv_setup.subprocess.DEVNULL,
            stdout=venv_setup.subprocess.DEVNULL,
            stderr=venv_setup.subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        try:
            venv_setup.suspend_process(process)
            self.assertIsNone(process.poll())
            venv_setup.resume_process(process)
            self.assertIsNone(process.poll())
            # 退出按钮必须也能结束**暂停中**的 pip，而不是等它自己恢复。
            venv_setup.suspend_process(process)
            venv_setup.terminate_process(process)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                venv_setup.terminate_process(process)


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("demo-package>=1\n", encoding="utf-8")
        self.managers: list[VenvSetupManager] = []

    def manager(self) -> VenvSetupManager:
        manager = VenvSetupManager(self.root, self.requirements)
        self.managers.append(manager)
        self.addCleanup(manager.shutdown)
        return manager

    @staticmethod
    def good_report(root: Path) -> IntegrityReport:
        return IntegrityReport(
            True,
            site_packages=str(root / ".venv" / "Lib" / "site-packages"),
            python_version=tuple(sys.version_info[:2]),
        )

    def test_integrity_success_unlocks_chat(self) -> None:
        manager = self.manager()
        good = self.good_report(self.root)
        with mock.patch.object(venv_setup, "inspect_venv", return_value=good), mock.patch.object(
            venv_setup, "activate_venv"
        ) as activate:
            manager.begin()
            status = manager.wait_until({"ready"})
        self.assertTrue(status["can_chat"])
        self.assertTrue(status["full_venv"])
        activate.assert_called_once()

    def test_skip_is_not_remembered_and_next_startup_asks_again(self) -> None:
        bad = IntegrityReport(False, ("demo-package",), ("venv_missing",))
        with mock.patch.object(venv_setup, "inspect_venv", return_value=bad):
            first = self.manager()
            first.begin()
            self.assertEqual(first.wait_until({"consent"})["state"], "consent")
            self.assertTrue(first.skip()["can_chat"])
            first.shutdown()

            second = self.manager()
            second.begin()
            status = second.wait_until({"consent"})
        self.assertEqual(status["state"], "consent")
        self.assertFalse(status["can_chat"])
        self.assertFalse((self.root / ".venv").exists())

    def test_success_uses_final_path_mirror_and_cleans_transaction(self) -> None:
        old = self.root / ".venv"
        old.mkdir()
        (old / "old.txt").write_text("old", encoding="utf-8")
        manager = self.manager()
        bad = IntegrityReport(False, ("demo-package",))
        good = self.good_report(self.root)
        commands: list[list[str]] = []

        def fake_run(command: list[str], stage: Path, error_code: str) -> None:
            commands.append(command)
            if "venv" in command:
                manager.venv_path.mkdir()
            else:
                (stage / "download-part.bin").write_bytes(b"new")
                (manager.venv_path / "downloaded.txt").write_text("new", encoding="utf-8")
                Path(good.site_packages).mkdir(parents=True)

        with mock.patch.object(venv_setup, "inspect_venv", side_effect=[bad, good]), mock.patch.object(
            venv_setup, "activate_venv"
        ), mock.patch.object(manager, "_run_command", side_effect=fake_run):
            manager.begin()
            manager.wait_until({"consent"})
            manager.start_install("https://mirror.example/simple")
            status = manager.wait_until({"ready"})
        self.assertEqual(status["state"], "ready")
        self.assertIn("https://mirror.example/simple", commands[-1])
        self.assertEqual(venv_setup.create_venv_command(manager.venv_path)[-1], str(manager.venv_path))
        self.assertTrue((manager.venv_path / "downloaded.txt").is_file())
        self.assertFalse((manager.venv_path / "old.txt").exists())
        self.assertEqual(list(self.root.glob(f"{venv_setup.STAGE_PREFIX}*")), [])

    def test_cancel_discards_new_content_and_restores_old_venv(self) -> None:
        old = self.root / ".venv"
        old.mkdir()
        (old / "old.txt").write_text("keep-me", encoding="utf-8")
        manager = self.manager()
        bad = IntegrityReport(False, ("demo-package",))
        entered_pip = threading.Event()

        def fake_run(command: list[str], stage: Path, error_code: str) -> None:
            if "venv" in command:
                manager.venv_path.mkdir()
                return
            (manager.venv_path / "partial.txt").write_text("download", encoding="utf-8")
            (stage / "partial.bin").write_bytes(b"download")
            entered_pip.set()
            deadline = time.monotonic() + 5
            while not manager._cancel_requested and time.monotonic() < deadline:
                time.sleep(0.01)

        with mock.patch.object(venv_setup, "inspect_venv", return_value=bad), mock.patch.object(
            manager, "_run_command", side_effect=fake_run
        ):
            manager.begin()
            manager.wait_until({"consent"})
            manager.start_install()
            self.assertTrue(entered_pip.wait(5))
            self.assertEqual(manager.cancel()["state"], "cancelling")
            status = manager.wait_until({"bypassed", "error"})
        self.assertEqual(status["state"], "bypassed", status)
        self.assertEqual((manager.venv_path / "old.txt").read_text(encoding="utf-8"), "keep-me")
        self.assertFalse((manager.venv_path / "partial.txt").exists())
        self.assertEqual(list(self.root.glob(f"{venv_setup.STAGE_PREFIX}*")), [])

    def test_download_failure_restores_old_and_can_continue_without(self) -> None:
        old = self.root / ".venv"
        old.mkdir()
        (old / "old.txt").write_text("keep-me", encoding="utf-8")
        manager = self.manager()
        bad = IntegrityReport(False, ("demo-package",))

        def fake_run(command: list[str], stage: Path, error_code: str) -> None:
            if "venv" in command:
                manager.venv_path.mkdir()
                return
            raise SetupError("download_failed", "offline test", http_status=500)

        with mock.patch.object(venv_setup, "inspect_venv", return_value=bad), mock.patch.object(
            manager, "_run_command", side_effect=fake_run
        ):
            manager.begin()
            manager.wait_until({"consent"})
            manager.start_install()
            status = manager.wait_until({"error"})
        self.assertEqual(status["error_code"], "download_failed")
        self.assertIn("offline test", status["error"])
        self.assertTrue((manager.venv_path / "old.txt").is_file())
        self.assertTrue(manager.skip()["can_chat"])

    def test_pause_and_resume_control_the_active_process(self) -> None:
        manager = self.manager()
        process = mock.Mock()
        process.poll.return_value = None
        manager._state = "downloading"
        manager._process = process
        with mock.patch.object(venv_setup, "suspend_process") as suspend, mock.patch.object(
            venv_setup, "resume_process"
        ) as resume:
            self.assertEqual(manager.pause()["state"], "paused")
            self.assertEqual(manager.resume()["state"], "downloading")
        suspend.assert_called_once_with(process)
        resume.assert_called_once_with(process)

    def test_interrupted_transaction_restores_backup(self) -> None:
        manager = self.manager()
        (manager.venv_path).mkdir()
        (manager.venv_path / "old.txt").write_text("old", encoding="utf-8")
        stage = self.root / f"{venv_setup.STAGE_PREFIX}interrupted"
        stage.mkdir()
        manager._write_journal(stage, had_old=True, committed=False)
        os.replace(manager.venv_path, stage / "old-venv")
        manager.venv_path.mkdir()
        (manager.venv_path / "partial.txt").write_text("new", encoding="utf-8")
        manager._recover_interrupted()
        self.assertEqual((manager.venv_path / "old.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((manager.venv_path / "partial.txt").exists())
        self.assertFalse(stage.exists())


class SetupHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))
        self.server = HarnessServer(("127.0.0.1", 0), HarnessHandler)
        self.server.settings = self.settings
        self.server.verbose = False
        self.manager = VenvSetupManager(Path(self._tmp.name), Path(self._tmp.name) / "requirements.txt")
        self.manager._state = "consent"
        self.manager._integrity = IntegrityReport(False, ("Pillow",), ("import_failed",))
        self.server.venv_setup = self.manager
        self.addCleanup(self.manager.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_status_is_available_while_normal_apis_are_gated(self) -> None:
        status, body = self.request("/api/setup/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "consent")
        self.assertFalse(body["can_chat"])
        self.assertIn(body["language"], ("zh", "en"))
        status, body = self.request("/api/config")
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "setup_in_progress")

    def test_start_requires_explicit_consent_and_valid_mirror(self) -> None:
        status, body = self.request("/api/setup/start", {"mirror": ""})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "consent_required")
        status, body = self.request(
            "/api/setup/start", {"consent": True, "mirror": "ftp://bad.example/simple"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_mirror")

    def test_skip_unlocks_the_normal_application(self) -> None:
        status, body = self.request("/api/setup/skip", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "bypassed")
        status, body = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("language", body)


if __name__ == "__main__":
    unittest.main()
