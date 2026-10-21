"""联网工具与供应商层的文案（中文）。

只导出 `LANG` 与 `MESSAGES`。命名：
`net.<工具>.<情形>`（联网工具的失败文案 —— **这些会作为工具结果回给模型**）、
`provider.<区域>.<名字>`（供应商/接口层的报错 —— 会显示在设置页上）。

**这里的中文是"改造前的原句"，逐字不动**：既有测试（`tests/test_web_tools.py`）
断言过其中好几条，而且模型对同一句话的行为反应是拿真实翻车换来的。
改这句话之前先读 `harness/net/fetch.py` 里 `_PRIVATE_HINT` 与
`search()` 的那段注释。
"""

from __future__ import annotations

LANG = "zh"

MESSAGES: dict[str, str] = {
    # ── 依赖缺失（fetch 与 search 共用同一条：两处都自己写会漏改一处）──────
    "net.dep.httpx": (
        "缺少依赖 httpx。请运行：run.cmd --setup（或 pip install -r requirements.txt）"
    ),
    # ── 抓取（`harness/net/fetch.py`）───────────────────────────────────
    # 这些都会作为**工具结果回给模型**，所以它们是指令，不是装饰。
    "net.fetch.empty_url": "URL 是空的。",
    "net.fetch.bad_scheme": "只支持 http/https，不接受 {scheme} 协议。",
    # 拿不到 scheme 时原来写成 `head or '这种'`，那句话单独一条 key：
    # 硬塞进 `{scheme}` 会让英文变成 "the that kind of scheme"。
    "net.fetch.bad_scheme_unnamed": "只支持 http/https，不接受 这种 协议。",
    "net.fetch.no_host": "这个 URL 里没有主机名：{url}",
    "net.fetch.private_hint": (
        "拒绝访问本机/内网地址（{host}）。"
        "这是为了防止网页内容诱导模型去读你本机的服务；"
        "确实需要的话可以在「设置 → 工具」里打开「允许内网地址」。"
    ),
    "net.fetch.numeric_host": (
        "拒绝：{host} 看起来是 IP 的一种写法，但无法解析成地址。"
        "为了不让非规范写法绕过内网检查，这里一律拒绝。"
    ),
    "net.fetch.timeout_total": "请求超时（{seconds} 秒，含跳转）。",
    "net.fetch.too_many_redirects": "跳转次数超过上限（{max_redirects} 次），已停止。",
    "net.fetch.body_timeout": "正文读取超时（{seconds} 秒，含跳转）。",
    "net.fetch.http_error": "对方返回 HTTP {status}。",
    "net.fetch.timeout": "请求超时（{seconds} 秒）。",
    "net.fetch.failed": "抓取失败：{kind}: {detail}",
    "net.fetch.truncated_bytes": "内容超过 {max_bytes} 字节，只取了前一部分。",
    "net.fetch.unsupported_type": "不支持的内容类型：{content_type}",
    "net.fetch.undeclared_type": "(未声明)",
    "net.fetch.truncated_chars": "正文超过 {max_chars} 字符，已截断。",
    "net.fetch.no_body": "没抽到正文（可能是需要 JS 渲染的页面）。",
    # 明确认识但不支持的几种：给一句人话，而不是把二进制丢进上下文。
    "net.fetch.binary.pdf": "PDF 暂不支持（后续可以接一个 PDF 解析库）",
    "net.fetch.binary.zip": "压缩包不支持",
    "net.fetch.binary.binary": "二进制文件不支持",
    # ── 搜索（`harness/net/search.py`）──────────────────────────────────
    "net.search.tavily.no_key": (
        "Tavily 需要 API key，请到「设置 → 工具」填写（或改用免 key 的 Bing）。"
    ),
    "net.search.tavily.rejected": "Tavily 拒绝了这个 key（401）。检查一下是不是复制少了字符。",
    "net.search.empty_query": "搜索关键词是空的。",
    "net.search.unknown_backend": "不认识的搜索后端：{backend}",
    # 「结果与关键词明显无关」时给模型的话：**如实说没搜到**、别再反复换说法搜。
    # 这是拿实测换来的（合规过滤后的结果喂给模型比"没结果"更糟）。
    "net.search.irrelevant": (
        "{backend} 返回的结果与关键词明显无关（抓取型后端会被「降级对待」，"
        "例如按当地法律要求过滤掉部分结果）。**如实告诉用户你没搜到**，"
        "可以建议他到「设置 → 工具」换一个搜索后端；不要再换说法反复搜。"
    ),
    # 解不开的 360 跳转链接要如实标注，免得模型以为那是个能打开的页面。
    "net.search.so360.redirect_note": "（此链接为 360 跳转链接，可能打不开）",
    # ── 搜索后端清单（设置 → 工具 里显示的那几行）────────────────────────
    "net.config.backend.bing.label": "Bing（免 key）",
    "net.config.backend.bing.notes": "直接抓 Bing 结果页。国内可达，中英文都能查。",
    "net.config.backend.tavily.label": "Tavily（需 key）",
    "net.config.backend.tavily.notes": (
        "专为 LLM 设计的搜索 API，结果更干净、摘要里常直接有答案。"
        "有免费额度。api.tavily.com 实测可达。**已确认质量很高**。"
    ),
    "net.config.backend.off.label": "关闭搜索",
    "net.config.backend.off.notes": "模型就用不了 web_search。",
    # key 的遮罩：中文里习惯用两个省略号，英文里用 `...`。
    "net.config.key_mask": "……",
}
