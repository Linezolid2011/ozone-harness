"""供应商层与运行期装配的文案（中文）。

只导出 `LANG` 与 `MESSAGES`。命名：`provider.<区域>.<名字>`、
`ui.runner.<名字>`（运行期装配的报错）。这些会显示在**设置 → 接口**上，
也会作为 400 的原话回到界面上。

**这里的中文是"改造前的原句"，逐字不动** —— 既有测试
（`tests/test_providers.py`、`tests/test_memory_limit.py`、`tests/test_web.py`）
断言过其中好几条。`provider.preset.*` 是预设的 label / notes：
它们由 `harness/providers/store.py` 在**用到它的那一刻**现取
（`get_preset()` / `presets()`），不是在 import 时算好。

`provider.memory.*` 是记忆条数上限的校验话术（`harness/memory_limit.py`）：
它同样显示在设置页、也会作为 400 回到界面，而 `validate_limit()` 是 CLI 与 Web
共用的纯函数 —— 所以那句人话必须走 catalog，校验逻辑自身一个字都没动。
"""

from __future__ import annotations

LANG = "zh"

MESSAGES: dict[str, str] = {
    # ── 运行期装配（`harness/runtime.py`）───────────────────────────────
    "ui.runner.no_provider": (
        "还没有配置任何接口。打开 Web 界面（run.cmd --web）→ 左下角「设置 → 接口」，"
        "挑一个预设、填入你自己的 API key 即可。本项目不预置任何 key。"
    ),
    "ui.runner.no_model": (
        "接口「{label}」还没有可用模型。请到「设置 → 接口」点"
        "「获取模型列表」，或手动把模型名填进去。"
    ),
    # 兜底 provider 的显示名（会出现在"{label} 返回 HTTP …"这类报错里）
    "ui.runner.default_label": "默认接口",
    # ── 记忆条数上限（`harness/memory_limit.py`）────────────────────────
    # `{value!r}` 是刻意的：坏值原样带出来，用户才知道自己填的是什么。
    "provider.memory.limit_not_integer": "上限必须是一个整数（收到 {value!r}）",
    "provider.memory.limit_not_whole_number": (
        "上限必须是整数，不能是小数（收到 {value!r}）"
    ),
    "provider.memory.limit_required": "上限必须是整数（收到 {value!r}）",
    "provider.memory.limit_out_of_range": (
        "上限只能是 {low} 到 {high} 之间的整数（收到 {wanted}）"
    ),
    "provider.memory.limit_too_low": (
        "上限太低：现在已经有 {active_count} 条记忆，上限不能低于它"
        "（收到 {wanted}）。请先删掉一些，或者把上限设成 {active_count} 以上"
    ),
    # ── 档案读写与模型发现（`harness/providers/store.py`）───────────────
    "provider.store.id_required": "供应商 id 不能为空",
    "provider.store.base_url_required": "base_url 不能为空",
    "provider.store.base_url_scheme": "地址要写完整：以 http:// 或 https:// 开头（例如 https://api.deepseek.com/v1）。",
    "provider.store.unknown": "没有这个供应商：{provider_id}",
    "provider.store.models_http": "HTTP {status}：{detail}",
    "provider.store.models_unreachable": "连不上 {url}：{detail}",
    "provider.store.models_not_json": "{url} 返回的不是 JSON",
    "provider.store.models_empty": "该端点没有返回任何模型 id",
    "provider.store.key_mask": "……",
    # ── 预设（"添加接口"的起点）────────────────────────────────────────
    "provider.preset.kimi-code.label": "Kimi Code（订阅 token plan）",
    "provider.preset.kimi-code.notes": (
        "Kimi Code 会员额度，国内站，**Anthropic 格式**端点。"
        "key 来自 kimi.com/code 控制台，与开放平台的 key 不是一回事。"
        "base_url 必须带 /v1（模型列表端点需要它）。已实测跑通。"
        "模型用「获取模型列表」拿（实测可拿到 k3 / k3-256k / kimi-for-coding 等）。"
    ),
    "provider.preset.kimi-code-global.label": "Kimi Code（海外）",
    "provider.preset.kimi-code-global.notes": "同上，海外站点。已实测的是国内站。",
    "provider.preset.moonshot.label": "Kimi 开放平台（按量付费）",
    "provider.preset.moonshot.notes": (
        "开放平台国内站，OpenAI 格式、按量付费。与 Kimi Code 是两套 key。"
    ),
    "provider.preset.moonshot-global.label": "Kimi 开放平台（海外）",
    "provider.preset.moonshot-global.notes": "同上，海外站点。",
    "provider.preset.deepseek.label": "DeepSeek",
    "provider.preset.deepseek.notes": (
        "本项目默认实测的供应商。要求回传历史思维链；缓存命中单价是未命中的 1/50。"
    ),
    "provider.preset.zhipu.label": "智谱 GLM",
    "provider.preset.zhipu.notes": "有免费档模型，适合先试。",
    "provider.preset.dashscope.label": "通义千问（阿里云）",
    "provider.preset.dashscope.notes": "兼容模式端点，有免费额度。",
    "provider.preset.siliconflow.label": "硅基流动",
    "provider.preset.siliconflow.notes": "聚合了多家开源模型，有免费模型。",
    "provider.preset.openrouter.label": "OpenRouter",
    "provider.preset.openrouter.notes": "一个 key 可以调很多家模型。",
    "provider.preset.openai.label": "OpenAI",
    "provider.preset.openai.notes": (
        "注意：消息里带未知字段可能被拒，所以本档案默认不回传思维链。"
    ),
    "provider.preset.ollama.label": "本地 Ollama",
    "provider.preset.ollama.notes": (
        "完全离线、零成本。需要本机已装 Ollama 并拉过模型；key 随便填一个非空值即可。"
    ),
    # ── 客户端报错（`providers/openai_compat.py`、`providers/anthropic_compat.py`）──
    # 两个协议共用的那几句放 `provider.http.*`：一处定义，两边都用。
    "provider.label.openai": "OpenAI 兼容",
    "provider.label.anthropic": "Anthropic 兼容",
    "provider.openai.no_key": "{label} 缺少 API key：请到 Web 界面的「设置 → 接口」里填写。",
    "provider.anthropic.no_key": "缺少 API key：请在 Web 界面的「设置 → 接口」里填写。",
    "provider.http.error": "{label} 返回错误：{message}",
    "provider.http.status": "{label} 返回 HTTP {status}：{detail}",
    "provider.http.network": "{label} 网络错误：{detail}",
    "provider.http.unknown_failure": "未知的请求失败",
    "provider.http.not_sse": "{label} 的响应不是 SSE 流，原始内容：{preview}",
    "provider.http.empty_body": "(空)",
    "provider.http.error_unreadable": "(无法读取错误响应体)",
}
