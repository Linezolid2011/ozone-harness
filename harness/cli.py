"""命令行入口：交互式对话 / 单轮执行 / 会话重放 / 记忆与用量查看。

用法（在项目目录下，run.cmd 与 run.ps1 等价）：
    run.cmd                          # 交互式对话
    run.cmd --once "记住我喜欢美式"    # 跑一轮就退出
    run.cmd --sessions               # 列出所有会话
    run.cmd --replay <session>       # 重放：看当时真正发出的 prompt
    run.cmd --memories               # 看记忆库
    run.cmd --usage                  # 看 token 用量
    run.cmd --search "关键词"         # 离线试一次联网搜索（不花 LLM 钱）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from typing import Any

from .attachments import gc_apply, gc_report
from . import PRODUCT_NAME, version
from . import i18n
from .config import Settings, display_path
from .dream import DreamRunner, DreamRunStore, render_check, render_diff
from .dream_records import pending_sessions, summarize_pending
from .events import list_sessions, read_events
from .llm_types import Usage
from .loop import TurnOutcome
from .memory import open_memory
from .memory_limit import MEMORY_FILE, MemoryLimitStore
from .providers.base import ProviderError
from .providers.store import fetch_models
from .tool_config import ToolConfigStore
from .runtime import (
    NO_PROVIDER_HELP,
    Runtime,
    provider_store,
    resolve_dream_provider,
    resolve_provider,
)
from .web import serve as serve_web
from .vault import (
    README_NAME,
    VaultPathError,
    VaultStore,
    file_preview,
)

# --------------------------------------------------------------------------
# 终端着色
# --------------------------------------------------------------------------

_CODES = {
    "dim": "\033[2m",
    "reset": "\033[0m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "bold": "\033[1m",
    "magenta": "\033[35m",
}

_COLOR_ENABLED = True


def _enable_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - 拿不到控制台就退化成无色
        return False


def style(text: str, color: str) -> str:
    if not _COLOR_ENABLED or color not in _CODES:
        return text
    return f"{_CODES[color]}{text}{_CODES['reset']}"


def emit_line(text: str = "", color: str | None = None) -> None:
    print(style(text, color) if color else text, flush=True)


# --------------------------------------------------------------------------
# 用量与花费
# --------------------------------------------------------------------------


def format_usage(usage: Usage, settings: Settings) -> str:
    return "  ".join(
        [
            f"prompt {usage.prompt_tokens}",
            f"completion {usage.completion_tokens}",
            f"total {usage.total_tokens}",
            f"reasoning {usage.reasoning_tokens}",
            f"cache hit {usage.cached_tokens}",
            f"cache miss {usage.cache_miss_tokens}",
        ]
    )


def _format_bytes(size: int) -> str:
    """人话字节数（附件清理报告用）。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.0f} KiB"
    return f"{size} 字节"


# 清理报告里最多逐条列几个文件。孤儿可能成百上千，全打出来会把有用的那几行
# （数量、占用、有没有删不掉）冲掉。
_GC_LIST_LIMIT = 50


# --------------------------------------------------------------------------
# 终端渲染器：AgentLoop 通过 emit(kind, **data) 回调它
# --------------------------------------------------------------------------


class ConsoleRenderer:
    def __init__(self, settings: Settings, *, show_prompt: bool = False) -> None:
        self.settings = settings
        self.show_prompt = show_prompt
        self._line_open = False
        self._mode: str | None = None

    def __call__(self, kind: str, **data: Any) -> None:
        handler = getattr(self, f"_on_{kind}", None)
        if handler is not None:
            handler(**data)

    # -- 内部小工具 --------------------------------------------------

    def _end_line(self) -> None:
        if self._line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_open = False
        self._mode = None

    def _stream(self, mode: str, text: str, color: str | None = None) -> None:
        if mode != self._mode:
            self._end_line()
            self._mode = mode
            if mode == "reasoning":
                sys.stdout.write(style("💭 ", "dim"))
        if not self._line_open:
            self._line_open = True
        sys.stdout.write(style(text, color) if color else text)
        sys.stdout.flush()

    # -- 事件处理 ----------------------------------------------------

    def _on_step(self, step: int, estimated_tokens: int = 0, **_: Any) -> None:
        if step > 1:
            self._end_line()
            emit_line(f"  · 第 {step} 步（上下文约 {estimated_tokens} token）", "dim")

    def _on_reasoning(self, text: str, **_: Any) -> None:
        if not self.settings.show_reasoning:
            return
        self._stream("reasoning", text, "dim")

    def _on_text(self, text: str, **_: Any) -> None:
        self._stream("text", text)

    def _on_tool_call(self, call: Any, **_: Any) -> None:
        self._end_line()
        emit_line(f"  ⚙ {call.name} {call.arguments}", "yellow")

    def _on_tool_result(self, call: Any, result: Any, **_: Any) -> None:
        color = "green" if result.ok else "red"
        emit_line(f"  ↳ {result.display}", color)

    def _on_memory(self, count: int = 0, limit: int = 0, omitted: int = 0, **_: Any) -> None:
        # 记忆是**全量注入**的，平时没什么可说的；只在"有内容没进来"时提醒一句
        if not omitted:
            return
        self._end_line()
        emit_line(
            f"  ⟲ 记忆 {count}/{limit} 条，其中 {omitted} 条因长度未进上下文", "magenta"
        )

    def _on_notice(self, text: str, level: str = "info", **_: Any) -> None:
        self._end_line()
        color = {"error": "red", "warn": "yellow"}.get(level, "cyan")
        emit_line(f"  ! {text}", color)

    def _on_assistant_done(self, message: Any = None, usage: Any = None, **_: Any) -> None:
        self._end_line()

    def _on_turn_end(self, outcome: TurnOutcome, **_: Any) -> None:
        self._end_line()

    def _on_prompt(self, messages: Any, estimated_tokens: int = 0, **_: Any) -> None:
        if not self.show_prompt:
            return
        self._end_line()
        emit_line(f"  ── 实际发送的 prompt（约 {estimated_tokens} token）──", "magenta")
        for index, message in enumerate(messages):
            body = message.content or ""
            if message.tool_calls:
                body += " " + json.dumps(
                    [call.to_record() for call in message.tool_calls], ensure_ascii=False
                )
            emit_line(f"  [{index}] {message.role}: {body}", "dim")
        emit_line("  ── prompt 结束 ──", "magenta")


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def _effort_from_args(args: argparse.Namespace) -> str:
    effort = (getattr(args, "effort", "") or "").strip().lower()
    return "" if effort in ("off", "none", "false") else effort


def open_runtime(
    settings: Settings, args: argparse.Namespace, session_id: str | None = None
) -> Runtime | None:
    """按 --provider / --model / --effort 装好运行时；都没给就走 providers.json 的默认。"""
    try:
        resolved = resolve_provider(
            settings,
            provider_id=getattr(args, "provider", "") or "",
            model=getattr(args, "model", "") or "",
            effort=_effort_from_args(args),
        )
    except ProviderError as exc:
        emit_line(str(exc), "red")
        return None
    return Runtime(
        resolved.settings,
        session_id,
        provider=resolved.provider,
        generation=resolved.choice,
    )


