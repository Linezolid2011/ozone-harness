"""Dream memory：后台把会话记录整理成**背景档案**（M7 整理流水线）。

## 它做什么

```
data/sessions/*.jsonl   ──(本模块)──▶   data/vault/{README.md, sections/, entities/}
   白天真实发生的记录                     压缩出来的长期档案（投影）
```

游标（`dream_cursor`）记着"每个会话整理到第几条"，所以每次只整理**新增**的部分；
整理完把游标推进到"连续读过的位置"（跳过没读的区间是**不允许**的，
那几条记录会永久丢掉 —— 见 `dream_records.ReadTracker`）。

## 它**不**做什么（这条比上面那条重要）

**它不改写用户的记忆条目。** 那是 ADR-053 划下的边界：

| | 记忆条目 | 背景档案 |
|---|---|---|
| 谁写 | 只有对话模型（用户明确要求） | 只有本模块 |
| 可信度 | 最高 | 次之（压缩推断） |

M4 的 dream 是在记忆表上做手术（合并/改写/删除），连"用户明确要求记住"的条目
也能被它改写 —— 那不是缺功能，是越界。所以那套机制连同它的工具一起退役（ADR-059）。

## 它是**单向门**（ADR-065 / ADR-072）

整理一旦跑完就**不可逆**：没有 `--dream-undo`，不留任何快照，**也没有预演**
（`--dream-dry-run` 已随 ADR-072 取消 —— 用户要的是"能重来"，不是"先看一眼"）。
所以：

* **重来的办法是"格式化并初始化 dream 层"**（`--dream-format` / 界面上的同名按钮）：
  清掉档案、运行记录与整理进度，下一次整理**从头**再来一遍。
  档案本来就是会话的投影，所以这条路真的能把结果重做出来 ——
  代价是再花一次整理的钱，而且手写的内容会一起没（那是不可逆的）；
* 差异靠**内存影像**算（运行前把 vault 读进内存，进程结束就没了，不落盘）；
* 想追回某份档案当时写了什么，只能看轨迹：`data/dreams/<run-id>.jsonl` 里
  存着每一次 `vault_write` 的**全文**。

## 安全设计

1. **可重放**：每次运行写 `data/dreams/<run-id>.jsonl`（完整轨迹）与
   `<run-id>.json`（结果、差异、用量、结构校验）；
2. **结构校验是机械的**：README 由模型写，所以跑完立刻用 `VaultStore.check()`
   查一遍（超预算 / 孤儿档案 / 坏 frontmatter / 坏文件名），结果写进运行记录并打给人看；
3. **显式触发**：绝不自动运行。整理要花钱，不能瞒着你花；
4. **"不可逆"必须写在运行之前**：CLI 与界面都先说清楚，不藏在文档里；
5. **记录里没有网页正文**：`dream_records` 只把"这里联网查过资料"留成一行标记，
   免得外部内容被洗成长期档案（ADR-039/048）。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .i18n import t
from .prompts import bundle
from .dream_records import PendingSession, ReadTracker, pending_sessions, summarize_pending
from .events import EventLog
from .llm_types import Message, Usage
from .loop import AgentLoop
from .memory import MemoryStore
from .providers.base import LLMProvider
from .tools.dream_tools import build_dream_tools
from .tools.registry import ToolRegistry
from .vault import (
    VaultStore,
    diff_images,
    vault_image,
)

# 整理侧的提示词文本在 `harness/prompts/{zh,en}.py`（ADR-085），
# 这里只保留"怎么拼"。取文本一律 `bundle()`。


def new_dream_id() -> str:
    return "dream-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def build_dream_system_message(
    settings: Settings, *, readme_budget: int | None = None
) -> Message:
    budget = (
        settings.vault_readme_token_budget
        if readme_budget is None
        else int(readme_budget)
    )
    return Message(
        role="system",
        content=bundle().DREAM_SYSTEM_PROMPT.format(readme_budget=budget),
    )


@dataclass
class DreamOutcome:
    run_id: str
    mode: str
    sessions: list[dict[str, Any]] = field(default_factory=list)
    diff: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    check: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "completed"
    error: str | None = None
    advanced: dict[str, int] = field(default_factory=dict)
    skipped: bool = False  # 没有待整理的记录，压根没调模型
    #: 这次整理用的是哪家 / 哪个模型（ADR-103）。写进运行记录，事后能回答
    #: "档案是哪个模型整理的" —— 换过模型之后这一点尤其重要。
    generation: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> int:
        return (
            len(self.diff.get("added", []))
            + len(self.diff.get("changed", []))
            + len(self.diff.get("removed", []))
        )


class DreamRunStore:
    """`data/dreams/` 下的运行记录。

    刻意**不用数据库**：一次运行的产物是"一份快照目录 + 一份 JSON + 一份轨迹"，
    三者天然是文件。放数据库反而会出现"快照在里面、文件在外面"的分裂，
    而且 `--dream-undo` 就依赖那张库表了（ADR-059 里把旧表删掉正是这个理由）。
    """

    def __init__(self, dreams_dir: Path) -> None:
        self.dir = Path(dreams_dir)

    @staticmethod
    def _checked(run_id: str) -> str:
        """run_id 要当成**文件名**用，所以必须先校验（同 ADR-049 的会话 id）。

        `data/dreams/<run_id>.json` 是拼出来的：一个带 `..\\` 或 `/` 的 id
        能让它读到、写到 vault 目录之外的地方。Web 端尤其要小心 ——
        URL 里的 `%5C` 会被解码成反斜杠，而 Windows 上反斜杠就是分隔符。
        """
        text = str(run_id or "").strip()
        if (
            not text
            or len(text) > 90
            or text in (".", "..")
            or ".." in text
            or any(ch in text for ch in "/\\:*?\"<>|")
            or any(ord(ch) < 32 for ch in text)
        ):
            raise ValueError(t("ui.dream.bad_run_id", run_id=repr(run_id)))
        return text

    def record_path(self, run_id: str) -> Path:
        return self.dir / f"{self._checked(run_id)}.json"

    def trace_path(self, run_id: str) -> Path:
        return self.dir / f"{self._checked(run_id)}.jsonl"

    def save(self, record: dict[str, Any]) -> Path:
        """原子写入：先写临时文件再 `os.replace`（同 vault 的写法）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        run_id = str(record.get("id") or "").strip()
        if not run_id:
            raise ValueError(t("ui.dream.run_needs_id"))
        path = self.record_path(run_id)
        temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:6]}")
        try:
            temp.write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, path)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
        return path

    def get(self, run_id: str) -> dict[str, Any] | None:
        try:
            path = self.record_path(run_id)
        except ValueError:
            # 非法 id 一律当"没有这次运行"：既不用区分，也不泄露路径存不存在
            return None
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        """按开始时间倒序列出运行记录。**只读**。"""
        if not self.dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in self.dir.glob("dream-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                out.append(data)
        out.sort(key=started_at_of, reverse=True)
        return out[: max(1, limit)]

    def set_status(self, run_id: str, status: str) -> dict[str, Any] | None:
        record = self.get(run_id)
        if record is None:
            return None
        record["status"] = status
        record["finished_at"] = record.get("finished_at") or _now()
        self.save(record)
        return record

    def mark_interrupted(self, *, keep: str = "") -> list[str]:
        """把"还挂在 `running`"的旧记录标成 `interrupted`，返回被标的那几个 id。

        什么时候会留下这种记录：进程被杀、断电、Ctrl+C —— 记录已经写成 `running`，
        但没人来得及收尾（实测把一次运行掐死就是这样）。**它不是"正在跑"**：
        同一个进程里同一时刻只可能有一次整理（Web 拿锁、CLI 是单进程），
        所以除自己之外还挂在 `running` 的，一定是死掉的那次。

        代价说清楚：两个终端同时各跑一次 CLI 整理时，后启动的会把先启动的
        短暂标成 interrupted；先启动那次收尾时会写回 completed。
        只是显示上的一瞬，**不动任何数据**。
        """
        flipped: list[str] = []
        for record in self.list(limit=10_000):
            run_id = str(record.get("id") or "")
            if not run_id or run_id == keep:
                continue
            if record.get("status") != "running":
                continue
            record["status"] = "interrupted"
            record["stop_reason"] = t("ui.dream.interrupted")
            self.save(record)
            flipped.append(run_id)
        return flipped


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def started_at_of(record: dict[str, Any]) -> datetime:
    """运行记录的 `started_at` → 可比较的 datetime。

    **不能拿 ISO 字符串直接比大小**：`2026-10-12T18:00:00+08:00` 与
    `2026-10-12T11:00:00+00:00` 是同一时刻，但字符串比较会说前者更大。
    同一台机器上时区通常不变，可一旦改过时区（或跨夏令时），
    "哪次运行更晚"就会算错 —— 而那正好是回滚时决定"要连带撤销哪些运行"的依据。
    """
    raw = str(record.get("started_at") or "")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)  # 坏值排在最前，不参与"更晚"
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value


