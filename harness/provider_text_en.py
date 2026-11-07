"""Provider-layer and runtime-assembly text (English).

Exports only `LANG` and `MESSAGES`. Naming: `provider.<area>.<name>`,
`ui.runner.<name>` (runtime assembly errors). These show up under **Settings → Providers**
and come back to the interface verbatim inside 400 responses.

`provider.preset.*` is the preset label / notes shown in the “add provider” picker:
`harness/providers/store.py` reads them **when they are used**
(`get_preset()` / `presets()`), never at import time — the language can change at runtime.

`provider.memory.*` is the memory-count limit wording (`harness/memory_limit.py`); that
function is a pure validator shared by the CLI and the Web UI, so only the sentence moved.
"""

from __future__ import annotations

LANG = "en"

MESSAGES: dict[str, str] = {
    # ── runtime assembly (`harness/runtime.py`) ─────────────────────────
    "ui.runner.no_provider": (
        "No API profile is configured yet. Open the Web UI (run.cmd --web) → "
        "“Settings → Providers” in the lower left, pick a preset and fill in your own "
        "API key. This project does not preset any key."
    ),
    "ui.runner.no_model": (
        "The API profile “{label}” has no usable model yet. Go to Settings → Providers and "
        "click “Fetch model list”, or type the model name in manually."
    ),
    # display name of the fallback provider (it shows up inside “{label} returned HTTP …”)
    "ui.runner.default_label": "Default endpoint",
    # ── memory-count limit (`harness/memory_limit.py`) ──────────────────
    # `{value!r}` is deliberate: the bad value is shown verbatim so the user sees what
    # they typed.
    "provider.memory.limit_not_integer": "The limit must be an integer (got {value!r})",
    "provider.memory.limit_not_whole_number": (
        "The limit must be a whole number, not a decimal (got {value!r})"
    ),
    "provider.memory.limit_required": "The limit must be an integer (got {value!r})",
    "provider.memory.limit_out_of_range": (
        "The limit must be an integer between {low} and {high} (got {wanted})"
    ),
    "provider.memory.limit_too_low": (
        "The limit is too low: there are already {active_count} memory items, and the limit "
        "cannot be below that (got {wanted}). Delete some items first, or set the limit to "
        "{active_count} or more."
    ),
    # ── profile storage and model discovery (`harness/providers/store.py`) ──
    "provider.store.id_required": "The provider id cannot be empty",
    "provider.store.base_url_required": "base_url cannot be empty",
    "provider.store.base_url_scheme": "Write the full address, starting with http:// or https:// (for example https://api.deepseek.com/v1).",
    "provider.store.unknown": "No such provider: {provider_id}",
    "provider.store.models_http": "HTTP {status}: {detail}",
    "provider.store.models_unreachable": "Cannot reach {url}: {detail}",
    "provider.store.models_not_json": "{url} did not return JSON",
    "provider.store.models_empty": "That endpoint returned no model ids",
    "provider.store.key_mask": "...",
    # ── presets (the starting point for “add provider”) ─────────────────
    "provider.preset.kimi-code.label": "Kimi Code (subscription token plan)",
    "provider.preset.kimi-code.notes": (
        "Kimi Code member quota, China site, an **Anthropic-format** endpoint. The key comes "
        "from the kimi.com/code console and is not the same thing as an open-platform key. "
        "base_url must include /v1 (the model-list endpoint needs it). Verified to work. "
        "Get the models with “Fetch model list” (testing returned k3 / k3-256k / "
        "kimi-for-coding and others)."
    ),
    "provider.preset.kimi-code-global.label": "Kimi Code (overseas)",
    "provider.preset.kimi-code-global.notes": (
        "Same as above, overseas site. The one verified in testing is the China site."
    ),
    "provider.preset.moonshot.label": "Kimi Open Platform (pay as you go)",
    "provider.preset.moonshot.notes": (
        "The Open Platform China site, OpenAI format, pay as you go. Its keys are separate "
        "from Kimi Code's."
    ),
    "provider.preset.moonshot-global.label": "Kimi Open Platform (overseas)",
    "provider.preset.moonshot-global.notes": "Same as above, overseas site.",
    "provider.preset.deepseek.label": "DeepSeek",
    "provider.preset.deepseek.notes": (
        "The provider this project was tested against by default. It requires historical "
        "reasoning content to be sent back; a cache hit costs 1/50 of a cache miss."
    ),
    "provider.preset.zhipu.label": "Zhipu GLM",
    "provider.preset.zhipu.notes": "Has free-tier models; a good place to start.",
    "provider.preset.dashscope.label": "Qwen (Alibaba Cloud)",
    "provider.preset.dashscope.notes": "Compatibility-mode endpoint, with a free quota.",
    "provider.preset.siliconflow.label": "SiliconFlow",
    "provider.preset.siliconflow.notes": (
        "Aggregates open-source models from several vendors; free models available."
    ),
    "provider.preset.openrouter.label": "OpenRouter",
    "provider.preset.openrouter.notes": (
        "One key gives you access to models from many vendors."
    ),
    "provider.preset.openai.label": "OpenAI",
    "provider.preset.openai.notes": (
        "Note: unknown fields in messages may be rejected, so this profile does not send "
        "reasoning content back by default."
    ),
    "provider.preset.ollama.label": "Local Ollama",
    "provider.preset.ollama.notes": (
        "Fully offline, zero cost. Requires Ollama installed locally with a model already "
        "pulled; any non-empty key will do."
    ),
    # ── client errors (`providers/openai_compat.py`, `providers/anthropic_compat.py`) ──
    # The sentences both protocols share live under `provider.http.*`: defined once, used twice.
    "provider.label.openai": "OpenAI-compatible",
    "provider.label.anthropic": "Anthropic-compatible",
    "provider.openai.no_key": (
        "{label} has no API key: fill it in under Settings → Providers in the Web UI."
    ),
    "provider.anthropic.no_key": (
        "No API key: fill it in under Settings → Providers in the Web UI."
    ),
    "provider.http.error": "{label} returned an error: {message}",
    "provider.http.status": "{label} returned HTTP {status}: {detail}",
    "provider.http.network": "{label} network error: {detail}",
    "provider.http.incomplete_stream": "SSE stream ended before a terminal event; retry the request",
    "provider.http.unknown_failure": "Unknown request failure",
    "provider.http.not_sse": "{label}'s response is not an SSE stream; raw content: {preview}",
    "provider.http.empty_body": "(empty)",
    "provider.http.error_unreadable": "(could not read the error response body)",
}