def cmd_providers(settings: Settings) -> int:
    store = provider_store(settings)
    profiles = store.providers()
    emit_line(f"接口配置：{settings.data_dir / 'providers.json'}", "bold")

    if not profiles:
        # 全新机器就是这种状态：一家都没有，也没有任何"预置的"
        emit_line("还没有配置任何接口。", "yellow")
        emit_line(
            "挑一个预设、填入你自己的 API key：run.cmd --web → 左下角「设置 → 接口」",
            "dim",
        )
        return 0

    default = store.defaults()
    emit_line(
        f"默认使用：{default['provider'] or '(未设置)'} · {default['model'] or '-'}"
        + (f" · thinking {default['effort']}" if default["effort"] else " · thinking 不发送")
    )
    emit_line()
    for profile in profiles:
        mark = "✔" if profile.configured else "✗"
        emit_line(
            f" {mark} {profile.id:<12} {profile.label:<18} {profile.base_url}"
        )
        emit_line(
            f"      key {profile.masked_key() or '(未填)'}"
            f"    模型 {len(profile.models)} 个"
            f"    档位 {'/'.join(profile.thinking_options) or '不发送'}"
            f"    回传思维链 {'是' if profile.send_reasoning_content else '否'}",
            "dim",
        )
    emit_line()
    emit_line("增删改接口请用 Web 界面：run.cmd --web → 设置 → 接口", "dim")
    return 0


def cmd_fetch_models(settings: Settings, provider_id: str) -> int:
    store = provider_store(settings)
    profile = store.get(provider_id)
    if profile is None:
        emit_line(f"没有这个接口：{provider_id}", "red")
        return 1
    emit_line(f"正在向 {profile.base_url}/models 获取模型列表…", "dim")
    try:
        models = fetch_models(profile.base_url, profile.api_key)
    except ProviderError as exc:
        emit_line(f"获取失败：{exc}", "red")
        return 1
    store.update_models(provider_id, models)
    emit_line(f"已保存 {len(models)} 个模型：", "green")
    for model in models:
        emit_line(f"  {model}")
    return 0


def cmd_sessions(settings: Settings) -> int:
    infos = list_sessions(settings.sessions_dir)
    if not infos:
        emit_line("(还没有任何会话)", "dim")
        return 0
    emit_line(f"{'会话 id':<26} {'轮数':>4}  {'标题'}", "bold")
    for info in infos:
        emit_line(f"{info.session_id:<26} {info.turns:>4}  {info.title}")
    return 0


def cmd_memories(settings: Settings) -> int:
    memory = open_memory(settings)
    try:
        stats = memory.stats()
        limit = memory.max_items
        full = "  ← 已满：模型现在记不了新东西" if stats["active"] >= limit else ""
        emit_line(
            f"记忆库：{settings.memory_instruction_path}\n"
            f"有效 {stats['active']}/{limit} 条{full}",
            "bold",
        )
        # 全量注入：条目总量超过 token 预算时会有条目**看不到**。
        # 这件事必须在这里说出来 —— 否则它只存在于模型收到的提示词里。
        _items, omitted = memory.injection_plan(
            max_tokens=settings.memory_token_budget
        )
        if omitted:
            emit_line(
                f"⚠ 记忆总量超过注入预算（{settings.memory_token_budget} token），"
                f"有 {omitted} 条**不会**进入模型上下文 —— 可以调小上限或精简内容",
                "yellow",
            )
        emit_line()
        for item in memory.active_items():
            emit_line(f"  [mem:{item.id}] {item.content}")
        emit_line()
        emit_line(
            "调上限：run.cmd --memory-limit 80  （或到界面：设置 → 记忆）", "dim"
        )
    finally:
        memory.close()
    return 0


def cmd_memory_limit(settings: Settings, value: str) -> int:
    """改记忆条数上限。校验规则与界面**共用同一个纯函数**（ADR-057）。"""
    memory = open_memory(settings)
    try:
        active = memory.stats()["active"]
        current = memory.max_items
        store = MemoryLimitStore(
            settings.data_dir / MEMORY_FILE, default=settings.memory_limit
        )
        change = store.set(value, active_count=active)
        if not change.ok:
            emit_line(change.error, "red")
            return 1
        emit_line(f"记忆上限：{current} → {change.limit}（当前 {active} 条）", "green")
        if change.limit == 0:
            emit_line("上限是 0：模型将无法新增任何记忆。", "yellow")
        return 0
    finally:
        memory.close()


def cmd_usage(settings: Settings, session_id: str | None = None) -> int:
    if session_id:
        paths = [settings.sessions_dir / f"{session_id}.jsonl"]
    else:
        paths = sorted(settings.sessions_dir.glob("*.jsonl"))
    if not paths:
        emit_line("(没有可统计的会话)", "dim")
        return 0
    grand = Usage()
    for path in paths:
        session_total = Usage()
        for record in read_events(path):
            if record.get("kind") == "assistant_message":
                session_total.add(Usage.from_dict(record.get("usage")))
        grand.add(session_total)
        emit_line(f"{path.stem:<26} total {session_total.total_tokens:>8}")
    emit_line()
    emit_line("合计：", "bold")
    emit_line(format_usage(grand, settings))
    return 0


def cmd_attachments_gc(settings: Settings, *, assume_yes: bool = False) -> int:
    """`--attachments-gc`：报告（或删除）没人引用的图片文件。

    为什么清理只能"反过来"做：附件按**内容哈希**寻址，同一张图发两次只有一个文件，
    所以"删掉一条引用"并不等于"这个文件没人要了"（别的消息、别的会话可能还引用着）。
    唯一的可靠做法是先扫出**所有**会话事件里引用过的 `file` 集合，
    再删 `data/attachments/` 下没人认领的那些（契约 §10）。

    **默认只报告**：删文件不可逆，而"没人引用"这个判断依赖会话日志被完整读到
    （日志被手工改坏、或正写到一半，都会让引用暂时看不见）。`--yes` 才真删。
    """
    report = gc_report(settings)
    emit_line("图片附件清理（attachments-gc）", "bold")
    emit_line(f"  附件目录：{display_path(settings.data_dir / 'attachments')}")
    emit_line(
        f"  目录里共 {report['total_files']} 个文件（{_format_bytes(report['total_bytes'])}），"
        f"其中被会话引用的有 {report['referenced']} 个"
    )
    orphans = report["orphans"]
    if not orphans:
        emit_line("  没有被引用的孤儿文件，无需清理。", "green")
        return 0

    emit_line(
        f"  没人引用（孤儿）：{len(orphans)} 个，占 {_format_bytes(report['orphan_bytes'])}",
        "yellow",
    )
    for relative in orphans[:_GC_LIST_LIMIT]:
        emit_line(f"    - {relative}", "dim")
    if len(orphans) > _GC_LIST_LIMIT:
        emit_line(f"    …（另有 {len(orphans) - _GC_LIST_LIMIT} 个未列出）", "dim")

    if not assume_yes:
        emit_line("  默认只报告，什么都没删。确认无误后加 --yes 再跑一次。", "yellow")
        return 0

    result = gc_apply(settings)
    emit_line(
        f"  已删除 {len(result['deleted'])} 个文件（{_format_bytes(result['deleted_bytes'])}）",
        "green",
    )
    for failure in result["failed"]:
        # 删不掉的要报出来 —— 不报就等于把"没删干净"伪装成"清干净了"
        emit_line(f"  ! 没删掉：{failure['file']}（{failure['error']}）", "red")
    return 1 if result["failed"] else 0


