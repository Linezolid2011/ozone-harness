"""按步统计 prompt 缓存命中，用来验证"记忆注入位置"这类设计是否真的省钱。

prompt cache 按 **token 前缀**命中，所以"命中率"直接反映请求之间共享了多少前缀。
任何改动只要动了消息顺序或易变内容的插入位置，都应该用这个报告确认没有退化。

用法：
    python -X utf8 scripts/cache_report.py <session-id>
    python -X utf8 scripts/cache_report.py            # 汇总所有会话
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.config import Settings  # noqa: E402
from harness.events import read_events  # noqa: E402


def report(path: Path) -> tuple[int, int, int]:
    events = read_events(path)
    if not events:
        return (0, 0, 0)

    print(f"\n=== {path.stem} ===")
    header = f"{'轮':>2} {'步':>2} {'prompt':>7} {'命中':>7} {'未命中':>7} {'命中率':>7}  记忆"
    print(header)
    print("-" * len(header))

    turn = 0
    total_prompt = total_cached = total_tokens = 0
    memory_by_step: dict[int, str] = {}
    turn_usage: dict[int, int] = {}

    for record in events:
        kind = record.get("kind")
        if kind == "user_message":
            turn += 1
        elif kind == "prompt_built":
            # 记忆是**全量注入**的（ADR-058），所以这里能看的是"注入了多少条、
            # 有没有因为长度被截掉" —— 以前那套检索决策（recall_mode/recall_summary）
            # 已经随检索机制一起删掉了。
            count = record.get("memory_count")
            limit = record.get("memory_limit")
            memory_by_step[int(record.get("step") or 0)] = (
                f"{count}/{limit} 条" if count is not None else "-"
            )
        elif kind == "assistant_message":
            usage = record.get("usage") or {}
            prompt = int(usage.get("prompt_tokens") or 0)
            cached = int(usage.get("cached_tokens") or 0)
            step = int(record.get("step") or 0)
            ratio = (cached / prompt) if prompt else 0.0
            summary = memory_by_step.get(step, "-")
            if len(summary) > 46:
                summary = summary[:46] + "…"
            print(
                f"{turn:>2} {step:>2} {prompt:>7} {cached:>7} {prompt - cached:>7} "
                f"{ratio:>6.0%}  {summary}"
            )
            total_prompt += prompt
            total_cached += cached
            total_tokens += int(usage.get("total_tokens") or 0)
            turn_usage[turn] = turn_usage.get(turn, 0) + int(usage.get("total_tokens") or 0)

    ratio = (total_cached / total_prompt) if total_prompt else 0.0
    print("-" * len(header))
    print(
        f"合计 prompt {total_prompt}  token 总量 {total_tokens}  "
        f"缓存命中 {total_cached}（{ratio:.0%}）"
    )
    if turn_usage:
        print("每轮 token 总量：" + "  ".join(f"轮{k}={v}" for k, v in sorted(turn_usage.items())))
    return (total_prompt, total_cached, total_tokens)


def main() -> int:
    settings = Settings.load()
    if len(sys.argv) > 1:
        paths = [settings.sessions_dir / f"{sys.argv[1]}.jsonl"]
    else:
        paths = sorted(settings.sessions_dir.glob("*.jsonl"))
    grand_prompt = grand_cached = grand_total = 0
    for path in paths:
        if not path.is_file():
            print(f"找不到会话：{path}")
            return 1
        prompt, cached, total = report(path)
        grand_prompt += prompt
        grand_cached += cached
        grand_total += total
    if len(paths) > 1 and grand_prompt:
        print(
            f"\n全部会话：prompt {grand_prompt}  总量 {grand_total}  "
            f"缓存命中 {grand_cached}（{grand_cached / grand_prompt:.0%}）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
