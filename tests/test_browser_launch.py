"""打开浏览器这条路的测试（ADR-080）。

它此前**一个用例都没有**：跑在 `threading.Timer` 的线程里、失败还静默，
正是"没人走的路才会坏"的典型。而它踩过一个很贵的坑 ——

    在**管理员**窗口里用 `os.startfile` 开浏览器，会把浏览器也抬成管理员权限；
    Edge/Chrome 又不会真的退出（启动增强 / 后台继续运行），那个提权实例会活很久。
    等你下次从**普通**窗口启动，系统就弹
    「现有实例正在以提升的权限运行，是否要用普通权限重启？」
    —— 症状出现在原因之后很久，所以看起来像随机抽风。

所以这里钉两件事：**提权时不能走 `os.startfile`**，以及**打不开必须说话**。
"""

from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from harness import web

from .helpers import test_settings


def _os_stub(name: str) -> types.SimpleNamespace:
    """只替换 `os.name`：真实 os 的其它属性照旧（`subprocess` 那边要真模块对象）。"""
    return types.SimpleNamespace(name=name)


class ElevatedDetectionTests(unittest.TestCase):
    def test_non_windows_is_never_elevated(self) -> None:
        """"提权会传染给浏览器"是 Windows 特有的概念，别的平台恒 False。"""
        with mock.patch.object(web, "os", _os_stub("posix")):
            self.assertFalse(web.is_elevated())

    def test_windows_admin_token_is_detected(self) -> None:
        shell32 = types.SimpleNamespace(IsUserAnAdmin=lambda: 1)
        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(shell32=shell32))
        with mock.patch.object(web, "os", _os_stub("nt")), mock.patch.dict(
            sys.modules, {"ctypes": fake_ctypes}
        ):
            self.assertTrue(web.is_elevated())

    def test_detection_failure_counts_as_normal_privileges(self) -> None:
        """探测本身炸了也不能拦住启动 —— 这个判断只决定"怎么开浏览器"。"""

        def boom() -> int:
            raise OSError("探测失败")

        shell32 = types.SimpleNamespace(IsUserAnAdmin=boom)
        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(shell32=shell32))
        with mock.patch.object(web, "os", _os_stub("nt")), mock.patch.dict(
            sys.modules, {"ctypes": fake_ctypes}
        ):
            self.assertFalse(web.is_elevated())