def cmd_replay(settings: Settings, session_id: str, *, show_prompts: bool = True) -> int:
    path = settings.sessions_dir / f"{session_id}.jsonl"
    if not path.is_file():
        emit_line(f"找不到会话 {session_id}", "red")
        return 1
    events = read_events(path)
    emit_line(f"会话 {session_id}：{len(events)} 条事件  ({path})", "bold")
    for record in events:
        kind = record.get("kind")
        if kind == "user_message":
            emit_line()
            emit_line(f"[{record['seq']}] 用户：{record.get('text')}", "cyan")
            # 带图的消息必须能看出"这里有一张图"：只贴图不说话的那种，
            # 重放里原本只会显示一行空的"用户："，看起来像日志坏了（契约 §9 同理）。
            # `attachments` 字段的形状不设前提（老日志没有它，手工改过的日志里
            # 还可能是别的类型）——重放**绝不能因为一条坏字段就整个崩掉**。
            items = record.get("attachments")
            # 契约 §13.3：**缺这个键 = "image"**（老日志）。只有"多模态关着"的那一轮
            # 才有 `ocr` 字段，也才需要把识别文字摆出来（§13.7 第 3 条：
            # 用户必须能看见模型当时到底收到了什么）。
            modality = str(record.get("modality") or "image").strip().lower()
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip() or "(未命名)"
                ocr = item.get("ocr")
                if modality == "text" and isinstance(ocr, dict):
                    emit_line(f"          [图片 {name}] 多模态已关，只发了 OCR 文字：", "dim")
                    body = str(ocr.get("text") or "").strip()
                    if body:
                        for line in body.splitlines():
                            emit_line(f"            {line}", "dim")
                    else:
                        emit_line("            （没有识别到任何文字）", "dim")
                    if ocr.get("error"):
                        emit_line(f"            ! OCR 失败：{ocr['error']}", "red")
                else:
                    emit_line(f"          [图片 {name}]", "dim")
        elif kind == "assistant_message":
            message = record.get("message") or {}
            reasoning = message.get("reasoning")
            if reasoning and settings.show_reasoning:
                emit_line(f"[{record['seq']}] 思考：{reasoning}", "dim")
            if message.get("content"):
                emit_line(f"[{record['seq']}] 助手：{message['content']}")
            for call in message.get("tool_calls") or []:
                emit_line(
                    f"[{record['seq']}] 调用：{call.get('name')} {call.get('arguments')}",
                    "yellow",
                )
            emit_line(
                f"          用量：{json.dumps(record.get('usage'), ensure_ascii=False)}",
                "dim",
            )
        elif kind == "tool_call":
            color = "green" if record.get("ok") else "red"
            emit_line(f"[{record['seq']}] 结果：{record.get('display')}", color)
        elif kind == "notice":
            emit_line(f"[{record['seq']}] 提示：{record.get('text')}", "yellow")
        elif kind == "error":
            emit_line(f"[{record['seq']}] 错误：{record.get('error')}", "red")
        elif kind == "turn_end":
            emit_line(
                f"[{record['seq']}] 轮结束：steps={record.get('steps')} "
                f"reason={record.get('stop_reason')} "
                f"elapsed={record.get('elapsed')}s",
                "dim",
            )
        elif kind == "prompt_built" and show_prompts:
            emit_line(
                f"[{record['seq']}] ── 该步实际发送的 prompt"
                f"（约 {record.get('estimated_tokens')} token）──",
                "magenta",
            )
            for index, message in enumerate(record.get("messages") or []):
                body = message.get("content") or ""
                if message.get("tool_calls"):
                    body += " " + json.dumps(message["tool_calls"], ensure_ascii=False)
                emit_line(f"    [{index}] {message.get('role')}: {body}", "dim")
    emit_line()
    emit_line("会话总用量：", "bold")
    total = Usage()
    for record in events:
        if record.get("kind") == "assistant_message":
            total.add(Usage.from_dict(record.get("usage")))
    emit_line(format_usage(total, settings))
    return 0


def cmd_chat(settings: Settings, args: argparse.Namespace, renderer: ConsoleRenderer) -> int:
    runtime = open_runtime(settings, args, args.session)
    if runtime is None:
        return 2
    runtime.loop.emit = renderer
    renderer.show_prompt = args.show_prompt

    effective = runtime.settings
    emit_line(PRODUCT_NAME, "bold")
    emit_line(f"  {effective.describe()}")
    emit_line(
        f"  接口：{runtime.generation.label} · {effective.model}"
        + (f" · thinking {effective.reasoning_effort}" if effective.reasoning_effort else "")
    )
    emit_line(f"  会话：{runtime.session_id}")
    emit_line(f"  记忆库：{settings.memory_instruction_path}（{runtime.memory.stats()['active']} 条有效）")
    emit_line("  命令：/memories  /usage  /prompt  /new  /exit", "dim")
    emit_line()

    session_usage = Usage()
    try:
        while True:
            try:
                line = input(style("你 > ", "cyan"))
            except (EOFError, KeyboardInterrupt):
                emit_line()
                break
            line = line.strip()
            if not line:
                continue

            if line.startswith("/"):
                command = line[1:].strip().lower()
                if command in ("exit", "quit", "q"):
                    break
                if command == "memories":
                    for item in runtime.memory.active_items():
                        emit_line(f"  [mem:{item.id}] {item.content}")
                    if not runtime.memory.active_items():
                        emit_line("  (记忆库为空)", "dim")
                    continue
                if command == "usage":
                    if session_usage.is_empty:
                        emit_line("  本会话还没有用量记录", "dim")
                    else:
                        emit_line("  " + format_usage(session_usage, settings))
                    continue
                if command == "prompt":
                    renderer.show_prompt = not renderer.show_prompt
                    emit_line(f"  显示完整 prompt：{'开' if renderer.show_prompt else '关'}", "dim")
                    continue
                if command == "new":
                    runtime.open_session()
                    runtime.loop.emit = renderer
                    session_usage = Usage()
                    emit_line(f"  已开启新会话：{runtime.session_id}", "dim")
                    continue
                emit_line(f"  未知命令：{line}（可用 /memories /usage /prompt /new /exit）", "yellow")
                continue

            outcome = runtime.loop.run(line)
            session_usage.add(outcome.usage)
            if outcome.stop_reason != "completed":
                emit_line(
                    f"  本轮非正常结束：{outcome.stop_reason}"
                    + (f"（{outcome.error}）" if outcome.error else ""),
                    "yellow",
                )
            emit_line(
                f"  ⏱ {outcome.elapsed:.1f}s  步数 {outcome.steps}  "
                f"token {outcome.usage.total_tokens}",
                "dim",
            )
            emit_line()
    finally:
        emit_line("本会话累计用量：", "bold")
        emit_line("  " + format_usage(session_usage, settings))
        runtime.close()
    return 0


