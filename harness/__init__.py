"""O₃ᶻᵒⁿᵉ —— 功能上对标 Kimi.com 网页端的服务端编排层。

产品名只有**这一处**定义（`PRODUCT_NAME`）：控制台横幅、命令行说明、HTTP 的
`Server` 头都从它取。`O₃ᶻᵒⁿᵉ` 是排版写法（O 带下角标 3、上角标 zone），
ASCII 环境（HTTP 头必须 latin-1）里退化成 `O3zone`。

**版本号也不写死在代码里**：它读 `docs/version.txt`（见 `version()`）——
界面（`/api/config` 的 `version`）与命令行（`--version`）都从那里取，
发布时只改那一个文件。

设计边界（重要）：
  * 记忆只由 mem_create / mem_edit / mem_del 写；档案另由 dream_create/edit/mkdir/delete 受控维护。
  * 模型没有通用文件系统能力，也不读写会话历史；时间/联网等工具由服务端受控执行。
  * 会话历史、记忆条目的落盘、上下文的组装与注入，全部由 harness 服务端完成。
"""

from __future__ import annotations

from pathlib import Path

# 排版写法（界面上由 HTML/CSS 画出上下角标，这里是纯文本形态）。
PRODUCT_NAME = "O₃ᶻᵒⁿᵉ"
# 只用 ASCII 的地方（HTTP `Server` 头、.cmd 文件、文件名）用它。
PRODUCT_SLUG = "O3zone"

ROOT = Path(__file__).resolve().parent.parent
#: 版本号的**唯一来源**。改它不需要动代码，也不需要重新安装。
VERSION_FILE = ROOT / "docs" / "version.txt"
#: 文件缺失/为空时的兜底：这不该发生，但"版本号读不到"更不该让程序起不来。
FALLBACK_VERSION = "0.0.0-dev"

#: 给 `import harness; harness.__version__` 这类打包惯例留一个名字（读一次）。
__version__ = ""


def version(path: Path | None = None) -> str:
    """读出产品版本号（默认 `docs/version.txt`）。

    为什么放文件而不是常量：发布时只想改一处，而**三个读端**（界面侧栏、
    `/api/config`、`--version`）必须同步。写死在代码里，就会出现"界面显示
    1.0.0-rc1、命令行还是 0.1.0"这种自相矛盾 —— 而版本号正是拿来回答
    "你跑的是哪一个"的。

    `path` 只为测试而存在；文件读不出来时退回 `FALLBACK_VERSION`（不抛异常）。
    """
    target = Path(path) if path is not None else VERSION_FILE
    try:
        text = target.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return FALLBACK_VERSION
    # 只取第一行：手写这个文件时很容易后面多打一个空行或写句备注
    return text.splitlines()[0].strip() if text else FALLBACK_VERSION


__version__ = version()
