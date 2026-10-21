"""联网工具的配置（搜索后端、key、抓取限制）。

存在 `data/tools.json` —— 和接口档案同一个套路：**key 只存在 `data/` 里**，
所以"恢复出厂设置"能连它一起抹掉（ADR-036 建立的规矩）。

没有任何后端是"预置需要 key"的：默认的 Bing 不需要 key，
Tavily 需要 key 但只是**可选升级**（它的搜索质量更适合喂给模型）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..atomic_io import write_text_atomic
from ..i18n import t

TOOLS_FILE = "tools.json"

# 抓到的正文最终要经 ToolRegistry 渲染成工具结果才进模型上下文，
# 那里还有一道上限（HARNESS_MAX_TOOL_RESULT_CHARS，见 harness/config.py，默认 2000）。
# **两个上限取小值才是真正生效的那个** —— 只把 max_fetch_chars 调到 8000
# 而 registry 只放 2000，用户看到的就是"设置不起作用、界面与事实不符"。
# 这里读同一个环境变量（而不是抄一个 2000 的常量）是为了让只拿到
# WebToolsConfig 的地方（runtime.py 就是这么构造抓取工具的）也能算出有效上限。
REGISTRY_MAX_CHARS_ENV = "HARNESS_MAX_TOOL_RESULT_CHARS"
REGISTRY_MAX_CHARS_DEFAULT = 2000


def registry_max_result_chars() -> int:
    """registry 单条工具结果的上限（字符）。环境变量缺失或非法时用默认值。"""
    try:
        value = int(os.environ.get(REGISTRY_MAX_CHARS_ENV, ""))
    except (TypeError, ValueError):
        return REGISTRY_MAX_CHARS_DEFAULT
    return value if value > 0 else REGISTRY_MAX_CHARS_DEFAULT


# 可选后端。`bing` 不需要 key；`tavily` 需要。
# 这里只列 **id**：label 与 notes 是界面文案（设置 → 工具 里那一排），由
# `backend_label()` / `backend_notes()` 在**用到它的那一刻**从 catalog 取 ——
# 语言是运行期可切的，写死在模块常量里就等于在 import 那一刻把语言钉死了。
SEARCH_BACKENDS: tuple[str, ...] = ("bing", "tavily", "off")


def _text_or(fallback: str, key: str) -> str:
    """取一条文案；catalog 里没有就退回 `fallback`。

    为什么这里要**吞掉 KeyError**（与别处"缺翻译要响"相反）：
    这些 key 里带着一个**来自配置的 id**（用户在 `data/tools.json` 里手改的，
    或者旧版本留下来的后端名）。`t()` 对缺 key 是抛错的，于是设置页会 500，
    而报错内容是"没有这条文案：net.config.backend.360.label" ——
    **症状指向完全错误的方向**（看起来像漏翻，真正的问题是"配置里有个不认识的后端"）。
    退回 id 本身的话，界面上直接显示那个名字，看得见、也改得动。
    """
    try:
        return t(key)
    except KeyError:
        return fallback


def backend_label(backend_id: str) -> str:
    """某个搜索后端在界面上的名字（**现取**当前语言；未知 id 退回 id 本身）。"""
    return _text_or(str(backend_id), f"net.config.backend.{backend_id}.label")


def backend_notes(backend_id: str) -> str:
    """某个搜索后端的一句话说明（**现取**当前语言；未知 id 没有说明）。"""
    return _text_or("", f"net.config.backend.{backend_id}.notes")


@dataclass
class WebToolsConfig:
    """联网工具的开关与参数。全部可在界面上改。"""

    enabled: bool = True
    search_backend: str = "bing"
    tavily_api_key: str = ""
    search_count: int = 5
    search_timeout: float = 20.0
    fetch_timeout: float = 20.0
    # 单次抓取返回给模型的正文上限（字符）。太小会把文章截断，太大会把上下文塞满。
    max_fetch_chars: int = 8000
    # 下载的字节上限：防止一个几 GB 的文件把内存吃光
    max_fetch_bytes: int = 2_000_000
    # SSRF 防护：默认禁止抓本机/内网地址（模型可能被骗去读 http://127.0.0.1:8787/...）
    allow_private_hosts: bool = False

    @property
    def search_available(self) -> bool:
        if not self.enabled or self.search_backend == "off":
            return False
        if self.search_backend == "tavily":
            return bool(self.tavily_api_key.strip())
        return True

    @property
    def effective_max_fetch_chars(self) -> int:
        """**真正生效**的正文上限：与 registry 的结果上限取小。

        `max_fetch_chars` 是"抓取阶段"的上限，而抓回来的正文还要过 registry
        那一道（结构化截断）。界面必须显示两者的较小值，否则用户把
        max_fetch_chars 从 2000 调到 8000、发现毫无变化，只会以为设置坏了。
        """
        return min(self.max_fetch_chars, registry_max_result_chars())

    def masked_tavily_key(self) -> str:
        key = self.tavily_api_key.strip()
        if not key:
            return ""
        mask = t("net.config.key_mask")
        if len(key) <= 10:
            return mask
        return f"{key[:6]}{mask}{key[-4:]}"

    def to_public(self) -> dict[str, Any]:
        """给前端的版本：**永不包含完整 key**。"""
        return {
            "enabled": self.enabled,
            "search_backend": self.search_backend,
            "search_available": self.search_available,
            "has_tavily_key": bool(self.tavily_api_key.strip()),
            "masked_tavily_key": self.masked_tavily_key(),
            "search_count": self.search_count,
            "search_timeout": self.search_timeout,
            "fetch_timeout": self.fetch_timeout,
            "max_fetch_chars": self.max_fetch_chars,
            # 界面上要显示**有效**上限，不是"设置里那个数"：
            # 超过 registry 上限的部分根本进不了模型上下文。
            "effective_max_fetch_chars": self.effective_max_fetch_chars,
            "max_fetch_bytes": self.max_fetch_bytes,
            "allow_private_hosts": self.allow_private_hosts,
            "backends": [
                {"id": backend_id, "label": backend_label(backend_id),
                 "notes": backend_notes(backend_id)}
                for backend_id in SEARCH_BACKENDS
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WebToolsConfig":
        def _int(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        def _float(key: str, default: float, low: float, high: float) -> float:
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        backend = str(raw.get("search_backend") or "bing").strip()
        if backend not in SEARCH_BACKENDS:
            backend = "bing"
        return cls(
            enabled=raw.get("enabled", True) is not False,
            search_backend=backend,
            tavily_api_key=str(raw.get("tavily_api_key") or ""),
            search_count=_int("search_count", 5, 1, 10),
            search_timeout=_float("search_timeout", 20.0, 3.0, 60.0),
            fetch_timeout=_float("fetch_timeout", 20.0, 3.0, 120.0),
            max_fetch_chars=_int("max_fetch_chars", 8000, 500, 100_000),
            max_fetch_bytes=_int("max_fetch_bytes", 2_000_000, 10_000, 20_000_000),
            allow_private_hosts=bool(raw.get("allow_private_hosts")),
        )


class ToolStore:
    """`data/tools.json` 的读写。文件不存在时用默认值，**不主动创建文件**。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            # `utf-8-sig`：容忍 BOM（手改的文件；带 BOM 时普通 utf-8 读会静默丢掉整份配置）
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def web(self) -> WebToolsConfig:
        return WebToolsConfig.from_dict(self._data.get("web") or {})

    def save_web(self, payload: dict[str, Any]) -> WebToolsConfig:
        """按前端传来的字段局部更新。**key 留空表示不改动**（前端拿不到完整 key）。"""
        current = self._data.get("web") or {}
        for field_name in (
            "enabled",
            "search_backend",
            "search_count",
            "search_timeout",
            "fetch_timeout",
            "max_fetch_chars",
            "max_fetch_bytes",
            "allow_private_hosts",
        ):
            if field_name in payload and payload[field_name] is not None:
                current[field_name] = payload[field_name]
        key = payload.get("tavily_api_key")
        if key is not None and str(key).strip():
            current["tavily_api_key"] = str(key).strip()
        elif payload.get("clear_tavily_key"):
            current["tavily_api_key"] = ""

        self._data["web"] = current
        self.save()
        return self.web()

    def save(self) -> None:
        # 原子写：半截文件读不出来时，读端会把它当成"没有配置"（见 atomic_io 的说明）。
        write_text_atomic(
            self.path, json.dumps(self._data, ensure_ascii=False, indent=2)
        )

    def public_state(self) -> dict[str, Any]:
        return {"web": self.web().to_public()}