def cmd_once(settings: Settings, args: argparse.Namespace, renderer: ConsoleRenderer) -> int:
    runtime = open_runtime(settings, args, args.session)
    if runtime is None:
        return 2
    runtime.loop.emit = renderer
    renderer.show_prompt = args.show_prompt
    try:
        outcome = runtime.loop.run(args.once)
        emit_line()
        emit_line("本轮用量：", "bold")
        emit_line("  " + format_usage(outcome.usage, runtime.settings))
        emit_line(
            f"  stop_reason={outcome.stop_reason}  steps={outcome.steps}  "
            f"elapsed={outcome.elapsed:.1f}s",
            "dim",
        )
        emit_line(f"  会话：{runtime.session_id}", "dim")
        return 0 if outcome.ok else 1
    finally:
        runtime.close()


def cmd_search(settings: Settings, query: str, *, count: int = 5, url: str = "") -> int:
    """离线调试联网工具：跑一次搜索/抓一个网页，**不经过模型、不花 LLM 钱**。

    它和 `--memories` 是同一类工具：调后端时不用在那儿反复跟模型对话。
    """
    from .net.config import TOOLS_FILE, ToolStore
    from .net.fetch import NetError as FetchError
    from .net.fetch import fetch_url
    from .net.search import search as run_search

    config = ToolStore(settings.data_dir / TOOLS_FILE).web()
    emit_line(f"后端：{config.search_backend}    联网总开关：{'开' if config.enabled else '关'}", "bold")

    if url:
        emit_line(f"抓取：{url}", "cyan")
        try:
            page = fetch_url(
                url,
                timeout=config.fetch_timeout,
                max_chars=config.max_fetch_chars,
                max_bytes=config.max_fetch_bytes,
                allow_private=config.allow_private_hosts,
            )
        except FetchError as exc:
            emit_line(f"  打不开：{exc}", "red")
            return 1
        emit_line(f"  HTTP {page.status}  {page.content_type}")
        if page.title:
            emit_line(f"  标题：{page.title}")
        if page.note:
            emit_line(f"  说明：{page.note}", "yellow")
        emit_line()
        emit_line(page.text[:2000] or "(没抽到正文)")
        if len(page.text) > 2000:
            emit_line(f"…（共 {len(page.text)} 字符，这里只显示前 2000）", "dim")
        return 0

    emit_line(f"搜索：{query}（要 {count} 条）", "cyan")
    try:
        results, backend, note = run_search(
            query,
            count=count,
            backend=config.search_backend,
            api_key=config.tavily_api_key,
            timeout=config.search_timeout,
        )
    except FetchError as exc:
        emit_line(f"  失败：{exc}", "red")
        return 1
    if backend != config.search_backend:
        emit_line(f"  （实际用了 {backend}）", "yellow")
    if note:
        emit_line(f"  说明：{note}", "yellow")
    if not results:
        emit_line("  没有结果。", "red")
        return 1
    for index, item in enumerate(results, 1):
        emit_line(f"  {index}. {item.title}", "bold")
        emit_line(f"     {item.url}", "cyan")
        if item.snippet:
            emit_line(f"     {item.snippet}", "dim")
    return 0


def cmd_dream(settings: Settings, args: argparse.Namespace, renderer: ConsoleRenderer) -> int:
    """跑一次整理。会调用模型，所以必须先打印"要处理什么、花多少预算"再开始。"""
    memory = open_memory(settings)
    try:
        vault = _vault_store(settings)
        pending = pending_sessions(settings.sessions_dir, memory.cursor_map())
        if not pending:
            # 没活干就不要调模型：白花一次钱，历史里还会多一条"什么都没做"的运行
            emit_line("没有待整理的记录（上一轮已经整理到最新）。", "dim")
            emit_line("  想看看当前档案：run.cmd --vault", "dim")
            return 0

        info = summarize_pending(pending)
        limit = args.dream_limit or settings.dream_max_sessions
        emit_line("整理流水线（dream）", "bold")
        emit_line(f"  数据目录：{settings.data_dir}")
        emit_line(
            f"  待整理 {info['sessions']} 个会话 / {info['events']} 条记录"
            f"，本次最多处理 {limit} 个"
        )
        for item in pending[:limit]:
            mark = "  ⚠日志比游标短，按从头整理" if item.stale else ""
            emit_line(
                f"    - {item.session_id}  {item.title[:26]}  +{item.new_events} 条{mark}"
            )
        if len(pending) > limit:
            emit_line(f"    …（另有 {len(pending) - limit} 个会话留到下次）", "dim")
        emit_line(
            f"  预算上限：{settings.dream_max_steps} 步 / "
            f"{settings.dream_token_budget} token / {settings.dream_wall_seconds:.0f} 秒",
            "dim",
        )
        # 运行**之前**就把"不可逆"说清楚（ADR-065/072）—— 没有回滚、也没有预演，
        # 这句话是唯一的安全提示，不能只写在文档里。同时给出"重来"的办法。
        emit_line(
            "  注意：整理结果**不可撤销**（没有回滚，也没有预演）。"
            "如果这一轮改坏了：先备份 data/vault/，再 run.cmd --dream-format 清空重来 —— "
            "档案是会话的投影，重做是可行的，代价是再花一次整理的钱。",
            "yellow",
        )
        emit_line()

        # 整理用哪个模型（ADR-103）：**默认与 Web 走同一个决策点** ——
        # Dream 页设了就用它，没设就跟全局默认。命令行上的 `--provider/--model`
        # 是**这一次**的覆盖（与 `--once` 那套一致），不改持久设置。
        override = bool(args.provider or args.model)
        try:
            if override:
                resolved = resolve_provider(
                    settings,
                    provider_id=args.provider or "",
                    model=args.model or "",
                    effort=args.effort or "",
                )
            else:
                resolved = resolve_dream_provider(settings, effort=args.effort or "")
        except ProviderError as exc:
            emit_line(str(exc), "red")
            return 2
        emit_line(
            f"  使用接口：{resolved.choice.label} · {resolved.settings.model}"
            + (
                f" · thinking {resolved.settings.reasoning_effort}"
                if resolved.settings.reasoning_effort
                else ""
            )
            + ("" if override else "  （来自 Dream 设置或全局默认）"),
            "dim",
        )
        emit_line()

        runner = DreamRunner(
            provider=resolved.provider,
            # 用**折算过的** settings：context_window 已按选中的模型解析，
            # 否则 dream 的窗口保证会拿兜底的 200k 去算（ADR-100）。
            settings=resolved.settings,
            memory=memory,
            vault=vault,
            emit=renderer,
            max_sessions=args.dream_limit or settings.dream_max_sessions,
            generation=resolved.choice.to_dict(),
        )
        outcome = runner.run()

        emit_line()
        emit_line("── 改动摘要 ──", "bold")
        emit_line(render_diff(outcome.diff))
        emit_line()
        emit_line("── 结构校验 ──", "bold")
        emit_line(render_check(outcome.check))
        emit_line()
        if outcome.summary:
            emit_line("模型总结：" + outcome.summary, "cyan")
        emit_line()
        emit_line(f"  运行 id：{outcome.run_id}")
        emit_line(
            f"  stop_reason={outcome.stop_reason}  改动 {outcome.changed} 份档案  "
            f"游标推进 {len(outcome.advanced)} 个会话",
            "dim",
        )
        emit_line("  用量：" + format_usage(outcome.usage, settings))
        emit_line(
            "  整理已生效，**不可撤销**。追回单份文件看 --dream-review 里的轨迹路径；"
            "整层重来用 --dream-format（会连同运行记录与整理进度一起清掉）。",
            "dim",
        )
        return 1 if outcome.error else 0
    finally:
        memory.close()


