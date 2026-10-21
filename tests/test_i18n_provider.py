"""联网工具层与供应商层的双语文案守卫（契约 `docs/12-i18n.md`；全局守卫是
`tests/test_i18n.py`，那个不动）。

这里查的是**这两层自己的四件事**：

1. **英文模式下，会发出去的话里一个汉字（含中文标点）都没有** ——
   联网工具的失败文案会作为**工具结果回给模型**，供应商层的报错会显示在
   **设置 → 接口**上、也会作为 400 的原话回到界面；
2. 中文那一套与改造前**逐字一致**（既有测试在断言它们：`test_web_tools.py`、
   `test_providers.py`、`test_memory_limit.py`、`test_web.py`）——
   所以这里直接断言**代码路径真的产出**的那些原句，而不是只查 catalog；
3. 两个语言的 key 集合一致、占位符一一对应；
4. 改造后的八个模块里**不留中文字面量**（注释、docstring，以及
   `_GENERIC_TERMS` 那张"中文泛词表"除外 —— 它只用来跟页面文字比对，从不外发）。

`net/fetch.py` 与 `net/search.py` 的失败文案是**给模型的行为指令**（拒绝本机/内网、
如实说没搜到、不要再反复换说法搜），所以对它们另有**关键词断言**：
翻译可以换词，指令一条都不许丢。另一条同源的"不要再猜地址"在工具层
（`harness/tools/web_tools.py` 的 `open_failed_hint()`），由 `tests/test_i18n_tools.py` 盯着。
"""

from __future__ import annotations

import ast
import io
import re
import string
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from harness import i18n
from harness import runtime as runtime_mod
from harness.memory_limit import validate_limit
from harness.net import config as net_config
from harness.net import fetch as net_fetch
from harness.net import search as net_search
from harness.net import text_en as net_text_en
from harness.net import text_zh as net_text_zh
from harness.provider_text_en import MESSAGES as PROVIDER_EN
from harness.provider_text_zh import MESSAGES as PROVIDER_ZH
from harness.providers.base import ProviderError
from harness.providers.openai_compat import OpenAICompatProvider
from harness.providers.store import (
    PRESETS,
    ProviderProfile,
    ProviderStore,
    fetch_models,
    get_preset,
    presets,
)
from harness.providers.anthropic_compat import AnthropicCompatProvider
from harness.runtime import resolve_provider

from .helpers import test_settings

#: 汉字 + 中文标点（比 `tests/test_i18n.py` 只查汉字的判据更严一点：
#: 英文文案里连一个全角标点都不该有）。
CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

#: 英文模式下**允许**出现汉字的地方。这两层是**空集合**（刻意的）：
#: 联网工具与供应商层没有"用自己的语言写的名字"这种必要。
EN_CJK_ALLOWED: set[str] = set()

#: 成对的 catalog：`(子系统, 中文, 英文, 允许的 key 前缀)`
CATALOGS = (
    ("net", net_text_zh.MESSAGES, net_text_en.MESSAGES, ("net.",)),
    ("provider", PROVIDER_ZH, PROVIDER_EN, ("provider.", "ui.runner.")),
)

#: 改造后不许再出现中文字面量的模块（注释与 docstring 是给人看的，不算）。
LITERAL_FREE_MODULES = (
    "harness/net/search.py",
    "harness/net/fetch.py",
    "harness/net/config.py",
    "harness/providers/store.py",
    "harness/providers/openai_compat.py",
    "harness/providers/anthropic_compat.py",
    "harness/memory_limit.py",
    "harness/runtime.py",
)

ROOT = Path(__file__).resolve().parent.parent


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _cjk(text: object) -> str:
    return "".join(CJK.findall(str(text)))


def _filled(text: str) -> str:
    """"能构造出来的失败文案"：每条模板用无害的假值填一遍。"""
    return text.format(**{name: "X" for name in _placeholders(text)})


def _failure(call) -> str:
    """跑一段**会失败**的代码，把它要发出去的那句话拿出来。"""
    try:
        call()
    except (net_fetch.NetError, ProviderError) as exc:
        return str(exc)
    raise AssertionError("这段代码没有失败")