class LaunchBrowserTests(unittest.TestCase):
    url = "http://127.0.0.1:8787/"

    def test_elevated_goes_through_explorer_and_never_touches_startfile(self) -> None:
        """提权时必须让 Explorer 代转：它自己是普通权限，浏览器才不会被抬权。"""
        with mock.patch.object(web, "os", _os_stub("nt")), mock.patch.object(
            web.subprocess, "Popen"
        ) as popen, mock.patch.object(web.webbrowser, "open") as opener:
            ok, message = web.launch_browser(self.url, elevated=True)

        self.assertTrue(ok)
        self.assertIn("Explorer", message)
        self.assertEqual(popen.call_args.args[0][:2], ["explorer.exe", self.url])
        opener.assert_not_called()

    def test_elevated_but_explorer_missing_is_reported_not_raised(self) -> None:
        with mock.patch.object(web, "os", _os_stub("nt")), mock.patch.object(
            web.subprocess, "Popen", side_effect=OSError("找不到 explorer.exe")
        ):
            ok, message = web.launch_browser(self.url, elevated=True)

        self.assertFalse(ok)
        self.assertIn("找不到 explorer.exe", message)

    def test_other_platforms_never_shell_out_to_explorer(self) -> None:
        """`explorer.exe` 只有 Windows 有；别的地方即便"elevated"也走默认浏览器。"""
        with mock.patch.object(web, "os", _os_stub("posix")), mock.patch.object(
            web.subprocess, "Popen"
        ) as popen, mock.patch.object(web.webbrowser, "open", return_value=True):
            ok, _message = web.launch_browser(self.url, elevated=True)

        self.assertTrue(ok)
        popen.assert_not_called()

    def test_normal_launch_uses_the_default_browser(self) -> None:
        with mock.patch.object(web, "os", _os_stub("nt")), mock.patch.object(
            web.webbrowser, "open", return_value=True
        ) as opener:
            ok, message = web.launch_browser(self.url, elevated=False)

        self.assertTrue(ok)
        self.assertEqual(message, "")
        opener.assert_called_once_with(self.url)

    def test_open_returning_false_is_a_failure_with_a_reason(self) -> None:
        with mock.patch.object(web, "os", _os_stub("nt")), mock.patch.object(
            web.webbrowser, "open", return_value=False
        ):
            ok, message = web.launch_browser(self.url, elevated=False)

        self.assertFalse(ok)
        self.assertIn("没有可用的默认浏览器", message)

    def test_open_raising_is_a_failure_not_a_crash(self) -> None:
        """打不开浏览器绝不能让 Web 端起不来 —— 那才是最糟的失败方式。"""
        with mock.patch.object(web, "os", _os_stub("nt")), mock.patch.object(
            web.webbrowser, "open", side_effect=RuntimeError("没有关联的应用")
        ):
            ok, message = web.launch_browser(self.url, elevated=False)

        self.assertFalse(ok)
        self.assertIn("没有关联的应用", message)

    def test_failure_is_printed_with_the_url(self) -> None:
        """原先返回值被丢掉，用户只知道"浏览器没开"；现在必须把地址再给一遍。"""
        with mock.patch.object(
            web, "launch_browser", return_value=(False, "浏览器没有自动打开（测试）")
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                web._open_ui_in_browser(self.url)

        text = buffer.getvalue()
        self.assertIn("请手动打开", text)
        self.assertIn(self.url, text)

    def test_success_says_nothing(self) -> None:
        """开成功了就别多话 —— 终端里已经有一行"界面：…"了。"""
        with mock.patch.object(web, "launch_browser", return_value=(True, "")):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                web._open_ui_in_browser(self.url)

        self.assertEqual(buffer.getvalue(), "")


class ServeWiringTests(unittest.TestCase):
    """`serve()` 里那两处接线：提权要提醒、开浏览器要在后台线程里做。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def _run_serve(self, *, elevated: bool) -> str:
        class FakeServer:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                # port=0 的真实 server 在 bind 后这里一定是 OS 分配的非零端口。
                self.server_address = ("127.0.0.1", 45678)

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                pass

        with mock.patch.object(web, "HarnessServer", FakeServer), mock.patch.object(
            web, "is_elevated", return_value=elevated
        ), mock.patch.object(
            # 钉死"没有 pywebview"：开发机真装了它时，这条路也必须走浏览器分支。
            web.native_window,
            "load_webview",
            return_value=None,
        ), mock.patch.object(web, "threading") as fake_threading:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = web.serve(self.settings, port=0, open_browser=True)
            self.assertTrue(fake_threading.Timer.called, "没有起定时器去开浏览器")
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_elevated_window_is_called_out_in_the_banner(self) -> None:
        text = self._run_serve(elevated=True)
        self.assertIn("管理员权限", text)
        self.assertIn("Explorer", text)

    def test_normal_window_says_nothing_extra(self) -> None:
        text = self._run_serve(elevated=False)
        self.assertNotIn("管理员权限", text)
        self.assertIn("Ctrl+C 停止", text)
        self.assertIn("http://127.0.0.1:45678/", text)
        self.assertNotIn("127.0.0.1:0", text)


class NativeWindowServeTests(unittest.TestCase):
    """原生窗口模式（ADR-112）在 `serve()` 里的接线。

    钉三件事：窗口拿到正确的标题与地址、关窗后服务线程被停掉且进程返回 0、
    窗口起不来时**退回浏览器**而不是让服务跟着死。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def _run_serve_window(self, *, start_exc: Exception | None = None) -> tuple[object, object, str]:
        class FakeServer:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.server_address = ("127.0.0.1", 45678)
                self.shutdown_called = False

            def serve_forever(self) -> None:
                return  # 后台线程里的服务：Fake 立即返回（相当于已被 shutdown）

            def shutdown(self) -> None:
                self.shutdown_called = True

            def server_close(self) -> None:
                pass

        server = FakeServer()
        webview = types.SimpleNamespace(
            create_window=mock.Mock(),
            start=mock.Mock(side_effect=start_exc),
        )
        with mock.patch.object(
            web, "HarnessServer", lambda *a, **k: server
        ), mock.patch.object(web, "is_elevated", return_value=False), mock.patch.object(
            web.native_window, "load_webview", return_value=webview
        ), mock.patch.object(web, "threading") as fake_threading:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = web.serve(self.settings, port=0, open_browser=True)
        self.assertEqual(code, 0)
        # 两条路都不许再用"开浏览器"的定时器：窗口路开的是窗口，退回路只有起不来才开浏览器
        return server, webview, buffer.getvalue(), fake_threading

    def test_window_mode_runs_gui_and_stops_server_on_close(self) -> None:
        server, webview, text, fake_threading = self._run_serve_window()
        webview.create_window.assert_called_once()
        args, _kwargs = webview.create_window.call_args
        self.assertEqual(args[1], "http://127.0.0.1:45678/")
        webview.start.assert_called_once_with()
        self.assertTrue(server.shutdown_called, "关窗后后台服务线程必须被停掉")
        self.assertFalse(
            fake_threading.Timer.called,
            "窗口模式下不许再起「开浏览器」定时器",
        )
        self.assertIn("原生窗口", text)

    def test_window_startup_failure_falls_back_to_browser(self) -> None:
        """装了但起不来（缺 WebView2…）：明说原因、退回浏览器、服务继续跑。"""
        server, webview, text, fake_threading = self._run_serve_window(
            start_exc=RuntimeError("no WebView2")
        )
        self.assertIn("no WebView2", text)
        self.assertIn("浏览器", text)
        self.assertTrue(fake_threading.Timer.called, "退回浏览器必须走定时器那条老路")
        self.assertTrue(server.shutdown_called)
        self.assertIn("http://127.0.0.1:45678/", text)

    def test_no_open_never_touches_window_or_browser(self) -> None:
        """--no-open 的语义不变：窗口与浏览器都不许出现。"""
        server = types.SimpleNamespace(
            server_address=("127.0.0.1", 45678),
            serve_forever=lambda: None,
            shutdown=lambda: None,
            server_close=lambda: None,
        )
        with mock.patch.object(
            web, "HarnessServer", lambda *a, **k: server
        ), mock.patch.object(
            web.native_window, "load_webview"
        ) as probe, mock.patch.object(web, "threading") as fake_threading:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = web.serve(self.settings, port=0, open_browser=False)
        self.assertEqual(code, 0)
        probe.assert_not_called()
        self.assertFalse(fake_threading.Timer.called)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