def cmd_dreams(settings: Settings) -> int:
    """列出历史运行。记录在 `data/dreams/*.json`（不再进数据库）。"""
    runs = DreamRunStore(settings.dreams_dir).list()
    if not runs:
        emit_line("(还没有任何整理运行记录)", "dim")
        return 0
    emit_line(
        f"{'运行 id':<28} {'时间':<20} {'模式':<8} {'状态':<12} {'会话':>4} {'改动':>4}  总结",
        "bold",
    )
    for run in runs:
        diff = run.get("diff") or {}
        changed = sum(len(diff.get(key) or []) for key in ("added", "changed", "removed"))
        pending = run.get("pending") or {}
        summary = str(run.get("summary") or "").replace("\n", " ")[:28]
        emit_line(
            f"{str(run.get('id')):<28} {str(run.get('started_at'))[:19]:<20} "
            f"{str(run.get('mode')):<8} {str(run.get('status')):<12} "
            f"{int(pending.get('sessions') or 0):>4} {changed:>4}  {summary}"
        )
    return 0


def cmd_dream_review(settings: Settings, run_id: str) -> int:
    store = DreamRunStore(settings.dreams_dir)
    run = store.get(run_id)
    if run is None:
        emit_line(f"找不到这次整理运行：{run_id}", "red")
        return 1
    emit_line(f"整理运行 {run_id}", "bold")
    emit_line(
        f"  开始 {run.get('started_at')}  结束 {run.get('finished_at')}"
        f"  模式 {run.get('mode')}  状态 {run.get('status')}"
    )
    pending = run.get("pending") or {}
    emit_line(
        f"  待整理 {pending.get('sessions', 0)} 个会话 / {pending.get('events', 0)} 条记录"
        f"  游标推进 {len(run.get('advanced') or {})} 个"
    )
    emit_line("  用量：" + format_usage(Usage.from_dict(run.get("usage")), settings))
    emit_line()
    emit_line("模型总结：" + (str(run.get("summary") or "") or "(无)"), "cyan")
    emit_line()
    diff = run.get("diff") or {}
    emit_line(render_diff(diff))
    emit_line()
    emit_line(render_check(run.get("check") or {}))

    # 以前这里会打逐行 diff —— 那要靠快照里的"改动前"。
    # ADR-065 取消回滚之后没有快照了，所以改为给你**改动后**的样子，
    # 并指出轨迹（里面存着每一次 vault_write 的全文，那是唯一的追回途径）。
    touched = [row.get("path") for row in (diff.get("added") or [])] + [
        row.get("path") for row in (diff.get("changed") or [])
    ]
    if touched and run.get("mode") != "dry-run":
        emit_line()
        emit_line("── 这些档案现在长这样 ──", "bold")
        for rel in touched[:3]:
            emit_line()
            emit_line(f"── {rel} ──", "dim")
            emit_line(file_preview(settings.vault_dir, str(rel)))
        if len(touched) > 3:
            emit_line(f"…（还有 {len(touched) - 3} 份未显示，用 --vault-show 单独看）", "dim")

    emit_line()
    emit_line("整理结果**不可撤销**（没有回滚，也没有预演）。", "yellow")
    trace = store.trace_path(run_id)
    if trace.is_file():
        emit_line(f"要追回某份文件当时写了什么，看轨迹：{trace}", "dim")
    emit_line("整层重来：run.cmd --dream-format（清空档案 + 运行记录 + 整理进度）", "dim")
    return 0


def cmd_dream_dry_run_gone() -> int:
    """**预演已经取消**（ADR-072）。同 `cmd_dream_undo` 的处理：把"为什么"讲清楚。

    一个被删掉的功能只留一句"未知参数"，用户会以为是坏了。
    """
    emit_line("整理没有预演了 —— 这是刻意的（ADR-072）。", "yellow")
    emit_line(
        "  「预演」要在一份一次性副本里真跑一遍模型（照样花钱），"
        "而它给出的只是「会改什么」的估计 —— 你定的取向是**要能重来**，不是先看一眼。",
        "dim",
    )
    emit_line("  现在改坏了的办法：", "dim")
    emit_line(
        "   * run.cmd --dream-format —— 清空档案 + 运行记录 + 整理进度，"
        "下次整理把所有会话重新整理一遍（会再花一次钱）",
        "dim",
    )
    emit_line(
        "   * 只追回某一份文件：data/dreams/<run-id>.jsonl 里有每一次 vault_write 的全文",
        "dim",
    )
    emit_line("   * 想先备份：整理之前整个复制一份 data/vault/ 就行", "dim")
    return 0


def cmd_dream_undo(settings: Settings, run_id: str) -> int:
    """**这个功能已经取消**（ADR-065：整理是单向门）。

    留着这个函数只为一件事：把"为什么没有了"讲清楚 —— 一个被删掉的功能如果
    只留下一句"未知参数"，用户会以为是坏了。按 ADR-047 的先例，
    报错信息要说清"下一步该做什么"。
    """
    emit_line("整理没有回滚 —— 这是刻意的（ADR-065）。", "yellow")
    emit_line(
        "  快照的唯一用途就是回滚；取消它之后每次运行留一份完整 vault 副本"
        "既白占磁盘、又给人一种'出事能退回去'的错觉。",
        "dim",
    )
    emit_line("  已经改过的档案不会被自动撤销。你还能做的是：", "dim")
    emit_line(
        "   * 看那次运行改了什么：run.cmd --dream-review " + str(run_id),
        "dim",
    )
    emit_line(
        "   * 追回某份文件当时写的内容：data/dreams/<run-id>.jsonl 里有每一次"
        " vault_write 的全文",
        "dim",
    )
    emit_line(
        "   * 整层重来：run.cmd --dream-format —— 清空档案与整理进度，"
        "下次整理会把所有会话重新整理一遍（会再花一次钱）",
        "dim",
    )
    return 0


