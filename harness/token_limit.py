"""token 数写法（`5000` / `5k` / `1M`）的唯一解析器。

**全项目只有这一个 Python 实现**：读 `providers.json` 的按模型上下文上限、
`/api/providers/save` 的 `model_limits`、以及「设置 → 常规」的单轮最大输出
（ADR-101）都走它，规则才不会分家。前端 `web/app.js` 的 `parseTokenLimit()`
是同一条规则的另一份实现（它负责"别发一个后端会静默丢掉的请求"），
两边靠同一批用例钉住。

规则（ADR-093 / ADR-101）：

* `k` / `K` = **1000**、`m` / `M` = **1000000** —— 不是 1024。各家标称的
  200k / 1M 都是十进制；把 k 当 1024 会让 200k 变成 204800，看起来只差 2%，
  但那是个没人核对过的数字。
* 只接受"数字 + 一个可选单位"这一种形式。`1.5M`、`1kk`、`1k5`、`1e6`、`1kb`
  一律看不懂。
* 返回 `None` 而不是抛异常：调用方本来就要把"看不懂"当成"没写"，
  但它们**各自决定要不要说出来**（设置界面会拦住）。
* 正值是普通上限。`-1` 是**只有显式允许时**才有的哨兵：表示"不限"
  （ADR-101 的单轮最大输出）。上下文上限那条路径不传 `allow_unlimited`，
  所以 `-1` 在那里仍然看不懂 —— 窗口没有"不限"这回事。
"""

from __future__ import annotations

import re
from typing import Any

#: `-1` = 不限（只对显式允许的调用方有效，见模块头）。
UNLIMITED = -1

_TOKEN_UNIT = {"k": 1_000, "m": 1_000_000}
# 允许一个前导 `-`：只有 `-1` 会被 `allow_unlimited` 接受，别的负值仍然 None。
_TOKEN_PATTERN = re.compile(r"^(-?)([0-9]+)([kKmM]?)$")


def parse_token_limit(raw: Any, *, allow_unlimited: bool = False) -> int | None:
    """把 `"1000000"` / `"1M"` / `"200k"` 解析成正整数；看不懂就返回 `None`。

    `allow_unlimited=True` 时额外接受 `-1`（返回 `-1`）。`0` 与其它负数
    一律 `None` —— "最多输出 0 个 token"不是一个有意义的上限，而"不限"
    已经有 `-1` 这个明确的写法。
    """
    if isinstance(raw, bool):  # bool 是 int 的子类：别把 True 当成 1
        return None
    if isinstance(raw, int):
        value = raw
    else:
        match = _TOKEN_PATTERN.match(str(raw if raw is not None else "").strip())
        if match is None:
            return None
        value = int(match.group(2)) * _TOKEN_UNIT.get(match.group(3).lower(), 1)
        if match.group(1) == "-":
            value = -value
    if allow_unlimited and value == UNLIMITED:
        return UNLIMITED
    return value if value > 0 else None
