"""受控实验：reasoning_effort 参数到底有没有被后端真正采纳？

背景：我们发现这个 API 对不认识的参数会**静默忽略**（连 anthropic 风格的
thinking 参数都不报错），所以"请求没报错"完全不能证明参数生效。
唯一可靠的判据是：同一道题、同一 prompt，不同档位下的 reasoning_tokens 是否系统性不同。

用法：python -X utf8 scripts/probe_effort.py
"""

from __future__ import annotations

import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.config import Settings  # noqa: E402
from harness.llm_types import Message, StreamAssembler  # noqa: E402
from harness.providers.openai_compat import OpenAICompatProvider  # noqa: E402

# 刻意选一道**难**题：effort 控制的是"思考预算上限"，而不是实际思考量。
# 简单题（例如鸡兔同笼）模型自然会早早停止思考，无论上限多高都看不出差别 ——
# 第一版实验就是这样得出错误结论的。必须用难题才能把上限压出来。
PROMPT = (
    "从 1 到 1000 中，有多少个整数的十进制表示恰好含有两个数字 7？"
    "只输出一个数字，不要任何解释。"
)
REPEATS = 3
# 官方文档：OpenAI 格式用 reasoning_effort，合法值 low / high / max，
# 思考模式默认开启且默认档位就是 high。
# 参考 https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
SETTINGS_TO_TEST: list[tuple[str, str | None]] = [
    ("low", "low"),
    ("high", "high"),
    ("max", "max"),
]


def measure(settings: Settings) -> dict[str, object]:
    provider = OpenAICompatProvider(settings, label="DeepSeek", send_reasoning_content=True)
    assembler = StreamAssembler()
    started = time.monotonic()
    for event in provider.stream([Message(role="user", content=PROMPT)], [], max_tokens=3000):
        assembler.feed(event)
    elapsed = time.monotonic() - started
    usage = assembler.usage
    return {
        "reasoning_tokens": usage.reasoning_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "reasoning_chars": len(assembler.reasoning),
        "elapsed": elapsed,
        "answer": assembler.text.strip().replace("\n", " ")[:40],
    }


def main() -> int:
    base = Settings.load()
    print(f"模型：{base.model}    题目：1~1000 中恰好含两个 7 的个数    重复次数：{REPEATS}\n")
    header = f"{'档位':<12} {'#':>2} {'reasoning_tok':>14} {'completion_tok':>15} {'思考字数':>9} {'耗时s':>7}  回答"
    print(header)
    print("-" * len(header))

    summary: dict[str, list[int]] = {}
    for label, effort in SETTINGS_TO_TEST:
        settings = replace(base, reasoning_effort=effort, max_output_tokens=3000)
        for index in range(1, REPEATS + 1):
            try:
                result = measure(settings)
            except Exception as exc:  # noqa: BLE001 - 探针要如实报告失败
                print(f"{label:<12} {index:>2}  请求失败：{exc}")
                continue
            summary.setdefault(label, []).append(int(result["reasoning_tokens"]))
            print(
                f"{label:<12} {index:>2} {result['reasoning_tokens']:>14} "
                f"{result['completion_tokens']:>15} {result['reasoning_chars']:>9} "
                f"{result['elapsed']:>7.1f}  {result['answer']}"
            )
        print()

    print("汇总（reasoning_tokens 中位数）：")
    for label, values in summary.items():
        if values:
            print(f"  {label:<12} 中位数 {statistics.median(values):>7.1f}   样本 {values}")
    if len(summary) >= 2:
        print(
            "\n判读：如果各档位中位数差异很小且样本区间互相重叠，"
            "说明该参数很可能被后端忽略，'设置成 low 省钱' 只是心理安慰。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