class DreamRunner:
    """跑一轮整理。

    与 M4 的 DreamRunner 同名，但**作用对象完全不同**（那时改记忆表，现在写 vault）。
    保留这个名字是为了让 CLI / Web 两处调用点不必大改，语义变化写在文档里。
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        settings: Settings,
        memory: MemoryStore,
        vault: VaultStore,
        emit: Callable[..., None] | None = None,
        max_sessions: int | None = None,
        generation: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.emit = emit or (lambda kind, **data: None)
        # 这次整理用的是哪家 / 哪个模型（ADR-103）。由调用方（CLI / Web）传进来，
        # 因为它才是决定解析那一步；runner 只负责把它写进运行记录。
        self.generation = dict(generation or {})
        self.run_id = new_dream_id()
        self.store = DreamRunStore(settings.dreams_dir)
        # 上一次被杀/断电留下的"跑步中"记录：这里收尾（见 mark_interrupted）
        self.interrupted = self.store.mark_interrupted(keep=self.run_id)

        self.cursors = memory.cursor_map()
        limit = settings.dream_max_sessions if max_sessions is None else max_sessions
        self.pending: list[PendingSession] = pending_sessions(
            settings.sessions_dir, self.cursors, limit=limit
        )
        self.tracker = ReadTracker(self.cursors)
        self.vault = vault
        # 全新/空 vault：第一次**真的有记录要整理**时才切到原生格式
        # （sections.yaml + 生成 README）。没记录就什么都不写 —— 那一次本来
        # 就不会调用模型，没有理由顺手在 data/ 里落一个文件。
        # 有内容但没有清单的旧 vault **不迁移**，保持"模型写 README"的旧行为。
        if self.pending:
            self.vault.ensure_manifest()
        if self.vault.is_native():
            budget = self.vault.manifest().inject_char_cap
        else:
            budget = settings.vault_readme_token_budget
        self.system_message = build_dream_system_message(
            settings, readme_budget=budget
        )

        self.real_root = Path(vault.root)
        # **运行前的内存影像**：算差异要有"之前"可比，但绝不能落盘成快照
        # （那又是备份了，ADR-065）。进程结束它自然就没了。
        self.before_image = vault_image(self.real_root)

        registry = ToolRegistry(max_result_chars=settings.max_tool_result_chars)
        for tool in build_dream_tools(
            store=self.vault,
            sessions_dir=settings.sessions_dir,
            cursors=self.cursors,
            tracker=self.tracker,
            record_max_events=settings.dream_record_max_events,
            record_max_chars=settings.dream_record_max_chars,
            max_result_chars=settings.max_tool_result_chars,
            today=datetime.now().strftime("%Y-%m-%d"),
        ):
            registry.register(tool)

        self.log = EventLog(self.store.trace_path(self.run_id), self.run_id)
        # dream 用自己的预算：与聊天分开，避免整理把聊天额度吃掉
        dream_settings = replace(
            settings,
            max_steps=settings.dream_max_steps,
            max_tokens_per_turn=settings.dream_token_budget,
            # 单次输出上限要单独给：整理要在一次回复里吐好几个文件的内容，
            # 而思考本身就能把默认的 2048 烧光（M4 实测过：reasoning 用满、
            # 一个工具都没调，却被当成"无事可做"）。
            max_output_tokens=settings.dream_max_output_tokens,
            max_wall_seconds=settings.dream_wall_seconds,
        )
        self.loop = AgentLoop(
            provider=provider,
            registry=registry,
            settings=dream_settings,
            memory=memory,
            log=self.log,
            emit=self.emit,
            planner=self._plan,
        )

    def _plan(
        self, history: list[Message], step: int, user_text: str
    ) -> tuple[list[Message], dict[str, Any]]:
        # 提示词每步都一样，history 里逐渐累积工具调用与结果
        return [self.system_message] + history, {
            "phase": "dream",
            "dream_run": self.run_id,
            "dream_sessions": [item.session_id for item in self.pending],
        }

    # ------------------------------------------------------------------

    def _pending_payload(self) -> dict[str, Any]:
        return {
            "sessions": len(self.pending),
            "events": sum(item.new_events for item in self.pending),
            "detail": [item.to_dict() for item in self.pending],
        }

    def run(self) -> DreamOutcome:
        started = _now()
        pending = self._pending_payload()
        # 现在只有一种模式：真整理。（`--dream-dry-run` 已随 ADR-072 取消 ——
        # 老运行记录里的 `mode: "dry-run"` 仍然照读，历史不改写。）
        mode = "apply"
        record: dict[str, Any] = {
            "id": self.run_id,
            "started_at": started,
            "finished_at": None,
            "mode": mode,
            "status": "running",
            "pending": pending,
            "generation": dict(self.generation),
            "advanced": {},
            "diff": {},
            "check": {},
            "summary": "",
            "usage": {},
            "stop_reason": "",
        }
        return self._run_locked(record, mode, started)

    def _run_locked(
        self, record: dict[str, Any], mode: str, started: str
    ) -> DreamOutcome:
        if not self.pending:
            # **没有待整理的就不要调模型**：白花一次钱，还会在历史里留下
            # 一次"什么都没做"的运行。先看有没有活干，再决定花不花钱。
            self.log.close()
            record.update(
                finished_at=_now(),
                status="empty",
                summary=t("ui.dream.nothing_pending"),
                stop_reason="nothing_pending",
            )
            self.store.save(record)
            return DreamOutcome(
                run_id=self.run_id,
                mode=mode,
                sessions=[],
                diff={},
                check=self.vault.check(),
                summary=str(record["summary"]),
                stop_reason="nothing_pending",
                skipped=True,
                generation=dict(self.generation),
            )

        self.store.save(record)
        self.emit("dream_start", run_id=self.run_id, mode=mode, pending=record["pending"])

        try:
            outcome = self.loop.run(bundle().DREAM_INSTRUCTION)
        except Exception as exc:  # noqa: BLE001 - 失败也要留下记录
            self.log.close()
            record.update(
                finished_at=_now(),
                status="error",
                stop_reason=f"{type(exc).__name__}: {exc}",
            )
            self.store.save(record)
            raise
        finally:
            # `close()` 是幂等的（文件对象重复关不报错），所以这里无条件调，
            # 不去看 EventLog 的内部句柄状态 —— 那是别人的私有字段。
            self.log.close()

        # 原生模式的生成物（README / index / earlier_months）在**模型写完之后**
        # 由 harness 重建。旧 vault 无清单，这里是 no-op。
        today = datetime.now().strftime("%Y-%m-%d")
        self.vault.rebuild_generated(today=today, last_dream=today)

        # 差异 = 内存影像对比：运行前那份在内存里，运行后从磁盘读（同一个函数）
        diff = diff_images(vault_image(self.vault.root), self.before_image)
        check = self.vault.check()

        advanced = self.tracker.advance_map()
        for session_id, last_seq in advanced.items():
            self.memory.advance_cursor(session_id, last_seq, self.run_id)

        record.update(
            finished_at=_now(),
            status="completed" if outcome.ok else outcome.stop_reason,
            advanced=advanced,
            diff=diff,
            check=check,
            summary=outcome.text,
            usage=outcome.usage.to_dict(),
            stop_reason=outcome.stop_reason,
        )
        self.store.save(record)
        self.emit(
            "dream_done",
            run_id=self.run_id,
            mode=mode,
            diff=diff,
            check=check,
            advanced=advanced,
            summary=outcome.text,
        )
        return DreamOutcome(
            run_id=self.run_id,
            mode=mode,
            sessions=[item.to_dict() for item in self.pending],
            diff=diff,
            check=check,
            summary=outcome.text,
            usage=outcome.usage,
            stop_reason=outcome.stop_reason,
            error=outcome.error,
            advanced=advanced,
            generation=dict(self.generation),
        )


# --------------------------------------------------------------------------
# 审查输出
# --------------------------------------------------------------------------


def render_diff(diff: dict[str, list[dict[str, Any]]]) -> str:
    """把文件级差异渲染成人能看的一段。"""
    added = diff.get("added", [])
    changed = diff.get("changed", [])
    removed = diff.get("removed", [])
    if not (added or changed or removed):
        return t("ui.dream.diff_none")

    lines: list[str] = []
    if added:
        lines.append(t("ui.dream.diff_added", count=len(added)))
        for row in added:
            summary = row.get("summary") or ""
            lines.append(f"  + {row['path']}  {summary}")
    if changed:
        lines.append(t("ui.dream.diff_changed", count=len(changed)))
        for row in changed:
            lines.append(
                t(
                    "ui.dream.diff_chars",
                    path=row["path"],
                    before=row.get("chars_before"),
                    after=row.get("chars_after"),
                )
            )
    if removed:
        lines.append(t("ui.dream.diff_removed", count=len(removed)))
        for row in removed:
            lines.append(f"  - {row['path']}")
    return "\n".join(lines)


def render_check(check: dict[str, Any]) -> str:
    """结构校验那一段（原生模式 README 是生成物，旧模式由模型写；都要机械地看一眼）。"""
    problems = list(check.get("problems") or [])
    if not check.get("exists"):
        return t("ui.dream.check_no_vault")
    cap = int(check.get("readme_char_cap") or 0)
    header = (
        t(
            "ui.dream.check_files_chars",
            files=check.get("files", 0),
            used=check.get("readme_chars", 0),
            cap=cap,
        )
        if cap
        else t(
            "ui.dream.check_files",
            files=check.get("files", 0),
            tokens=f"{check.get('readme_tokens', 0)}/{check.get('readme_budget', 0)}",
        )
    )
    if not problems:
        return header + t("ui.dream.check_ok")
    return header + t("ui.dream.check_problems") + "\n".join(
        f"  - {problem}" for problem in problems
    )


def dream_format_confirm() -> str:
    """格式化 dream 层时必须一字不差地说出的那个词（**每次现取**）。

    **它不是安全机制**（本机单用户、能跑这个程序的人本来就能删文件），
    它是一个**减速带**：这个动作不可逆，所以让人停一下、把那几个字打出来，
    而不是顺手点两下。CLI 与网页界面用的是同一个词，改就一起改。

    中英各一套：中文是「格式化」，英文是 `format` —— 前端从接口拿这个词
    （见 `/api/dream/format` 的 `confirm_word`），不硬编码，所以换语言时它也换。
    """
    return t("ui.dream.format_confirm")


def format_dream(
    settings: Settings, memory: MemoryStore, *, initialize: bool = True
) -> dict[str, Any]:
    """**格式化并初始化 dream 层**：档案、运行记录、整理进度一起清掉。

    **只动 dream 那三样东西**：`data/vault/`、`data/dreams/`、
    记忆库里的 `dream_cursor` 表。会话、记忆条目、接口配置、工具开关**一个字节都不碰** ——
    这正是它区别于"恢复出厂设置"的地方（那个会把会话与 API key 一起抹掉）。

    `initialize=True` 时顺手建一个空骨架（目录 + 占位 README，与 `--vault-init` 同一个
    动作），于是 Dream 页打开就是一个干净的、可以开始用的状态。

    **为什么这个动作是有意义的**：档案是会话的**投影**，所以清掉之后重新整理一遍
    就能把它重做出来（代价是再花一次整理的钱）。ADR-072 取消预演之后，
    这是"这次整理得不好"唯一的补救路径 —— 而且它比预演更符合"能重来"的取向。

    ⚠️ **不可逆**：手写的档案也会一起没，删掉的东西不在回收站里。
    所以调用方必须先让人 double-check（`DREAM_FORMAT_CONFIRM`）。
    """
    from .reset import dream_state

    data_dir = Path(settings.data_dir)
    vault_dir = Path(settings.vault_dir)
    dreams_dir = Path(settings.dreams_dir)
    # 防手滑：这两个目录**必须真的在 data_dir 底下**才敢动手。
    # （它们来自配置，理论上可以被指到别处；"理论上不会"不该成为 rmtree 的理由。）
    for target in (vault_dir, dreams_dir):
        if data_dir not in target.parents:
            raise ValueError(
                t("ui.dream.format_not_in_data_dir", path=target, data_dir=data_dir)
            )

    before = dream_state(settings)
    staged = data_dir / f".dream-format-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    staged.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    cleared = 0
    leftovers: list[str] = []
    try:
        for target in (vault_dir, dreams_dir):
            if target.exists():
                # **先改名挪走再删**（同 reset.py 的理由）：直接删一半失败会留下
                # 一个"删了一半"的目录，看起来像坏了；改名是原子的。
                destination = staged / target.name
                os.replace(target, destination)
                moved.append(target.name)
        # 进度：清掉 dream_cursor（这一步不能失败 —— 它决定下次整理会不会重来）
        cleared = memory.clear_cursors()
        if initialize:
            VaultStore(
                vault_dir,
                read_max_chars=settings.vault_read_max_chars,
                write_max_chars=settings.vault_write_max_chars,
                max_files=settings.vault_max_files,
                name_max_chars=settings.vault_name_max_chars,
                readme_token_budget=settings.vault_readme_token_budget,
            ).init_skeleton()
    except BaseException:
        # **失败时把已经挪走的东西放回去**，绝不删。原来的写法是 `finally: 删暂存目录`，
        # 于是"移到一半失败"或"清游标失败"会把用户的档案（含手写的那份）直接销毁，
        # 而 `dream_cursor` 还停在"这些会话已经整理过了" —— 下次整理也不会重建它们，
        # 一次可恢复的失败变成永久丢失。
        restored = _restore_staged(staged, data_dir)
        try:
            staged.rmdir()
        except OSError:
            pass
        detail = "、".join(moved) if moved else "（没有东西被移动）"
        raise RuntimeError(
            t(
                "ui.dream.format_failed",
                detail=detail,
                restored="、".join(restored) or "（无）",
                staged=staged,
            )
        ) from None
    # 走到这里说明每一步都成功了：这时才可以删暂存目录
    leftovers = _delete_staged(staged)
    return {
        "ok": True,
        "before": before,
        "after": dream_state(settings),
        "moved": moved,
        "cursors_cleared": cleared,
        "leftovers": leftovers,
        "initialized": bool(initialize),
    }


def _restore_staged(staged: Path, data_dir: Path) -> list[str]:
    """把暂存目录里的东西**搬回原处**（格式化失败时用）。返回搬回去的名字。

    比"发现失败就删掉暂存目录"多这一步，是因为那些目录里装的是用户的档案 ——
    包括手写的那几份，删了就再也回不来了（ADR-095）。同名目标已存在时**不覆盖**：
    那说明初始化骨架已经建出来了，让用户自己决定保留哪份更安全。
    """
    restored: list[str] = []
    if not staged.is_dir():
        return restored
    for child in sorted(staged.iterdir()):
        target = data_dir / child.name
        if target.exists():
            continue
        try:
            os.replace(child, target)
            restored.append(child.name)
        except OSError:
            continue
    return restored


def _delete_staged(staged: Path) -> list[str]:
    """删掉暂存目录，返回**没删掉**的路径（空列表 = 干净）。"""
    import shutil

    leftovers: list[str] = []
    if not staged.is_dir():
        return leftovers
    for child in sorted(staged.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            leftovers.append(str(child))
    try:
        staged.rmdir()
    except OSError:
        leftovers.append(str(staged))
    return leftovers


__all__ = [
    "DreamOutcome",
    "DreamRunner",
    "DreamRunStore",
    "build_dream_system_message",
    "dream_format_confirm",
    "format_dream",
    "new_dream_id",
    "render_check",
    "render_diff",
    "started_at_of",
    "summarize_pending",
]