def _fake_backend(results: list) -> object:
    def run(query: str, count: int, timeout: float, api_key: str = "") -> list:
        return results

    return run


# --------------------------------------------------------------------------
# catalog 的形状
# --------------------------------------------------------------------------


class CatalogParityTests(unittest.TestCase):
    def test_both_languages_define_exactly_the_same_keys(self) -> None:
        for name, zh, en, _prefixes in CATALOGS:
            with self.subTest(catalog=name):
                self.assertEqual(sorted(set(zh) - set(en)), [], f"{name}：这些 key 英文里没有")
                self.assertEqual(sorted(set(en) - set(zh)), [], f"{name}：这些 key 中文里没有")

    def test_placeholders_match_one_to_one(self) -> None:
        for name, zh, en, _prefixes in CATALOGS:
            for key in sorted(zh):
                with self.subTest(catalog=name, key=key):
                    self.assertEqual(
                        sorted(_placeholders(zh[key])),
                        sorted(_placeholders(en[key])),
                        f"{key} 的占位符两边不一致",
                    )

    def test_no_key_is_defined_in_both_catalogs(self) -> None:
        """`net.*` 与 `provider.*` / `ui.runner.*` 不许撞车（撞了就是"一处生效看导入顺序"）。"""
        net_keys = set(net_text_zh.MESSAGES) | set(net_text_en.MESSAGES)
        provider_keys = set(PROVIDER_ZH) | set(PROVIDER_EN)
        self.assertEqual(sorted(net_keys & provider_keys), [])

    def test_keys_use_the_documented_prefixes(self) -> None:
        for name, zh, en, prefixes in CATALOGS:
            for key in sorted(set(zh) | set(en)):
                with self.subTest(catalog=name, key=key):
                    self.assertTrue(
                        key.startswith(prefixes), f"{key} 不在约定的命名空间里：{prefixes}"
                    )

    def test_english_values_have_no_chinese(self) -> None:
        """**这条才是"漏翻"的判据。** 模板与填好假值之后的成品都查一遍。"""
        offenders = []
        for name, _zh, en, _prefixes in CATALOGS:
            for key, value in en.items():
                if key in EN_CJK_ALLOWED:
                    continue
                if _cjk(value) or _cjk(_filled(value)):
                    offenders.append(f"{name}:{key}")
        self.assertEqual(offenders, [], f"这些英文文案里还有汉字或中文标点：{offenders}")

    def test_chinese_values_are_not_empty(self) -> None:
        for name, zh, _en, _prefixes in CATALOGS:
            for key, value in zh.items():
                with self.subTest(catalog=name, key=key):
                    self.assertTrue(value.strip(), f"{key} 是空的")

    def test_every_key_resolves_in_both_languages(self) -> None:
        for name, zh, _en, _prefixes in CATALOGS:
            for language in i18n.LANGUAGES:
                with i18n.use(language):
                    for key, value in zh.items():
                        with self.subTest(language=language, key=key):
                            self.assertTrue(
                                i18n.t(key, **{p: "X" for p in _placeholders(value)}).strip()
                            )

    def test_no_template_leaks_a_placeholder_after_formatting(self) -> None:
        for name, _zh, en, _prefixes in CATALOGS:
            for key, value in en.items():
                with self.subTest(catalog=name, key=key):
                    filled = _filled(value)
                    self.assertNotIn("{", filled.replace("{{", "").replace("}}", ""))


# --------------------------------------------------------------------------
# 中文那一套必须逐字是原句（直接断言**代码路径真的产出**的那句话）
# --------------------------------------------------------------------------


