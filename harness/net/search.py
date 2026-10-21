"""关键词搜索引擎后端。

## 为什么默认是 Bing（这一段是实测结论，不是偏好）

这台机器在中国大陆。开工前把候选引擎逐个连了一遍：

| 引擎 | 结果 |
|---|---|
| `bing.com` / `cn.bing.com` | ✅ 可达，中英文都能查 |
| `mojeek.com` | ⚠️ 首页可达，但 `/search` 返回 **403** |
| `sogou.com` | ⚠️ 200 但只有 5KB，是反爬验证页 |
| `so.com`（360） | ⚠️ 有真实结果，但只有 7 条且链接多是 `so.com/link?m=…` 跳转 |
| `google.com` / `duckduckgo`（html+lite）/ `brave` / `yahoo` / `startpage` / `searx.be` | ❌ 全部无路由（`WinError 10051`） |
| `api.tavily.com` | ✅ 可达（401 = 端点活着，等 key） |

所以：
* **默认 `bing`**：唯一一个既可达又不需要 key、还能干净解析的；
* **可选 `tavily`**：专为 LLM 设计的搜索 API，国内可达，有免费额度 —— 想要更好的结果就填 key；
* **`ddgs`（DuckDuckGo 库）被排除**：它只打 DDG 和 Google，在这台机器上完全用不了。
  依赖装了却用不了，比不装更糟。

## 已知脆弱点

抓 HTML 就是在跟人家的页面结构赛跑：Bing 改版就会失效。
所以解析器写得**防御性**（选择器多层回退、拿不到就返回空而不是抛异常），
并且后端是可换的 —— 真坏了换个 `backend` 就行。
"""

from __future__ import annotations

import base64
import binascii
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ..i18n import t

from .fetch import USER_AGENT, NetError

BING_SEARCH_URL = "https://www.bing.com/search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SO360_SEARCH_URL = "https://www.so.com/s"


# 泛词：出现在几乎任何页面上，拿它们判断相关性等于没判断。
# 这串是踩出来的：查询「上海生活垃圾分类最新的官方政策」时，
# 降级结果里的上海旅游页恰好含「上海」+「最新」（"2025年最新旅行数据"），
# 于是被判成"相关"，回退**没触发**，模型就拿着无关结果继续乱试。
_GENERIC_TERMS = (
    "最新", "新的", "官方", "什么", "怎么", "如何", "哪些", "哪个", "哪里", "为什么",
    "介绍", "相关", "信息", "内容", "问题", "方法", "推荐", "攻略", "可以", "进行",
    "以及", "一个", "这个", "那个", "我们", "你们", "他们", "现在", "今天", "明天",
    "昨天", "多少", "图片", "视频", "网站", "首页", "大全", "下载",
)


def _terms(query: str) -> list[str]:
    """把查询切成用于"相关性粗判"的词。

    中文没有空格，所以取**二元组**当词（"垃圾分类" → 垃圾/圾分/分类）；
    英文按空格取长度 ≥3 的词。**与泛词重叠的二元组会被丢掉** ——
    否则"最新""官方"这种词会让任何页面看起来都相关。
    这是个粗糙的启发式，只用来判断"返回的东西跟问题是不是完全没关系"。
    """
    import re

    query = query.strip()
    found: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(chunk) == 1:
            found.append(chunk)
        else:
            found.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    found.extend(word.lower() for word in re.findall(r"[A-Za-z0-9]{3,}", query))
    # 去重但保序，同时丢掉泛词
    seen: set[str] = set()
    unique: list[str] = []
    for term in found:
        if term in seen or any(generic in term for generic in _GENERIC_TERMS):
            continue
        seen.add(term)
        unique.append(term)
    return unique[:12]


