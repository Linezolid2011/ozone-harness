"""分叉会话测试：fork_session + 会话视图暴露的 seq / turn。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.events import EventLog, fork_session, read_events
from harness.llm_types import Message
from harness.web import build_session_view

from .helpers import test_settings


def _build_session(settings, turns: int = 3) -> list[int]:
    """造一个有 N 轮对话的会话，返回每轮 user_message 的 seq。

    每轮 3 条事件（user / assistant / turn_end），所以 seq 是 1, 4, 7, …。
    """
    log = EventLog(settings.sessions_dir / "src.jsonl", "src")
    seqs: list[int] = []
    for i in range(1, turns + 1):
        record = log.append("user_message", text=f"问题{i}")
        seqs.append(int(record["seq"]))
        log.append(
            "assistant_message",
            step=1,
            finish_reason="stop",
            usage={"total_tokens": 10},
            message=Message(role="assistant", content=f"回答{i}").to_record(),
        )
        log.append("turn_end", steps=1, stop_reason="completed", elapsed=1.0, usage={})
    log.close()
    return seqs


class ForkSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_fork_keeps_up_to_and_including_the_fork_point(self) -> None:
        seqs = _build_session(self.settings, turns=3)
        new_sid, new_path = fork_session(self.settings, "src", seqs[2])  # 第 3 轮
        texts = [r["text"] for r in read_events(new_path) if r.get("kind") == "user_message"]
        self.assertEqual(texts, ["问题1", "问题2", "问题3"])
        self.assertNotEqual(new_sid, "src")
        self.assertTrue(new_path.is_file())

    def test_original_session_is_untouched(self) -> None:
        _build_session(self.settings, turns=3)
        before = read_events(self.settings.sessions_dir / "src.jsonl")
        fork_session(self.settings, "src", 4)  # 第 2 轮
        after = read_events(self.settings.sessions_dir / "src.jsonl")
        self.assertEqual(before, after)

    def test_fork_at_first_turn_keeps_just_that_turn(self) -> None:
        seqs = _build_session(self.settings, turns=2)
        _new_sid, new_path = fork_session(self.settings, "src", seqs[0])
        texts = [r["text"] for r in read_events(new_path) if r.get("kind") == "user_message"]
        self.assertEqual(texts, ["问题1"])

    def test_fork_with_unknown_seq_raises(self) -> None:
        _build_session(self.settings, turns=2)
        with self.assertRaises(ValueError):
            fork_session(self.settings, "src", 999999)

    def test_fork_inherits_the_dream_cursor(self) -> None:
        from harness.memory import open_memory

        seqs = _build_session(self.settings, turns=3)  # seq 1 / 4 / 7
        memory = open_memory(self.settings)
        try:
            memory.advance_cursor("src", 4, "run-1")  # 原会话已整理到 seq 4（前两轮）
        finally:
            memory.close()

        new_sid, _new_path = fork_session(self.settings, "src", seqs[2])  # 复制 seq 1..9
        memory = open_memory(self.settings)
        try:
            cursors = memory.cursor_map()
        finally:
            memory.close()
        self.assertEqual(cursors.get(new_sid), 4, "已整理的部分照原样继承")
        self.assertEqual(cursors.get("src"), 4, "原会话游标不动")

    def test_fork_caps_the_cursor_at_the_copied_tail(self) -> None:
        from harness.memory import open_memory

        seqs = _build_session(self.settings, turns=3)
        memory = open_memory(self.settings)
        try:
            memory.advance_cursor("src", 999, "run-1")  # 原会话整理得比复制范围更靠后
        finally:
            memory.close()

        new_sid, _new_path = fork_session(self.settings, "src", seqs[2])  # 复制 seq 1..9
        memory = open_memory(self.settings)
        try:
            cursors = memory.cursor_map()
        finally:
            memory.close()
        self.assertEqual(cursors.get(new_sid), 9, "封顶到新会话最后一条事件的 seq")

    def test_fork_with_no_original_cursor_writes_nothing(self) -> None:
        from harness.memory import open_memory

        seqs = _build_session(self.settings, turns=2)
        new_sid, _new_path = fork_session(self.settings, "src", seqs[1])
        memory = open_memory(self.settings)
        try:
            cursors = memory.cursor_map()
        finally:
            memory.close()
        self.assertNotIn(new_sid, cursors, "没有可继承的进度就不写游标行")

    def test_fork_rewrites_attachment_paths_in_the_copied_log(self) -> None:
        src_file = self.settings.data_dir / "attachments" / "src" / "aaaa111122223333.png"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_bytes(b"fake-image-bytes")

        log = EventLog(self.settings.sessions_dir / "src.jsonl", "src")
        log.append(
            "user_message",
            text="看图",
            attachments=[{
                "id": "aaaa111122223333",
                "file": "attachments/src/aaaa111122223333.png",
                "name": "图.png",
                "mime": "image/png",
                "width": 10,
                "height": 10,
                "stored_width": 10,
                "stored_height": 10,
                "bytes": 16,
                "stored_bytes": 16,
            }],
        )
        log.append(
            "assistant_message",
            step=1,
            finish_reason="stop",
            usage={"total_tokens": 10},
            message=Message(role="assistant", content="看到了").to_record(),
        )
        log.append("turn_end", steps=1, stop_reason="completed", elapsed=1.0, usage={})
        second = log.append("user_message", text="第二个问题")
        log.close()

        new_sid, new_path = fork_session(self.settings, "src", int(second["seq"]))
        user = [r for r in read_events(new_path) if r.get("kind") == "user_message"][0]
        self.assertEqual(
            user["attachments"][0]["file"], f"attachments/{new_sid}/aaaa111122223333.png"
        )
        self.assertTrue(
            (self.settings.data_dir / "attachments" / new_sid / "aaaa111122223333.png").is_file()
        )


class SessionViewForkFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_user_messages_carry_seq(self) -> None:
        seqs = _build_session(self.settings, turns=3)
        view = build_session_view(self.settings, "src")
        users = [m for m in view["messages"] if m["role"] == "user"]
        self.assertEqual([m["seq"] for m in users], seqs)


if __name__ == "__main__":
    unittest.main()