class ChineseIsTheOriginalTextTests(unittest.TestCase):
    def test_fetch_failures_are_verbatim(self) -> None:
        with i18n.use("zh"):
            cases = (
                ("", "URL 是空的。"),
                (
                    "javascript:alert(1)",
                    "只支持 http/https，不接受 javascript 协议。",
                ),
                ("ftp://x/y", "只支持 http/https，不接受 ftp 协议。"),
                ("://x", "只支持 http/https，不接受 这种 协议。"),
                ("https://", "这个 URL 里没有主机名：https://"),
                (
                    "http://127.0.0.1/",
                    "拒绝访问本机/内网地址（127.0.0.1）。"
                    "这是为了防止网页内容诱导模型去读你本机的服务；"
                    "确实需要的话可以在「设置 → 工具」里打开「允许内网地址」。",
                ),
                (
                    "http://999.1.1.1/",
                    "拒绝：999.1.1.1 看起来是 IP 的一种写法，但无法解析成地址。"
                    "为了不让非规范写法绕过内网检查，这里一律拒绝。",
                ),
            )
            for url, expected in cases:
                with self.subTest(url=url):
                    self.assertEqual(_failure(lambda url=url: net_fetch.check_url(url)), expected)

    def test_fetch_notes_are_verbatim(self) -> None:
        """截断 / 二进制 / 没抽到正文这几条 note 也是给模型看的。"""
        with i18n.use("zh"):
            self.assertEqual(
                i18n.t("net.fetch.truncated_bytes", max_bytes=2_000_000),
                "内容超过 2000000 字节，只取了前一部分。",
            )
            self.assertEqual(
                i18n.t("net.fetch.truncated_chars", max_chars=8000),
                "正文超过 8000 字符，已截断。",
            )
            self.assertEqual(
                i18n.t("net.fetch.no_body"), "没抽到正文（可能是需要 JS 渲染的页面）。"
            )
            self.assertEqual(
                i18n.t("net.fetch.binary.pdf"), "PDF 暂不支持（后续可以接一个 PDF 解析库）"
            )
            self.assertEqual(i18n.t("net.fetch.binary.zip"), "压缩包不支持")
            self.assertEqual(i18n.t("net.fetch.binary.binary"), "二进制文件不支持")
            self.assertEqual(
                i18n.t("net.fetch.timeout_total", seconds="20"),
                "请求超时（20 秒，含跳转）。",
            )
            self.assertEqual(
                i18n.t("net.fetch.too_many_redirects", max_redirects=5),
                "跳转次数超过上限（5 次），已停止。",
            )
            self.assertEqual(
                i18n.t("net.fetch.body_timeout", seconds="20"), "正文读取超时（20 秒，含跳转）。"
            )
            self.assertEqual(i18n.t("net.fetch.http_error", status=404), "对方返回 HTTP 404。")
            self.assertEqual(i18n.t("net.fetch.timeout", seconds="20"), "请求超时（20 秒）。")
            self.assertEqual(
                i18n.t("net.fetch.unsupported_type", content_type="image/png"),
                "不支持的内容类型：image/png",
            )
            self.assertEqual(
                i18n.t("net.fetch.unsupported_type", content_type="(未声明)"),
                "不支持的内容类型：(未声明)",
            )
            self.assertEqual(
                i18n.t("net.dep.httpx"),
                "缺少依赖 httpx。请运行：run.cmd --setup（或 pip install -r requirements.txt）",
            )

    def test_search_failures_are_verbatim(self) -> None:
        with i18n.use("zh"):
            self.assertEqual(
                _failure(lambda: net_search.search("  ")), "搜索关键词是空的。"
            )
            self.assertEqual(
                _failure(lambda: net_search.search("x", backend="nope")),
                "不认识的搜索后端：nope",
            )
            self.assertEqual(
                _failure(lambda: net_search._search_tavily("q", 1, 1.0, api_key="")),
                "Tavily 需要 API key，请到「设置 → 工具」填写（或改用免 key 的 Bing）。",
            )
            self.assertEqual(
                i18n.t("net.search.tavily.rejected"),
                "Tavily 拒绝了这个 key（401）。检查一下是不是复制少了字符。",
            )
            self.assertEqual(
                i18n.t("net.search.so360.redirect_note"),
                "（此链接为 360 跳转链接，可能打不开）",
            )

    def test_irrelevant_note_is_verbatim(self) -> None:
        """搜索结果"明显无关"时给模型的那句话：既有测试在断言它的几个片段。"""
        results = [net_search.SearchResult("上海旅游攻略", "https://a", "2025年最新旅行数据")]
        with i18n.use("zh"), mock.patch(
            "harness.net.search._backends", lambda: {"bing": _fake_backend(results)}
        ):
            _results, backend, note = net_search.search("上海 垃圾分类 官方政策", backend="bing")
        self.assertEqual(backend, "bing")
        self.assertEqual(
            note,
            "bing 返回的结果与关键词明显无关（抓取型后端会被「降级对待」，"
            "例如按当地法律要求过滤掉部分结果）。**如实告诉用户你没搜到**，"
            "可以建议他到「设置 → 工具」换一个搜索后端；不要再换说法反复搜。",
        )

    def test_search_backend_rows_are_verbatim(self) -> None:
        with i18n.use("zh"):
            rows = {
                item["id"]: (item["label"], item["notes"])
                for item in net_config.WebToolsConfig().to_public()["backends"]
            }
            self.assertEqual(set(rows), {"bing", "tavily", "off"})
            self.assertEqual(rows["bing"], ("Bing（免 key）", "直接抓 Bing 结果页。国内可达，中英文都能查。"))
            self.assertEqual(rows["tavily"][0], "Tavily（需 key）")
            self.assertEqual(
                rows["tavily"][1],
                "专为 LLM 设计的搜索 API，结果更干净、摘要里常直接有答案。"
                "有免费额度。api.tavily.com 实测可达。**已确认质量很高**。",
            )
            self.assertEqual(rows["off"], ("关闭搜索", "模型就用不了 web_search。"))
            self.assertIn("……", net_config.WebToolsConfig(tavily_api_key="tvly-x").masked_tavily_key())

    def test_memory_limit_sentences_are_verbatim(self) -> None:
        """`validate_limit()` 的那几句人话（CLI 与 Web 共用）。**校验逻辑没动。**"""
        with i18n.use("zh"):
            self.assertEqual(
                validate_limit(3, active_count=5).error,
                "上限太低：现在已经有 5 条记忆，上限不能低于它（收到 3）。"
                "请先删掉一些，或者把上限设成 5 以上",
            )
            self.assertEqual(
                validate_limit(200, active_count=0).error,
                "上限只能是 0 到 100 之间的整数（收到 200）",
            )
            self.assertEqual(
                validate_limit(3.7, active_count=0).error,
                "上限必须是整数，不能是小数（收到 3.7）",
            )
            self.assertEqual(
                validate_limit("abc", active_count=0).error, "上限必须是整数（收到 'abc'）"
            )
            self.assertEqual(
                validate_limit(True, active_count=0).error,
                "上限必须是一个整数（收到 True）",
            )
            # 合法的取值照旧通过，逻辑一条都没改
            self.assertTrue(validate_limit(5, active_count=5).ok)
            self.assertEqual(validate_limit(50.0, active_count=0).limit, 50)

    def test_provider_failures_are_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp))
            with i18n.use("zh"):
                self.assertEqual(
                    _failure(lambda: resolve_provider(settings)),
                    "还没有配置任何接口。打开 Web 界面（run.cmd --web）→ 左下角"
                    "「设置 → 接口」，挑一个预设、填入你自己的 API key 即可。"
                    "本项目不预置任何 key。",
                )
                store = ProviderStore(settings.data_dir / "providers.json")
                store.upsert(
                    ProviderProfile(id="x", label="X", base_url="https://x/v1", api_key="k")
                )
                self.assertEqual(
                    _failure(lambda: resolve_provider(settings)),
                    "接口「X」还没有可用模型。请到「设置 → 接口」点「获取模型列表」，"
                    "或手动把模型名填进去。",
                )
                self.assertEqual(
                    _failure(lambda: store.upsert(ProviderProfile(id="", label="", base_url="https://x"))),
                    "供应商 id 不能为空",
                )
                self.assertEqual(
                    _failure(lambda: store.upsert(ProviderProfile(id="y", label="", base_url=""))),
                    "base_url 不能为空",
                )
                self.assertEqual(
                    _failure(lambda: store.update_key("nope", "k")), "没有这个供应商：nope"
                )
                self.assertIn(
                    "OpenAI 兼容",
                    _failure(lambda: OpenAICompatProvider(test_settings(Path(tmp), api_key=""))),
                )
                # Anthropic 那一侧"缺少 API key"的那句话里没有 label（原文如此），
                # 默认显示名单独查。
                self.assertEqual(
                    AnthropicCompatProvider(test_settings(Path(tmp), api_key="k")).label,
                    "Anthropic 兼容",
                )
                self.assertEqual(
                    OpenAICompatProvider(test_settings(Path(tmp), api_key="k")).label,
                    "OpenAI 兼容",
                )

    def test_preset_text_verbatim(self) -> None:
        with i18n.use("zh"):
            rows = {preset["id"]: preset for preset in presets()}
            self.assertEqual(rows["kimi-code"]["label"], "Kimi Code（订阅 token plan）")
            self.assertEqual(
                rows["kimi-code"]["notes"],
                "Kimi Code 会员额度，国内站，**Anthropic 格式**端点。"
                "key 来自 kimi.com/code 控制台，与开放平台的 key 不是一回事。"
                "base_url 必须带 /v1（模型列表端点需要它）。已实测跑通。"
                "模型用「获取模型列表」拿（实测可拿到 k3 / k3-256k / kimi-for-coding 等）。",
            )
            self.assertEqual(rows["deepseek"]["label"], "DeepSeek")
            self.assertEqual(
                rows["deepseek"]["notes"],
                "本项目默认实测的供应商。要求回传历史思维链；缓存命中单价是未命中的 1/50。",
            )
            self.assertEqual(rows["ollama"]["label"], "本地 Ollama")
            self.assertEqual(rows["zhipu"]["notes"], "有免费档模型，适合先试。")