def looks_relevant(query: str, results: list[SearchResult]) -> bool:
    """结果跟关键词是不是**明显无关**。

    为什么要这一条：抓取型后端会被"降级对待"。实测 bing.com 对
    「上海 垃圾分类 政策」返回的全是上海**旅游**页面，页面里还写着
    「为回应符合本地法律要求的通知，部分搜索结果未予显示」——
    也就是**合规过滤**。这种结果喂给模型比"没有结果"更糟：
    它会以为"网上就这些"，然后开始猜 URL（真实发生过的翻车）。

    判据：查询词里至少**两个**在结果的标题+摘要里出现过（单词查询放宽到一个）。
    宁可漏判（把无关当有关），也不要误判（把有关当无关）。
    """
    terms = _terms(query)
    if not terms:
        return True
    if not results:
        return False
    blob = " ".join(f"{item.title} {item.snippet}" for item in results).lower()
    hits = sum(1 for term in terms if term in blob)
    need = 1 if len(terms) == 1 else 2
    return hits >= need


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _client(timeout: float):
    """统一的 httpx 客户端（延迟导入，缺依赖时给得出人话）。"""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise NetError(t("net.dep.httpx")) from exc
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def bing_target_url(href: str) -> str:
    """Bing 有时把结果包成 `bing.com/ck/a?...&u=a1<base64url>`，这里解回真实地址。

    解不开就原样返回 —— 上层（web_open_url）会跟随跳转，所以不至于坏掉，
    只是模型看到的链接难看一点。
    """
    if "bing.com/ck/a" not in href:
        return href
    try:
        query = urllib.parse.urlparse(href).query
        raw = urllib.parse.parse_qs(query).get("u", [""])[0]
    except ValueError:
        return href
    if not raw.startswith("a1"):
        return href
    payload = raw[2:]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return href
    return decoded if decoded.startswith("http") else href


def _search_bing(
    query: str, count: int, timeout: float, api_key: str = ""
) -> list[SearchResult]:
    with _client(timeout) as client:
        response = client.get(BING_SEARCH_URL, params={"q": query, "count": max(count * 2, 10)})
        response.raise_for_status()
        return parse_bing_html(response.text, count)


def _search_tavily(
    query: str, count: int, timeout: float, api_key: str = ""
) -> list[SearchResult]:
    if not api_key.strip():
        raise NetError(t("net.search.tavily.no_key"))
    payload = {
        "query": query,
        "max_results": count,
        "search_depth": "basic",
        # 老版本 API 从 body 读 key，新版本读 Authorization 头 —— 两个都给，省得踩版本差异
        "api_key": api_key.strip(),
    }
    with _client(timeout) as client:
        response = client.post(
            TAVILY_SEARCH_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
        )
        if response.status_code == 401:
            raise NetError(t("net.search.tavily.rejected"))
        response.raise_for_status()
        data: Any = response.json()
    return parse_tavily_json(data, count)


def _backends() -> dict[str, Any]:
    """后端查表。**所有后端签名一致**（多一个用不上的 api_key 参数）。

    刻意做成函数而不是模块级常量：这样后端的定义顺序随便排，
    也不会出现"表里引用了一个还没定义的函数"这种启动即崩的问题。

    签名统一是为了让 `search()` 只有**一条**分派路径 ——
    早先 tavily 因为要传 key 而走了特例分支，结果"表"和"特例"并存，
    测试里 mock 表也拦不住那次调用（踩过）。
    """
    return {"bing": _search_bing, "tavily": _search_tavily, "360": _search_360}


def search(
    query: str,
    *,
    count: int = 5,
    backend: str = "bing",
    api_key: str = "",
    timeout: float = 20.0,
) -> tuple[list[SearchResult], str, str]:
    """跑一次搜索，返回 (结果, 用的后端, 说明)。

    **只用你指定的那一个后端，程序不会自动换引擎。**
    这条是刻意的：换后端是用户的决定（设置 → 工具），不是程序偷偷做的。
    早先写过一版"主后端结果无关就自动换另一个引擎、甚至升级到 Tavily"，
    被否掉了，理由很直白 —— 用户明明选了免 key 的抓取型后端，
    程序却可能悄悄花掉他的 Tavily 额度；而且这跟"静默覆盖用户配置"是同一类毛病。

    结果与关键词明显无关时（抓取型后端会被"降级对待"），
    说明（note）里会讲清楚，让模型**如实告诉用户"没搜到"**，
    用户可以自己换个后端再试。这比程序替他做主更诚实。
    """
    query = (query or "").strip()
    if not query:
        raise NetError(t("net.search.empty_query"))
    backend_map = _backends()
    chosen = (backend or "bing").strip().lower()
    if chosen not in backend_map:
        raise NetError(t("net.search.unknown_backend", backend=backend))
    count = max(1, min(int(count or 5), 10))

    results = backend_map[chosen](query, count, timeout, api_key)
    if looks_relevant(query, results):
        return results, chosen, ""
    return (
        results,
        chosen,
        # **现取**：语言是运行期可切的，这句又是给模型的行为指令。
        t("net.search.irrelevant", backend=chosen),
    )


