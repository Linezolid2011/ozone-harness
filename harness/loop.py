"""Agent 循环：一次输入 → 若干步「模型思考 / 调工具 / 再思考」 → 最终回复。

这个文件是 harness 的心脏。它只做编排，不认识任何具体模型或具体工具。

**两个 agent 共用同一个循环**：
  * 对话 agent（默认 planner）：每轮开始时按 M2 规则规划一次记忆注入，整轮固定不变；
  * dream agent（M4）：`dream.py` 注入自己的 planner，去做记忆巩固。
这样四道熔断、工具执行、流式拼装、事件日志**只有一份实现**，不会各自漂移 ——
熔断逻辑尤其不能有两份，那是"钱"的防线。

五道熔断（都是为了防止"半夜自己循环烧钱"）：
  1. max_steps           单轮最多几步工具调用
  2. max_tokens_per_turn 单轮累计 token 上限（含压缩调用）
  3. max_wall_seconds    单轮墙钟上限
  4. 重复调用检测         连续 3 次完全相同的工具调用即中止
  5. max_tool_failures   连续工具失败达到上限即中止
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence

from .config import Settings, UNLIMITED_OUTPUT
from .context import (
    build_messages,
    render_for_summary,
    request_overhead_tokens,
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
    estimate_tools_tokens,
)
from .memory import MemoryStore
from .providers.base import LLMProvider, ProviderError, record_prompt
from .tools.registry import ToolRegistry

Emit = Callable[..., None]

_REPEAT_LIMIT = 3

# 装进窗口之后"输出"至少还剩多少 token 才值得发这一次请求：见
# `Settings.min_output_tokens`（config.py，默认 64，可用 HARNESS_MIN_OUTPUT_TOKENS 调）。
# 以前它是这个模块里的一个常量 —— 阈值属于配置层，不留在循环里。

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
    #: 本轮那条 `user_message` 的 seq —— 前端「分叉」按钮靠它定位分叉点。
    #: 历史回放时由 `build_session_view` 从事件里补；这里是**实时流**那一侧。
    user_seq: int | None = None
    #: 本轮最后一次模型请求实际收到的输入 token。多步循环不能把各步输入相加，
    #: 否则会把同一段历史重复算成"当前上下文"。
    context_tokens: int = 0

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
        vault_text_provider: Callable[[], str] | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.settings = settings
        self.memory = memory
        self.log = log
        self.session_id = log.session_id
        self.emit: Emit = emit or _silent_emit
        self._planner: Planner = planner or self.plan_chat_turn
        # 是不是对话那条路？dream 注入自己的 planner，它有自己的 system 与预算口径，
        # 不能替它渲染聊天记忆、也不能拿聊天的固定开销去算它的窗口（ADR-100）。
        self._chat_planner = planner is None
        # 系统提示里关于联网工具的那一段（由 Runtime 按配置生成）
        self.web_tools_text = web_tools_text
        # 背景档案（dream memory）那一整段：README 的正文已经拼在里面。
        # 与 web_tools_text 同样由 Runtime 生成 —— 循环不认识 vault，
        # 只负责把这段字节原样放进 system，这样"什么内容进提示词"只有一个决策点。
        self.vault_text = vault_text
        # 长驻 CLI 每轮重读一次；轮内仍固定，保住 system 前缀与工具循环的缓存。
        self._vault_text_provider = vault_text_provider
        # 本轮固定的记忆块（整轮只渲染一次，见 plan_chat_turn）
        self._turn_memory_text: str | None = None
        # 本轮的压缩摘要（如果有）
        self._turn_summary: str = ""
        # 本轮固定的时间戳（run() 开头设置，见 build_tail_message）
        self._turn_now: datetime = datetime.now().astimezone()
        # 本轮的工具定义：估算与真正发出去的必须是同一份（ADR-100）
        self._turn_tools: list[dict[str, Any]] = []
        # 本轮的历史预算（窗口 - 输出 - 余量 - 固定开销），由 run() 算好
        self._turn_history_budget: int | None = None
        # "输出上限被临时压低"这条提示一轮只说一次
        self._output_capped_noted = False
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
        self._ensure_turn_memory(history)
        memory_text = self._turn_memory_text or ""
        messages, omitted_groups = build_messages(
            self.settings,
            history,
            memory_text,
            summary_text=self._turn_summary,
            web_tools=self.web_tools_text,
            vault_text=self.vault_text,
            now=self._turn_now,
            # 整条请求的预算（窗口 - 输出 - 余量 - 工具/系统/记忆/摘要），
            # 不是只有历史那一份（ADR-100）。run() 会先把它算好。
            history_budget=self._turn_history_budget,
        )
        return messages, {
            "omitted_groups": omitted_groups,
            "memory_text": memory_text,
            "memory_count": len(self.memory.active_items()),
            "memory_limit": self.memory.max_items,
            "summary_chars": len(self._turn_summary),
            # 这一轮实际用的历史预算：重放时要能看出"为什么这里没有更早的对话了"
            "history_budget": self._turn_history_budget,
            # README 的全文也写进轨迹：`--replay` 要能看到"当时到底注入了什么"
            "vault_text": self.vault_text,
        }

    def _ensure_turn_memory(self, history: list[Message]) -> None:
        """按当前历史算出"这一轮用哪一份记忆块"，整轮只渲染一次。

        从 `plan_chat_turn` 里提出来，是因为压缩必须在第一步之前就知道
        记忆块有多大 —— 它是整条请求固定开销的一部分（ADR-100）。
        渲染只做一次；原生存储不再记录"这条记忆是第几轮写的"这类库内元数据。
        """
        if self._turn_memory_text is not None:
            return
        budget = self.settings.memory_token_budget
        # 正文、省略数与总数必须来自同一份文件快照，不能在两次读取之间换了一份。
        self._turn_memory_text, omitted, total = self.memory.injection_view(max_tokens=budget)
        self.emit(
            "memory",
            count=total,
            limit=self.memory.max_items,
            omitted=omitted,
        )

    def _request_overhead(self, summary_text: str) -> int:
        """这一轮"不属于历史"的固定开销（工具 + system + 摘要 + 尾巴）。"""
        return request_overhead_tokens(
            self.settings,
            self._turn_memory_text or "",
            summary_text=summary_text,
            web_tools=self.web_tools_text,
            vault_text=self.vault_text,
            tools=self._turn_tools,
            now=self._turn_now,
        )

    def _prepare_budget(self, history: list[Message], summary_text: str) -> int:
        """算这一轮历史能占多少 token。

        只有对话 planner 才有"记忆块 / 摘要 / README"这些固定开销；
        dream 用自己的 system 与 planner，这里不替它渲染聊天记忆（那是白查库），
        它的窗口约束由 `_fit_request` 在真正发请求之前兜住。
        """
        if not self._chat_planner:
            return self.settings.history_budget()
        self._ensure_turn_memory(history)
        overhead = self._request_overhead(summary_text)
        return self.settings.history_budget(overhead_tokens=overhead)

    def _fit_request(
        self, estimated_input: int, output_tokens: int
    ) -> tuple[int, bool] | None:
        """把这一条请求收进模型窗口。返回 (真正要发的 max_tokens, 是否压低过)。

        窗口管的是"**输入 + 输出**"（ADR-100）。历史已被 `build_messages` 按预算
        裁过，所以这里通常只是复核一次。真正需要动手的只有一种情形：
        本轮最新一组（工具结果 / 一条超大提问）自己就把窗口占满了 ——
        这时先压低输出上限；压到底也装不下就返回 None，由调用方**本地中止**。

        返回 None 表示"这条请求不该发出去"。它是一条**本地**失败，
        不是 API 的 context length exceeded —— 后者才是这个改动要消灭的东西。
        """
        limit = int(self.settings.context_window) - self.settings.context_margin()
        room = limit - max(0, int(estimated_input))
        if int(output_tokens) == UNLIMITED_OUTPUT:
            # 不限输出（ADR-101）：发给 provider 的仍是 -1（OpenAI 兼容那边
            # 会**不发送** max_tokens）；这里只要求输入给输出留出预算里的那块
            # 预留，否则输入会把窗口占满、模型没有生成空间。
            if room < self.settings.output_reserve():
                return None
            return UNLIMITED_OUTPUT, False
        if room <= 0 or room < self.settings.min_output_tokens:
            return None
        if room >= output_tokens:
            return output_tokens, False
        return room, True

    # ------------------------------------------------------------------
    # 自动 compact
    # ------------------------------------------------------------------

    def _maybe_compact(
        self,
        history: list[Message],
        summary_text: str,
        mark: CoveredMark,
        budget: int,
    ) -> bool:
        """历史接近预算上限时，把较早的那部分压成摘要。

        `budget` 是**整条请求**留给历史的那一份（窗口 - 输出 - 余量 - 工具/
        system/记忆/摘要，见 `_prepare_budget`），不是"只看窗口"的粗数字。
        所以"在哪里 compact"确实取决于用户这轮选了什么模型：换个 32K 窗口的
        模型会明显更早触发（ADR-100）。

        无法压缩时（只剩一组、模型调用失败）返回 False，
        由 `trim_history` 兜底按组丢弃，行为与 M5 之前一致。
        """
        if not self.settings.compact_enabled or budget <= 0:
            return False

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

        # 摘要调用**本身也是一次 API 请求**：它同样不能超过窗口（ADR-100）。
        # 放不下就从最新的待折叠组往回丢（水位线因此更保守），而不是硬发一条
        # 注定 400 的请求。`fold_groups` 始终是原列表的前缀，所以水位线仍然连续。
        summary_limit = (
            int(self.settings.context_window)
            - self.settings.compact_max_output_tokens
            - self.settings.context_margin()
        )
        while fold_groups:
            folded = [message for group in fold_groups for message in group]
            if (
                estimate_messages_tokens(self._summary_prompt(folded, summary_text))
                <= summary_limit
            ):
                break
            fold_groups = fold_groups[:-1]
        if not fold_groups:
            self._notice(t("ui.loop.summary_too_long"), "warn")
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
        # 空摘要与被截断的摘要同样已经花了一次 API 调用的钱；不能只在成功时记账。
        self._compact_usage.add(usage)
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
        self.emit(
            "compact",
            summary=summary,
            folded=len(folded),
            covered=mark.messages + len(folded),
            usage=usage.to_dict(),
        )
        return True

    def _summary_prompt(
        self, folded: list[Message], previous_summary: str
    ) -> list[Message]:
        """摘要调用真正发出去的消息。

        **估算与发送共用这一处**：否则"检查时算的"与"发出去的"会慢慢分家，
        而这件事刚好是窗口保证的依据（ADR-100）。
        """
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
        return [
            Message(role="system", content=bundle().COMPACT_SYSTEM_PROMPT),
            Message(role="user", content="\n\n".join(parts)),
        ]

    def _summarize(
        self, folded: list[Message], previous_summary: str
    ) -> tuple[str, Usage]:
        """让模型把这段历史压成摘要。返回 (摘要文本, 用量)。"""
        prompt = self._summary_prompt(folded, previous_summary)
        assembler = StreamAssembler()
        try:
            for event in self.provider.stream(
                prompt, [], max_tokens=self.settings.compact_max_output_tokens
            ):
                assembler.feed(event)
        except ProviderError as exc:
            self.log.append("error", error=t("ui.loop.summarize_failed", error=exc))
            return "", Usage()
        usage = assembler.usage or Usage()
        if assembler.finish_reason == "length":
            # 半份摘要不覆盖任何历史：把它当成功会推进水位线，没进摘要的部分就此消失。
            return "", usage
        return assembler.text.strip(), usage

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
        if self._vault_text_provider is not None:
            # 上一轮可能通过 dream_edit/dream_create 改过 vault；不要等到重启才注入。
            self.vault_text = self._vault_text_provider()
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
        # 工具定义在整轮内不变：**估算与真正发出去的必须是同一份**（ADR-100）。
        self._turn_tools = self.registry.specs()
        # "输出上限被临时压低"这条提示一轮只说一次
        self._output_capped_noted = False
        self._turn_history_budget: int | None = None

        # 压缩是优化，**绝不能因为它让这一轮失败**：任何异常都降级为"不压缩"，
        # 由 trim_history 按组丢弃兜底，并且照常走到 turn_end。
        self._turn_summary = ""
        try:
            try:
                summary_text, history, mark = self.log.compacted_context()
                budget = self._prepare_budget(history, summary_text)
                if self._maybe_compact(history, summary_text, mark, budget):
                    # 压缩改变了上下文，重新读一次
                    summary_text, history, mark = self.log.compacted_context()
                    # 摘要变了，固定开销也跟着变：预算必须重算（ADR-100）
                    budget = self._prepare_budget(history, summary_text)
                self._turn_summary = summary_text
                self._turn_history_budget = budget
            except Exception as exc:  # noqa: BLE001 - 压缩不该有能力弄坏一轮对话
                self._turn_summary = ""
                history = self.log.conversation()
                # 压缩阶段出错也要有一条**安全的预算**：退回"按窗口硬约束"的算法，
                # 否则这一轮会拿 None 去拼 prompt —— 那等于没有窗口保证（ADR-100）。
                try:
                    self._turn_history_budget = self._prepare_budget(history, "")
                except Exception:  # noqa: BLE001 - 连预算都算不出来时用最保守的那条
                    self._turn_history_budget = self.settings.history_budget()
                self._notice(
                    f"压缩阶段出错，本轮跳过压缩：{type(exc).__name__}: {exc}", "warn"
                )
        except BaseException as exc:
            # 主步循环还没开始时也可能被停止/断开；这里同样要留下已中止的轮尾。
            # 放在外层还能覆盖降级 notice 再次遇到客户端断开的情况。
            elapsed = time.monotonic() - started
            compact_usage = Usage()
            compact_usage.add(self._compact_usage)
            try:
                self.log.append(
                    "turn_end",
                    steps=0,
                    stop_reason="aborted",
                    aborted_by=f"{type(exc).__name__}: {exc}",
                    elapsed=round(elapsed, 2),
                    usage=compact_usage.to_dict(),
                    user_seq=user_seq,
                )
            except Exception:  # noqa: BLE001 - 中止时不能再掩盖原异常
                pass
            raise

        total_usage = Usage()
        context_tokens = 0
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
                # 摘要也是本轮开销：不能等正常对话都跑完、收尾记账时才参与熔断。
                spent_tokens = total_usage.total_tokens + self._compact_usage.total_tokens
                if spent_tokens >= self.settings.max_tokens_per_turn:
                    stop_reason = "token_budget"
                    self._notice(
                        t("ui.loop.token_budget", tokens=spent_tokens),
                        "warn",
                    )
                    break

                messages, extra = self._planner(history, step, user_text)
                # **整条请求（输入 + 输出）必须装进模型窗口**（ADR-100）。
                # 历史已经按预算裁过；这里兜住"本轮最新一组自己就超了"的情形：
                # 先压低输出上限，实在装不下就本地中止 —— 绝不把一条注定 400 的
                # 请求交给 API（那正是这个改动要消灭的报错）。
                estimated = estimate_messages_tokens(messages) + estimate_tools_tokens(
                    self._turn_tools
                )
                fit = self._fit_request(estimated, int(self.settings.max_output_tokens))
                if fit is None:
                    stop_reason = "context_limit"
                    self._notice(
                        t(
                            "ui.loop.context_limit",
                            window=int(self.settings.context_window),
                            # 要报**真实要留出的输出量**：`-1`（不限）时它不是 -1，
                            # 而是窗口感知的那块预留（`output_reserve()`）——
                            # 直接把 max_output_tokens 加进去会少报一整块。
                            needed=estimated + self.settings.output_reserve(),
                        ),
                        "warn",
                    )
                    break
                output_tokens, output_capped = fit
                if output_capped and not self._output_capped_noted:
                    self._output_capped_noted = True
                    self._notice(
                        t("ui.loop.output_capped", max_tokens=output_tokens), "warn"
                    )
                self.log.append(
                    "prompt_built",
                    step=step,
                    estimated_tokens=estimated,
                    # 用 to_prompt_record 而不是 to_api：**带图消息的 base64 绝不能进日志**
                    # （契约 §5）。每步都写一次，一张图就能把 JSONL 撑成几 MB，
                    # 之后 grep、--replay、乃至 data/ 的可搬运性全都会坏掉。
                    messages=record_prompt(self.provider, messages),
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
                        self._turn_tools,
                        max_tokens=output_tokens,
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
                context_tokens = usage.prompt_tokens
                steps_run += 1
                message = assembler.message()
                if message.content:
                    final_text = message.content

                # 输出被 max_tokens 截断时必须显式说出来。
                # 否则"模型想到一半被砍断"会伪装成"本轮无事可做"——实测踩过：
                # dream 整理 80 条记忆时 reasoning 用满 2048、零工具调用，却报告 completed。
                if assembler.finish_reason == "length":
                    truncated = True
                    if output_tokens == UNLIMITED_OUTPUT:
                        # 没设上限时被截断，只可能是**服务端自己**的上限/窗口。
                        # 不能报一句 "max_tokens (-1)" 让人以为是我们设的（ADR-101）。
                        self._notice(t("ui.loop.output_truncated_unlimited"), "warn")
                    else:
                        self._notice(
                            t(
                                "ui.loop.output_truncated",
                                # 报**这次真正发出去的**上限：它可能因为窗口被临时压低，
                                # 报配置值会让用户以为还有余量（ADR-100）。
                                max_tokens=output_tokens,
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
                    # 真实结果已经落盘，立刻从"待补桩"里摘掉，再做可能断开连接的 UI emit。
                    # 否则中止处理会给同一个 tool_call_id 再补一条"没执行"的假结果。
                    pending_calls = [c for c in pending_calls if c.id != call.id]
                    self.emit("tool_result", call=call, result=result)
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
                    user_seq=user_seq,
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
            user_seq=user_seq,
        )
        outcome = TurnOutcome(
            text=final_text,
            steps=steps_run,
            usage=total_usage,
            elapsed=elapsed,
            stop_reason=stop_reason,
            error=error,
            user_seq=user_seq,
            context_tokens=context_tokens,
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
        2. 给请求里每条带图消息追加 `images_undelivered`（`for_seq` 指向它的来源事件）：
           那些图片**事实上从未被模型看到**，不记下来的话，这个会话之后
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
        image_messages = [message for message in messages if message.images]
        if not image_messages:
            return
        error = str(exc)
        # 历史图片也会随这次请求重发。必须按来源逐条标记，不能把旧图的拒绝
        # 错记到本轮那条纯文字消息上，否则旧图仍在，下一轮还会继续被拒。
        rejected_seqs: list[int] = []
        for message in image_messages:
            source_seq = message.source_seq
            # 自定义 planner 可能直接构造 Message；保留旧调用方式的当前轮兜底。
            if source_seq is None:
                source_seq = user_seq if isinstance(user_seq, int) else None
            if source_seq is not None and source_seq not in rejected_seqs:
                rejected_seqs.append(source_seq)
        for source_seq in rejected_seqs:
            self.log.append("images_undelivered", for_seq=source_seq, reason=error)
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