# --------------------------------------------------------------------------
# 英文：会发出去的失败文案里没有汉字，行为指令一条都没丢
# --------------------------------------------------------------------------


class EnglishFailureTextTests(unittest.TestCase):
    def test_fetch_failures_have_no_chinese(self) -> None:
        with i18n.use("en"):
            cases = (
                "",
                "javascript:alert(1)",
                "ftp://x/y",
                "://x",
                "https://",
                "http://127.0.0.1/",
                "http://2130706433/",
                "http://[::ffff:127.0.0.1]/",
                "http://999.1.1.1/",
            )
            for url in cases:
                with self.subTest(url=url):
                    message = _failure(lambda url=url: net_fetch.check_url(url))
                    self.assertEqual(_cjk(message), "", f"{url} 的英文失败文案漏翻了：{message}")

    def test_search_failures_have_no_chinese(self) -> None:
        results = [net_search.SearchResult("Shanghai travel", "https://a", "travel data 2025")]
        with i18n.use("en"):
            self.assertEqual(_cjk(_failure(lambda: net_search.search("  "))), "")
            self.assertEqual(
                _cjk(_failure(lambda: net_search.search("x", backend="nope"))), ""
            )
            self.assertEqual(
                _cjk(_failure(lambda: net_search._search_tavily("q", 1, 1.0, api_key=""))), ""
            )
            with mock.patch(
                "harness.net.search._backends", lambda: {"bing": _fake_backend(results)}
            ):
                _results, _backend, note = net_search.search(
                    "Shanghai waste sorting official policy", backend="bing"
                )
            self.assertEqual(_cjk(note), "", f"英文的无关结果提示漏翻了：{note}")

    def test_provider_failures_have_no_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp))
            store = ProviderStore(settings.data_dir / "providers.json")
            store.upsert(
                ProviderProfile(id="x", label="X", base_url="https://x/v1", api_key="k")
            )
            with i18n.use("en"):
                messages = [
                    _failure(lambda: resolve_provider(settings)),
                    _failure(lambda: store.update_key("nope", "k")),
                    _failure(lambda: OpenAICompatProvider(test_settings(Path(tmp), api_key=""))),
                    _failure(
                        lambda: AnthropicCompatProvider(test_settings(Path(tmp), api_key=""))
                    ),
                    # `validate_limit()` **不抛异常**：失败是一句话（`LimitChange.error`）
                    validate_limit(3, active_count=5).error,
                    validate_limit(3.7, active_count=0).error,
                ]
                provider = OpenAICompatProvider(settings, label="X")
                messages.append(
                    _failure(
                        lambda: list(
                            provider._parse_sse([b'data: {"error": {"message": "boom"}}'])
                        )
                    )
                )
                messages.append(_failure(lambda: list(provider._parse_sse([b"event: ping"]))))
                for message in messages:
                    with self.subTest(message=message):
                        self.assertEqual(_cjk(message), "", f"英文的报错漏翻了：{message}")

    def test_fetch_directives_survive_translation(self) -> None:
        """`fetch.py` 的失败文案是**给模型的指令**：拒绝内网 + 出口在哪。

        关键词断言刻意宽松（翻译可以换词），但**语义一个都不许少**。
        """
        with i18n.use("en"):
            private = _failure(lambda: net_fetch.check_url("http://127.0.0.1:8787/"))
            self.assertIn("127.0.0.1", private, "地址要原样带出来")
            self.assertIn("Refused", private, "要明说这是拒绝")
            self.assertIn("local/private", private)
            # 为什么拒（防止网页内容诱导模型读本机服务）
            self.assertIn("web page content", private)
            self.assertIn("your own machine", private)
            # 出口在哪
            self.assertIn("Settings → Tools", private)
            self.assertIn("Allow private addresses", private)

            numeric = _failure(lambda: net_fetch.check_url("http://999.1.1.1/"))
            self.assertIn("999.1.1.1", numeric)
            self.assertIn("Refused", numeric)
            self.assertIn("rejected", numeric, "要明说这里一律拒绝")
            self.assertIn("private-address", numeric)

            binary = i18n.t("net.fetch.binary.pdf")
            self.assertIn("PDF", binary)
            self.assertIn("not supported", binary)

            unsupported = i18n.t("net.fetch.unsupported_type", content_type="image/png")
            self.assertIn("image/png", unsupported)

            no_body = i18n.t("net.fetch.no_body")
            self.assertIn("JavaScript", no_body)

    def test_search_directives_survive_translation(self) -> None:
        """搜不到时的指令：**如实说没搜到**、别再反复换说法搜、换后端在哪。"""
        with i18n.use("en"):
            note = i18n.t("net.search.irrelevant", backend="bing")
            self.assertIn("bing", note, "用的哪个后端要带出来")
            self.assertIn("unrelated", note)
            self.assertIn("honestly", note, "要如实说")
            self.assertIn("did not find anything", note, "要明说没搜到")
            self.assertIn("Settings → Tools", note, "换后端在哪要说清")
            self.assertRegex(note, r"[Dd]o not", "要明确劝阻反复搜")

            no_key = i18n.t("net.search.tavily.no_key")
            self.assertIn("API key", no_key)
            self.assertIn("Settings → Tools", no_key)

            snippet = i18n.t("net.search.so360.redirect_note")
            self.assertIn("may not open", snippet, "打不开要如实标注")


