"""pywebview 原生窗口（ADR-112）的离线守卫。

这个外壳的全部约定是：**可选、可退、不静默**。

* 可选 —— 没装 pywebview（新 clone、拒绝引导安装、venv 被精简）时启动照常，
  只是退到浏览器标签页；`load_webview()` 对"任何失败"都返回 None；
* 可退 —— 装了但起不来（典型：缺 WebView2 运行时）时 `run_window()` 返回
  False 并打印原因，服务本身不许跟着死；
* 不静默 —— 退回浏览器那一步在 `web._decide_ui_open` 里有**明说**的提示。

测试全部用假 webview 模块，不 import 真 pywebview（测试环境没装它，
装了也不许真开窗口 —— CI/无人值守机器上没有 display）。
"""

from __future__ import annotations

import builtins
import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness import native_window, web


class LoadWebviewTests(unittest.TestCase):
    def test_missing_pywebview_is_none_not_an_error(self) -> None:
        """没装是最常见的形态（新 clone 就没有 venv）：探测必须安静地返回 None。"""

        def no_webview(name: str, *args: object, **kwargs: object) -> object:
            if name == "webview":
                raise ImportError("No module named 'webview'")
            return real_import(name, *args, **kwargs)

        real_import = builtins.__import__
        with mock.patch.object(builtins, "__import__", side_effect=no_webview):
            self.assertIsNone(native_window.load_webview())

    def test_available_pywebview_is_returned(self) -> None:
        """装了就把它交给调用方：探测成功与失败要同样可测。"""
        fake = types.ModuleType("webview")
        with mock.patch.dict(sys.modules, {"webview": fake}):
            self.assertIs(native_window.load_webview(), fake)

    def test_broken_pywebview_counts_as_missing(self) -> None:
        """ABI 坏 / pythonnet 初始化炸这类 import 阶段异常 = 与"没装"同一种退路。"""

        def broken(name: str, *args: object, **kwargs: object) -> object:
            if name == "webview":
                raise OSError("dll load failed")
            return real_import(name, *args, **kwargs)

        real_import = builtins.__import__
        with mock.patch.object(builtins, "__import__", side_effect=broken):
            self.assertIsNone(native_window.load_webview())


class RunWindowTests(unittest.TestCase):
    url = "http://127.0.0.1:45678/"

    def _fake_webview(self, *, start_exc: Exception | None = None) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            create_window=mock.Mock(),
            start=mock.Mock(side_effect=start_exc),
        )

    def test_window_runs_with_title_and_url_and_true_on_close(self) -> None:
        fake = self._fake_webview()
        with redirect_stdout(io.StringIO()):
            closed = native_window.run_window(fake, self.url, title="O₃ᶻᵒⁿᵉ")
        self.assertTrue(closed)
        fake.create_window.assert_called_once()
        args, kwargs = fake.create_window.call_args
        # 标题与地址是窗口的全部内容，钉住参数形态（第 1/2 个位置参数）。
        self.assertEqual(args[0], "O₃ᶻᵒⁿᵉ")
        self.assertEqual(args[1], self.url)
        self.assertIn("width", kwargs)
        self.assertIn("min_size", kwargs)
        fake.start.assert_called_once_with()

    def test_startup_failure_is_false_with_a_reason_not_a_crash(self) -> None:
        """装了但起不来（缺 WebView2 运行时…）：原因要打印，返回值是 False 不是异常。"""
        fake = self._fake_webview(start_exc=RuntimeError("WebView2 runtime missing"))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            closed = native_window.run_window(fake, self.url, title="t")
        self.assertFalse(closed)
        self.assertIn("WebView2 runtime missing", buffer.getvalue())

    def test_failure_message_names_the_way_out(self) -> None:
        """报错必须说清下一步（ADR-113）：两种实测过的典型原因与出路都要出现。

        这是"环境级失败"——产品修不了它，但用户得知道能做什么、不做什么。
        """
        fake = self._fake_webview(start_exc=RuntimeError("Operation is not supported"))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            native_window.run_window(fake, self.url, title="t")
        text = buffer.getvalue()
        self.assertIn("WebView2", text)
        self.assertIn("不可信区域", text)
        self.assertIn("移到普通目录", text)
        self.assertIn("浏览器", text)


class DecideUiOpenTests(unittest.TestCase):
    """`web._decide_ui_open`：三种模式的选择与"没装就明说"那条规矩。"""

    def test_no_open_means_none_and_no_probe(self) -> None:
        with mock.patch.object(
            web.native_window, "load_webview"
        ) as probe, redirect_stdout(io.StringIO()):
            mode, webview = web._decide_ui_open(False)
        self.assertEqual(mode, "none")
        self.assertIsNone(webview)
        probe.assert_not_called()  # --no-open 连探测都不做

    def test_pywebview_present_chooses_window_silently(self) -> None:
        fake = types.ModuleType("webview")
        with mock.patch.object(
            web.native_window, "load_webview", return_value=fake
        ), redirect_stdout(io.StringIO()) as buffer:
            mode, webview = web._decide_ui_open(True)
        self.assertEqual(mode, "window")
        self.assertIs(webview, fake)
        self.assertEqual(buffer.getvalue(), "")  # 成功路径不多话

    def test_pywebview_missing_falls_back_and_says_so(self) -> None:
        """退回浏览器标签页必须**明说** —— 静默退回是这个项目最讨厌的错误类型。"""
        with mock.patch.object(
            web.native_window, "load_webview", return_value=None
        ), redirect_stdout(io.StringIO()) as buffer:
            mode, webview = web._decide_ui_open(True)
        self.assertEqual(mode, "browser")
        self.assertIsNone(webview)
        self.assertIn("浏览器", buffer.getvalue())
        self.assertIn("pywebview", buffer.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
