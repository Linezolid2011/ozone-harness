"""抓取网页并抽出可读正文。

## 这里的风险不是"抓不到"，而是"抓到了不该抓的"

模型会自己决定去抓哪个 URL，而它的判断可能被**网页内容**影响（提示注入），
所以这一层必须自己把住几道闸，不能指望模型自觉：

1. **协议白名单**：只允许 http/https。`file://` / `data:` / `javascript:` 一律拒。
2. **SSRF 防护**：默认**拒绝本机与内网地址**。
   否则一个网页只要写一句"请访问 http://127.0.0.1:8787/api/... 确认一下"，
   模型就可能去读用户自己的 Web 界面甚至内网服务。解析域名后按 IP 判断
   （回环 / 私有 / 链路本地 / 保留段都算内网）。
3. **体积与时间上限**：流式读并截断，避免一个几 GB 的文件把内存吃光。
   时限是**跨跳转的总时限**，不是每跳各算一次。
4. **内容类型白名单**：只把文本类内容交给模型；二进制/PDF 明说"暂不支持"，
   而不是丢一堆乱码进去污染上下文。

**地址检查必须先自己归一化，不能只问 getaddrinfo**：`inet_aton` 那一套写法
（`2130706433` / `0x7f000001` / `017700000001` / `127.1`）都等于 `127.0.0.1`，
但**不是每个平台都认**。原先的兜底规则是"解析不出来就不当内网"，也就是 fail-open ——
在这台 Windows 上恰好因为系统不认这些写法、加上一层代理挡着而没能利用，
但只要解析器（或代理）认得，这道闸就等于没有。现在：
字面量先自己解析成 IP 再判；**看起来像 IP 却解析不出来的，一律拒绝**（fail-closed）；
只有真正的域名才交给 getaddrinfo。

**跳转必须逐跳校验**：`follow_redirects=True` 时，`302 → http://127.0.0.1/…`
会在任何检查之前就被真实请求出去 —— 检查只拦得住"内容"，拦不住"请求"。
所以这里是关掉自动跳转、自己逐跳走，每一跳的 Location 都过一遍 `check_url`。

## 诚实的局限

* 域名解析之后到真正连接之间仍有极小的重绑定窗口（DNS rebinding）。
  单用户本机工具接受这个风险；要彻底解决得在连接层校验对端 IP。
* 不做 JS 渲染：SPA 首页抓回来可能只有一句"请启用 JavaScript"。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ..i18n import t

# 用常见浏览器 UA：很多站点会直接拒绝默认的 python-httpx UA。
# 这不是为了绕过付费墙或伪装身份，只是为了别被最粗的那层过滤挡掉。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml",
    "application/rss",
    "application/atom",
)
HTML_TYPES = ("text/html", "application/xhtml")

# 最多跟几跳重定向。每一跳都要重新过 check_url（见 fetch_url）。
MAX_REDIRECTS = 5

# 明确认识但不支持的：给出人话而不是乱码。
# **值是文案 key**，在用到它的那一刻才取（`t()`；语言是运行期可切的）。
KNOWN_BINARY = {
    "application/pdf": "net.fetch.binary.pdf",
    "application/zip": "net.fetch.binary.zip",
    "application/octet-stream": "net.fetch.binary.binary",
}


class NetError(RuntimeError):
    """联网工具的可预期失败（不可达、被拒、类型不支持……）。"""


@dataclass
class FetchedPage:
    url: str                      # 跳转之后的最终地址
    status: int
    content_type: str
    title: str = ""
    text: str = ""
    truncated: bool = False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": self.url,
            "status": self.status,
            "content_type": self.content_type,
            "title": self.title,
            "text": self.text,
            "truncated": self.truncated,
        }
        if self.note:
            payload["note"] = self.note
        if self.extra:
            payload.update(self.extra)
        return payload


# --------------------------------------------------------------------------
# SSRF 防护
# --------------------------------------------------------------------------


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """一个具体地址是否属于"本机/内网"。

    `::ffff:127.0.0.1` 这类 **IPv4 映射地址**要单独看一眼：它在 IPv6 里既不是
    `is_loopback` 也不是 `is_private`，必须取出里面的 IPv4 再判 ——
    否则 `http://[::ffff:127.0.0.1]/` 就能绕过检查（实测过）。
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _parse_component(part: str) -> int | None:
    """按 `inet_aton` 的语义解析一段：`0x` 开头是十六进制，`0` 开头是八进制。"""
    try:
        if part[:2].lower() == "0x":
            return int(part, 16)
        if len(part) > 1 and part.startswith("0"):
            return int(part, 8)
        return int(part, 10)
    except ValueError:
        return None


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """解析非规范的 IPv4 写法，认不出来返回 None。

    `inet_aton` 允许的几种都在这儿：单个整数（`2130706433`）、十六进制
    （`0x7f000001`）、八进制（`017700000001`）、以及不足四段的短写（`127.1`）——
    后者的最后一段会"吃掉"剩下的字节数。这些都是 `127.0.0.1` 的合法写法。
    """
    if not host:
        return None
    parts = host.split(".")
    if len(parts) > 4 or any(part == "" for part in parts):
        return None

    values: list[int] = []
    for part in parts:
        value = _parse_component(part)
        if value is None:
            return None
        values.append(value)

    if len(values) == 1:
        if values[0] > 0xFFFFFFFF:
            return None
        return ipaddress.IPv4Address(values[0])

    head, tail = values[:-1], values[-1]
    if any(value > 0xFF for value in head):
        return None
    remaining = 4 - len(head)
    if tail >= (1 << (8 * remaining)):
        return None
    total = 0
    for value in head:
        total = (total << 8) | value
    total = (total << (8 * remaining)) | tail
    return ipaddress.IPv4Address(total)


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """能当成地址字面量就返回地址（含非规范写法），否则返回 None。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _parse_legacy_ipv4(host)


# 域名不可能"全是数字与点"（顶级域不允许纯数字），所以这些判断不会误伤正常域名。
_ALL_DIGITS_AND_DOTS = re.compile(r"[0-9.]+")
_HEX_NUMBER = re.compile(r"0[xX][0-9a-fA-F]+")
_OCTAL_NUMBER = re.compile(r"0[0-7]+")


def _looks_numeric(host: str) -> bool:
    """这个主机名看起来是"某种 IP 写法"，而不是域名。"""
    return bool(
        _ALL_DIGITS_AND_DOTS.fullmatch(host)
        or _HEX_NUMBER.fullmatch(host)
        or _OCTAL_NUMBER.fullmatch(host)
    )


def _is_private_address(host: str) -> bool:
    """**域名**解析后是否指向本机/内网。

    解析不出来（DNS 失败）**不当作内网** —— 那是"连不上"，交给真正的请求去报错。
    注意这条 fail-open 只对**域名**成立；看起来像 IP 的写法不走这里（见 check_url）。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_private_ip(ip):
            return True
    return False