# --------------------------------------------------------------------------
# 文案是**用到它的那一刻**取的（语言是运行期可切的）
# --------------------------------------------------------------------------


class LazyResolutionTests(unittest.TestCase):
    def test_the_same_call_follows_the_language(self) -> None:
        with i18n.use("zh"):
            chinese = _failure(lambda: net_fetch.check_url(""))
        with i18n.use("en"):
            english = _failure(lambda: net_fetch.check_url(""))
        self.assertTrue(_cjk(chinese))
        self.assertEqual(_cjk(english), "")
        self.assertNotEqual(chinese, english)

    def test_memory_limit_follows_the_language(self) -> None:
        with i18n.use("zh"):
            chinese = validate_limit(3, active_count=5).error
        with i18n.use("en"):
            english = validate_limit(3, active_count=5).error
        self.assertIn("上限太低", chinese)
        self.assertIn("too low", english)
        self.assertEqual(_cjk(english), "")

    def test_no_provider_help_alias_is_lazy(self) -> None:
        """`NO_PROVIDER_HELP` 是旧名字（`cli.py` 从模块顶层 import 它）。

        用模块级 `__getattr__`（PEP 562）保留：**取值时**才解析语言，
        写成模块常量就等于在 import 那一刻把语言钉死了。
        """
        with i18n.use("zh"):
            chinese = runtime_mod.NO_PROVIDER_HELP
        with i18n.use("en"):
            english = runtime_mod.NO_PROVIDER_HELP
            self.assertEqual(english, runtime_mod.no_provider_help())
        self.assertIn("还没有配置任何接口", chinese)
        self.assertEqual(_cjk(english), "")
        self.assertNotEqual(chinese, english)

    def test_default_provider_label_follows_the_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp), api_key="")
            with i18n.use("zh"):
                chinese = _failure(lambda: OpenAICompatProvider(settings))
                self.assertIn("OpenAI 兼容", chinese)
            with i18n.use("en"):
                english = _failure(lambda: OpenAICompatProvider(settings))
                self.assertIn("OpenAI-compatible", english)
                self.assertEqual(_cjk(english), "")

    def test_presets_follow_the_language(self) -> None:
        with i18n.use("zh"):
            chinese = {preset.id: get_preset(preset.id) for preset in PRESETS}
        with i18n.use("en"):
            english = {preset.id: get_preset(preset.id) for preset in PRESETS}
        self.assertEqual(sorted(chinese), sorted(english))
        for preset_id, preset in english.items():
            with self.subTest(preset=preset_id):
                assert preset is not None and chinese[preset_id] is not None
                self.assertEqual(_cjk(preset.label), "", f"{preset_id} 的英文 label 漏翻了")
                self.assertEqual(_cjk(preset.notes), "", f"{preset_id} 的英文 notes 漏翻了")
                self.assertTrue(preset.label.strip())
                # 两边都写英文的专有名词（DeepSeek / OpenRouter …）当然可以一样；
                # 中文那边有汉字的必须真的换掉了。
                if _cjk(chinese[preset_id].label):
                    self.assertNotEqual(preset.label, chinese[preset_id].label)
                self.assertNotEqual(preset.notes, chinese[preset_id].notes)
        # 预设清单（前端下拉那一份）走的是同一条路
        with i18n.use("en"):
            for row in presets():
                with self.subTest(row=row["id"]):
                    self.assertEqual(_cjk(row["label"]), "")
                    self.assertEqual(_cjk(row["notes"]), "")

    def test_search_backend_rows_follow_the_language(self) -> None:
        with i18n.use("zh"):
            chinese = net_config.backend_label("bing")
        with i18n.use("en"):
            english = net_config.backend_label("bing")
            for backend_id in net_config.SEARCH_BACKENDS:
                with self.subTest(backend=backend_id):
                    label = net_config.backend_label(backend_id)
                    notes = net_config.backend_notes(backend_id)
                    self.assertTrue(label.strip() and notes.strip())
                    self.assertEqual(_cjk(label), "", f"{backend_id} 的英文 label 漏翻了")
                    self.assertEqual(_cjk(notes), "", f"{backend_id} 的英文 notes 漏翻了")
        self.assertNotEqual(chinese, english)


