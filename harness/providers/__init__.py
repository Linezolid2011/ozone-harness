"""模型后端适配层。

上层只依赖 `LLMProvider` 抽象与 `ProviderError`；
具体实现是 `OpenAICompatProvider`（一个客户端覆盖绝大多数供应商）。
"""

from .base import LLMProvider, ProviderError
from .openai_compat import OpenAICompatProvider
from .store import (
    PRESETS,
    ProviderProfile,
    ProviderStore,
    fetch_models,
    get_preset,
    presets,
    resolve_settings,
)

__all__ = [
    "LLMProvider",
    "ProviderError",
    "OpenAICompatProvider",
    "ProviderProfile",
    "ProviderStore",
    "PRESETS",
    "presets",
    "get_preset",
    "fetch_models",
    "resolve_settings",
]