# 没有 "://" 但明确是危险协议的，必须直接拒
_DANGEROUS_SCHEMES = frozenset(
    {"javascript", "data", "file", "vbscript", "about", "blob", "chrome", "ms-its"}
)


def check_url(url: str, *, allow_private: bool = False) -> str:
    """校验并归一化 URL。不合法就抛 NetError（消息要能直接给模型看）。

    拒绝内网地址的那句话（`net.fetch.private_hint`）两处共用，
    免得改了一处漏一处 —— 但它是**现取**的：`t()` 每次调用都查当前语言。
    """
    raw = (url or "").strip()
    if not raw:
        raise NetError(t("net.fetch.empty_url"))
    if "://" not in raw:
        # 没有 "://" 时不能一律加 https:// —— 否则 `javascript:alert(1)`
        # 会变成 `https://javascript:alert(1)`，scheme 检查形同虚设。
        head = raw.split(":", 1)[0].strip().lower()
        if head in _DANGEROUS_SCHEMES or not head:
            if head:
                raise NetError(t("net.fetch.bad_scheme", scheme=head))
            raise NetError(t("net.fetch.bad_scheme_unnamed"))
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        if parsed.scheme:
            raise NetError(t("net.fetch.bad_scheme", scheme=parsed.scheme))
        raise NetError(t("net.fetch.bad_scheme_unnamed"))
    host = parsed.hostname or ""
    if not host:
        raise NetError(t("net.fetch.no_host", url=url))

    if not allow_private:
        literal = _parse_ip_literal(host)
        if literal is not None:
            if _is_private_ip(literal):
                raise NetError(t("net.fetch.private_hint", host=host))
        elif _looks_numeric(host):
            # fail-closed：长得像地址、我们却解不出来。
            # 放行它就是在赌"对端解析器也不认"—— 而 `2130706433` / `0x7f000001` /
            # `127.1` 这三种在别的平台上恰恰是认的，那时这道闸就等于没有。
            raise NetError(t("net.fetch.numeric_host", host=host))
        elif _is_private_address(host):
            raise NetError(t("net.fetch.private_hint", host=host))
    return raw