def cmd_dream_format(settings: Settings, *, assume_yes: bool = False) -> int:
    """**格式化并初始化 dream 层**（ADR-072）。只动 dream 那三样东西。

    这是**不可逆**的：档案（包括你手写的那几份）、运行记录、整理进度一起没。
    所以它要求你**打一遍那个词**才动手 —— 不是安全机制，是减速带：
    本机单用户，能跑这个程序的人本来就能删文件；这一句是为了让你停一下。
    """
    from .dream import dream_format_confirm, format_dream
    from .reset import dream_state

    # 确认词跟着**界面语言**走（ADR-085）：中文是「格式化」，英文是 `format`。
    # CLI 自己的输出保持中文（契约 §1 的例外），但用户要打的这个词必须和
    # 界面上要求的一致 —— 它是同一道闸。
    confirm_word = dream_format_confirm()

    state = dream_state(settings)
    emit_line("格式化并初始化 dream 层", "bold")
    emit_line(f"  档案目录：{display_path(settings.vault_dir)}")
    emit_line(f"  运行记录：{display_path(settings.dreams_dir)}")
    emit_line(f"  整理进度：记忆库里的 dream_cursor 表", "dim")
    emit_line()
    emit_line("会清掉：", "yellow")
    emit_line(f"  * 档案 {state['vault_files']} 份（**包括你手写的那几份**）")
    emit_line(f"  * 整理运行记录 {state['dream_runs']} 次（含轨迹 —— 那是追回内容的唯一凭据）")
    emit_line(f"  * 整理进度 {state['cursor_sessions']} 个会话（下次整理会**从头**再整理一遍）")
    emit_line("不会碰：", "green")
    emit_line("  * 对话历史、记忆条目、接口配置与 API key、工具开关", "dim")
    emit_line()
    emit_line(
        "为什么这不是灾难：档案是**会话的投影**，重新整理一遍就能重做出来 —— "
        "代价是再花一次整理的钱，而手写的内容不会回来。",
        "dim",
    )
    emit_line()

    if not assume_yes:
        emit_line(f"确认请在下面输入「{confirm_word}」（其它任何输入都会取消）：", "yellow")
        try:
            answer = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            emit_line("  已取消。", "dim")
            return 1
        if answer != confirm_word:
            emit_line(f"  输入的是「{answer}」，不是「{confirm_word}」—— 已取消，什么都没动。", "dim")
            return 1
    else:
        emit_line("（--yes：跳过二次确认）", "dim")

    memory = open_memory(settings)
    try:
        result = format_dream(settings, memory)
    finally:
        memory.close()

    emit_line()
    emit_line("已格式化并初始化：", "green")
    emit_line(f"  档案 {result['before']['vault_files']} → {result['after']['vault_files']} 份")
    emit_line(f"  运行记录 {result['before']['dream_runs']} → {result['after']['dream_runs']} 次")
    emit_line(
        f"  整理进度 {result['before']['cursor_sessions']} → "
        f"{result['after']['cursor_sessions']} 个会话"
    )
    if result["initialized"]:
        emit_line("  已建好空骨架（sections/、entities/ 与一份占位 README）", "dim")
    if result["leftovers"]:
        emit_line(
            "  ⚠ 下面这些没删掉（里面是刚清掉的旧内容，请手动删）：", "yellow"
        )
        for item in result["leftovers"]:
            emit_line(f"    {item}", "dim")
    emit_line()
    emit_line("下一步：run.cmd --dream 会把所有会话重新整理一遍（会花钱）。", "dim")
    return 0


# --------------------------------------------------------------------------
# 背景档案（dream vault）
# --------------------------------------------------------------------------


def _vault_store(settings: Settings) -> VaultStore:
    return VaultStore(
        settings.vault_dir,
        read_max_chars=settings.vault_read_max_chars,
        write_max_chars=settings.vault_write_max_chars,
        max_files=settings.vault_max_files,
        name_max_chars=settings.vault_name_max_chars,
        readme_token_budget=settings.vault_readme_token_budget,
    )


def cmd_vault(settings: Settings) -> int:
    """看一眼后台整理的档案：有哪些文件、README 占多少预算、结构上有没有毛病。"""
    store = _vault_store(settings)
    report = store.check()
    emit_line("背景档案（dream memory）", "bold")
    emit_line(f"  目录：{settings.vault_dir}")

    # 顺带报"还有多少没整理"：这是用户决定要不要跑一次 --dream 的唯一依据，
    # 而它只有在**同一屏**里看到才有用（不然得跑两个命令）。
    memory = open_memory(settings)
    try:
        info = summarize_pending(pending_sessions(settings.sessions_dir, memory.cursor_map()))
    finally:
        memory.close()
    if info["sessions"]:
        emit_line(
            f"  待整理：{info['sessions']} 个会话 / {info['events']} 条记录"
            "（run.cmd --dream 开始整理）",
            "yellow",
        )
        if info.get("stale"):
            emit_line(
                f"  ⚠ 其中 {info['stale']} 个会话的日志比游标还短（被改过或换过），"
                "已按「从头整理」处理 —— 那次会多花一点，但不这么做那些记录会永久读不到。",
                "yellow",
            )
    else:
        emit_line("  待整理：无（已经整理到最新）", "dim")

    if not report.get("exists"):
        emit_line(
            "  档案还没有建立。整理流程会在这里写 README 与实体档案；"
            "想先有个空骨架可以跑 --vault-init。",
            "dim",
        )
        return 0

    emit_line(
        f"  档案 {report.get('files', 0)} 个（上限 {settings.vault_max_files}）"
        + (
            ""
            # "没有 README"只在**有档案**时才值得提：空 vault 那句话已经说了
            if report.get("has_readme") or not report.get("files")
            else "  ← 没有 README，模型看不到索引"
        )
    )
    char_cap = int(report.get("readme_char_cap") or 0)
    if char_cap:
        emit_line(
            f"  README {int(report.get('readme_chars') or 0)}/{char_cap} 字符"
            + (
                "  ← 已超注入上限，注入时会被截断"
                if report.get("readme_truncated")
                else ""
            )
        )
    else:
        used = int(report.get("readme_tokens") or 0)
        budget = int(report.get("readme_budget") or 0)
        emit_line(
            f"  README {used}/{budget} token"
            + ("  ← 已超预算，注入时会被截断" if report.get("readme_truncated") else "")
        )
    manifest = report.get("manifest") or {}
    if manifest:
        emit_line(
            f"  sections {len(manifest.get('sections') or [])} 个"
            f"（inject_char_cap {manifest.get('inject_char_cap')}）",
            "dim",
        )
    if report.get("raw_files"):
        emit_line(f"  raw 素材 {report['raw_files']} 个", "dim")
    emit_line()
    entries = [entry for entry in store.list_files() if entry.path != README_NAME]
    if entries:
        for entry in entries:
            kind = f"[{entry.kind}]" if entry.kind else ""
            emit_line(f"    {entry.path:<28} {entry.title[:18]:<20} {kind} {entry.summary}")
    elif report.get("has_readme"):
        # README 是索引，不是一个"档案"：只有它的时候等于什么都还没有
        emit_line("  (只有 README，还没有实体档案 —— 这种状态下不会注入给模型)", "dim")
    else:
        emit_line("  (vault 是空的：既没有档案也没有 README —— 不会注入给模型)", "dim")

    problems = report.get("problems") or []
    if problems:
        emit_line()
        emit_line("结构校验发现的问题：", "yellow")
        for problem in problems:
            emit_line(f"  - {problem}", "yellow")
    elif report.get("files") or report.get("has_readme"):
        emit_line()
        emit_line("结构校验通过。", "green")
    else:
        emit_line()
        emit_line("（vault 还是空的，没有可校验的内容）", "dim")
    return 0


