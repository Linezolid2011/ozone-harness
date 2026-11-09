"""事件溯源日志。

会话就是一份 append-only 的 JSONL：人可读、可 grep、可重放、崩溃不丢。
它是**对话历史的唯一真相来源** —— 每轮送给模型的历史，都是从这里重放出来的；
所以"自动保存对话历史"这件事不需要模型参与，是 harness 的职责。

刻意不记录每个 token 级 delta（一份回复会变成几十行噪声），
只记录拼装完成的 assistant 消息与用量。需要逐 delta 复盘时再加开关。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .attachments import copy_records_for_fork, load_parts
from .i18n import t
from .llm_types import Message, Usage, split_into_groups


def new_session_id() -> str:
    """可排序 + 可区分：20261004-210125-a1b2"""
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


# -- seq 分配 ---------------------------------------------------------------
# 同一个会话文件可能同时被**两个** `EventLog` 打开：聊天那一轮握着它流式写，
# 而界面上的"改名 / 切联网开关 / 换模型"各自新开一个去追加。两个都从"文件行数"
# 起算，就会把同一个 seq 写两遍 —— 而 seq 是整理游标**唯一**的定位依据
# （`dream_cursor` 记的是"整理到第几条"），撞了就等于有一条记录被游标无声吞掉，
# 以后永远不会进档案。一份进程内计数器就够：Web 端是单进程多线程。
_SEQ_LOCK = threading.Lock()
_SEQ_BY_PATH: dict[str, int] = {}


def _seq_key(path: Path) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(path).casefold()


def stub_tool_result() -> str:
    """给"没有结果的工具调用"补的占位结果。见 _repair_dangling_tool_calls。

    **是函数而不是常量**：这段文字会发给模型，所以它跟着当前语言走，
    而语言是运行期可切的（`docs/12-i18n.md` §3）—— import 时算好就再也换不动了。
    """
    return t("ui.events.stub_tool_result", reason=t("ui.events.stub_tool_reason"))


@dataclass
class CoveredMark:
    """压缩的"已覆盖"水位线。

    以**组数**为准：组内补一条 tool 结果（熔断或中断时会发生）不会让边界错位，
    而按消息条数记的话，重放时补一条就会让后面整体偏移一位、把某个 tool 消息
    的调用者切掉 —— 那正是 ADR-013 要防的 400。
    老日志只记了 `covered_messages`，所以条数也留着做回退。
    """

    groups: int = 0
    messages: int = 0


@dataclass
class SessionInfo:
    session_id: str
    path: Path
    updated_at: float
    turns: int
    title: str


class EventLog:
    def __init__(
        self,
        path: Path,
        session_id: str | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or self.path.stem
        # 数据目录：附件记录里存的是**相对 data_dir 的相对路径**，重放时要拿它去拼
        # 真实文件；事件日志自己不带这个信息，所以由构造方给。
        #
        # 不给时的推导规则：会话日志的约定位置就是 `<data_dir>/sessions/<sid>.jsonl`，
        # 往上两级就是 data_dir。这样所有老调用点（只传路径）不用改也能重放图片 ——
        # 而位置不在这套约定里的日志（例如 dream 的运行轨迹）里根本没有附件记录，
        # 推错一级也无害。
        self.data_dir = Path(data_dir) if data_dir is not None else self.path.parent.parent
        self._key = _seq_key(self.path)
        counted = self._count_existing()
        with _SEQ_LOCK:
            # 三种情况要分开：
            #   * 文件在这之前就有内容（新进程 / 另一个句柄）→ 从行数接着数；
            #   * 文件被**改短/换掉**（手工编辑、从备份恢复）→ 行数比计数器小，
            #     说明"已经写到第几条"这个前提不成立了，**以文件为准**（否则新事件
            #     会接着旧计数往上数，`stale` 那条检测就再也报不出来）；
            #   * 另一个句柄正在写（行数 == 计数器）→ 用计数器。
            remembered = _SEQ_BY_PATH.get(self._key, 0)
            self._seq = counted if counted < remembered else max(remembered, counted)
            _SEQ_BY_PATH[self._key] = self._seq
        self._ensure_append_boundary()
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")

    def _ensure_append_boundary(self) -> None:
        """给没有换行符的旧末尾补上记录边界，再往日志里追加。

        进程在写完一条 JSON、但还没写换行时退出，会留下半行；完整 JSON 也可能只是
        缺少最后的换行。两种情况下直接 append 都会把新记录粘到旧末尾上，连新事件
        也一起变成 `read_events()` 会跳过的坏行。这里用二进制检查最后一个字节，
        免得半行里的坏 UTF-8 在真正需要它的追加路径上再制造一次解码异常。
        """
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        with self.path.open("rb") as handle:
            handle.seek(-1, 2)
            terminated = handle.read(1) in (b"\n", b"\r")
        if not terminated:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(self, kind: str, **data: Any) -> dict[str, Any]:
        # 序号分配与落盘必须同属一个临界区：先释放锁再写，另一个句柄可能先写入
        # 更大的 seq，导致文件顺序变成 [2, 1]，违背游标依赖的递增约定。
        with _SEQ_LOCK:
            self._seq = _SEQ_BY_PATH.get(self._key, self._seq) + 1
            _SEQ_BY_PATH[self._key] = self._seq
            record: dict[str, Any] = {
                "seq": self._seq,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "kind": kind,
            }
            record.update(data)
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()
            return record

    def close(self) -> None:
        try:
            self._handle.close()
        except Exception:  # noqa: BLE001 - 关闭失败无需打扰用户
            pass

    def _count_existing(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read_events(self) -> list[dict[str, Any]]:
        return read_events(self.path)

    def conversation(self) -> list[Message]:
        """把事件重放成模型上下文里的消息序列（不处理摘要）。"""
        return _build_messages(self.read_events(), self.data_dir)

    def compacted_context(self) -> tuple[str, list[Message], CoveredMark]:
        """返回 (摘要文本, 摘要之后的消息, 已覆盖水位线)。

        这是自动 compact 的读取端：一旦发生过压缩，被覆盖的旧消息**不再**进入上下文，
        取而代之的是那条摘要 —— 信息不会像"直接丢弃"那样凭空消失。
        """
        events = self.read_events()
        summary, mark = _last_summary(events)
        messages = _build_messages(events, self.data_dir)

        if mark.groups:
            groups = split_into_groups(messages)
            kept = [message for group in groups[mark.groups :] for message in group]
            if not kept:
                # 水位线比实际组数还大（例如日志被手工改过）：**至少留住最后一组**。
                # 否则这一轮的提问会凭空消失，模型会答非所问。
                kept = list(groups[-1]) if groups else list(messages)
            return summary, kept, CoveredMark(
                groups=mark.groups, messages=len(messages) - len(kept)
            )

        kept = _safe_slice(messages, mark.messages)
        # 老日志只有条数水位线；这里按**实际切掉的组数**升级成组水位线。
        # 否则下一次压缩会从 0 累计 covered_groups，让早已折叠的组重新进入上下文。
        covered_groups = len(split_into_groups(messages)) - len(split_into_groups(kept))
        return summary, kept, CoveredMark(
            groups=covered_groups, messages=len(messages) - len(kept)
        )

    def usage_totals(self) -> Usage:
        total = Usage()
        for record in self.read_events():
            if record.get("kind") == "assistant_message":
                total.add(Usage.from_dict(record.get("usage")))
        return total

    def prompts(self) -> list[dict[str, Any]]:
        return [r for r in self.read_events() if r.get("kind") == "prompt_built"]


def wire_attachments(
    message: Message, record: dict[str, Any], data_dir: Path | None
) -> Message:
    """把一条 `user_message` 事件里的 `attachments` 还原成挂在消息上的 ImagePart。

    单独成函数（而不是塞在 _build_messages 的循环里）是为了让"事件 → 图片"这条
    唯一的接缝有一个名字：历史里的图片**每轮都要重发**，这条路径错了不会报错，
    只会静默地少一张图 —— 那正是最需要被单独测住的东西。
    """
    records = record.get("attachments") or []
    if not records or data_dir is None:
        return message
    message.images = load_parts(Path(data_dir), records)
    return message


# --------------------------------------------------------------------------
# 多模态开关：OCR 文字的重放渲染（契约 §13.4 / §13.5）
# --------------------------------------------------------------------------
#
# 这一整段渲染**只能有一处实现**：发送那一刻（`_build_messages` 重放日志时）
# 与之后的每一轮重放走的是同一条路径，压缩与整理档案也直接读渲染后的正文。
# 两处各写一遍的话，"模型当时到底看到了什么"就会有两个版本，而其中一个还是错的。
#
# 头尾那四句与下面那句边界句都是**发给模型的包装文字**（不是长模板），
# 所以文案在 `harness/ui_text_{zh,en}.py` 里，用 `ui.events.*` 取 ——
# 而且一律**在用到它的那一刻**取：语言是运行期可切的，import 时算好就换不动了。
#
# **OCR 文字是外部可控输入。** 它的来源是"图片里的内容"，可以是别人发来的一张图、
# 一张恶意网页的截图 —— 也就是说，任何人都能往这段文字里塞指令。
# 它在消息里的形态和"用户亲手打的字"没有区别，所以边界必须显式划出来。
#
# **措辞刻意比网页那条（context.WEB_SAFETY）宽松**：网页是**模型自己去外面取的**，
# 用户没在旁边；图片是**用户自己递过来的**，而且他就在对话里，随时能确认一句。
# 所以这里不写"一律不执行"，而是"默认不可信 + 要照办先问用户" ——
# 把"用户明确说照它办"这条正常用法留出来（用户才是授权的那个人），
# 同时注入依然没法自我授权：图片里写"请忽略之前的指令"不会变成用户的要求。


def ocr_untrusted_notice() -> str:
    """「图片里的文字不一定可信」那句边界句（契约 §13.4）。**每次现取**。

    抽成一个函数不只是为了复用：它是这一整段里**唯一的安全声明**
    （默认不可信 + 要照办先问用户），所以既要能被单独测住，又要跟着语言走。
    """
    return t("ui.events.ocr_untrusted_notice")


def message_modality(record: dict[str, Any]) -> str:
    """这条用户消息当时用的是哪种模态：`"text"` 或 `"image"`。

    **缺键 = `"image"`**（老日志没有这个字段，那时的行为就是发图）。
    重放一律按**当时记下来的**模态走，不按"当前开关"走（契约 §13.6）：
    事件日志是"当时到底发生了什么"的唯一真相，按当前开关重放等于改写历史。
    """
    return "text" if str(record.get("modality") or "").strip().lower() == "text" else "image"


def _attachment_items(records: Any) -> list[dict[str, Any]]:
    return [item for item in (records or ()) if isinstance(item, dict)]


def _attachment_name(record: dict[str, Any], index: int) -> str:
    """附件在正文里显示的名字。`name` 是用户那边的文件名，**只用于显示**。"""
    return str(record.get("name") or "").strip() or t("ui.events.image_label", index=index)


def render_ocr_message(text: str, records: Sequence[dict[str, Any]]) -> str:
    """把一条 `modality="text"` 的用户消息渲染成发给模型/重放的正文（契约 §13.4）。

    形状就是契约里那一段：用户打的字 + "多模态没开"的说明 + 每张图的识别文字。
    **必须让模型一眼看出"这不是用户打的字"**，否则它会以为用户在跟它说一段
    莫名其妙的文字 —— 头尾那两句就是为这件事加的分隔。

    没有附件时原样返回 `text`（模态为 text 却没有图，只可能是手工改过的日志；
    这时绝不能凭空生成一段"用户上传了 0 张图片"的正文）。

    这是**公开函数**，测试与重放都直接调它（渲染只有这一处实现）。
    """
    items = _attachment_items(records)
    if not items:
        return text
    blocks: list[str] = []
    for index, record in enumerate(items, 1):
        name = _attachment_name(record, index)
        ocr = record.get("ocr") if isinstance(record.get("ocr"), dict) else {}
        error = str(ocr.get("error") or "").strip()
        if error:
            # 失败要**如实写出来**：模型看到"识别失败"才知道该请用户补一句，
            # 而不是对着空白瞎猜（这比静默丢掉一张图安全）。
            blocks.append(t("ui.events.image_error_block", index=index, name=name, error=error))
            continue
        body = str(ocr.get("text") or "").strip()
        if body:
            blocks.append(t("ui.events.image_text_block", index=index, name=name, text=body))
        else:
            # 空 text 是"真的没有文字"，不是失败 —— 也必须说清楚，
            # 否则模型会以为漏了一段内容。
            blocks.append(t("ui.events.image_empty_block", index=index, name=name))
    rendered = (
        "\n".join(
            [
                t("ui.events.ocr_header"),
                t("ui.events.ocr_intro", count=len(items)),
                ocr_untrusted_notice(),
                "",
            ]
        )
        + "\n"
        + "\n\n".join(blocks)
        + "\n"
        + t("ui.events.ocr_footer")
    )
    prefix = (text or "").strip()
    return f"{prefix}\n\n{rendered}" if prefix else rendered


def render_undelivered_marker(reason: str, records: Sequence[dict[str, Any]]) -> str:
    """图片**事实上没发出去**时贴在正文里的标记（契约 §13.5 第 2 条）。

    这不是降级重试，是**让历史自洽**：那次请求被服务端拒了，模型从未看到这些图。
    不记下来的话，之后每一轮重放都会带着图片再发一次、再被拒一次，
    用户连"重试"都做不到（ADR-045：停下来也必须停在自洽的状态上）。
    """
    items = _attachment_items(records)
    head = [
        t("ui.events.undelivered_header"),
        t("ui.events.undelivered_intro"),
    ]
    if str(reason or "").strip():
        head.append(t("ui.events.undelivered_reason", reason=str(reason).strip()))
    blocks = [
        t("ui.events.undelivered_block", index=index, name=_attachment_name(record, index))
        for index, record in enumerate(items, 1)
    ]
    if not blocks:
        blocks = [t("ui.events.undelivered_no_items")]
    return (
        "\n".join(head)
        + "\n\n"
        + "\n".join(blocks)
        + "\n"
        + t("ui.events.undelivered_footer")
    )


def _as_int(value: Any) -> int | None:
    """把事件里的整数字段读出来，坏值返回 None（**读端要容错，别炸**）。"""
    if isinstance(value, bool):  # bool 是 int 的子类，别把它当序号
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_seq(record: dict[str, Any]) -> int | None:
    """取事件自己的序号（`seq`），坏值返回 None。"""
    return _as_int(record.get("seq"))


def undelivered_by_seq(events: list[dict[str, Any]]) -> dict[int, str]:
    """收集 `images_undelivered` 事件：`user_message` 的 seq → 当时的错误原话。

    键取的是事件里的 **`for_seq`**（它指向那条 `user_message`），
    不是事件自己的 `seq` —— 两者搞混会让降级落到隔壁那条消息上，
    而那等于**改写历史**（一条本来正常发出去的图片消息被说成没发出去）。

    `for_seq` 坏了就**忽略这一条**（不降级任何消息）：宁可让一条老消息照旧带图重放，
    也不能因为一个坏字段把整个会话的图片全砍掉。
    """
    out: dict[int, str] = {}
    for record in events:
        if record.get("kind") != "images_undelivered":
            continue
        target = _as_int(record.get("for_seq"))
        if target is None:
            continue
        out[target] = str(record.get("reason") or "")
    return out


def _build_messages(
    events: list[dict[str, Any]], data_dir: Path
) -> list[Message]:
    """按顺序重放成消息序列（不处理摘要覆盖）。

    用户消息按**当时记下来的 `modality`** 重放（契约 §13.6）：

    * `"text"` —— 不挂图片，把 OCR 那段渲染进正文（`render_ocr_message`）；
    * 缺键或 `"image"` —— 照旧挂图片（**老日志的向后兼容**：上一轮发出去的
      图片消息必须一字不差地接着重放）；
    * 有 `images_undelivered` 命中这条消息 —— 不挂图片，改成文字标记
      （"当时这张图没有发出去"）。**别的消息一律不受影响**。

    末尾会走一次悬空 tool_calls 修补 —— 见 _repair_dangling_tool_calls。
    `data_dir` 是必须的：带附件的用户消息要靠它把相对路径还原成真实文件。
    """
    messages: list[Message] = []
    undelivered = undelivered_by_seq(events)
    for record in events:
        kind = record.get("kind")
        if kind == "user_message":
            text = str(record.get("text") or "")
            attachments = record.get("attachments") or []
            seq = _as_seq(record)
            if message_modality(record) == "text":
                messages.append(
                    Message(
                        role="user",
                        content=render_ocr_message(text, attachments),
                        source_seq=seq,
                    )
                )
                continue
            message = Message(role="user", content=text, source_seq=seq)
            if seq is not None and seq in undelivered:
                marker = render_undelivered_marker(undelivered[seq], attachments)
                body = text.strip()
                message.content = f"{body}\n\n{marker}" if body else marker
            else:
                wire_attachments(message, record, data_dir)
            messages.append(message)
        elif kind == "assistant_message":
            messages.append(Message.from_record(record.get("message") or {}))
        elif kind == "tool_call":
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=str(record.get("tool_call_id") or ""),
                    name=str(record.get("name") or ""),
                    content=str(record.get("result_content") or ""),
                )
            )
    return _repair_dangling_tool_calls(messages)


def _repair_dangling_tool_calls(messages: list[Message]) -> list[Message]:
    """给没有对应结果的 assistant.tool_calls 补一条 tool 消息。

    什么时候会出现悬空：熔断在"执行工具之前"中止、或客户端在工具执行中途断开。
    协议要求 `tool_calls` 必须与 `tool` 消息配对，否则**之后每一轮请求都会 400**，
    而且不修的话那个会话就永久废了（裁剪和压缩都不会动它）。

    在重放层兜底的好处：既防新问题，也把历史上已经写坏的日志修回来。
    """
    repaired: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        repaired.append(message)
        if message.role != "assistant" or not message.tool_calls:
            index += 1
            continue

        following: list[Message] = []
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == "tool":
            following.append(messages[cursor])
            cursor += 1

        answered = {item.tool_call_id for item in following}
        for call in message.tool_calls:
            if call.id not in answered:
                repaired.append(
                    Message(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=stub_tool_result(),
                    )
                )
        repaired.extend(following)
        index = cursor
    return repaired


def _safe_slice(messages: list[Message], count: int) -> list[Message]:
    """按条数切片，但保证切完的第一条是 user 消息。

    只在读**老日志**（水位线里只有条数）时用到。条数会因重放层补桩而漂移，
    所以这里再校验一次边界：宁可多丢一组，也不能让一个 tool 消息当开头
    —— 那种请求 API 会直接 400。

    找不到 user 消息时**退回不切**：漏掉裁剪只是多花点 token（后面还有
    `trim_history` 兜着），而丢掉"用户当前的提问"会让模型答非所问。
    """
    if count <= 0:
        return list(messages)
    kept = messages[count:]
    for index, message in enumerate(kept):
        if message.role == "user":
            return kept[index:]
    return list(messages)


def _last_summary(events: list[dict[str, Any]]) -> tuple[str, CoveredMark]:
    """最后一条压缩摘要，以及它的覆盖水位线。"""
    summary = ""
    mark = CoveredMark()
    for record in events:
        if record.get("kind") != "session_summary":
            continue
        summary = str(record.get("summary") or "")
        try:
            mark = CoveredMark(
                groups=int(record.get("covered_groups") or 0),
                messages=int(record.get("covered_messages") or 0),
            )
        except (TypeError, ValueError):
            mark = CoveredMark()
    return summary, mark


def session_tools(path: Path) -> dict[str, Any]:
    """读会话里**最后一次**生效的工具开关。

    这是"这个会话能不能联网搜索、发不发图片"的唯一真相来源（追加式，最后一条为准）。
    没记录过的键**不补默认值** —— 由上层决定默认值：
    `web_search` 默认关，`multimodal` 默认**开**（契约 §13.1：缺键 = 开，
    否则加个开关就悄悄改变了既有会话的行为）。

    `multimodal` 是 M9 加的，老会话的 `session_tools` 事件里根本没有这个键，
    所以两个键要各判各的：一条老事件不该把 `web_search` 的历史读丢，
    一条只写了 `multimodal` 的新事件也不该顺手把 `web_search` 变成默认值。

    `tools`（ADR-114，启用的工具名名单）同理：只有事件里真的写了它才更新，
    老事件缺这个键不影响；读出的名单逐项校验类型，坏项丢弃 —
    — 名单只是审计线索，配置文件才是真相来源，坏一条不该拖垮整个会话视图。
    """
    latest: dict[str, Any] = {}
    for record in read_events(path):
        if record.get("kind") == "session_tools":
            if "web_search" in record:
                latest["web_search"] = bool(record.get("web_search"))
            if "multimodal" in record:
                latest["multimodal"] = bool(record.get("multimodal"))
            if "tools" in record:
                raw = record.get("tools")
                if isinstance(raw, list):
                    latest["tools"] = [str(v) for v in raw if isinstance(v, str)]
    return latest


def read_events(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 半行（崩溃残留）直接跳过，不影响其余历史
    return records


def session_generation_settings(path: Path) -> dict[str, str]:
    """读会话里**最后一次**生效的接口/模型/档位选择。

    多供应商（M6）之后，`session_start` 记的是"开始时是什么"，
    而用户可能中途换过模型，所以以最后一条 `session_settings` 事件为准。
    """
    latest: dict[str, str] = {}
    for record in read_events(path):
        if record.get("kind") == "session_settings":
            latest = {
                "provider": str(record.get("provider") or ""),
                "model": str(record.get("model") or ""),
                "effort": str(record.get("effort") or ""),
                "label": str(record.get("label") or ""),
            }
    return latest


def session_start_info(path: Path) -> dict[str, Any]:
    for record in read_events(path):
        if record.get("kind") == "session_start":
            return record
    return {}


def last_summary(path: Path) -> tuple[str, int]:
    """这个会话最后一次压缩的摘要，以及它覆盖了多少条消息。"""
    summary, mark = _last_summary(read_events(path))
    return summary, mark.messages


def last_summary_mark(path: Path) -> tuple[str, CoveredMark]:
    """同上，但返回完整的覆盖水位线（含**组数**）。

    组数才是判断"压缩有没有把边界切在组上"的依据；条数只用于展示。
    """
    return _last_summary(read_events(path))


def session_usage(path: Path) -> Usage:
    """一个会话的全部用量，**含压缩时那次摘要调用的开销**。

    摘要调用也是真花钱的，漏掉它会让用量页少算。
    """
    total = Usage()
    for record in read_events(path):
        kind = record.get("kind")
        if kind in ("assistant_message", "session_summary"):
            total.add(Usage.from_dict(record.get("usage")))
    return total


def session_usage_by_provider(path: Path) -> dict[str, Usage]:
    """把一个会话的用量按**当时生效的供应商**归属。

    不能只看 session_start：一个会话中途可能换过接口/模型，
    所以沿着事件走，遇到 session_settings 就更新当前供应商 ——
    这样"换模型之后的那几轮"才会算到新供应商头上。
    """
    current = ""
    out: dict[str, Usage] = {}
    for record in read_events(path):
        kind = record.get("kind")
        if kind == "session_start":
            current = str(record.get("provider") or "")
        elif kind == "session_settings":
            current = str(record.get("provider") or current)
        elif kind == "assistant_message":
            key = current or t("ui.events.provider_unrecorded")
            out.setdefault(key, Usage()).add(Usage.from_dict(record.get("usage")))
        elif kind == "session_summary":
            # 摘要调用也是这家花的钱
            key = current or t("ui.events.provider_unrecorded")
            out.setdefault(key, Usage()).add(Usage.from_dict(record.get("usage")))
    return out


def session_title(path: Path) -> str:
    """用户自定义的会话名。没改过就返回空字符串（由调用方退回"第一条消息"）。"""
    title = ""
    for record in read_events(path):
        if record.get("kind") == "session_title":
            title = str(record.get("title") or "").strip()
    return title


def session_display_title(path: Path) -> str:
    """会话显示名：用户改过就用改的，否则用第一条用户消息。"""
    custom = session_title(path)
    if custom:
        return custom[:60]
    for record in read_events(path):
        if record.get("kind") == "user_message":
            text = str(record.get("text") or "").replace("\n", " ").strip()
            if text:
                return text[:40]
            # 只发图不说话的首条消息：这里退回原符号会让侧栏出现一个空标题。
            # 老日志（没有 attachments 字段）仍然返回空，保持既有行为不变。
            if record.get("attachments"):
                return t("ui.events.image_only_title")
            return ""
    return ""


def delete_session(path: Path) -> bool:
    """删掉一个会话文件。返回是否真的删到了东西。

    **这个函数只做这一件事，而且必须永远只做这一件事**：
    不碰记忆库、不碰别的会话、不碰接口/工具配置。

    特别是**不要**"顺手清理"这个会话创建的记忆 ——
    记忆是独立于对话存在的长期资产（ADR 里那条边界），
    在那次对话里记住的东西，用户删对话不等于想忘掉它。
    想忘掉请去记忆页删。有测试钉住这条边界。
    """
    target = Path(path)
    if not target.is_file():
        return False
    target.unlink()
    return True


def fork_session(settings: Any, session_id: str, before_seq: int) -> tuple[str, Path]:
    """分叉会话：把 `before_seq` 这条 user_message **所在那一轮、以及它之前**的全部事件
    复制成一个新会话（即"保留分叉点及以前的内容"）。

    返回 `(新会话 id, 新会话文件路径)`。原会话**一个字都不改**（保留所有上下文）；
    新会话从分叉点那一轮结束处截断 —— 两个会话从此互相独立。

    `before_seq` 不指向一条 user_message 时抛 `ValueError`（由 web 层翻译成 400）。

    引用的图片会**复制**进新会话目录并改写 `file` 字段（`attachments.copy_records_for_fork`），
    否则"删掉原会话"（连带删附件目录）会让新会话的图变成悬空引用 —— 那就不是"独立"了。
    """
    path = settings.sessions_dir / f"{session_id}.jsonl"
    events = read_events(path)

    # 找分叉点：**第一条** `seq == before_seq` 的 user_message。用**下标**而不是按
    # seq 数值比较来切 —— seq 是"日志里第几条"，手改过的日志可能重复/缺失，
    # 而下标才是"这条消息之前在文件里的真实位置"。
    cut = -1
    for index, record in enumerate(events):
        if record.get("kind") == "user_message" and _as_seq(record) == before_seq:
            cut = index
            break
    if cut < 0:
        raise ValueError(t("ui.events.fork_no_turn"))

    # 一轮从它的 `user_message` 开始、到下一条 `user_message` 之前结束。
    # "保留分叉点及以前" = 截到**下一条 user_message** 之前（含分叉点那一轮的
    # assistant / tool / notice / turn_end 全部尾巴）。没有下一条就是"最后一轮"，
    # 整段照抄。
    end = len(events)
    for index in range(cut + 1, len(events)):
        if events[index].get("kind") == "user_message":
            end = index
            break

    copied = events[:end]
    new_sid = new_session_id()
    new_path = settings.sessions_dir / f"{new_sid}.jsonl"
    for record in copied:
        if record.get("kind") == "user_message":
            items = record.get("attachments")
            if isinstance(items, list) and items:
                record["attachments"] = copy_records_for_fork(
                    settings, session_id, new_sid, items
                )

    # 逐条重写（read_events 已经解析成 dict，写回去是**干净**的一份 ——
    # 半截行会被丢掉，而不是把坏数据带进新会话）。
    with new_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in copied:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 继承整理游标：复制来的历史里"已经整理进背景档案"的那部分，不该因为换了个
    # 会话 id 就再整理一遍（否则档案会出现近似重复）。游标按 seq 继承，封顶到
    # 新会话最后一条事件的 seq —— 原会话整理得比这更靠后也不关新会话的事。
    last_copied_seq = _as_seq(copied[-1]) if copied else 0
    if last_copied_seq and last_copied_seq > 0:
        from .memory import open_memory

        memory = open_memory(settings)
        try:
            memory.inherit_cursor(session_id, new_sid, last_copied_seq)
        finally:
            memory.close()

    return new_sid, new_path


def list_sessions(sessions_dir: Path) -> list[SessionInfo]:
    """列出会话。**跳过没有用户消息的半截文件**（见下）。"""
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return []
    infos: list[SessionInfo] = []
    for path in sessions_dir.glob("*.jsonl"):
        events = read_events(path)
        turns = sum(1 for r in events if r.get("kind") == "user_message")
        if not turns:
            # 只有 session_start 的半截文件：客户端在首轮中途断开时会留下它。
            # 它不是一次对话，列出来只会让侧栏多出一个"（空会话）"。
            continue
        infos.append(
            SessionInfo(
                session_id=path.stem,
                path=path,
                updated_at=path.stat().st_mtime,
                turns=turns,
                title=session_display_title(path)[:60] or t("ui.events.empty_session_title"),
            )
        )
    infos.sort(key=lambda info: info.updated_at, reverse=True)
    return infos
