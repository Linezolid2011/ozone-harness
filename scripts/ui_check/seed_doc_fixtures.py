"""给"重拍文档截图"准备一份**夹具数据目录**（不碰真实 data/）。

为什么要它：`docs/screenshots/` 里那批整窗截图（对话 / 记忆 / Dream / 用量 / 设置…）
要有内容才看得出功能 —— 空数据目录拍出来全是空状态。这些图以前是手工拍的，
产品改名之后它们就过时了（截图会腐烂，ADR-073），所以给它们一条"一条命令重来"的路。

它只做三件事，全都写在**夹具目录**里：

1. `providers.json`：一个假 key 的接口（否则图里会出现"还没有可用的接口"）；
2. `memory.db`：几条**明写"截图夹具"**的记忆条目（记忆页要有东西可看）；
3. 一条会话 JSONL：几轮对话 + 用量 + 一条压缩摘要（对话页 / 用量页 / compact 那块）。

两次安全闸（同 `shot.mjs`）：路径必须像一次性夹具目录（含 `tmp`），
而且拒绝指向项目真实的 `data/`。

用法（在项目根；路径一律用正斜杠，回到 cmd 里再换成反斜杠也行）：

    powershell -File scripts/ui_check/seed-vault.ps1 -DataDir .tmp-ui-check/data
    python scripts/ui_check/seed_doc_fixtures.py .tmp-ui-check/data
    set HARNESS_DATA_DIR=.tmp-ui-check/data
    python -m harness.cli --web --port 8793 --no-open
    :: 然后按 scripts/ui_check/README.md 里那条 chrome 命令逐页拍
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.config import Settings, parse_env_file  # noqa: E402
from harness.events import EventLog  # noqa: E402
from harness.memory import open_memory  # noqa: E402

SESSION_ID = "doc-shots-0001"
FAKE_KEY = "sk-doc-fixture-not-a-real-key"

PROVIDER = {
    "id": "deepseek",
    "label": "DeepSeek",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": FAKE_KEY,
    "models": ["deepseek-flash", "deepseek-v4-pro"],
    "thinking_options": ["low", "high", "max"],
    "send_reasoning_content": True,
    "supports_tools": True,
    "notes": "（截图夹具：这里的 key 是假的，只为了让图里不出现“还没有可用的接口”）",
    "type": "openai",
    "model_limits": {"deepseek-v4-pro": 1_000_000},
    "model_labels": {"deepseek-flash": "DeepSeek V4.1 Flash"},
    "vision_models": ["deepseek-flash"],
}

MEMORIES = [
    "用户在做一个小小的本地对话项目，第一阶段刚收尾",
    "用户喜欢手冲咖啡，浅烘为主，习惯早上七点起来磨豆子",
    "用户是中学生，预算有限，希望每轮对话的花费能看得见",
]

TURNS = [
    (
        "我最近开始学手冲咖啡，浅烘和中烘哪个更适合早上喝？",
        "早上我更建议**中烘**：酸度低、body 更厚，空着肚子喝也不容易觉得刺激。\n\n"
        "浅烘的风味更亮（花香、柑橘），但它更适合下午或者饭后 —— 早上喝容易觉得“太冲”。\n"
        "如果你已经习惯浅烘，可以从水温和粉水比调：90℃ 左右、1:15，别再拉长了。",
        812,
        96,
    ),
    (
        "那磨豆机呢？我现在用的是买豆子送的赠品。",
        "赠品磨豆机最大的问题是**粒径不均**：细粉被过度萃取（苦），粗颗粒又没出味（酸）。\n"
        "换一台手摇（比如 C40 那一档）比换豆子更能立刻喝出差别，而且噪声小、不占地方。\n"
        "预算有限的话，先别买电磨 —— 同样的钱花在磨芯上更值。",
        934,
        118,
    ),
]

SUMMARY = (
    "（截图夹具）前面聊过：用户刚开始学手冲咖啡，习惯早上七点起来磨豆子；"
    "他问过早上的烘焙度选择（结论：中烘更稳），现在在问磨豆机要不要换。"
)


def main(argv: list[str]) -> int:
    target = Path(argv[1] if len(argv) > 1 else ".tmp-ui-check/data")
    data_dir = (ROOT / target).resolve() if not target.is_absolute() else target.resolve()
    shown = str(data_dir)
    # 两道闸：像一次性夹具目录，而且不是项目真实的 data/
    if "tmp" not in shown.lower().replace("\\", "/"):
        print(f"拒绝运行：{shown} 不像一次性夹具目录（路径里要有 tmp）。")
        return 2
    if data_dir == (ROOT / "data").resolve():
        print("拒绝运行：那是项目真实的 data/。")
        return 2

    data_dir.mkdir(parents=True, exist_ok=True)
    env = parse_env_file(ROOT / ".env")
    settings = Settings.load()
    # 用夹具目录当作数据目录（Settings 是不可变的 dataclass，直接换一个）
    from dataclasses import replace

    settings = replace(settings, data_dir=data_dir)

    providers_path = data_dir / "providers.json"
    if providers_path.exists():
        print("  · providers.json 已存在 —— 不动它")
    else:
        providers_path.write_text(
            json.dumps(
                {"providers": [PROVIDER], "default": {"provider": "deepseek", "model": "deepseek-flash", "effort": ""}},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print("  已写 providers.json（假 key）")

    memory = open_memory(settings)
    try:
        existing = {item.content for item in memory.active_items()}
        added = 0
        for content in MEMORIES:
            if content in existing:
                continue
            memory.create(content)
            added += 1
        print(f"  记忆：新增 {added} 条（共 {len(memory.active_items())} 条）")
    finally:
        memory.close()

    session_path = settings.sessions_dir / f"{SESSION_ID}.jsonl"
    if session_path.exists():
        print(f"  · {SESSION_ID} 已存在 —— 不动它")
        return 0

    log = EventLog(session_path, SESSION_ID, data_dir=data_dir)
    try:
        log.append("session_start", model="deepseek-flash", reasoning_effort="", tools=[])
        for question, answer, prompt, completion in TURNS:
            log.append("user_message", text=question)
            turn_usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "reasoning_tokens": 0,
                "cached_tokens": prompt - 172,
                "cache_miss_tokens": 172,
            }
            log.append(
                "assistant_message",
                finish_reason="stop",
                usage=turn_usage,
                message={"content": answer},
            )
            # 统计条的轮数与平均速度以 turn_end 为准；缺了它，夹具虽然有对话，
            # 截图却会错误地显示 0 轮 / 0 tok/s。
            log.append(
                "turn_end",
                steps=1,
                stop_reason="completed",
                elapsed=2.0,
                usage=turn_usage,
            )
            # 一条压缩摘要（对话页顶部那块「更早的对话已压缩」）。
            # covered_groups=1：只把**第一组**算作已压缩 —— 全部压掉的话，
            # 对话页就只剩那一行摘要了（第一版就这么拍到一张空对话页）。
            log.append(
                "session_summary",
                summary=SUMMARY,
                covered_groups=1,
                covered_messages=2,
                folded_groups=1,
                folded_messages=2,
                budget=24000,
                model="deepseek-flash",
            )
    finally:
        log.close()
    print(f"  会话夹具：{SESSION_ID}（{len(TURNS)} 轮 + 一条压缩摘要）")
    print("数据目录：", data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