def _search_360(
    query: str, count: int, timeout: float, api_key: str = ""
) -> list[SearchResult]:
    """360 搜索（so.com）。中文内容比 Bing 全 —— 因为它是国内引擎，不被同一套过滤。

    代价：外部结果的链接是 `so.com/link?m=…` **跳转链接**，而那个跳转页
    在没有 cookie 时返回 400，解不出真实地址。所以这些结果的
    **摘要**往往才是可用信息（实测摘要里常直接包含答案）。
    """
    with _client(timeout) as client:
        response = client.get(SO360_SEARCH_URL, params={"q": query})
        response.raise_for_status()
        return parse_360_html(response.text, count)


def parse_360_html(body: str, count: int = 5) -> list[SearchResult]:
    """从 360 结果页 HTML 里抽结果（**离线可测**）。"""
    doc = _document(body)
    if doc is None:
        return []
    results: list[SearchResult] = []
    for item in doc.xpath('//li[contains(@class,"res-list")]'):
        anchors = item.xpath(".//h3//a")
        if not anchors:
            continue
        anchor = anchors[0]
        href = (anchor.get("href") or "").strip()
        title = _clean("".join(anchor.itertext()))
        if not href.startswith("http") or not title:
            continue
        parts = item.xpath(".//p//text()")
        snippet = _clean(" ".join(parts))[:400]
        if "so.com/link" in href:
            # 解不开的跳转链接：如实标注，免得模型以为那是个能打开的页面
            snippet = (snippet + t("net.search.so360.redirect_note")).strip()
        results.append(SearchResult(title=title, url=href, snippet=snippet))
        if len(results) >= count:
            break
    return results


def _document(body: str):
    """把 HTML 解析成 lxml 文档；**解析不了就返回 None**。

    空响应体（有些站点会回 200 + 空内容）会让 lxml 直接抛
    `ParserError: Document is empty`。抓取型后端必须能承受这个 ——
    "对方改版/抽风"是常态，不能让它把整轮对话打挂。
    """
    from lxml import html as lxml_html

    if not (body or "").strip():
        return None
    try:
        return lxml_html.fromstring(body)
    except Exception:  # noqa: BLE001 - 任何解析失败都按"没结果"处理
        return None


def parse_bing_html(body: str, count: int = 5) -> list[SearchResult]:
    """从一段 Bing 结果页 HTML 里抽结果（**离线可测**，不联网）。"""
    doc = _document(body)
    if doc is None:
        return []
    results: list[SearchResult] = []
    for item in doc.xpath('//li[contains(@class,"b_algo")]'):
        anchors = item.xpath(".//h2//a")
        if not anchors:
            continue
        anchor = anchors[0]
        href = (anchor.get("href") or "").strip()
        title = _clean("".join(anchor.itertext()))
        if not href.startswith("http") or not title:
            continue
        snippet_parts = item.xpath('.//div[contains(@class,"b_caption")]//p//text()') or item.xpath(
            ".//p//text()"
        )
        results.append(
            SearchResult(
                title=title,
                url=bing_target_url(href),
                snippet=_clean(" ".join(snippet_parts))[:400],
            )
        )
        if len(results) >= count:
            break
    return results


def parse_tavily_json(data: dict[str, Any], count: int = 5) -> list[SearchResult]:
    """从 Tavily 的响应里抽结果（**离线可测**）。"""
    results: list[SearchResult] = []
    for item in (data.get("results") or [])[:count]:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            SearchResult(
                title=_clean(str(item.get("title") or url)),
                url=url,
                snippet=_clean(str(item.get("content") or ""))[:400],
            )
        )
    return results
