"""time 工具：让模型知道当前的日期与时间。

设计边界：
  * 这是**只读**工具，不改变任何状态（记忆、档案、会话都不碰）；
  * 时区是**运行期可改**的设置（存 `data/settings.json`，默认 UTC+8，即偏移 +8 小时）。
    `runtime.py` 在装配时读进来传给 `TimeTool(offset_hours=…)` —— 构造参数就是那个缝。

为什么不放进 system 提示里解决：提示词末尾已经带了一条分钟精度的时间
（`context.build_tail_message`），但那条是**轮首快照** —— 一轮里多步工具调用、
联网抓取、本地 OCR 都会耗掉几十秒到几分钟，模型在轮末想知道"现在到底几点"时，
靠那行快照是不准的。这个工具给的是**调用那一刻**的秒级当前时间。

刻意用 `datetime.timezone(timedelta(hours=offset))` 而不是 `zoneinfo.ZoneInfo`：
Windows 上没有 tzdata 时 `ZoneInfo("Asia/Shanghai")` 会直接抛错，而固定偏移
不需要任何额外数据。等以后要支持 IANA 名字时再升级到 zoneinfo。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..i18n import t
from .registry import ToolResult

# 时区的固定偏移（小时）。UTC+8 = 8。这是"以后做成可改动"的缝。
_DEFAULT_OFFSET_HOURS = 8.0


def _offset_string(now: datetime) -> str:
    """`+0800` 这种 strftime 形态 → `+08:00`（给人看 / 放进 ISO 都更稳）。"""
    delta = now.utcoffset() or timedelta(0)
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _timezone_label(now: datetime) -> str:
    """时区的短标签：`+08:00` → `UTC+8`，`-05:30` → `UTC-5:30`。"""
    delta = now.utcoffset() or timedelta(0)
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if minutes == 0:
        return f"UTC{sign}{hours}"
    return f"UTC{sign}{hours}:{minutes:02d}"


class TimeTool:
    name = "time"

    def __init__(self, offset_hours: float = _DEFAULT_OFFSET_HOURS) -> None:
        self._tz = timezone(timedelta(hours=offset_hours))
        self.description = t("tool.time.desc")
        # 无参数：模型只要说"用 time"就能拿到时间，没有可填错的东西。
        self.parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, args: dict[str, Any]) -> ToolResult:
        now = datetime.now(self._tz)
        offset = _offset_string(now)
        # 字段名与取值是**契约**（JSON 里不翻），所以全用英文键名与机器可读值；
        # 唯一给人看的那句（CLI 简报）走 i18n。
        payload: dict[str, Any] = {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "iso": now.isoformat(timespec="seconds"),
            "timezone": _timezone_label(now),
            "offset": offset,
            "weekday": now.strftime("%A"),
            "unix": int(now.timestamp()),
        }
        return ToolResult(
            ok=True,
            payload=payload,
            display=t("tool.time.display", now=payload["now"], offset=offset),
        )