# --------------------------------------------------------------------------
# 正文抽取
# --------------------------------------------------------------------------


def charset_of(content_type: str) -> str:
    """从 Content-Type 里抠出 charset。"""
    lowered = (content_type or "").lower()
    if "charset=" not in lowered:
        return ""
    return lowered.split("charset=", 1)[1].split(";")[0].strip().strip('"')


def html_to_text(body: bytes | str, *, charset: str = "") -> tuple[str, str]:
    """HTML → (标题, 正文)。

    `charset` 由调用方从 HTTP 头里取。**必须自己先解码**：
    lxml 只认 HTML 里的 `<meta charset>`，不认 HTTP 头，
    而中文站点经常只在一处声明 —— 拿 HTTP 头的 utf-8 内容当 bytes 交给 lxml，
    标题会变成 `æµè¯é¢` 这种乱码（测试里踩过）。

    正文优先用 trafilatura（质量最好）；它对**非文章页**（首页、列表页）
    可能返回 None，那就退回到"去掉脚本样式后的全文"。两级回退比单级可靠。
    """
    if charset and isinstance(body, bytes):
        try:
            body = body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass

    title = ""
    try:
        from lxml import html as lxml_html

        # 直接交给 lxml：str 它按原样解析，bytes 它自己去猜。
        # **不要**把已经解码好的 str 再 encode 成 bytes —— 那样等于把上面
        # 用 HTTP 头 charset 做的解码又撤销了，标题会重新变乱码。
        doc = lxml_html.fromstring(body)
        titles = doc.xpath("//title//text()")
        if titles:
            title = " ".join("".join(titles).split())[:300]
    except Exception:  # noqa: BLE001 - 标题拿不到不该影响正文
        title = ""

    text = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            body,
            include_comments=False,
            include_tables=True,
            favor_recall=True,   # 宁可多留一点，也别把正文判成样板文字
        )
        text = (extracted or "").strip()
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - 抽取器内部异常不该让整个工具挂掉
        text = ""

    if not text:
        text = _fallback_text(body)
    return title, text


def _fallback_text(body: bytes | str) -> str:
    """兜底：扔掉脚本/样式/导航，剩下的文本按行拼起来。"""
    try:
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(body)
        for bad in doc.xpath("//script | //style | //noscript | //template"):
            bad.getparent().remove(bad)
        parts = [line.strip() for line in doc.text_content().splitlines()]
        return "\n".join(line for line in parts if line)
    except Exception:  # noqa: BLE001
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return body


def decode_text(body: bytes, content_type: str) -> str:
    """非 HTML 的文本：按声明的字符集解码，没声明就依次猜。

    顺序是**实验换来的**：
      1. HTTP 头声明的 charset；
      2. utf-8 严格模式（现代站点绝大多数是它）；
      3. charset-normalizer —— 但它**吃样本长度**：
         12 字节的 GBK 会被猜成 `cp949`（韩语），解出来是 `櫓匡꿎桿`；
         60 字节以上才稳定猜对 gb18030。所以短样本直接跳过它；
      4. gb18030：中文场景下"最后一个合理猜测"。它几乎不会解码失败，
         所以只能放在最后 —— 放前面会把 Big5/Shift-JIS 也"解成功"成乱码；
      5. utf-8 带替换字符兜底（至少不会抛异常）。
    """
    charset = charset_of(content_type)
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    if len(body) >= 64:
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(body).best()
            if best is not None:
                return str(best)
        except Exception:  # noqa: BLE001
            pass
    try:
        return body.decode("gb18030")
    except (LookupError, UnicodeDecodeError):
        pass
    return body.decode("utf-8", errors="replace")


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


