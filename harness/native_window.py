"""pywebview 原生窗口：可选的"应用外壳"（ADR-112）。

产品本体仍然是那个零依赖的本地 Web 服务；这个模块只是**当 pywebview 可用时**
把界面装进一个原生窗口，让它"看起来像一个应用"。pywebview 不是硬依赖，
两条退路都必须明说、不许静默（ADR-032/052/095 的老规矩）：

* **没装**（新 clone 没有 venv、用户在引导门里拒绝安装、venv 事后被精简）——
  `load_webview()` 探测不到，启动走浏览器标签页那条老路；
* **装了但起不来**（典型：Windows 机器缺 WebView2 运行时）——`run_window()`
  捕获异常、打印原因、返回 False，调用方同样退回浏览器标签页。

为什么不在这里 import 时就把 webview 钉死：pywebview 在 Windows 上拖来
pythonnet，只有"真的要用窗口"才值得付这个 import 的代价；而且测试环境
没装它也必须能跑。
"""

from __future__ import annotations

from typing import Any

#: 窗口初始尺寸。取 1280x840 是为了让设置页（最宽的那页）不用横向滚动；
#: min_size 兜底防止用户把窗口拖到小得没法用。
_WINDOW_SIZE = (1280, 840)
_WINDOW_MIN_SIZE = (760, 500)


def load_webview() -> Any | None:
    """延迟 import pywebview；**任何**失败都返回 None（调用方退回浏览器标签页）。

    为什么连广捕 `Exception` 而不是只捕 `ImportError`：ABI 不匹配、pythonnet
    初始化失败这类问题在 import 阶段同样会炸 —— 对调用方来说它们与
    "没装"是同一种情况："这个外壳不可用"，不值得区分成几种失败。
    """
    try:
        import webview  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - 见 docstring：任何失败 = 没有外壳
        return None
    return webview


def run_window(webview: Any, url: str, *, title: str) -> bool:
    """开原生窗口并**阻塞**到用户关窗。返回 True = 正常关窗；False = 没能起来。

    起不来时打印原因并返回 False —— 调用方（`web.serve()`）据此退回浏览器
    标签页，服务本身不许跟着死：窗口是壳，服务才是产品。

    只能在主线程调用：pywebview 的 GUI 循环（Windows 上是 pythonnet/WinForms）
    要求跑在主线程，HTTP 服务因此退到后台线程（接线见 `web.serve()`）。
    """
    try:
        webview.create_window(
            title,
            url,
            width=_WINDOW_SIZE[0],
            height=_WINDOW_SIZE[1],
            min_size=_WINDOW_MIN_SIZE,
        )
        webview.start()
    except Exception as exc:  # noqa: BLE001 - 窗口起不来绝不能让服务起不来
        print(f"  ! 原生窗口没能打开（{exc}）", flush=True)
        # 实测过的两种典型原因（ADR-113），把出路说清楚而不是只丢一句报错：
        # ① 机器缺 WebView2 运行时（pywebview 的 EdgeChromium 后端要用它）；
        # ② 项目目录被系统/安全软件整体标记为"不可信区域"——.NET Framework 会因此
        #    拒绝加载 .venv 里任何未 Authenticode 签名的 DLL（Python.Runtime.dll 正是），
        #    表现为 0x80131515 / "Operation is not supported"，且标准解封手段无效。
        print(
            "    出路：① 缺 WebView2 运行时 → 装「Microsoft Edge WebView2 运行时」；\n"
            "        ② 项目目录被标记为不可信区域 → 把项目移到普通目录即可；\n"
            "        两者都不必折腾 —— 浏览器标签页功能完全不受影响。",
            flush=True,
        )
        return False
    return True
