"""Agent 循环：一次输入 → 若干步「模型思考 / 调工具 / 再思考」 → 最终回复。

这个文件是 harness 的心脏。它只做编排，不认识任何具体模型或具体工具。

**两个 agent 共用同一个循环**：
  * 对话 agent（默认 planner）：每轮开始时按 M2 规则规划一次记忆注入，整轮固定不变；
  * dream agent（M4）：`dream.py` 注入自己的 planner，去做记忆巩固。
这样四道熔断、工具执行、流式拼装、事件日志**只有一份实现**，不会各自漂移 ——
熔断逻辑尤其不能有两份，那是"钱"的防线。

四道熔断（都是为了防止"半夜自己循环烧钱"）：
  1. max_steps           单轮最多几步工具调用
  2. max_tokens_per_turn 单轮累计 token 上限
  3. max_wall_seconds    单轮墙钟上限
  4. 重复调用检测         连续 3 次完全相同的工具调用即中止
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence

from .config import Settings
from .context import (
    build_messages,
    render_for_summary,
    split_into_groups,
)
from .i18n import t
from .prompts import bundle
from .events import CoveredMark, EventLog
from .llm_types import (
    Message,
    ReasoningDelta,
    StreamAssembler,
    TextDelta,
    Usage,
    estimate_messages_tokens,
)
from .memory import MemoryStore
from .providers.base import LLMProvider, ProviderError
from .tools.registry import ToolRegistry

Emit = Callable[..., None]

_REPEAT_LIMIT = 3

# 服务端**明确拒绝这次请求**的状态码（契约 §13.5）。只有这些才动历史：
# 400 参数/格式不对、404 模型不存在、415 不支持的媒体类型、422 语义上不接受。
# `429`（限流）与 `5xx`（服务端故障）是暂时的，图还在，重试就好。
_IMAGE_REJECTION_STATUSES = frozenset({400, 404, 415, 422})


class Planner(Protocol):
    """决定"这一步要发给模型什么消息"。

    返回值第二项是要一并写进 `prompt_built` 事件的额外字段，
    这样不同 agent 的提示词决策都能在重放时看到。
    """

    def __call__(
        self, history: list[Message], step: int, user_text: str
    ) -> tuple[list[Message], dict[str, Any]]:
        ...


def _silent_emit(kind: str, **data: Any) -> None:  # pragma: no cover - 默认空实现
    return None


@dataclass
class TurnOutcome:
    text: str = ""
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    elapsed: float = 0.0
    stop_reason: str = "completed"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.stop_reason == "completed"


class AgentLoop:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: ToolRegistry,
        settings: Settings,
        memory: MemoryStore,
        log: EventLog,
        emit: Emit | None = None,
        planner: Planner | None = None,
        web_tools_text: str = "",
        vault_text: str = "",
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.settings = settings
        self.memory = memory
        self.log = log
        self.session_id = log.session_id
        self.emit: Emit = emit or _silent_emit
        self._planner: Planner = planner or self.plan_chat_turn
        # 系统提示里关于联网工具的那一段（由 Runtime 按配置生成）
        self.web_tools_text = web_tools_text
        # 背景档案（dream memory）那一整段：README 的正文已经拼在里面。
        # 与 web_tools_text 同样由 Runtime 生成 —— 循环不认识 vault，
        # 只负责把这段字节原样放进 system，这样"什么内容进提示词"只有一个决策点。
        self.vault_text = vault_text
        # 本轮固定的记忆块（整轮只渲染一次，见 plan_chat_turn）
        self._turn_memory_text: str | None = None
        # 本轮的压缩摘要（如果有）
        self._turn_summary: str = ""
        # 本轮固定的时间戳（run() 开头设置，见 build_tail_message）
        self._turn_now: datetime = datetime.now().astimezone()
        # 本轮为压缩花掉的 token，计入回合用量
        self._compact_usage = Usage()

    # ------------------------------------------------------------------
    # 对话 agent 的 planner
    # ------------------------------------------------------------------

    def plan_chat_turn(
        self, history: list[Message], step: int, user_text: str
    ) -> tuple[list[Message], dict[str, Any]]:
        """对话路径：整轮只在第一步渲染一次记忆块。

        ADR-009 的两个理由：① system 消息在整轮内字节一致，第 2 步起全额命中
        前缀缓存；② 避免"刚写入的记忆立刻出现在已有记忆里"让模型怀疑自己重复写入。

        记忆是**全量注入**的（ADR-058）—— 条数上限就是那道闸，不再按相关性挑选。
        """
        turn_index = sum(1 for message in history if message.role == "user")
        # 工具写入需要知道"哪轮对话写了什么记忆"
        self.memory.set_context(self.session_id, turn_index)

        if self._turn_memory_text is None:
            budget = self.settings.memory_token_budget
            # 先问一次"实际会注入哪些、有几条因预算看不到"，再渲染 ——
            # 这两件事必须来自同一次判断，否则界面上的数字会和实际注入不符。
            _items, omitted = self.memory.injection_plan(max_tokens=budget)
            self._turn_memory_text = self.memory.render_for_prompt(max_tokens=budget)
            self.emit(
                "memory",
                count=len(self.memory.active_items()),
                limit=self.memory.max_items,
                omitted=omitted,
            )

        memory_text = self._turn_memory_text
        messages, omitted_groups = build_messages(
            self.settings,
            history,
            memory_text,
            summary_text=self._turn_summary,
            web_tools=self.web_tools_text,
            vault_text=self.vault_text,
            now=self._turn_now,
        )
        return messages, {
            "omitted_groups": omitted_groups,
            "memory_text": memory_text,
            "memory_count": len(self.memory.active_items()),
            "memory_limit": self.memory.max_items,
            "summary_chars": len(self._turn_summary),
            # README 的全文也写进轨迹：`--replay` 要能看到"当时到底注入了什么"
            "vault_text": self.vault_text,
        }

    # ------------------------------------------------------------------
    # 自动 compact
    # ------------------------------------------------------------------

    def _maybe_compact(
        self, history: list[Message], summary_text: str, mark: CoveredMark
    ) -> bool:
        """历史接近预算上限时，把较早的那部分压成摘要。

        触发点取 `settings.history_budget()` —— 它是 **成本上限** 与
        **本轮这个模型的上下文窗口** 两者中的较小值。所以"在哪里 compact"
        确实取决于用户这轮选了什么模型：换个 32K 窗口的模型会明显更早触发。

        无法压缩时（只剩一组、模型调用失败）返回 False，
        由 `trim_history` 兜底按组丢弃，行为与 M5 之前一致。
        """
        if not self.settings.compact_enabled:
            return False

        budget = self.settings.history_budget()
        threshold = int(budget * self.settings.compact_threshold)
        if estimate_messages_tokens(history) <= threshold:
            return False

        groups = split_into_groups(history)
        if len(groups) < 2:
            return False  # 只有当前这一组，压不了

        target = int(budget * self.settings.compact_target)
        keep: list[list[Message]] = []
        used = 0
        for group in reversed(groups):
            cost = estimate_messages_tokens(group)
            if keep and used + cost > target:
                break
            keep.append(group)
            used += cost
        keep.reverse()
        fold_groups = groups[: len(groups) - len(keep)]
        if not fold_groups:
            return False

        # 再受一次"摘要输入长度"的约束，而且**按整组退让、绝不截断文本**。
        # 截断会造成最坏的一种丢失：被截掉的部分既没进摘要，又被标成"已覆盖"，
        # 于是永久消失 —— 那正是这个功能要消灭的东西。
        max_chars = self.settings.compact_max_input_chars
        chosen: list[list[Message]] = []
        used_chars = 0
        for group in fold_groups:
            cost_chars = len(render_for_summary(group, max_chars=None))
            if not chosen:
                if cost_chars > max_chars:
                    self._notice(t("ui.loop.early_group_too_long"), "warn")
                    return False
            elif used_chars + cost_chars + 1 > max_chars:
                break
            chosen.append(group)
            used_chars += cost_chars + 1
        fold_groups = chosen
        if not fold_groups:
            return False

        folded = [message for group in fold_groups for message in group]
        if not folded:
            return False

        self._notice(
            t(
                "ui.loop.compacting",
                tokens=estimate_messages_tokens(history),
                budget=budget,
                count=len(folded),
            )
        )
        summary, usage = self._summarize(folded, summary_text)
        if not summary:
            self._notice(t("ui.loop.compact_failed"), "warn")
            return False

        self.log.append(
            "session_summary",
            summary=summary,
            # 水位线以**组数**为准：组内补一条 tool 结果不会让边界错位
            covered_groups=mark.groups + len(fold_groups),
            covered_messages=mark.messages + len(folded),
            folded_groups=len(fold_groups),
            folded_messages=len(folded),
            budget=budget,
            model=self.settings.model,
            usage=usage.to_dict(),
        )
        self._compact_usage.add(usage)
        self.emit(
            "compact",
            summary=summary,
            folded=len(folded),
            covered=mark.messages + len(folded),
            usage=usage.to_dict(),
        )
        return True

    def _summarize(
        self, folded: list[Message], previous_summary: str
    ) -> tuple[str, Usage]:
        """让模型把这段历史压成摘要。返回 (摘要文本, 用量)。"""
        parts: list[str] = []
        if previous_summary:
            parts.append(bundle().COMPACT_PREVIOUS.format(summary=previous_summary))
        parts.append(
            bundle().COMPACT_INPUT.format(
                text=render_for_summary(
                    folded, max_chars=self.settings.compact_max_input_chars
                )
            )
        )
        prompt = [
            Message(role="system", content=bundle().COMPACT_SYSTEM_PROMPT),
            Message(role="user", content="\n\n".join(parts)),
        ]
        assembler = StreamAssembler()
        try:
            for event in self.provider.stream(
                prompt, [], max_tokens=self.settings.compact_max_output_tokens
            ):
                assembler.feed(event)
        except ProviderError as exc:
            self.log.append("error", error=t("ui.loop.summarize_failed", error=exc))
            return "", Usage()
        return assembler.text.strip(), assembler.usage or Usage()

    # ------------------------------------------------------------------

    def run(
        self,
        user_text: str,
        attachments: Sequence[dict[str, Any]] = (),
        *,
        modality: str = "image",
    ) -> TurnOutcome:
        """跑一轮对话。

        `attachments` 是**已经落盘、已经校验过**的附件记录（形状见契约 §3）——
        循环不认识文件、也不认识图片格式，它只负责把这份记录如实写进事件日志、
        从而在重放时把图片重新挂回那条用户消息。校验与落盘在 web 层/attachments 层，
        这样"哪张图能发"只有一个判断点。

        `modality` 是这一轮图片怎么进请求（契约 §13.3）：`"image"` = 图片直接发，
        `"text"` = 只发本地 OCR 文字。它**只记成两种值**：事件日志是 append-only 的，
        往里写第三种值等于让之后所有读端都要猜它是什么意思。缺键 = `"image"`
        （老日志），所以没有附件时连这个键都不写（写它只是噪音）。
        """
        started = time.monotonic()
        # 归一化成契约规定的两种取值。读端把任何非 "text" 都当 "image"，
        # 所以这里不归一也不会读错，只是会往日志里留一个没人认识的值。
        mode = "text" if str(modality or "").strip().lower() == "text" else "image"
        # 附件记录为空时**不写这个字段**：老日志没有它、含义是"没有附件"，
        # 而写一个空数组会让"这条消息有没有图"出现两种等价但不同的表示。
        if attachments:
            record = self.log.append(
                "user_message",
                text=user_text,
                attachments=list(attachments),
                modality=mode,
            )
        else:
            record = self.log.append("user_message", text=user_text)
        # 这一轮的 `user_message` 序号。图片没送出去时要靠它把 `images_undelivered`
        # 对到**具体哪条消息**上（契约 §13.5）—— 对错了就等于改写了别的历史。
        user_seq = record.get("seq")
        # 本轮是不是把**图片里的文字**（OCR）当成内容送给了模型。
        # OCR 文字是外部可控输入（别人发来的一张图、一张恶意网页截图就能带指令进来），
        # 所以它和"联网抓回来的网页"（ADR-048）是同一类东西，要留同形的审计痕迹。
        ocr_seen = any(
            isinstance(item, dict) and "ocr" in item for item in (attachments or ())
        )
        self._turn_memory_text = None
        self._compact_usage = Usage()
        # 时间在**整轮开始时固定一次**：每步各算一次的话，轮内跨分钟会让第 2 步的
        # prompt 不再是第 1 步的前缀，缓存白白失效（ADR-009 的实测命中率就是这么掉的）。
        self._turn_now = datetime.now().astimezone()

        # 压缩是优化，**绝不能因为它让这一轮失败**：任何异常都降级为"不压缩"，
        # 由 trim_history 按组丢弃兜底，并且照常走到 turn_end。
        self._turn_summary = ""
        try:
            summary_text, history, mark = self.log.compacted_context()
            if self._maybe_compact(history, summary_text, mark):
                # 压缩改变了上下文，重新读一次
                summary_text, history, mark = self.log.compacted_context()
            self._turn_summary = summary_text
        except Exception as exc:  # noqa: BLE001 - 压缩不该有能力弄坏一轮对话
            self._turn_summary = ""
            history = self.log.conversation()
            self._notice(
                f"压缩阶段出错，本轮跳过压缩：{type(exc).__name__}: {exc}", "warn"
            )

        total_usage = Usage()
        step = 0
        # 与循环变量 step 区分：step 是"第几次迭代"，steps_run 才是真正发生的模型调用次数。
        # 预算熔断会在下一次迭代开头触发，此时 step 已经自增，不能拿来当步数上报。
        steps_run = 0
        stop_reason = "completed"
        error: str | None = None
        final_text = ""
        last_signature: tuple[Any, ...] | None = None
        repeated = 0
        truncated = False
        # 第五道熔断的计数器必须**跨步累计**：失败往往是一步一次（模型看到失败、
        # 换个 URL 再试），每步清零的话"连续 4 步各失败一次"永远触发不了 ——
        # 而那正是这条熔断为之而生的场景（实测一轮连开 4 个打不开的地址、烧光 8 步）。
        failed_streak = 0
        # 本轮是否已经出现过联网工具的结果；以及"联网后又写记忆"的审计提示是否已发过。
        web_result_seen = False
        web_memory_audited = False
        # 同上，但针对的是图片 OCR 文字（同一类不可信输入，契约 §13.4）。
        ocr_memory_audited = False
        # 尚未拿到结果的工具调用（用于中止时补桩，见 _stub_unexecuted_calls）
        pending_calls: list[Any] = []
        # 这一步实际发给模型的消息。异常处理里要用它判断"这次请求里到底有没有图片"
        # （契约 §13.5 的第一个条件），所以在循环外先给一个空值。
        messages: list[Message] = []

        try:
            for step in range(1, self.settings.max_steps + 1):
                elapsed = time.monotonic() - started
                if elapsed > self.settings.max_wall_seconds:
                    stop_reason = "wall_clock"
                    self._notice(t("ui.loop.wall_clock", seconds=elapsed), "warn")
                    break
                if total_usage.total_tokens > self.settings.max_tokens_per_turn:
                    stop_reason = "token_budget"
                    self._notice(
                        t("ui.loop.token_budget", tokens=total_usage.total_tokens),
                        "warn",
                    )
                    break

                messages, extra = self._planner(history, step, user_text)
                estimated = estimate_messages_tokens(messages)
                self.log.append(
                    "prompt_built",
                    step=step,
                    estimated_tokens=estimated,
                    # 用 to_prompt_record 而不是 to_api：**带图消息的 base64 绝不能进日志**
                    # （契约 §5）。每步都写一次，一张图就能把 JSONL 撑成几 MB，
                    # 之后 grep、--replay、乃至 data/ 的可搬运性全都会坏掉。
                    messages=[message.to_prompt_record() for message in messages],
                    **extra,
                )
                self.emit("step", step=step, estimated_tokens=estimated)
                self.emit("prompt", messages=messages, estimated_tokens=estimated)

                assembler = StreamAssembler()
                try:
                    # 输出上限由**循环自己的 settings** 决定，而不是 provider 构造时的配置。
                    # 否则 dream 改了自己的 settings 也是白改：provider 仍按聊天配置发 2048，
                    # 结果是"想到一半被截断"（实测踩过）。
                    for event in self.provider.stream(
                        messages,
                        self.registry.specs(),
                        max_tokens=self.settings.max_output_tokens,
                    ):
                        assembler.feed(event)
                        self._relay(event)
                except ProviderError as exc:
                    error = str(exc)
                    stop_reason = "error"
                    self.log.append("error", step=step, error=error)
                    self.emit("notice", level="error", text=error)
                    self._note_rejected_images(messages, exc, user_seq)
                    break

                usage = assembler.usage or Usage()
                total_usage.add(usage)
                steps_run += 1
                message = assembler.message()
                if message.content:
                    final_text = message.content

                # 输出被 max_tokens 截断时必须显式说出来。
                # 否则"模型想到一半被砍断"会伪装成"本轮无事可做"——实测踩过：
                # dream 整理 80 条记忆时 reasoning 用满 2048、零工具调用，却报告 completed。
                if assembler.finish_reason == "length":
                    truncated = True
                    self._notice(
                        t(
                            "ui.loop.output_truncated",
                            max_tokens=self.settings.max_output_tokens,
                        ),
                        "warn",
                    )

                self.log.append(
                    "assistant_message",
                    step=step,
                    finish_reason=assembler.finish_reason,
                    usage=usage.to_dict(),
                    message=message.to_record(),
                )
                self.emit("assistant_done", message=message, usage=usage)

                if not message.tool_calls:
                    if truncated:
                        stop_reason = "output_truncated"
                    break

                history.append(message)
                pending_calls = list(message.tool_calls)

                signature = tuple(sorted((c.name, c.arguments) for c in message.tool_calls))
                if signature == last_signature:
                    repeated += 1
                else:
                    repeated = 1
                    last_signature = signature
                if repeated >= _REPEAT_LIMIT:
                    stop_reason = "repeated_tool_calls"
                    self._notice(
                        t("ui.loop.repeated_calls", limit=_REPEAT_LIMIT),
                        "warn",
                    )
                    # assistant 消息已经落盘，必须给它这些没执行的调用补上结果，
                    # 否则这个会话之后的每一轮重放都会因为悬空 tool_calls 直接 400。
                    self._stub_unexecuted_calls(message.tool_calls, history, step)
                    pending_calls = []
                    break

                for call in message.tool_calls:
                    self.emit("tool_call", call=call)
                    result = self.registry.call(call.name, call.arguments)
                    content = self.registry.render_content(result)
                    history.append(
                        Message(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content=content,
                        )
                    )
                    self.log.append(
                        "tool_call",
                        step=step,
                        tool_call_id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                        ok=result.ok,
                        display=result.display,
                        result=result.payload,
                        result_content=content,
                    )
                    self.emit("tool_result", call=call, result=result)
                    # 这一条**已经有结果了**，从"待补桩"里摘掉。
                    # 不摘的话，中止（点停止 / 关标签页）时 `_stub_unexecuted_calls`
                    # 会给它补一条"没执行"的假结果 —— 同一个 tool_call_id 出现两条
                    # 互相矛盾的 tool 消息，而重放层的修补按 id 去重（集合），
                    # 根本发现不了这种重复。
                    pending_calls = [c for c in pending_calls if c.id != call.id]
                    failed_streak = failed_streak + 1 if not result.ok else 0

                    # 联网结果与记忆写入出现在同一轮时留一条审计线索。
                    # 网页内容是不可信数据，本不该成为记忆的来源（系统提示里已明令禁止），
                    # 但"模型有没有照做"只能事后核查 —— 所以这里留痕，不拦截。
                    if call.name.startswith("web_"):
                        web_result_seen = True
                    elif (
                        web_result_seen
                        and call.name.startswith("mem_")
                        and not web_memory_audited
                    ):
                        web_memory_audited = True
                        self._notice(
                            t("ui.loop.web_memory_audit", tool=call.name),
                            "warn",
                        )
                    # OCR 文字与网页内容是同一类不可信输入（图片里的字可以是别人放的），
                    # 所以审计也同形：**只留痕、不拦截**（拦截会让模型卡住，
                    # 留痕才能让人事后发现），并且一轮只发一次。
                    if (
                        ocr_seen
                        and call.name.startswith("mem_")
                        and not ocr_memory_audited
                    ):
                        ocr_memory_audited = True
                        self._notice(
                            t("ui.loop.ocr_memory_audit", tool=call.name),
                            "warn",
                        )
                pending_calls = []

                # 第五道熔断：连续失败。
                # 这条是给**联网工具**准备的：抓取失败是常态，而模型遇到失败时
                # 容易开始"猜地址"式乱试 —— 实测有一轮连开 4 个打不开的地址，
                # 烧光 8 步、耗掉 54k token。失败多了就主动收手，比让它试到没步数便宜得多。
                if failed_streak >= self.settings.max_tool_failures:
                    stop_reason = "tool_failures"
                    self._notice(
                        t("ui.loop.tool_failures", count=failed_streak),
                        "warn",
                    )
                    break
            else:
                stop_reason = "max_steps"
                self._notice(
                    t("ui.loop.max_steps", max_steps=self.settings.max_steps), "warn"
                )
        except BaseException as exc:
            # 客户端断开（Web 端点了"停止"）或键盘中断时，本轮不会正常收尾，
            # 但**必须留下 turn_end**，否则事件日志里这一轮看起来像从未结束过。
            stop_reason = "aborted"
            # 工具执行到一半被中止时，assistant 的 tool_calls 已经落盘了：
            # 同样要补桩，否则下一轮重放就是一个悬空的 tool_calls（API 直接 400）。
            if pending_calls:
                self._stub_unexecuted_calls(pending_calls, history, step)
                pending_calls = []
            elapsed = time.monotonic() - started
            try:
                self.log.append(
                    "turn_end",
                    steps=steps_run,
                    stop_reason=stop_reason,
                    aborted_by=f"{type(exc).__name__}: {exc}",
                    elapsed=round(elapsed, 2),
                    usage=total_usage.to_dict(),
                )
            except Exception:  # noqa: BLE001 - 连日志都写不进去时不能再抛
                pass
            raise

        # 压缩那一次调用也是本轮的真实开销，必须计入
        total_usage.add(self._compact_usage)
        elapsed = time.monotonic() - started
        self.log.append(
            "turn_end",
            steps=steps_run,
            stop_reason=stop_reason,
            elapsed=round(elapsed, 2),
            usage=total_usage.to_dict(),
        )
        outcome = TurnOutcome(
            text=final_text,
            steps=steps_run,
            usage=total_usage,
            elapsed=elapsed,
            stop_reason=stop_reason,
            error=error,
        )
        self.emit("turn_end", outcome=outcome)
        return outcome

    # ------------------------------------------------------------------

    def _note_rejected_images(
        self, messages: Sequence[Message], exc: ProviderError, user_seq: Any
    ) -> None:
        """这一轮的图片被服务端明确拒绝：提示用户 + 把"图片没送出去"记进历史。

        **不做 fallback、不自动重试**（用户明确否掉了 fallback）。只做两件事
        （契约 §13.5）：

        1. 多发一条 notice，内容是**原错误 + 一句明确的指引**。原错误必须一起显示，
           不许用一句泛泛的提示把它盖掉（既有的那条 error notice 也照旧保留）；
        2. 追加 `images_undelivered`（`for_seq` 指向这一轮的 `user_message`）：
           那条消息的图片**事实上从未被模型看到**，不记下来的话，这个会话之后
           每一轮重放都会带着图片再发一次、再被拒一次，用户连"重试"都做不到
           （ADR-045：停下来也必须停在自洽的状态上）。

        判据：这次请求的 prompt 里**确实有图片**，**且** status 属于"请求被拒"那一类。
        `429`/`5xx`/网络错误（status 为 None）**什么都不加** —— 那些是暂时的，
        图还在，重试就好，动历史只会让用户白丢一次图片。

        **报错但静默丢图的情况这里检测不了**：有的模型对带图请求照回 200，
        图片却被丢掉了（实测 prompt_tokens 只多了 8，模型答"无法确定"）。
        那一类只能靠发送前按能力声明判定（契约 §13.8），不在这条路径上。
        """
        status = getattr(exc, "status", None)
        if status not in _IMAGE_REJECTION_STATUSES:
            return
        if not any(message.images for message in messages):
            return
        error = str(exc)
        if user_seq is not None:
            self.log.append("images_undelivered", for_seq=user_seq, reason=error)
        self._notice(
            t("ui.loop.images_rejected", error=error),
            "warn",
        )

    def _stub_unexecuted_calls(
        self, calls: list[Any], history: list[Message], step: int
    ) -> None:
        """给"没有执行就中止"的工具调用补一条失败结果。

        为什么非补不可：assistant 消息（带 tool_calls）已经写进事件日志了，而协议要求
        `tool_calls` 必须与 `tool` 消息成对出现。缺了它们，**这个会话之后每一轮重放
        都会直接 400**，而且裁剪和压缩都不会修它 —— 一个会话就永久废了。
        两条路会产生这种悬空：重复调用熔断（在执行工具之前就 break）、
        以及客户端中途断开。重放层还有一层兜底（events._repair_dangling_tool_calls），
        这里补桩是为了让日志本身就自洽，而不是每次读的时候再修。
        """
        content = json.dumps(
            {"ok": False, "error": t("ui.loop.stub_tool_reason")}, ensure_ascii=False
        )
        for call in calls:
            history.append(
                Message(role="tool", tool_call_id=call.id, name=call.name, content=content)
            )
            self.log.append(
                "tool_call",
                step=step,
                tool_call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                ok=False,
                skipped=True,
                display=t("ui.loop.stub_display"),
                result={"ok": False, "skipped": True},
                result_content=content,
            )

    def _relay(self, event: Any) -> None:
        """把流式事件转成 UI 事件。工具调用碎片不转发（太吵）。"""
        if isinstance(event, ReasoningDelta):
            self.emit("reasoning", text=event.text)
        elif isinstance(event, TextDelta):
            self.emit("text", text=event.text)

    def _notice(self, text: str, level: str = "info") -> None:
        self.log.append("notice", level=level, text=text)
        self.emit("notice", level=level, text=text)