# --------------------------------------------------------------------------
# 进程内复用的 HTTP 客户端
#
# 为什么要有这一层：在 Windows 上建一个 `httpx.Client()` 实测要 **1.6 秒**
# （2026-10-17 本机；SSL 上下文 + 连接池初始化）。每次抓取都新建一个，
# 就等于每次抓取白扔 1.6 秒 —— 而且那 1.6 秒还曾被算进"抓取总时限"里，
# 让小预算的抓取（以及测试）偶发报出一个"没碰到网络的网络超时"（ADR-090）。
# 复用之后它只在**进程第一次抓取**时付一次，顺带把连接也留下：
# 抓同一个站点的第二个页面不用重新握手。
#
# 这里没有用 httpx 的 `mounts=` 来"只让回环直连"：那要额外建三个 transport，
# 实测一起要 **4.4~6.7 秒**，比省下来的还多。两个客户端（走代理 / 不走代理）
# 各按需建一次，成本更低，也更好解释。
# --------------------------------------------------------------------------

_CLIENTS: dict[str, Any] = {}
_CLIENTS_LOCK = threading.Lock()

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _is_loopback_target(url: str) -> bool:
    """这个 URL 指向**本机**吗？（`127.0.0.0/8`、`::1`、`localhost`）

    **只看字面量，不做 DNS。** 这里要回答的是"该不该让系统代理去连它"，
    而不是"它是不是内网"。一个解析后落到 `127.0.0.1` 的**域名**仍然按公网目标
    处理（走代理、由代理去解析）—— 那条已知局限记在 ADR-090 里。
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost", "localhost.") or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _client_for(httpx_module: Any, *, direct: bool) -> Any:
    """取一个**进程内复用**的 httpx 客户端（懒建 + 双检锁，同 `ocr._engine()`）。

    `direct=True` 的那个 `trust_env=False`：**本机地址绝不该经过系统代理**。
    httpx 默认会读系统代理（Windows 上取注册表），而那份列表通常**没有**回环地址
    的绕过规则 —— 实测一次 `127.0.0.1` 的抓取被代理绕一圈要 **2.1~3.9 秒**
    （本该是毫秒）。公网目标照旧走代理：这台机器访问外网**靠**它。

    **两个客户端都不关**（托管到进程结束）：它们的全部价值就在于"活到下次抓取"。
    代价是系统代理改了要重启程序才生效 —— 与"接口档案改了要重开一次会话"是同一类
    取舍，写在这里免得以后当成漏了。
    """
    key = "direct" if direct else "env"
    client = _CLIENTS.get(key)
    if client is not None:
        return client
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)  # 双检：等锁的时候别人可能已经建好了
        if client is None:
            client = httpx_module.Client(
                follow_redirects=False,
                headers=_HEADERS,
                trust_env=not direct,
            )
            _CLIENTS[key] = client
    return client


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def fetch_url(
    url: str,
    *,
    timeout: float = 20.0,
    max_chars: int = 8000,
    max_bytes: int = 2_000_000,
    allow_private: bool = False,
) -> FetchedPage:
    """抓一个 URL 并抽出可读正文。失败抛 NetError（消息可直接展示）。"""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise NetError(t("net.dep.httpx")) from exc

    target = check_url(url, allow_private=allow_private)

    try:
        # **必须关掉自动跳转**：开着的话 `302 → http://127.0.0.1/…` 会在任何校验
        # 之前就被真实请求出去，校验只能拦住"把内容交给模型"，拦不住"请求本身"。
        # 所以自己逐跳走，每一跳的 Location 都过一遍 check_url。
        #
        # 客户端是**进程内复用**的（见上面的 `_client_for`）。第一次取它会花 1.6 秒，
        # 所以先把它拿到手、**再**开始计时：文档承诺的"20 秒上限"讲的是抓取本身，
        # 不是我们自己的初始化（ADR-090）。
        client = _client_for(httpx, direct=_is_loopback_target(target))
        deadline = time.monotonic() + timeout
        response = None
        for hop in range(MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetError(t("net.fetch.timeout_total", seconds=f"{timeout:g}"))
            # 跳转可能把"公网 → 本机"（或反过来），所以每一跳重新选一次客户端。
            # 两个客户端最多各建一次，这里通常只是一个字典查询。
            client = _client_for(httpx, direct=_is_loopback_target(target))
            request = client.build_request("GET", target, timeout=remaining)
            response = client.send(request, stream=True)
            location = response.headers.get("location")
            if not (response.is_redirect and location):
                break
            response.close()
            if hop == MAX_REDIRECTS:
                raise NetError(
                    t("net.fetch.too_many_redirects", max_redirects=MAX_REDIRECTS)
                )
            target = check_url(
                urllib.parse.urljoin(target, location), allow_private=allow_private
            )
        assert response is not None  # 上面的循环至少执行一次
        try:
            content_type = (response.headers.get("content-type") or "").lower()
            status = response.status_code
            chunks: list[bytes] = []
            total = 0
            truncated_bytes = False
            for chunk in response.iter_bytes():
                # **读正文时也必须看总时限。**
                # httpx 的 read timeout 是"每一次读操作"的上限，不是整体上限：
                # 一个每 0.4 秒吐 1 字节的服务器永远能喂饱每一次读，
                # 读超时全程不触发（实测 timeout=1.0 时一路读到 4.9 秒）。
                # 结果是"总时限"名存实亡，整轮对话卡死在工具调用里。
                #
                # 这个检查能保证什么、不能保证什么（别把它当成硬上限）：
                #   * 只要对端**还在吐字节**，最坏也只是"超时 + 一个块的间隔"
                #     （下面每收到一块就查一次）；
                #   * 对端彻底不动时这里根本没机会执行，最后收手的是 httpx
                #     那次读的 read timeout（发起请求时设成了 remaining），
                #     所以最坏情况约为 timeout + 一个 read timeout（≈2×timeout），
                #     而不是无限等待。要压得更紧得在连接层重设超时，代价不小。
                if time.monotonic() > deadline:
                    raise NetError(t("net.fetch.body_timeout", seconds=f"{timeout:g}"))
                # `iter_bytes()` 的一块可以比剩余额度大得多；先切到边界再落进列表，
                # 否则 max_bytes 只是"停止读取的触发点"，并不是返回体的硬上限。
                remaining_bytes = max(0, max_bytes - total)
                if len(chunk) > remaining_bytes:
                    chunk = chunk[:remaining_bytes]
                    truncated_bytes = True
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    truncated_bytes = True
                    break
            body = b"".join(chunks)
            final_url = str(response.url)
        finally:
            # client.send(stream=True) 拿到的响应是流式的，必须自己关
            response.close()
    except httpx.HTTPStatusError as exc:
        raise NetError(t("net.fetch.http_error", status=exc.response.status_code)) from exc
    except httpx.TimeoutException as exc:
        raise NetError(t("net.fetch.timeout", seconds=f"{timeout:g}")) from exc
    except httpx.HTTPError as exc:
        raise NetError(
            t("net.fetch.failed", kind=type(exc).__name__, detail=exc)
        ) from exc

    if status >= 400:
        raise NetError(t("net.fetch.http_error", status=status))

    page = FetchedPage(url=final_url, status=status, content_type=content_type)
    if truncated_bytes:
        page.truncated = True
        page.note = t("net.fetch.truncated_bytes", max_bytes=max_bytes)

    if any(kind in content_type for kind in HTML_TYPES) or (not content_type and b"<html" in body[:2000].lower()):
        page.title, text = html_to_text(body, charset=charset_of(content_type))
    elif any(kind in content_type for kind in TEXT_TYPES):
        text = decode_text(body, content_type)
    else:
        for kind, reason_key in KNOWN_BINARY.items():
            if kind in content_type:
                page.note = (page.note + " " + t(reason_key)).strip()
                page.text = ""
                return page
        raise NetError(
            t(
                "net.fetch.unsupported_type",
                content_type=content_type or t("net.fetch.undeclared_type"),
            )
        )

    page.text, truncated = _truncate(text.strip(), max_chars)
    if truncated:
        page.truncated = True
        note = t("net.fetch.truncated_chars", max_chars=max_chars)
        page.note = (page.note + " " + note).strip()
    if not page.text:
        page.note = (page.note + " " + t("net.fetch.no_body")).strip()
    return page