def cmd_vault_show(settings: Settings, rel: str) -> int:
    """打印一份档案的原文。离线调试用，不花钱。"""
    store = _vault_store(settings)
    try:
        file = store.read(rel)
    except FileNotFoundError:
        emit_line(f"找不到这份档案：{rel}", "red")
        return 1
    except VaultPathError as exc:
        emit_line(str(exc), "red")
        return 2
    emit_line(f"── {file.path} ──", "bold")
    if file.frontmatter.to_dict():
        emit_line(json.dumps(file.frontmatter.to_dict(), ensure_ascii=False), "dim")
    emit_line(file.body)
    for warning in file.warnings:
        emit_line(f"[警告] {warning}", "yellow")
    if file.truncated:
        emit_line("（正文超过单次读取上限，已截断）", "dim")
    return 0


def cmd_vault_init(settings: Settings) -> int:
    """建立空骨架：只有目录与一份占位 README，**不预置任何"内容"**。"""
    store = _vault_store(settings)
    created = store.init_skeleton()
    if not created:
        emit_line(f"背景档案已经在了：{settings.vault_dir}", "dim")
        return 0
    emit_line(f"已建立背景档案骨架：{settings.vault_dir}", "green")
    for item in created:
        emit_line(f"  + {item}")
    emit_line(
        "（README 的正文由第一次整理按 sections 生成；现在这份只是占位，"
        "所以对话模型还看不到任何档案内容。）",
        "dim",
    )
    return 0


def cmd_vault_import(settings: Settings, zip_path: str, *, assume_yes: bool = False) -> int:
    """导入原生 Kimi Dream vault（zip）。**只写空 vault**，除非显式 --yes。"""
    from .vault_import import VaultImportError, import_vault_zip

    store = _vault_store(settings)
    try:
        result = import_vault_zip(zip_path, store, overwrite=assume_yes)
    except VaultImportError as exc:
        emit_line(str(exc), "red")
        return 2
    emit_line(
        f"已导入 {result['files']} 个文件"
        f"（档案 {result['notes']} / raw {result['raw']}）：{result['path']}",
        "green",
    )
    emit_line(
        f"  原生格式：{'是（有 sections.yaml）' if result.get('native') else '否（没有 sections.yaml）'}",
        "dim",
    )
    for warning in result.get("warnings") or []:
        emit_line(f"  ⚠ {warning}", "yellow")
    report = store.check()
    problems = report.get("problems") or []
    if problems:
        emit_line("结构校验发现的问题：", "yellow")
        for problem in problems:
            emit_line(f"  - {problem}", "yellow")
    else:
        emit_line("结构校验通过。", "green")
    return 0