# --------------------------------------------------------------------------
# 模型发现的报错（`fetch_models`）也要跟着语言走
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class ModelDiscoveryErrorTests(unittest.TestCase):
    URL = "https://api.example.com/v1/models"

    def _error(self, side_effect: object) -> str:
        with mock.patch("harness.providers.store.urllib.request.urlopen", side_effect=side_effect):
            return _failure(lambda: fetch_models("https://api.example.com/v1", "k"))

    def test_unreachable_is_readable_in_both_languages(self) -> None:
        boom = urllib.error.URLError("name resolution failed")
        with i18n.use("zh"):
            self.assertEqual(self._error(boom), f"连不上 {self.URL}：{boom}")
        with i18n.use("en"):
            message = self._error(boom)
            self.assertEqual(message, f"Cannot reach {self.URL}: {boom}")
            self.assertEqual(_cjk(message), "")

    def test_http_error_is_readable_in_both_languages(self) -> None:
        def raise_http(*_args: object, **_kwargs: object) -> None:
            raise urllib.error.HTTPError(
                self.URL, 401, "Unauthorized", {}, io.BytesIO(b'{"error":{"message":"bad key"}}')
            )

        with i18n.use("zh"):
            self.assertEqual(
                self._error(raise_http), 'HTTP 401：{"error":{"message":"bad key"}}'
            )
        with i18n.use("en"):
            message = self._error(raise_http)
            self.assertEqual(message, 'HTTP 401: {"error":{"message":"bad key"}}')
            self.assertEqual(_cjk(message), "")

    def test_not_json_and_empty_are_readable(self) -> None:
        with i18n.use("zh"):
            with mock.patch(
                "harness.providers.store.urllib.request.urlopen",
                lambda *a, **k: _FakeResponse(b"<html>nope</html>"),
            ):
                self.assertEqual(
                    _failure(lambda: fetch_models("https://api.example.com/v1", "k")),
                    f"{self.URL} 返回的不是 JSON",
                )
            with mock.patch(
                "harness.providers.store.urllib.request.urlopen",
                lambda *a, **k: _FakeResponse(b'{"data": []}'),
            ):
                self.assertEqual(
                    _failure(lambda: fetch_models("https://api.example.com/v1", "k")),
                    "该端点没有返回任何模型 id",
                )
        with i18n.use("en"):
            with mock.patch(
                "harness.providers.store.urllib.request.urlopen",
                lambda *a, **k: _FakeResponse(b'{"data": []}'),
            ):
                message = _failure(lambda: fetch_models("https://api.example.com/v1", "k"))
            self.assertEqual(message, "That endpoint returned no model ids")
            self.assertEqual(_cjk(message), "")


