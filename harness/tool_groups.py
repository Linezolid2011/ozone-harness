"""工具分组树（ADR-115）：弹窗里那几层折叠分组从哪来。

分组定义放在**随 harness 发布的** ``harness/tool_groups.json``，而不是
``data/`` —— 这是给工具插件时代留的扩展点：社区插件加工具时，把工具名
挂进某个组（或新组），弹窗就自动多出一层，**前端和校验都不需要改代码**。
如果分组写死在代码/前端里，每加一个插件就要动三处，那"可插拔"就是句空话。

文件形状（``version`` 字段留给将来的结构演进）::

    {"version": 1,
     "groups": [{"id": "memory", "tools": ["mem_create", ...]}, ...]}

三条规则：

1. 组 id 是稳定契约：显示名走 i18n 键 ``ui.toolconfig.groups.<id>``，
   **文件里不写任何语言的显示名**（shipped 文件保持语言中立）。
2. ``tools`` 必须恰好划分 ``tool_config.CHAT_TOOLS``：一个工具只属一组、
   不许有未知工具、不许漏（有机械守卫钉住）。插件加了新工具名但 harness
   还没注册它时，加载器**丢弃并警告** —— 弹窗里不许出现永远注册不出来的
   "幽灵工具"勾选项，那是"看起来能选其实没用"的一种。
3. 文件坏/缺时**回退到内置默认分组**（那一份就是下面的 ``DEFAULT_GROUPS``），
   分组只影响界面布局，不值得为它的损坏让整个服务起不来；但 shipped 文件
   与内置默认不一致会被测试当场抓红 —— 回退是运行时的 resilience，
   一致发布是开发期的纪律，两者不矛盾。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .tool_config import CHAT_TOOLS

GROUPS_FILE = Path(__file__).resolve().parent / "tool_groups.json"

#: 内置默认分组：文件坏/缺时的回退，也是测试比对的"发布基线"。
#: 顺序即弹窗里的显示顺序（general 这种杂项放最后）。
DEFAULT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("memory", ("mem_create", "mem_edit", "mem_del")),
    (
        "dream",
        (
            "dream_list",
            "dream_read",
            "dream_search",
            "dream_create",
            "dream_edit",
            "dream_mkdir",
            "dream_delete",
        ),
    ),
    ("web", ("web_open_url", "web_search")),
    ("general", ("time",)),
)


@dataclass(frozen=True)
class ToolGroup:
    """一个折叠分组：稳定 id + 组内工具名（按文件里的顺序显示）。"""

    id: str
    tools: tuple[str, ...]


def _default_groups() -> list[ToolGroup]:
    return [ToolGroup(id=given_id, tools=tools) for given_id, tools in DEFAULT_GROUPS]


def _parse(data: object) -> list[ToolGroup]:
    """校验一份分组数据，非法抛 ``ValueError``（消息说清哪里不对）。

    校验是 fail-closed 的：不认识的一律拒，不"宽容跳过" —— 分组错了
    弹窗布局就悄悄变形，那种错误比"起不来"更难发现。
    """
    if not isinstance(data, dict):
        raise ValueError("tool_groups.json 顶层必须是对象")
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("tool_groups.json 的 groups 必须是非空数组")
    seen_ids: set[str] = set()
    seen_tools: set[str] = set()
    groups: list[ToolGroup] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("每个分组必须是对象")
        given_id = raw.get("id")
        if not isinstance(given_id, str) or not given_id.strip():
            raise ValueError("分组缺 id（或 id 不是字符串）")
        given_id = given_id.strip()
        if given_id in seen_ids:
            raise ValueError(f"分组 id 重复：{given_id}")
        seen_ids.add(given_id)
        tools = raw.get("tools")
        if not isinstance(tools, list):
            raise ValueError(f"分组 {given_id} 的 tools 必须是数组")
        cleaned: list[str] = []
        for name in tools:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"分组 {given_id} 里有非字符串工具名")
            name = name.strip()
            if name in seen_tools:
                raise ValueError(f"工具 {name} 出现在多个分组里")
            if name not in CHAT_TOOLS:
                raise ValueError(f"未知工具：{name}（harness 还没注册它，分组不认）")
            seen_tools.add(name)
            cleaned.append(name)
        groups.append(ToolGroup(id=given_id, tools=tuple(cleaned)))
    missing = sorted(set(CHAT_TOOLS) - seen_tools)
    if missing:
        raise ValueError(f"这些工具没有分组：{', '.join(missing)}")
    return groups


def load_groups(path: Path | None = None, *, quiet: bool = False) -> list[ToolGroup]:
    """读分组树；坏/缺回退到 ``DEFAULT_GROUPS`` 并打印警告（``quiet`` 给测试用）。

    用 ``utf-8-sig``：与所有配置 store 同口径（用户可能手改这个文件，
    Windows 记事本会加 BOM —— ADR-085 的教训在 shipped 文件上同样适用）。
    """
    target = Path(path) if path is not None else GROUPS_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        return _parse(data)
    except (OSError, ValueError) as exc:
        if not quiet:
            print(
                f"! 工具分组文件 {target.name} 缺失或损坏（{exc}），"
                "已回退到内置默认分组。",
                flush=True,
            )
        return _default_groups()
