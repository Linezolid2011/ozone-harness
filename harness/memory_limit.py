"""记忆条数上限：一个**可以在设置里改**的运行期配置（ADR-057）。

## 为什么它不在 `.env` 里

上限是用户会**经常改**的东西（"让它多记一点"／"50 条太多了"），
而 `.env` 是"装一次就不动"的启动配置（ADR-036 之后连 key 都不在里面）。
所以它和接口档案、联网工具设置一样存在 `data/` 下的 JSON 里：

* **改了立刻生效** —— 每次构造 `MemoryStore` 都重新读一次；
* **恢复出厂设置会连它一起抹掉**，回到默认的 50；
* 它**不是**"另一种真相"：`data/memory.json` 在就听它的，不在就用种子值。

## 三条边界，各自的错误故事

1. 只能是 **0–100 的整数**。Kimi 的 memory instructions 上线就是 50 条，
   我们允许在这个量级上小幅调整，但**不允许调到几百条**：记忆是全量注入的，
   条数上限几乎就等于"每一轮固定要花多少 token"；
2. **不能低于当前已有条数**。那等于"瞬间让若干条记忆消失"—— 它们没有被删除，
   也不再进上下文，是最糟的一种中间状态。这种情况报"上限太低"，
   并且把"现在有几条"一起说出来；
3. 满了以后再写 → `mem_create` 直接失败（见 `MemoryStore.create`），
   而不是悄悄丢掉最旧的那条。**满要满得响**：模型看到失败才知道该合并或删除。

## 读操作不创建文件

和 `reset.preview_reset()`、`VaultStore` 一样：界面上的"看一眼"不能改磁盘。
`data/memory.json` 只在**真正保存**时才写出来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic_io import write_text_atomic
from .i18n import t

MEMORY_FILE = "memory.json"

DEFAULT_LIMIT = 50
MIN_LIMIT = 0
MAX_LIMIT = 100


@dataclass(frozen=True)
class LimitChange:
    """一次"改上限"的结果。失败时 `error` 是给人看的一句话。"""

    ok: bool
    limit: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": self.ok, "limit": self.limit}
        if self.error:
            data["error"] = self.error
        return data


def validate_limit(
    value: Any,
    *,
    active_count: int,
    low: int = MIN_LIMIT,
    high: int = MAX_LIMIT,
) -> LimitChange:
    """能不能把这个值设成新上限？不行就说清楚为什么。**纯函数**。

    `active_count` 由调用方给（库里现在有多少条）—— 于是这个函数
    不需要碰数据库，可以被离线逐条测（含各种坏输入）。

    失败时那句话是**现取**的（`t()`，`provider.memory.*`）：它显示在设置页、
    也会作为 400 原话回到界面。**校验逻辑本身一个字都没动** ——
    只有"怎么说"换了地方。
    """
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        return LimitChange(
            False, error=t("provider.memory.limit_not_integer", value=value)
        )
    if isinstance(value, float) and not value.is_integer():
        # **不静默截断**：`int(3.7)` 会变成 3，用户设了 3.7 却得到 3 还不报错 ——
        # 这正是这个项目一直在清的那种"看起来成功了"的失败。
        # 整数值的浮点（JSON 里写成 `50.0`）仍然接受。
        return LimitChange(
            False, error=t("provider.memory.limit_not_whole_number", value=value)
        )
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return LimitChange(
            False, error=t("provider.memory.limit_required", value=value)
        )
    if wanted < low or wanted > high:
        return LimitChange(
            False,
            error=t("provider.memory.limit_out_of_range", low=low, high=high, wanted=wanted),
        )
    if wanted < active_count:
        return LimitChange(
            False,
            error=t(
                "provider.memory.limit_too_low",
                active_count=active_count,
                wanted=wanted,
            ),
        )
    return LimitChange(True, limit=wanted)


class MemoryLimitStore:
    """`data/memory.json` 的读写。文件不存在时用种子值，**不主动创建文件**。"""

    def __init__(self, path: Path, *, default: int = DEFAULT_LIMIT) -> None:
        self.path = Path(path)
        self._default = max(MIN_LIMIT, min(MAX_LIMIT, int(default)))
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            # `utf-8-sig`：容忍 BOM（这文件是给人手改的；记事本旧版本默认写 BOM，
            # 而带 BOM 的 JSON 用普通 utf-8 读会静默退回默认值）。
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def limit(self) -> int:
        """当前生效的上限（**只读**）。

        夹到范围内而不是报错：这里读的是磁盘上的文件，坏值只可能是被手工改坏的。
        那种情况下"退回一个合法值"比"整个程序起不来"好。
        真正需要严格的地方是**设置界面**那条路（`set()`）。
        """
        raw = self._data.get("max_items", self._default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return self._default
        return max(MIN_LIMIT, min(MAX_LIMIT, value))

    def set(self, value: Any, *, active_count: int) -> LimitChange:
        change = validate_limit(value, active_count=active_count)
        if not change.ok:
            return change
        self._data["max_items"] = change.limit
        self.save()
        return change

    def save(self) -> None:
        # 原子写：半截文件读不出来时，读端会把它当成"没有配置"（见 atomic_io 的说明）。
        write_text_atomic(
            self.path, json.dumps(self._data, ensure_ascii=False, indent=2)
        )

    def public_state(self, *, active_count: int) -> dict[str, Any]:
        """给前端的一份：值、范围、以及"最低能设到多少"。"""
        return {
            "max_items": self.limit(),
            "active_count": int(active_count),
            "min_allowed": MIN_LIMIT,
            "max_allowed": MAX_LIMIT,
            # 现在最少能设到多少：不能低于已有条数（那条规则要在界面上看得见，
            # 否则用户只会觉得"这个框不让我改"）
            "lowest_now": max(MIN_LIMIT, min(MAX_LIMIT, int(active_count))),
        }


def limit_for(data_dir: Path, default: int = DEFAULT_LIMIT) -> int:
    """当前生效的条数上限。

    **每个构造 `MemoryStore` 的地方都必须经过这里** —— 上限是运行期可改的，
    散落各处自己拼参数，迟早会有一个地方忘了读新值，
    那正是"设置改了却不生效"这类 bug 的来源。
    """
    return MemoryLimitStore(Path(data_dir) / MEMORY_FILE, default=default).limit()