# --------------------------------------------------------------------------
# 代码里不留中文字面量
# --------------------------------------------------------------------------


class _LiteralCollector(ast.NodeVisitor):
    """收集字符串常量与 f-string 模板（f-string 的插值位置折成 `{}`），跳过 docstring。"""

    def __init__(self) -> None:
        self.docstrings: set[int] = set()
        self.found: list[tuple[int, str]] = []

    def _mark(self, node: ast.AST) -> None:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                self.docstrings.add(id(body[0].value))

    def visit_Module(self, node: ast.Module) -> None:
        self._mark(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._mark(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._mark(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self.docstrings:
            if CJK.search(node.value):
                self.found.append((node.lineno, node.value))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        text = "".join(
            str(piece.value) if isinstance(piece, ast.Constant) else "{}"
            for piece in node.values
        )
        if CJK.search(text):
            self.found.append((node.lineno, text))
        # 刻意不 generic_visit：片段已经折进上面那一份模板里


def _deliberate(text: str) -> bool:
    """刻意保留的中文字面量（每一条都要能说出理由）。"""
    # 相关性粗判用的**中文泛词表**：与页面文字做 `in` 比较，从不作为文案外发。
    return text in set(net_search._GENERIC_TERMS)


class NoChineseLiteralsTests(unittest.TestCase):
    def _offenders(self, module: str) -> list[str]:
        collector = _LiteralCollector()
        collector.visit(
            ast.parse((ROOT / module).read_text(encoding="utf-8"), filename=module)
        )
        return [
            f"{module}:{line}: {text!r}" for line, text in collector.found if not _deliberate(text)
        ]

    def test_my_modules_keep_no_chinese_string_literals(self) -> None:
        """会发出去的文案一律走 `i18n.t()`（契约 §3）。注释与 docstring 不受影响。"""
        for module in LITERAL_FREE_MODULES:
            with self.subTest(module=module):
                self.assertEqual(self._offenders(module), [], f"{module} 里还有中文字面量")

    def test_the_chinese_generic_terms_are_the_only_exception(self) -> None:
        """`_GENERIC_TERMS` 是**判据**不是文案：拿它跟页面文字比对，从不发给模型。

        这条断言同时钉住"例外只有它一个"：往这张表里加了别的中文词，
        上面那条测试仍然会红（除非它确实是泛词表里的一员）。
        """
        self.assertGreater(len(net_search._GENERIC_TERMS), 20)
        self.assertIn("最新", net_search._GENERIC_TERMS)
        self.assertEqual(self._offenders("harness/net/search.py"), [])


if __name__ == "__main__":
    unittest.main()
