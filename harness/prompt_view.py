"""提示词与工具定义的**只读视图**：给界面「设置 → 提示词」那一页用。

## 这一页要回答的两个问题

1. **我们到底跟模型说了什么？** —— 提示词；
2. **模型手里有哪些工具？** —— 工具定义（名字、描述、参数 schema）。

两件事都必须**照实**回答，所以这里不另抄一份文案：

* 提示词直接读当前语言的 `harness/prompts/` 模块（`i18n.bundle()`）——
  页面列出来的**就是**那一份，不是复制品；
* 工具定义**用真的工具类构造一遍**，取它们的 `description` / `parameters`。
  那正是模型收到的那一份（工具的文案在**实例化时**按当前语言取，
  见 `docs/12-i18n.md` §3）。

## 为什么不复用 `Runtime`

`Runtime` 会开一个会话日志文件（写盘）。而"点开一页看一眼"不该在 `data/` 里
留下任何东西 —— 这是这个项目一贯的规矩（`VaultStore` 的读操作不建目录、
`preview_reset` 用只读方式开库、语言设置的读操作不建文件）。

所以这里自己装配一份**零落盘**的：

* 记忆三件套用一个 `:memory:` 的 `MemoryStore` 当替身 —— 工具的 `__init__`
  只把 store 存起来，真正碰存储的是 `run()`，而这一页从不 `run`；
* 档案三件套用只读的 `VaultStore`（读操作不创建目录）；
* 联网配置走只读的 `ToolStore`（构造时不写文件，实测过）。

`tests/test_prompt_view.py` 里有一条断言守着"在全新数据目录上跑一遍，
磁盘上一个文件都不许多"—— 上面哪一条将来开始写盘，它就会红。

## "当前状态"必须标出来

一部分提示词与工具是**有条件**的：联网关着就没有 `web_*` 工具；
档案还空着就没有档案三件套；`OPEN_ONLY_PROMPT` 与 `WEB_TOOLS_PROMPT` 二选一。
只列定义会让人以为模型一直看得到它们，所以每一项都带：

* `state`：`"on"` / `"off"` / `"session"`。
  **它只回答一件事：按当前配置，这句话/这个工具现在会不会被用上。**
  "按情形才出现"的那些（压缩、整理、裁剪提示）一律是 `"on"`，
  细节写在 `condition` 里 —— 否则 `state` 会变成一句需要解释的话。
* `condition`：为什么是 off、或者什么时候才出现（人话，已按当前语言取好）。

## 现在是只读的，但 `id` 已经是稳定契约

每一项都带稳定 `id`：提示词 = bundle 里的常量名，工具 = 工具名。
下一步如果允许在界面上改这些内容，改的就是这个 `id` 指向的东西 ——
所以 **`id` 不随展示顺序或分组变化**（分组、排序都只是展示层的事）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import i18n
from . import prompts as prompt_store
from .config import Settings
from .dream_records import ReadTracker
from .i18n import t
from .memory import MemoryStore
from .net.config import TOOLS_FILE, ToolStore, WebToolsConfig
from .tools.dream_tools import build_dream_tools
from .tools.memory_tools import MemCreateTool, MemDeleteTool, MemEditTool
from .tools.time_tool import TimeTool
from .tools.vault_tools import VaultListTool, VaultReadTool, VaultSearchTool
from .tools.web_tools import WebOpenUrlTool, WebSearchTool
from .vault import VaultStore

# 提示词的分组。**这是"有没有漏"的唯一判据**：`_check_groups()` 会拿它和
# bundle 的公开常量对一遍，对不上就抛异常（有测试钉着）—— 新增一条提示词却
# 忘了归组会当场炸，而不是在界面上静默少一条。
PROMPT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "system",
        (
            "SYSTEM_PROMPT",
            "VAULT_PROMPT",
            "VAULT_EMPTY_PROMPT",
            "VAULT_NO_README_PROMPT",
            "WEB_TOOLS_PROMPT",
            "OPEN_ONLY_PROMPT",
            "WEB_DISABLED",
        ),
    ),
    ("tail", ("TAIL_NOW", "TAIL_OMITTED_NOTICE")),
    ("compact", ("COMPACT_SYSTEM_PROMPT", "COMPACT_PREVIOUS", "COMPACT_INPUT")),
    (
        "render",
        (
            "SUMMARY_HEADER",
            "RENDER_USER",
            "RENDER_ASSISTANT",
            "RENDER_CALL",
            "RENDER_TOOL_RESULT",
            "RENDER_IMAGE",
            "RENDER_TRUNCATED_SUFFIX",
            "WEB_SAFETY",
        ),
    ),
    ("dream", ("DREAM_SYSTEM_PROMPT", "DREAM_INSTRUCTION")),
)

# bundle 里不是提示词的名字（`LANG` 是语言代码）。
PROMPT_SKIP = frozenset({"LANG"})

# "按情形才出现"的那几条：state 一律 on，什么时候出现写在这里。
_PROMPT_TIMING = {
    "SYSTEM_PROMPT": "ui.prompts.cond.system_skeleton",
    "TAIL_OMITTED_NOTICE": "ui.prompts.cond.trimmed_turn",
    "COMPACT_SYSTEM_PROMPT": "ui.prompts.cond.compact_turn",
    "COMPACT_PREVIOUS": "ui.prompts.cond.compact_turn",
    "COMPACT_INPUT": "ui.prompts.cond.compact_turn",
    "SUMMARY_HEADER": "ui.prompts.cond.summary_only",
    "RENDER_USER": "ui.prompts.cond.summary_only",
    "RENDER_ASSISTANT": "ui.prompts.cond.summary_only",
    "RENDER_CALL": "ui.prompts.cond.summary_only",
    "RENDER_TOOL_RESULT": "ui.prompts.cond.summary_only",
    "RENDER_IMAGE": "ui.prompts.cond.summary_only",
    "RENDER_TRUNCATED_SUFFIX": "ui.prompts.cond.summary_only",
    "WEB_SAFETY": "ui.prompts.cond.summary_only",
    "DREAM_SYSTEM_PROMPT": "ui.prompts.cond.dream_only",
    "DREAM_INSTRUCTION": "ui.prompts.cond.dream_only",
}

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass(frozen=True)
class _Conditions:
    """按当前配置算出来的几件事实。提示词与工具**共用同一份判断**，
    免得页面上写的和实际拼出来的不一样。"""

    vault: VaultStore
    vault_available: bool
    vault_has_readme: bool
    web_config: WebToolsConfig
    web_enabled: bool
    web_search_live: bool  # 搜索后端配好了（还要乘上会话级开关）


def _conditions(settings: Settings) -> _Conditions:
    vault = VaultStore(
        settings.vault_dir,
        read_max_chars=settings.vault_read_max_chars,
        write_max_chars=settings.vault_write_max_chars,
        max_files=settings.vault_max_files,
        name_max_chars=settings.vault_name_max_chars,
        readme_token_budget=settings.vault_readme_token_budget,
    )
    available = vault.has_content()
    readme, _ = vault.readme_text() if available else ("", False)
    web_config = ToolStore(settings.data_dir / TOOLS_FILE).web()
    return _Conditions(
        vault=vault,
        vault_available=available,
        vault_has_readme=bool(readme.strip()),
        web_config=web_config,
        web_enabled=bool(web_config.enabled),
        web_search_live=bool(web_config.enabled and web_config.search_available),
    )


def _check_groups(bundle: Any) -> None:
    """分组表必须与 prompts 模块**严格互补**。

    这条不是防御性编程：分组表是手写的，而提示词会一直长。
    少了/多了必须当场说出来，否则界面上会静默少一条。
    """
    public = {name for name in dir(bundle) if name.isupper()} - PROMPT_SKIP
    grouped = {name for _group, names in PROMPT_GROUPS for name in names}
    if public != grouped:
        raise RuntimeError(
            "提示词分组表与 prompts 模块不一致："
            f"漏了 {sorted(public - grouped)}，多出 {sorted(grouped - public)}"
        )


def _placeholders(text: str) -> list[str]:
    """文本里的 `{name}` 占位符（去重、保持出现顺序）。

    给界面用：让人一眼看出这句是要被填的，而不是最终发给模型的原文。
    """
    seen: list[str] = []
    for match in _PLACEHOLDER.finditer(text or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _prompt_state(name: str, cond: _Conditions) -> tuple[str, str]:
    """这条提示词现在会不会被用上，以及为什么（`(state, condition_key)`）。"""
    if name == "VAULT_PROMPT":
        return ("on", "") if cond.vault_has_readme else ("off", "ui.prompts.cond.vault_no_readme")
    if name == "VAULT_EMPTY_PROMPT":
        return ("on", "") if not cond.vault_available else ("off", "ui.prompts.cond.vault_ready")
    if name == "VAULT_NO_README_PROMPT":
        live = cond.vault_available and not cond.vault_has_readme
        return ("on", "") if live else ("off", "ui.prompts.cond.vault_has_readme")
    if name == "WEB_TOOLS_PROMPT":
        if cond.web_search_live:
            return "on", ""
        return "off", ("ui.prompts.cond.web_off" if not cond.web_enabled else "ui.prompts.cond.web_search_off")
    if name == "OPEN_ONLY_PROMPT":
        if cond.web_enabled and not cond.web_search_live:
            return "on", ""
        return "off", ("ui.prompts.cond.web_off" if not cond.web_enabled else "ui.prompts.cond.web_search_live")
    if name == "WEB_DISABLED":
        return ("on", "") if not cond.web_enabled else ("off", "ui.prompts.cond.web_enabled")
    return "on", _PROMPT_TIMING.get(name, "")


def _spec(tool: Any, *, state: str, condition_key: str = "") -> dict[str, Any]:
    """一个工具定义。字段就是发给接口的那几个（外层的 `type: function` 由 registry 包）。"""
    parameters = tool.parameters or {}
    return {
        # `id` 与 `name` 现在一样，但**语义不同**：`id` 是稳定契约（下一步要按它改），
        # `name` 是模型看到的函数名。以后若要给同一份定义做多语言覆盖，两者会分开。
        "id": tool.name,
        "name": tool.name,
        "state": state,
        "condition": t(condition_key) if condition_key else "",
        "description": tool.description,
        "parameters": parameters,
        "required": list(parameters.get("required") or []),
        "properties": len(parameters.get("properties") or {}),
    }


def _chat_specs(cond: _Conditions, settings: Settings) -> list[dict[str, Any]]:
    """对话 agent 的工具定义（**全部列出来**，状态按当前配置标）。"""
    cap = settings.max_tool_result_chars
    rows: list[dict[str, Any]] = []
    # 记忆三件套的 store 是个一次性替身：`__init__` 只存引用，这一页从不 `run()`。
    # 这样"看一眼工具定义"不会在你 data/ 里种出 memory.db 或 memory_instruction.md。
    proxy = MemoryStore(instruction_path=":memory:", max_items=1, max_chars=1000)
    try:
        for tool in (MemCreateTool(proxy), MemEditTool(proxy), MemDeleteTool(proxy)):
            rows.append(_spec(tool, state="on"))
    finally:
        proxy.close()

    # 时间工具：无条件在线，只读、不花钱。
    rows.append(_spec(TimeTool(), state="on"))

    vault_state = "on" if cond.vault_available else "off"
    vault_condition = "" if cond.vault_available else "ui.prompts.cond.vault_empty"
    for tool in (
        VaultListTool(cond.vault, max_result_chars=cap),
        VaultReadTool(cond.vault, max_result_chars=cap),
        VaultSearchTool(cond.vault, max_result_chars=cap),
    ):
        rows.append(_spec(tool, state=vault_state, condition_key=vault_condition))

    rows.append(
        _spec(
            WebOpenUrlTool(cond.web_config, max_result_chars=cap),
            state="on" if cond.web_enabled else "off",
            condition_key="" if cond.web_enabled else "ui.prompts.cond.web_off",
        )
    )
    # web_search 还要乘上**会话级**开关（顶栏那个），所以这里只敢标 session
    if not cond.web_search_live:
        rows.append(
            _spec(
                WebSearchTool(cond.web_config),
                state="off",
                condition_key="ui.prompts.cond.web_search_unavailable",
            )
        )
    else:
        rows.append(
            _spec(
                WebSearchTool(cond.web_config),
                state="session",
                condition_key="ui.prompts.cond.web_search_session",
            )
        )
    return rows


def _dream_specs(cond: _Conditions, settings: Settings) -> list[dict[str, Any]]:
    """整理 agent 的工具定义（那 7 个，永远只有它拿得到）。"""
    tools = build_dream_tools(
        store=cond.vault,
        sessions_dir=settings.sessions_dir,
        cursors={},
        tracker=ReadTracker({}),
        record_max_events=settings.dream_record_max_events,
        record_max_chars=settings.dream_record_max_chars,
        max_result_chars=settings.max_tool_result_chars,
        today="",
    )
    return [
        _spec(tool, state="on", condition_key="ui.prompts.cond.dream_only") for tool in tools
    ]


def describe_prompts(settings: Settings) -> dict[str, Any]:
    """这一页要的一切。**只读**：不写盘、不联网、不调模型。

    这里的"只读"指**不写**；但它会把数据目录配好并读一次 `prompts.json`
    （用户覆盖），所以页面上的 `text` 是**当前真正生效的那一份**，
    而 `default_text` 是内置原文 —— 两者并排才能看出"改了什么、怎么回去"。
    """
    # 覆盖层要知道数据目录在哪（幂等）。放在这里而不是只靠启动那一次：
    # 换数据目录起服务（测试、双开）时也一定读到对的那份文件。
    prompt_store.configure(settings.data_dir)
    store_state = prompt_store.state()
    overridden = {language: set(names) for language, names in store_state["overrides"].items()}
    bundle = prompt_store.bundle()
    builtin = prompt_store.builtin()
    _check_groups(bundle)
    cond = _conditions(settings)

    groups: list[dict[str, Any]] = []
    for group_id, names in PROMPT_GROUPS:
        items: list[dict[str, Any]] = []
        for name in names:
            text = str(getattr(bundle, name, ""))
            default_text = str(getattr(builtin, name, ""))
            state, condition_key = _prompt_state(name, cond)
            is_overridden = name in overridden.get(i18n.current(), set())
            # 手工编辑过的文件可能存在**非法**内容（保存时校验过，但文件是纯文本）。
            # 页面不该因此 500 —— 把问题当成一条 warning 如实显示出来。
            warnings: list[str] = []
            missing: list[str] = []
            if is_overridden:
                try:
                    checked = prompt_store.inspect(
                        i18n.current(), name, text, max_chars=settings.prompts_max_chars
                    )
                    warnings = list(checked["warnings"])
                    missing = list(checked["missing"])
                except prompt_store.PromptError as exc:
                    warnings = [str(exc)]
            items.append(
                {
                    "id": name,
                    "title": name,  # 常量名是标识符，**不翻译**（契约 §1）
                    "state": state,
                    "condition": t(condition_key) if condition_key else "",
                    "when": t(f"ui.prompts.item.{name}.when"),
                    "text": text,
                    "chars": len(text),
                    "placeholders": _placeholders(text),
                    # ── 可编辑（契约 `docs/14-editable-prompts.md`）──
                    "default_text": default_text,
                    "overridden": is_overridden,
                    "warnings": warnings,
                    "placeholders_missing": missing,
                }
            )
        groups.append(
            {
                "id": group_id,
                "title": t(f"ui.prompts.group.{group_id}.title"),
                "note": t(f"ui.prompts.group.{group_id}.note"),
                "items": items,
            }
        )

    tools = [
        {
            "id": "chat",
            "title": t("ui.prompts.tools.chat.title"),
            "note": t("ui.prompts.tools.chat.note"),
            "items": sorted(_chat_specs(cond, settings), key=lambda row: row["name"]),
        },
        {
            "id": "dream",
            "title": t("ui.prompts.tools.dream.title"),
            "note": t("ui.prompts.tools.dream.note"),
            "items": sorted(_dream_specs(cond, settings), key=lambda row: row["name"]),
        },
    ]

    return {
        "language": i18n.current(),
        "groups": groups,
        "tools": tools,
        # 两门语言各改了哪几条 —— 界面上"改中文不影响英文"就靠它显示出来
        "overrides": store_state["overrides"],
        # 覆盖文件坏了的话这里是那句人话（现在用的是内置）：
        # **不静默**，页首会显示一行警告
        "store_error": store_state["error"],
        "summary": {
            "prompts": sum(len(group["items"]) for group in groups),
            "prompt_groups": len(groups),
            "tools": sum(len(group["items"]) for group in tools),
            "tools_on": sum(
                1 for group in tools for item in group["items"] if item["state"] == "on"
            ),
            # 当前语言里改过几条（契约 §4.1）
            "overridden": sum(
                1 for group in groups for item in group["items"] if item["overridden"]
            ),
        },
    }


__all__ = ["PROMPT_GROUPS", "PROMPT_SKIP", "describe_prompts"]