def cmd_vault_rebuild(settings: Settings) -> int:
    """重建原生 vault 的 README / index / earlier_months。旧 vault 拒绝。"""
    store = _vault_store(settings)
    if not store.is_native():
        emit_line(
            "这个 vault 没有 sections.yaml（旧格式），不重建 README/index。"
            "旧档案继续由模型写 README；要迁移请手工加 sections.yaml。",
            "yellow",
        )
        return 1
    result = store.rebuild_generated()
    emit_line(
        f"已重建 README / index / earlier_months（sections {len(result['sections'])} 个）",
        "green",
    )
    report = store.check()
    problems = report.get("problems") or []
    if problems:
        emit_line("结构校验发现的问题：", "yellow")
        for problem in problems:
            emit_line(f"  - {problem}", "yellow")
    else:
        emit_line("结构校验通过。", "green")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.cli",
        description=f"{PRODUCT_NAME}（对标 Kimi.com 网页端的服务端编排层）",
    )
    parser.add_argument("--session", help="指定会话 id（默认新建一个）")
    # 版本号从 `docs/version.txt` 读（`harness.version()`）—— 命令行与界面**同一个来源**，
    # 不会出现"界面显示 1.0.0-rc1、--version 还写着旧号"的情况。
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PRODUCT_NAME} {version()}",
        help="打印版本号并退出",
    )
    parser.add_argument("--once", metavar="TEXT", help="非交互：只跑一轮然后退出")
    parser.add_argument("--sessions", action="store_true", help="列出所有会话")
    parser.add_argument("--replay", metavar="SESSION", help="重放指定会话（含当时发送的 prompt）")
    parser.add_argument("--memories", action="store_true", help="列出记忆库（含当前上限占用）")
    parser.add_argument(
        "--memory-limit",
        metavar="N",
        help="设置记忆条数上限（0-100，不能低于已有条数）",
    )
    parser.add_argument(
        "--search", metavar="QUERY", help="离线调试联网搜索：跑一次后端并打印结果（不花 LLM 钱）"
    )
    parser.add_argument("--open-url", metavar="URL", help="配合 --search：改为抓取并打印该网页正文")
    parser.add_argument("--search-count", type=int, default=5, help="配合 --search：要几条结果（1-10）")
    parser.add_argument(
        "--dream", action="store_true", help="跑一次整理：会话记录 → 背景档案（会调用模型，产生费用）"
    )
    parser.add_argument(
        "--dream-dry-run",
        action="store_true",
        help="已取消：没有预演了（ADR-072）；要重来就用 --dream-format",
    )
    parser.add_argument(
        "--dream-limit", type=int, help="配合 --dream：本次最多整理几个会话（不是几条记忆）"
    )
    parser.add_argument("--dreams", action="store_true", help="列出历史整理运行")
    parser.add_argument("--dream-review", metavar="RUN", help="查看某次整理改了哪些档案（含结构校验与轨迹路径）")
    parser.add_argument(
        "--dream-format",
        action="store_true",
        help="格式化并初始化 dream 层：清掉档案 + 运行记录 + 整理进度（不可逆，要二次确认）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="配合 --dream-format / --attachments-gc：跳过二次确认、真的动手删（给脚本用）",
    )
    parser.add_argument(
        "--attachments-gc",
        action="store_true",
        help="清理没人引用的图片附件：默认只报告孤儿文件与占用，加 --yes 才真删",
    )
    parser.add_argument(
        "--dream-undo",
        metavar="RUN",
        help="已取消：整理是单向门、不可回滚（ADR-065）；加上它只会看到解释",
    )
    parser.add_argument(
        "--vault",
        action="store_true",
        help="查看背景档案（dream memory）：文件清单、README 预算、结构校验",
    )
    parser.add_argument(
        "--vault-show", metavar="PATH", help="打印一份背景档案（如 entities/小林.md）"
    )
    parser.add_argument(
        "--vault-init", action="store_true", help="建立背景档案的空骨架（不写任何内容）"
    )
    parser.add_argument(
        "--vault-import",
        metavar="ZIP",
        help="导入原生 Kimi Dream vault（zip；目标必须是空 vault，--yes 覆盖）",
    )
    parser.add_argument(
        "--vault-rebuild",
        action="store_true",
        help="按 sections.yaml 重建 README / index / earlier_months（仅原生 vault）",
    )
    parser.add_argument("--web", action="store_true", help="启动 Web 界面（默认只监听本机）")
    parser.add_argument(
        "--guided-venv",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="配合 --web：默认检查并引导安装项目 venv；--no-guided-venv 可显式关闭",
    )
    parser.add_argument("--port", type=int, default=8787, help="Web 端口，默认 8787；0 = 自动选空闲端口")
    parser.add_argument("--host", default="127.0.0.1", help="Web 监听地址，默认仅本机 127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="Web 启动时不自动打开浏览器")
    parser.add_argument("--usage", action="store_true", help="汇总 token 用量")
    parser.add_argument("--show-prompt", action="store_true", help="显示每轮实际发送的 prompt")
    parser.add_argument("--no-thinking", action="store_true", help="不显示思维链")
    parser.add_argument("--no-prompt-in-replay", action="store_true", help="重放时不打印 prompt")
    parser.add_argument("--plain", action="store_true", help="禁用颜色")
    parser.add_argument("--provider", help="用哪家接口（见 --providers）")
    parser.add_argument("--providers", action="store_true", help="列出已配置的接口（含默认模型）")
    parser.add_argument("--fetch-models", metavar="PROVIDER", help="向该接口的 /models 拉取模型列表并保存")
    parser.add_argument("--model", help="覆盖模型名（缺省用该接口保存的默认）")
    parser.add_argument("--effort", help="thinking 档位：low / high / max / off（off 表示不下发）")
    return parser


def main(argv: list[str] | None = None) -> int:
    global _COLOR_ENABLED

    args = build_parser().parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    _COLOR_ENABLED = (not args.plain) and _enable_ansi()

    settings = Settings.load()
    # 界面语言（ADR-085）：**模型侧的文案跟着它走**（提示词、工具定义、工具回给模型的文本）。
    # CLI 自己的输出保持中文（它是开发/调试工具，见契约 §1），所以这里只设语言、不改渲染。
    # 必须在这里设一次：进程一起来就得知道用哪套提示词。
    i18n.apply_settings(settings)
    settings = replace(settings, language=i18n.current())
    # 注意：接口/模型/档位不再改 Settings，而是交给 resolve_provider
    # （它读 data/providers.json）。这里只处理与显示有关的开关。
    if args.no_thinking:
        settings = replace(settings, show_reasoning=False)

    # 工具配置完整性自检（ADR-114）：CLI 入口与 Web serve 都过这道闸，
    # 保证"启动时检查"对两种前端成立。修复说明打到终端（静默自愈也是一种
    # "悄悄降级"，用户至少该知道哪份配置坏过）。
    for kind, sid in ToolConfigStore(settings.data_dir).check_all(settings.sessions_dir):
        if kind == "default":
            emit_line("! 默认工具配置缺失或损坏，已按全部启用重建。", "yellow")
        else:
            emit_line(f"! 会话 {sid} 的工具配置缺失或损坏，已从默认配置重建。", "yellow")

    renderer = ConsoleRenderer(settings, show_prompt=args.show_prompt)

    # 不需要模型后端的子命令
    if args.sessions:
        return cmd_sessions(settings)
    if args.memories:
        return cmd_memories(settings)
    if args.providers:
        return cmd_providers(settings)
    if args.fetch_models:
        return cmd_fetch_models(settings, args.fetch_models)
    if args.memory_limit:
        return cmd_memory_limit(settings, args.memory_limit)
    if args.search or args.open_url:
        return cmd_search(
            settings,
            args.search or "",
            count=args.search_count,
            url=args.open_url or "",
        )
    if args.dreams:
        return cmd_dreams(settings)
    if args.dream_review:
        return cmd_dream_review(settings, args.dream_review)
    if args.dream_undo:
        return cmd_dream_undo(settings, args.dream_undo)
    if args.dream_dry_run:
        return cmd_dream_dry_run_gone()
    if args.dream_format:
        return cmd_dream_format(settings, assume_yes=args.yes)
    if args.attachments_gc:
        return cmd_attachments_gc(settings, assume_yes=args.yes)
    if args.vault:
        return cmd_vault(settings)
    if args.vault_show:
        return cmd_vault_show(settings, args.vault_show)
    if args.vault_init:
        return cmd_vault_init(settings)
    if args.vault_import:
        return cmd_vault_import(settings, args.vault_import, assume_yes=args.yes)
    if args.vault_rebuild:
        return cmd_vault_rebuild(settings)
    if args.usage:
        return cmd_usage(settings, args.session)
    if args.replay:
        return cmd_replay(
            settings, args.replay, show_prompts=not args.no_prompt_in_replay
        )

    if args.web:
        if not provider_store(settings).providers():
            emit_line(
                "提示：还没有配置任何接口（本项目不预置 key）。"
                "界面能打开，进去后到左下角「设置 → 接口」挑一个预设、填入你自己的 key。",
                "yellow",
            )
        return serve_web(
            settings,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            guide_venv=args.guided_venv,
        )

    if not provider_store(settings).providers():
        emit_line(NO_PROVIDER_HELP, "red")
        return 2

    try:
        if args.dream:
            return cmd_dream(settings, args, renderer)
        if args.once:
            return cmd_once(settings, args, renderer)
        return cmd_chat(settings, args, renderer)
    except ProviderError as exc:
        emit_line(f"模型后端错误：{exc}", "red")
        return 3
    except KeyboardInterrupt:
        emit_line()
        emit_line("已中断。", "dim")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
