"""给召回测试灌入一批演示记忆。

**不会碰你的真实记忆库**：默认写到 data-demo/，也可以显式指定目录。
记忆库的查重逻辑会让这个脚本可以反复运行。

用法：
    python -X utf8 scripts/seed_demo_memories.py
    python -X utf8 scripts/seed_demo_memories.py --data-dir data-m2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.memory import MemoryStore  # noqa: E402

# 45 条，故意超过默认的 HARNESS_FULL_INJECTION_LIMIT=30，
# 这样才会走"核心档案 + 检索"分支。
MEMORIES: list[tuple[str, str]] = [
    ("用户叫小林", "身份"),
    ("小林住在上海", "身份"),
    ("小林的生日是 3 月 14 日", "身份"),
    ("小林在互联网公司上班", "身份"),
    ("小林对花生过敏", "身份"),
    ("小林养了一只叫豆豆的猫", "关系"),
    ("小林的同事叫老王", "关系"),
    ("小林的团队有五个人", "关系"),
    ("用户偏好用中文交流", "约定"),
    ("小林每周三晚上和同学开项目会", "约定"),
    ("小林喜欢喝美式咖啡", "偏好"),
    ("小林更喜欢拿铁", "偏好"),
    ("小林喜欢喝手冲咖啡", "偏好"),
    ("小林通勤靠地铁 10 号线", "偏好"),
    ("小林周末喜欢去爬山", "偏好"),
    ("小林不喜欢开会", "偏好"),
    ("小林喜欢看科幻电影", "偏好"),
    ("小林最爱的电影是银翼杀手", "偏好"),
    ("小林每天早上七点起床", "偏好"),
    ("小林睡前会读一会儿书", "偏好"),
    ("小林会弹吉他", "偏好"),
    ("小林想学冲浪", "偏好"),
    ("小林讨厌堵车", "偏好"),
    ("小林最近在减肥", "偏好"),
    ("小林每周健身三次", "偏好"),
    ("小林喜欢用深色主题", "偏好"),
    ("小林喝咖啡不加糖", "偏好"),
    ("小林讨厌打电话，喜欢文字沟通", "偏好"),
    ("小林周末习惯睡到自然醒", "偏好"),
    ("小林喜欢川菜，不太能吃辣", "偏好"),
    ("小林正在和几个同学做一个 O₃ᶻᵒⁿᵉ 项目", "项目"),
    ("小林用 Python 写代码", "项目"),
    ("小林计划明年去日本旅行", "项目"),
    ("小林在公司负责数据平台", "项目"),
    ("小林学过三年日语", "项目"),
    ("小林的电脑是 Windows 系统", "其他"),
    ("小林的手机是安卓", "其他"),
    ("小林最近在读一本讲分布式系统的书", "其他"),
    ("小林讨厌在周末加班", "其他"),
    ("小林喜欢在早上处理最难的活", "其他"),
    ("小林希望每月能存下三成收入", "其他"),
    ("小林的显示器是 27 寸 4K", "其他"),
    ("小林习惯用快捷键而不是鼠标", "其他"),
    ("小林有个叫阿哲的发小在做游戏", "关系"),
    ("小林去年换过一次工作", "其他"),
]


def filler_memories(count: int) -> list[tuple[str, str]]:
    """生成一批规模填充记忆。

    目的是把记忆库推到接近上限（默认 200），因为 M2 的召回只有在记忆量大的时候
    才体现出价值；45 条时全量注入本来也不贵。
    内容刻意与演示查询（咖啡 / 周末 / 工作 / 猫）没有字面重叠，
    这样"检索有没有挑对"才是一个干净的检验。
    """
    parts = ["登录", "支付", "报表", "搜索", "推荐", "风控", "结算", "消息"]
    topics = ["分布式共识", "缓存穿透", "索引优化", "灰度发布", "压测方法", "日志治理"]
    places = ["成都", "西安", "青岛", "厦门", "长沙", "昆明", "大连", "苏州"]
    tools = ["编辑器插件", "终端工具", "截图工具", "笔记软件", "密码管理器"]
    templates = (
        lambda n: (f"小林在 {n} 号项目里负责过{parts[n % len(parts)]}模块", "项目"),
        lambda n: (f"小林收藏了第 {n} 篇讲{topics[n % len(topics)]}的文章", "其他"),
        lambda n: (f"小林去过{places[n % len(places)]}，那次出差是 {n % 12 + 1} 月", "其他"),
        lambda n: (f"小林以前用过第 {n} 款{tools[n % len(tools)]}", "其他"),
    )
    generated: list[tuple[str, str]] = []
    for index in range(1, count + 1):
        generated.append(templates[index % len(templates)](index))
    return generated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data-demo", help="写到哪个数据目录")
    parser.add_argument("--max-items", type=int, default=200)
    parser.add_argument("--max-chars", type=int, default=400)
    parser.add_argument(
        "--filler", type=int, default=0, help="额外生成多少条规模填充记忆（用于压到 200 条）"
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    store = MemoryStore(data_dir / "memory.db", max_items=args.max_items, max_chars=args.max_chars)
    try:
        created = skipped = 0
        for content, category in MEMORIES + filler_memories(args.filler):
            result = store.create(content, category)
            if result.get("ok"):
                created += 1
            else:
                skipped += 1
        stats = store.stats()
        print(f"数据目录：{data_dir}")
        print(f"本次新建 {created} 条，跳过 {skipped} 条（重复或超限）")
        print(f"当前有效记忆 {stats['active']} 条")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
