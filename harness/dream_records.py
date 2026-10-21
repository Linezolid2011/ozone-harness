"""把会话的原始事件读成"可整理的记录"（M7 整理流水线的输入端）。

## 它解决什么问题

Kimi 的 dream 输入是"白天每个会话新产生的记录"，我们的记录早就在了 ——
`data/sessions/*.jsonl`，append-only、可重放（ADR-006）。缺的只是两件事：

1. **哪些还没整理过**（游标）；
2. **怎么把一串事件渲染成一段人能读、模型能读的文本**（本模块）。

渲染刻意做成**纯函数**：输入事件列表、输出文本，不碰文件系统、不碰数据库。
于是"某段对话会被整理成什么样"可以离线逐条测 —— 这正是提示词层的成本所在，
不能在真跑一次模型之后才发现渲染得不对。

## 三个刻意的取舍

**一、工具调用里只有"记忆三件套"会被渲染。**
对话里的 `mem_create` / `mem_edit` 参数是**用户明确要求记住**的信号，
对整理很有价值（"这件事被当成长期事实记下来过"）。而网页抓取的正文
**一律不进记录** —— 网页是不可信输入（ADR-039/048），把它喂给一个会写档案的
模型，等于给了它一条"把外部内容洗成长期事实"的通道。只留一行标记，
让模型知道"这段信息来自外部"。

**二、分页是"从游标开始连续读"，不跳读。**
`read_records` 返回 `next_seq`，调用方据此推进游标。跳读会让中间那几条
永远没人看（而游标一旦越过它们就再也回不来了）。

**三、渲染有硬上限，并且**如实报告**还剩多少。**
一份会话可能有几百条事件，一次全塞进上下文既烧钱又没必要 ——
模型该做的是先读最近的、需要时再往前翻。截断发生在**事件边界**上，
并明确写出"还有 N 条未读"，而不是把半条消息切一半。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .events import read_events
from .i18n import t

# 会被渲染进记录的对话工具。其余工具（联网、背景档案）只留标记。
_MEMORY_TOOLS = {"mem_create", "mem_edit"}
_FORGET_TOOLS = {"mem_del"}
_WEB_TOOLS = {"web_search", "web_open_url"}

# 单条消息最多留多少字符。超长的一条（贴了一大段代码）不该把整个预算吃掉。
_MAX_ONE_EVENT_CHARS = 1200


@dataclass
class PendingSession:
    """一个"还有没整理的记录"的会话。"""

    session_id: str
    title: str
    path: Path
    cursor: int          # 已整理到第几条（含）；0 表示一条都没整理过
    last_seq: int        # 会话里最后一条事件的 seq
    updated_at: float
    total_turns: int
    stale: bool = False  # 日志比游标还短（被改过/换过），已按"从头整理"处理

    @property
    def new_events(self) -> int:
        return max(0, self.last_seq - self.cursor)

    @property
    def is_pending(self) -> bool:
        return self.new_events > 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "title": self.title,
            "cursor": self.cursor,
            "last_seq": self.last_seq,
            "new_events": self.new_events,
            "turns": self.total_turns,
        }
        if self.stale:
            data["stale"] = True
        return data


@dataclass
class RenderedRecords:
    """一次读取的产物。`next_seq` 是调用方该写回游标的值。"""

    text: str
    from_seq: int
    to_seq: int
    rendered: int            # 真正渲染出内容的条数
    skipped: int             # 跳过的条数（工具噪声等）
    remaining: int           # 这个会话里还没读的可渲染条数
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text.strip()


def pending_sessions(
    sessions_dir: Path,
    cursors: Mapping[str, int],
    *,
    limit: int | None = None,
) -> list[PendingSession]:
    """列出还有未整理记录的会话（按最后更新时间倒序）。

    **不创建任何东西**：目录不存在就返回空列表（同 reset / vault 的只读原则）。

    **坏会话一律跳过，不拖垮整轮整理**：一份日志被手工改坏（比如 `seq` 不是数字）
    不该让所有会话都整理不了。跳过的那些记在 `unreadable` 里，
    由调用方如实报出来（`pending_sessions` 的返回值不带它，
    需要时用 `scan_sessions` 拿完整结果）。
    """
    scanned = scan_sessions(sessions_dir, cursors, limit=limit)
    return scanned["pending"]


def scan_sessions(
    sessions_dir: Path,
    cursors: Mapping[str, int],
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """`pending_sessions` 的完整版：连"读不出来的会话"一起报出来。"""
    sessions_dir = Path(sessions_dir)
    out: list[PendingSession] = []
    unreadable: list[str] = []
    if not sessions_dir.is_dir():
        return {"pending": [], "unreadable": unreadable}
    for path in sorted(sessions_dir.glob("*.jsonl")):
        try:
            events = read_events(path)
        except Exception:  # noqa: BLE001 - 一份坏日志不该让整轮整理停摆
            unreadable.append(path.stem)
            continue
        if not events:
            continue
        turns = sum(1 for record in events if record.get("kind") == "user_message")
        if not turns:
            # 只有 session_start 的半截文件：没有对话可言（与 list_sessions 同口径）
            continue
        session_id = path.stem
        seqs = _iter_seq(events)
        last_seq = max(seqs)
        cursor = max(0, int(cursors.get(session_id, 0)))
        # 日志比游标还短（文件被改短、被换掉、或者从备份恢复过）：
        # 说明"前 N 条已整理"这个前提不成立了 —— 那些记录会**永久**读不到。
        # 所以按"从没整理过"处理，并把这个事实标出来（不然它就是静默的）。
        stale = last_seq < cursor
        if stale:
            cursor = 0
        out.append(
            PendingSession(
                session_id=session_id,
                title=_title_from(events),
                path=path,
                cursor=cursor,
                last_seq=last_seq,
                updated_at=path.stat().st_mtime,
                total_turns=turns,
                stale=stale,
            )
        )
    out.sort(key=lambda item: item.updated_at, reverse=True)
    pending = [item for item in out if item.is_pending]
    if limit is not None and limit > 0:
        pending = pending[:limit]
    return {"pending": pending, "unreadable": unreadable}


def _seq_of(record: dict[str, Any], fallback: int) -> int:
    """取事件序号。**坏值不许把整条流水线炸掉。**

    `seq` 是别人（`EventLog`）写进去的普通字段，任何人手工编辑日志、
    或跑过旧版本之后，里面都可能有非数字。第一版直接 `int(...)`，
    结果**一个坏会话会让所有会话都整理不了** —— `pending_sessions`、
    `records_list`、`--vault`、Web 的整理页全都在同一处抛 ValueError。
    所以这里退回"行序号"（至少保持单调），并且在下游如实报出来。
    """
    value = record.get("seq")
    if isinstance(value, bool):  # bool 是 int 的子类，别把它当序号
        return fallback
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _iter_seq(events: Sequence[dict[str, Any]]) -> list[int]:
    """把一串事件变成"位置 → 序号"，坏值退回位置 + 1（1-based，与 EventLog 一致）。"""
    return [_seq_of(record, index + 1) for index, record in enumerate(events)]


def _title_from(events: Sequence[dict[str, Any]]) -> str:
    """从**已经读到的**事件里算标题。

    `events.session_display_title()` 会为了一份标题把整个文件再读两遍
    （`session_title` 一次、`read_events` 一次），而 `pending_sessions`
    已经读过一遍了 —— 120 个会话时要多花两秒多（审查实测 3.5s）。
    """
    custom = ""
    first_user = ""
    for record in events:
        kind = record.get("kind")
        if kind == "session_title":
            custom = str(record.get("title") or "").strip()
        elif kind == "user_message" and not first_user:
            first_user = str(record.get("text") or "").replace("\n", " ").strip()[:40]
    return (custom[:60] if custom else first_user) or t("ui.record.no_title")


def _clip(text: str, limit: int = _MAX_ONE_EVENT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + t("ui.record.clipped", count=len(text) - limit)


def _was_clipped(record: dict[str, Any]) -> bool:
    """这条事件的内容有没有被单条上限截掉一部分。

    **必须单独算并报出来**：`remaining` / `truncated` 只表达"还有没有没读的**事件**"，
    单条被截掉的那几百字既不进 `warnings` 也没有再读一次的口子 ——
    而游标已经越过它了。不报，模型就会以为它看全了。
    """
    kind = record.get("kind")
    if kind == "user_message":
        if len(str(record.get("text") or "").strip()) > _MAX_ONE_EVENT_CHARS:
            return True
        # 多模态关着时，真正长的是 **OCR 文本**（用户打的字可能一个字都没有）。
        # 不把它算进来的话，一段几千字的识别结果会被悄悄截掉而没有 warning。
        if str(record.get("modality") or "").strip().lower() == "text":
            ocr_text, _failed = _ocr_digest(record)
            return len(ocr_text.strip()) > _MAX_ONE_EVENT_CHARS
        return False
    if kind == "assistant_message":
        message = record.get("message") or {}
        return len(str(message.get("content") or "").strip()) > _MAX_ONE_EVENT_CHARS
    if kind == "session_summary":
        return len(str(record.get("summary") or "").strip()) > _MAX_ONE_EVENT_CHARS
    return False


def _tool_line(record: dict[str, Any]) -> str | None:
    """工具调用那一行。**只渲染记忆三件套的内容，联网只留标记。**"""
    name = str(record.get("name") or "")
    if name in _MEMORY_TOOLS:
        content = ""
        try:
            args = json.loads(record.get("arguments") or "{}")
            if isinstance(args, dict):
                content = str(args.get("content") or "")
        except (json.JSONDecodeError, TypeError):
            content = ""
        if not content:
            result = record.get("result")
            if isinstance(result, dict):
                content = str(result.get("content") or "")
        if not content:
            return None
        key = "ui.record.mem_create" if name == "mem_create" else "ui.record.mem_edit"
        return t(key, content=_clip(content, 200))
    if name in _FORGET_TOOLS:
        reason = ""
        try:
            args = json.loads(record.get("arguments") or "{}")
            if isinstance(args, dict):
                reason = str(args.get("reason") or "")
        except (json.JSONDecodeError, TypeError):
            reason = ""
        if reason:
            return t("ui.record.mem_del", reason=reason)
        return t("ui.record.mem_del_no_reason")
    if name in _WEB_TOOLS:
        # 只有标记，没有正文：网页内容不得成为档案的来源（ADR-039/048）
        return t("ui.record.web_lookup")
    return None


def _attachment_count(record: dict[str, Any]) -> int:
    """这条事件引用了几张图（老日志没有 attachments 字段 = 0）。

    **只看数量，绝不看文件名**：文件名是用户给的显示字段，对"这段时间发生了什么"
    没有信息量，却会让整理输入里的噪声变多（整理者据此写的是长期档案）。
    这条计数存在的唯一目的，是让"用户发过一张图"这件事在档案里留下痕迹。
    """
    items = record.get("attachments")
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if isinstance(item, dict))


def _ocr_digest(record: dict[str, Any]) -> tuple[str, int]:
    """把这条消息里各张图的 OCR 结果拼成一段文字，返回 (文字, 识别失败的张数)。

    **多模态关着时，OCR 文字就是用户那次传过来的全部内容** ——
    它在长期档案里消失，等于这段时间被整理成"用户什么都没说"。
    所以这里要把它带进记录（契约 §13.4 的同一件事，只是面向整理而不是面向模型）。

    失败的张数单独报出来：档案里必须能区分"这张图没文字"与"这张图没识别成功"。
    """
    items = record.get("attachments")
    if not isinstance(items, list):
        return "", 0
    texts: list[str] = []
    failed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        ocr = item.get("ocr")
        if not isinstance(ocr, dict):
            continue
        if str(ocr.get("error") or "").strip():
            failed += 1
            continue
        body = str(ocr.get("text") or "").strip()
        if body:
            texts.append(body)
    return "\n".join(texts), failed


def render_event(record: dict[str, Any]) -> str | None:
    """把一条事件渲染成一行（或 None = 这条不进记录）。**纯函数**。

    助手那两行的前缀是**刻意加的**：助手在对话里说的话（包括它复述档案、
    转述它刚在网上读到的内容）不是事实来源，而整理者会拿这份记录去写长期档案。
    只靠提示词里一句"只有用户说的话算证据"不够 —— 在这里把来源标出来，
    模型判断时才有依据（ADR-061）。
    """
    kind = record.get("kind")
    if kind == "user_message":
        text = _clip(str(record.get("text") or ""))
        images = _attachment_count(record)
        # 多模态关着的那一轮：**OCR 文字才是用户真正传过来的内容**，
        # 不能因为"图片本身没发出去"就让它在档案里消失（契约 §13.4/§9）。
        # 同时要标明它从哪来 —— 档案里必须能分清"用户打的字"与"图片识别出的字"。
        modality = "text" if str(record.get("modality") or "").strip().lower() == "text" else "image"
        if modality == "text" and images:
            ocr_text, failed = _ocr_digest(record)
            note = (
                t("ui.record.ocr_note_failed", failed=failed)
                if failed
                else t("ui.record.ocr_note")
            )
            body = (
                _clip(ocr_text)
                if ocr_text.strip()
                else t("ui.record.ocr_no_text")
            )
            head = text or t("ui.record.user_images_only", count=images)
            return t("ui.record.user_ocr", head=head, note=note, body=body)
        # 只有图没有字的消息以前会被当成空文本**整条丢掉**，于是档案里那段时间
        # 会凭空少一轮（契约 §9）。这里给它一个占位渲染，让"用户发过一张图"
        # 留在记录里；有文字时照旧，只补一个小标记。
        if text and images:
            return t("ui.record.user_with_images", text=text, count=images)
        if text:
            return t("ui.record.user", text=text)
        if images:
            return t("ui.record.user_images_only", count=images)
        return None
    if kind == "assistant_message":
        message = record.get("message") or {}
        text = _clip(str(message.get("content") or ""))
        # 思考内容刻意丢弃：它是过程，不是事实，而且往往比正文长好几倍
        return t("ui.record.assistant", text=text) if text else None
    if kind == "tool_call":
        return _tool_line(record)
    if kind == "session_summary":
        summary = _clip(str(record.get("summary") or ""))
        return t("ui.record.summary", summary=summary) if summary else None
    return None


def render_records(
    events: Sequence[dict[str, Any]],
    *,
    start_seq: int = 1,
    max_events: int = 40,
    max_chars: int = 6000,
) -> RenderedRecords:
    """从 `start_seq` 开始**连续**渲染，直到事件数或字符数到上限。

    `next_seq` 的语义是"下一个还没被渲染的 seq"：它落在**最后一个渲染成功的事件之后**，
    所以跳过的（工具噪声）也会被越过 —— 那些本来就不需要整理。
    分页边界只落在事件之间，绝不切开一条消息。
    """
    seqs = _iter_seq(events)
    indexed = [
        (seq, record)
        for seq, record in zip(seqs, events)
        if seq >= start_seq
    ]
    indexed.sort(key=lambda item: item[0])
    if not indexed:
        return RenderedRecords("", start_seq, start_seq - 1, 0, 0, 0)

    lines: list[str] = []
    used = 0
    rendered = 0
    skipped = 0
    clipped = 0
    last_seq = start_seq - 1
    for seq, record in indexed:
        line = render_event(record)
        if line is None:
            skipped += 1
            last_seq = seq
            continue
        cost = len(line) + 1
        if rendered and (rendered >= max_events or used + cost > max_chars):
            break
        # 单条就超预算时也要收下第一条，否则模型会看到"空记录"而误判无事可做
        lines.append(line)
        used += cost
        rendered += 1
        if _was_clipped(record):
            clipped += 1
        last_seq = seq

    remaining = sum(
        1
        for seq, record in indexed
        if seq > last_seq and render_event(record) is not None
    )
    warnings: list[str] = []
    if clipped:
        warnings.append(
            t("ui.record.clipped_warning", count=clipped, limit=_MAX_ONE_EVENT_CHARS)
        )
    # 序号缺口：崩溃留下的半行会被 `read_events` 跳过，但它**占过一个 seq**。
    # 不报的话，那条记录就永远消失得无声无息（游标越过它之后再也不会回来）。
    gaps = sum(
        1
        for previous, current in zip([seq for seq, _ in indexed], [seq for seq, _ in indexed][1:])
        if current > previous + 1
    )
    if gaps:
        warnings.append(t("ui.record.gap_warning", count=gaps))
    truncated = remaining > 0
    text = "\n".join(lines)
    if truncated:
        text += t("ui.record.truncated", count=remaining)
    return RenderedRecords(
        text=text,
        from_seq=start_seq,
        to_seq=last_seq,
        rendered=rendered,
        skipped=skipped,
        remaining=remaining,
        truncated=truncated,
        warnings=warnings,
    )


def read_session(
    path: Path,
    *,
    start_seq: int = 1,
    max_events: int = 40,
    max_chars: int = 6000,
) -> RenderedRecords:
    """读一个会话文件的一段。**只读**。"""
    return render_records(
        read_events(Path(path)),
        start_seq=start_seq,
        max_events=max_events,
        max_chars=max_chars,
    )


def summarize_pending(sessions: Iterable[PendingSession]) -> dict[str, Any]:
    """给界面/CLI 用的一句话摘要。"""
    items = [item for item in sessions if item.is_pending]
    return {
        "sessions": len(items),
        "events": sum(item.new_events for item in items),
        "turns": sum(item.total_turns for item in items),
        "latest": items[0].session_id if items else "",
        # 日志被改短过的会话数：这不是正常状态，界面上要说出来
        "stale": sum(1 for item in items if item.stale),
    }


class ReadTracker:
    """记录"本次整理读过哪些区间"，用来决定游标能推进到哪。

    为什么不能简单地"读过哪个会话就推进到会话末尾"：模型完全可以只读一段
    （它该这么做 —— 一次读完一个几百条的会话既烧钱也没必要）。
    而游标一旦越过**没读过**的区间，那几条记录就再也进不了档案了。

    所以这里维护的是"读过的区间集合"，游标取**从游标起、一条不漏的那一段**：

        cursor=10，读了 [15,25]  →  推不动（11~14 没读）
        又读了 [11,14]           →  两段接上，推进到 25

    第一版只记了"连续读到的最大序号"，于是补上缺口时会把 [15,25] 忘掉，
    下次还得重读一遍 —— 那是真金白银的重复开销（审查式的自测发现的）。
    """

    def __init__(self, cursors: Mapping[str, int]) -> None:
        self._base = {str(key): max(0, int(value)) for key, value in dict(cursors).items()}
        self._ranges: dict[str, list[tuple[int, int]]] = {}

    def note(self, session_id: str, from_seq: int, to_seq: int) -> None:
        """登记一次读取，并与已有区间**合并**（相接或重叠的算一段）。"""
        sid = str(session_id)
        try:
            start, end = int(from_seq), int(to_seq)
        except (TypeError, ValueError):
            return
        if end < start:
            return
        ranges = self._ranges.setdefault(sid, [])
        ranges.append((start, end))
        ranges.sort()
        merged: list[tuple[int, int]] = []
        for item in ranges:
            if merged and item[0] <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], item[1]))
            else:
                merged.append(item)
        self._ranges[sid] = merged

    def advance_map(self) -> dict[str, int]:
        """该写回游标的会话 → 新值（只含**确实前进**了的）。"""
        out: dict[str, int] = {}
        for sid, ranges in self._ranges.items():
            base = self._base.get(sid, 0)
            cursor = base
            for start, end in ranges:  # 已按起点排序
                if start <= cursor + 1:
                    cursor = max(cursor, end)
                else:
                    break
            if cursor > base:
                out[sid] = cursor
        return out

    def read_sessions(self) -> list[str]:
        """本次读过（哪怕只读了一半）的会话，按 id 排序。"""
        return sorted(self._ranges)
