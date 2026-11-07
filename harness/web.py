"""Web 端：零依赖 HTTP 服务 + SSE 流式对话（M5）。

## 为什么还是零依赖

ADR-001 定了"不引入第三方包"，M5 继续遵守：用标准库 `http.server`，
前端是原生 HTML/CSS/JS（无构建步骤）。收益很直接 ——
`run.cmd --web` 就能跑，不需要 venv、不需要 npm install，
几个同学谁都不用先折腾环境。代价是我手写 HTTP 管道（无 WebSocket、无自动 API 文档）。

真要换 FastAPI，只需把本文件替换掉：**前端只认 HTTP + SSE，一行都不用改**。

## 安全默认值

默认只监听 `127.0.0.1`。这不是随便定的 —— 记忆库里存的是用户画像，
把它绑到 `0.0.0.0` 等于把聊天记录和记忆暴露给整个局域网。
要对外提供必须显式 `--host 0.0.0.0`，并且那时应当先加鉴权。

## 端点

    GET  /                       前端页面
    GET  /api/config             当前模型/思考档位 + 界面语言（ADR-085）
    GET  /api/settings/language  界面语言的当前值与可选清单
    POST /api/settings/language  切界面语言（只改语言，不动别的设置）
    GET  /api/prompts            设置 → 提示词：全部提示词与工具定义（只读、不写盘）
    POST /api/prompts/update     保存一条用户覆盖（按语言分家）
    POST /api/prompts/revert     复原一条（给 id）或一门语言的全部（不给 id）
    GET  /api/sessions           会话列表
    GET  /api/sessions/<id>      会话消息（由事件日志重放得到）
    GET  /api/memories           记忆库
    POST /api/memories/update    编辑一条记忆
    POST /api/memories/delete    删除一条记忆（软删除）
    POST /api/memories/limit     改记忆条数上限（ADR-057）
    GET  /api/vault              背景档案：目录树 + README 预算 + 结构校验 + 待整理数
    GET  /api/vault/file?path=…  一个档案的全文 + 元信息 + 邻域（界面上点开看）
    POST /api/dream/format       格式化并初始化 dream 层（**不可逆**，要 confirm 确认词）
    GET  /api/dreams             整理运行列表（记录在 data/dreams/*.json）
    GET  /api/dreams/<id>        某次运行的逐文件改动 + 结构校验（**不可回滚**，ADR-065）
    GET  /api/usage              用量汇总
    POST /api/attachments        上传一张图片（JSON + base64，落盘到 data/attachments/）
    GET  /api/attachments        取回一张图片（按内容哈希寻址的字节流）
    POST /api/chat               SSE：流式对话
    POST /api/dream              SSE：流式整理（会话记录 → 背景档案）

## 请求校验（别把本机服务当成"反正只有我能访问"）

* **会话 id 一律走 `_session_path()`**。它先用白名单正则，再用
  `is_relative_to(sessions_dir)` 兜一层 —— 因为 id 是从 URL / body 里来的，
  直接拿去拼路径就是路径穿越：id 里只要带 ".." 和路径分隔符（Windows 上把
  分隔符换成反斜杠效果一样），`sessions_dir / f"{id}.jsonl"` 就会落到数据目录
  外面，并且**创建 / 追加 / 删除**那个文件。不合法一律 400，且不碰磁盘。
* **默认回环服务先验 Host**（`_host_rejection()`）：GET / POST 都只接受
  回环 IPv4、`localhost`、回环 IPv6 与当前监听端口；不做 DNS，避免重绑定。
  显式非回环 `--host` 保持既有托管行为。
* **改状态的 POST 先过同源闸**（`_post_rejection()`）：带了 `Origin`
  就必须和本站 Host 一致，带了 `Sec-Fetch-Site: cross-site` 就拒绝。
  为什么必须做：本服务默认无鉴权，一个网页就能跨站 POST `/api/reset`，
  把会话、记忆和 API key 一次抹掉。浏览器跨站发 POST 一定会带 `Origin`
  （表单也一样），所以这道闸挡得住；而两个头都没有 = 脚本 / CLI（curl、
  urllib、本项目测试），照旧放行。
* **JSON 接口要求 `Content-Type: application/json`**：表单能造出的类型只有
  urlencoded / multipart / text/plain，而自定义 Content-Type 的跨站请求
  会先触发 CORS 预检 —— 我们根本不回预检，浏览器自己就会拦下来。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import PRODUCT_NAME, PRODUCT_SLUG, attachments, version
from . import i18n
from .config import ROOT, Settings, display_path
from .dream import DreamRunner, DreamRunStore
from .dream_records import pending_sessions, summarize_pending
from .i18n import SETTINGS_FILE, LanguageStore
from .events import (
    EventLog,
    delete_session,
    fork_session,
    last_summary,
    list_sessions,
    new_session_id,
    read_events,
    session_display_title,
    session_generation_settings,
    session_start_info,
    session_tools,
    session_usage,
    session_usage_by_provider,
)
from .llm_types import Usage
from .memory import open_memory
from .net.config import TOOLS_FILE, ToolStore
from .net.search import search as run_search
from .prompt_view import describe_prompts
from . import prompts as prompt_store
from .providers.base import ProviderError
from .providers.store import (
    VISION_SOURCE_BUILTIN,
    VISION_SOURCE_PROFILE,
    ProviderProfile,
    fetch_models,
    get_preset,
    parse_token_limit,
    vision_support,
)
from .reset import UnsafeDataDirError, perform_reset, preview_reset
from .runtime import Runtime, provider_store, resolve_dream_provider, resolve_provider
from .session_ids import SESSION_ID_PATTERN, session_log_path
from .venv_setup import SetupError, VenvSetupManager
from .vault import (
    GENERATED_RELS,
    INDEX_NAME,
    LOG_NAME,
    MEMORY_INSTRUCTION_NAME,
    OBSERVATIONS_NAME,
    README_NAME,
    SECTIONS_CONFIG,
    Frontmatter,
    VaultEntry,
    VaultExistsError,
    VaultPathError,
    VaultStore,
    parse_frontmatter,
)

WEB_DIR = ROOT / "web"


def _t(key: str, /, **kwargs: Any) -> str:
    """发给界面的那句话，**在用到它的这一刻**取（语言是运行期可切的）。

    这一层只是把 `i18n.t` 起个短名字：本文件里每一句给人看的文案都要走它，
    所以调用的地方短一点更好读（`docs/12-i18n.md` §3）。
    """
    return i18n.t(key, **kwargs)


# SESSION_ID_PATTERN 仍从这里 re-export，保留已有调用方；真正的白名单与路径
# 包含关系只在 session_ids.py 实现，Web 与 Runtime 不会再各写一份。


def _short_session_id(session_id: Any, limit: int = 64) -> str:
    """回显给用户的 id 片段：截断 + 转义，别让一个超长 id 撑爆错误信息。"""
    return repr(str(session_id)[:limit])


def _session_path(settings: Settings, session_id: Any) -> Path | None:
    """Web 的 None-on-error 包装；白名单与路径包含关系由共享实现负责。"""
    try:
        return session_log_path(settings.sessions_dir, session_id)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 图片附件的对外形状（契约 §7.1 / §7.2 / §7.4）
# --------------------------------------------------------------------------


def attachment_url(session_id: str, note_id: str, ext: str) -> str:
    """一张附件的取图 URL。**全项目只有这一处拼它**（契约 §7.1 与 §7.4）。

    上传响应与会话视图都调这个函数。两处各拼一次的话，改一处忘一处的表现是
    "缩略图 404、而消息照样发出去了" —— 那种坏法最难查，因为两边各自看起来都对
    （这一条是契约 §7.1 补 `url` 字段时明确写下的理由）。

    三个参数在到达这里之前都过了白名单（会话 id / 内容哈希 / 扩展名），
    所以不需要再做百分号编码。
    """
    return f"/api/attachments?session={session_id}&id={note_id}&ext={ext}"


def _attachment_ext(record: dict[str, Any]) -> str:
    """附件的扩展名：**落盘名**（内容哈希.ext）是唯一真相，不从别处猜。"""
    pure = PurePosixPath(str(record.get("file") or "").replace("\\", "/"))
    return pure.suffix.lstrip(".").lower()


def _first_int(*values: Any) -> int:
    """取第一个能转成整数的值，全都不行就是 0（与 attachments._as_int 同口径）。"""
    for value in values:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _attachment_view(session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """契约 §7.4 的形状：界面显示 + `url` 取图，不含 `file` 这类内部字段。

    `width/height/bytes` 取**落盘那一版**（`stored_*`），也就是用户在历史气泡里
    看到、并且真正发给模型的那张图；原始尺寸是人看的另一套数字，界面不需要。

    `ocr`（契约 §13.3）：只有"多模态关着、走了本地 OCR"的那一轮才有。
    **有就原样带出来**（含 `text`/`engine`/`seconds`/`chars`，或失败的 `error`）——
    §13.7 第 3 条要求用户在任何时候都能看见"模型当时到底收到了什么"，
    刷新页面之后也得看得见，所以这份数据必须出现在会话视图里。
    """
    note_id = str(record.get("id") or "")
    view: dict[str, Any] = {
        "id": note_id,
        "name": str(record.get("name") or ""),
        "mime": str(record.get("mime") or ""),
        "width": _first_int(record.get("stored_width"), record.get("width")),
        "height": _first_int(record.get("stored_height"), record.get("height")),
        "bytes": _first_int(record.get("stored_bytes"), record.get("bytes")),
        "url": attachment_url(session_id, note_id, _attachment_ext(record)),
    }
    ocr = record.get("ocr")
    if isinstance(ocr, dict):
        result: dict[str, Any] = {
            "engine": str(ocr.get("engine") or ""),
            "seconds": ocr.get("seconds"),
            "chars": _first_int(ocr.get("chars")),
            "text": str(ocr.get("text") or ""),
        }
        if ocr.get("error"):
            # 契约 §13.3：失败的 OCR 只记 error，**不写半截 text**。界面据此显示
            # "这张图没识别成文字"，而不是把它当成"识别结果是空"。
            result["error"] = str(ocr.get("error"))
        view["ocr"] = result
    return view


# --------------------------------------------------------------------------
# 「多模态」开关与 OCR 降级（契约 §13）
# --------------------------------------------------------------------------


def _load_ocr():
    """拿到本地 OCR 引擎模块（契约 §13.2 的接缝）。

    刻意**不在模块顶层 import**：Web 端在"无依赖模式"（系统 Python，没装
    rapidocr）下也必须能起来 —— 引擎缺失该在发送那一刻变成一条明确的 400
    （带 `need: "ocr"`），而不是让整个服务 import 失败、连界面都打不开。
    """
    from . import ocr

    return ocr


def _vision_source_text(source: str) -> str:
    """判定来源 → 给人看的说法（错误信息里要说清"凭什么这么判"）。

    **是函数而不是模块级字典**：这几个词要跟着界面语言走，而语言是运行期可切的
    （`docs/12-i18n.md` §3）—— import 时算好就再也换不动了。
    """
    if source == VISION_SOURCE_PROFILE:
        return _t("web.vision.source.profile")
    if source == VISION_SOURCE_BUILTIN:
        return _t("web.vision.source.builtin")
    return _t("web.vision.source.unknown")


def vision_refusal_message(model: str, source: str) -> str:
    """契约 §13.8 的 400 文案：判定来源 + 三条出路。

    为什么必须有第三条出路：内置已知表是**一次带日期的观测**，它可能已经过期。
    不给用户纠正的手段，就等于让一个过时的硬编码把"其实支持图片的模型"永久挡在门外。
    留空 `vision_models` = 未知 = 不再拦，这是用户自己就能做的动作。
    """
    return _t(
        "web.vision.refused",
        model=repr(model),
        where=_vision_source_text(source),
    )


def history_image_notice(model: str, count: int) -> str:
    """契约 §13.8 末条：历史里有图、而当前模型被判"不支持"时的提醒。

    这里**不拒绝、不改写历史、也不自动改模态**：用户完全可能只是想在这个会话里
    接着聊文字（"把上面聊的总结一下"）。但如果他问的是"刚才那张图里是什么"，
    那句迷糊答案的根源必须已经摆在屏幕上。
    """
    return _t("web.vision.history_images", model=repr(model), count=count)


def history_image_count(log: Any) -> int:
    """这个会话重放出来的历史里挂着多少张图片。

    口径：**图片份数** —— 每个挂在 user 消息上的图片块算一张；同一张图在历史里
    出现两次就算两张（它确实会被发两遍）。只支持无副作用的读：调用方给的是
    `EventLog`（`log.conversation()`），它自己去磁盘读事件与附件记录。

    刻意**不去重、也不算"扩展名"**：要回答的问题是"模型这次会丢几张图"，
    而那正好等于请求里图片块的个数。压缩摘要覆盖掉的部分不在内（它本来就没进上下文）。
    """
    messages = log.conversation()
    return sum(len(message.images or ()) for message in messages)


def ocr_summary_notice(records: list[dict[str, Any]]) -> str:
    """OCR 之后的汇总 notice（契约 §13.7 第 3 条）：人话 + 数字。

    必须让用户看见"模型到底收到了什么"。张数取自**实际要发的那份记录**，
    所以它和事件日志里写下的东西必然一致。

    统计口径：`ocr.text` 去掉空白后为空（含 OCR 失败的那几条）就算"没识别到文字"
    —— 对模型来说它们确实是空的，不能在界面上说成"识别成功但内容为空"。
    """
    total = len(records)
    empty = 0
    for record in records:
        ocr = record.get("ocr")
        text = str(ocr.get("text") or "") if isinstance(ocr, dict) else ""
        if not text.strip():
            empty += 1
    text = _t("web.ocr.summary", total=total)
    if empty:
        text += _t("web.ocr.summary_empty", empty=empty)
    return text


def ocr_unavailable_payload(exc: BaseException) -> dict[str, Any]:
    """契约 §13.2：引擎不可用 → 400，`need: "ocr"`，并给出两条出路。"""
    return {
        "error": _t("web.ocr.unavailable", error=exc),
        "need": "ocr",
    }



def _upload_result(session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """契约 §7.1 的 200 响应：落盘记录原样 + `url`，warning 提到顶层。

    `warnings` 从 attachment 里挪到顶层是刻意的 —— 它是"这次上传要注意的事"
    （声明类型与内容不符、动图被当静帧等），不是这张图的属性。
    """
    attachment = dict(record)
    warnings = list(attachment.pop("warnings", ()) or ())
    attachment["url"] = attachment_url(
        session_id, str(attachment.get("id") or ""), _attachment_ext(attachment)
    )
    return {
        "ok": True,
        "session_id": session_id,
        "attachment": attachment,
        "warnings": warnings,
    }


# 请求体里除了 base64 还有 JSON 的键名与文件名，留一点余量。余量刻意很小：
# 它是"合法 JSON 包装"的空间，不是"再塞一张小图"的空间。
_ATTACHMENT_BODY_SLACK_BYTES = 8192

# 丢弃请求体时的分块大小（见 `_drain_body`）。
_DRAIN_CHUNK_BYTES = 65536


def _attachment_body_limit(max_bytes: int) -> int:
    """把"单张图片的字节上限"换算成"请求体的字节上限"。

    base64 每 3 字节编成 4 字节，所以图片上限换算到请求体约是 4/3 倍。
    这道闸只为一件事存在：**在 `rfile.read()` 之前**判断这个请求值不值得读。
    没有它，一个 200 MB 的 body 会先被整块读进内存，然后才被一句"太大"拒绝。
    """
    return 4 * ((max(0, int(max_bytes)) + 2) // 3) + _ATTACHMENT_BODY_SLACK_BYTES


def _human_size(size: int) -> str:
    """人话字节数。**只在 web 层自己的错误信息里用**（attachments 层那份是它自己的）。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.0f} KiB"
    return f"{size} 字节"


class ClientGone(Exception):
    """客户端断开（点了"停止"或关了页面）。SSE 写入失败时抛出。"""


# --------------------------------------------------------------------------
# 数据组装
# --------------------------------------------------------------------------


def build_session_view(settings: Settings, session_id: str) -> dict[str, Any]:
    """把事件日志重放成前端要的消息列表。

    前端不自己解析 JSONL，也不缓存历史 —— 事件日志是唯一真相来源（ADR-006），
    刷新页面即与磁盘状态一致。

    id 仍然在这里再校验一次（纵深防御）：Web 层已经先挡过一道，
    但这个函数是公开的，别的调用方可能不知道这个规矩。
    """
    path = _session_path(settings, session_id)
    if path is None:
        return {"error": _t("web.error.bad_session_id", session_id=_short_session_id(session_id))}
    if not path.is_file():
        return {"error": _t("web.error.no_session", session_id=session_id)}

    messages: list[dict[str, Any]] = []
    usage = Usage()
    summary_text, covered = last_summary(path)
    seen = 0  # 已数过的"对话消息"条数，口径与 events._build_messages 一致
    current_turn_seq: Any = None  # 本轮 user_message 的 seq：meta 那一行的「分叉」要用
    if summary_text:
        # 被摘要覆盖的旧消息不再逐条展示，改成一个摘要块放在最前面
        messages.append({"role": "summary", "text": summary_text, "covered": covered})

    for record in read_events(path):
        kind = record.get("kind")
        if kind == "user_message":
            # **在 covered 判断之前**记下 seq：被摘要覆盖的那几轮，它们的 turn_end
            # 仍然会画成 meta 行，那一行的分叉点也还是这条 user_message。
            current_turn_seq = record.get("seq")
        if kind in ("user_message", "assistant_message", "tool_call"):
            if seen < covered:
                seen += 1
                continue
            seen += 1

        if kind == "user_message":
            message: dict[str, Any] = {
                "role": "user",
                "text": record.get("text") or "",
                "ts": record.get("ts"),
                # 分叉用：seq 是后端定位分叉点的凭据
                "seq": record.get("seq"),
            }
            # 老日志没有 attachments 字段 = 没有图，此时**连键都不加**：
            # "没有图"只有一种表示，前端不必区分 null / [] / 缺字段。
            raw_items = record.get("attachments")
            views = [
                _attachment_view(session_id, item)
                for item in (raw_items if isinstance(raw_items, list) else [])
                if isinstance(item, dict)
            ]
            if views:
                message["attachments"] = views
            # `modality`（契约 §13.3）：**缺这个键 = "image"**（老日志）。
            # 也只在日志里真的写了它的时候才带出来 —— 老会话的视图形状一个字不变，
            # 而前端"没有这个键 = 发图"的默认也就自然成立。
            if record.get("modality"):
                message["modality"] = str(record["modality"])
            messages.append(message)
        elif kind == "assistant_message":
            message = record.get("message") or {}
            if message.get("content") or message.get("reasoning"):
                messages.append(
                    {
                        "role": "assistant",
                        "text": message.get("content") or "",
                        "reasoning": message.get("reasoning") or "",
                        "ts": record.get("ts"),
                    }
                )
            usage.add(Usage.from_dict(record.get("usage")))
        elif kind == "tool_call":
            messages.append(
                {
                    "role": "tool",
                    "name": record.get("name") or "",
                    "arguments": record.get("arguments") or "",
                    "ok": bool(record.get("ok")),
                    "display": record.get("display") or "",
                    "result": record.get("result") or {},
                    "ts": record.get("ts"),
                }
            )
        elif kind == "notice":
            messages.append(
                {
                    "role": "notice",
                    "level": record.get("level") or "info",
                    "text": record.get("text") or "",
                }
            )
        elif kind == "turn_end":
            messages.append(
                {
                    "role": "meta",
                    "steps": record.get("steps"),
                    "stop_reason": record.get("stop_reason"),
                    "elapsed": record.get("elapsed"),
                    "usage": record.get("usage") or {},
                    # 这一轮的分叉点（那条 user_message 的 seq）；前端「分叉」按钮用它
                    "turn_seq": current_turn_seq,
                }
            )
    session_switches = session_tools(path)
    return {
        "session_id": session_id,
        "messages": messages,
        "usage": usage.to_dict(),
        "generation": _session_generation(path),
        "tools": {
            "web_search": bool(session_switches.get("web_search", False)),
            # 缺键 = **开**（契约 §13.1）：老会话沿用上一轮的行为。
            "multimodal": bool(session_switches.get("multimodal", True)),
        },
        "title": session_display_title(path),
    }


def _session_generation(path: Path) -> dict[str, str]:
    """会话当前生效的接口/模型/档位：优先取最后一次显式切换，否则取开始时的记录。"""
    explicit = session_generation_settings(path)
    if explicit.get("model") or explicit.get("provider"):
        return explicit
    start = session_start_info(path)
    return {
        "provider": str(start.get("provider") or ""),
        "model": str(start.get("model") or ""),
        "effort": str(start.get("reasoning_effort") or ""),
        "label": "",
    }


def build_memory_view(settings: Settings) -> dict[str, Any]:
    from .memory_limit import MEMORY_FILE, MemoryLimitStore

    memory = open_memory(settings)
    try:
        stats = memory.stats()
        _items, omitted = memory.injection_plan(
            max_tokens=settings.memory_token_budget
        )
        limit_state = MemoryLimitStore(
            settings.data_dir / MEMORY_FILE, default=settings.memory_limit
        ).public_state(active_count=stats["active"])
        return {
            "stats": stats,
            "limit": limit_state,
            # 全量注入下"有没有条目因为太长没进上下文"是用户该知道的事实
            "injection": {
                "omitted": omitted,
                "token_budget": settings.memory_token_budget,
            },
            "items": [
                {
                    "id": item.id,
                    "content": item.content,
                    "category": item.category,
                    "status": item.status,
                    "revision": item.revision,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in memory.all_items()
            ],
        }
    finally:
        memory.close()


def _vault_store(settings: Settings) -> VaultStore:
    return VaultStore(
        settings.vault_dir,
        read_max_chars=settings.vault_read_max_chars,
        write_max_chars=settings.vault_write_max_chars,
        max_files=settings.vault_max_files,
        name_max_chars=settings.vault_name_max_chars,
        readme_token_budget=settings.vault_readme_token_budget,
    )


# 界面上"点开一个档案"能看多少字符。刻意**远大于**单文件写入上限
# （`vault_write_max_chars`，默认 12000）：真人阅读不该被"给模型看的读取上限"截断。
# 但也不能不设上限（用户手写一个超大文件时会把整个页面拖死），
# 所以给一个宽松的帽子，并如实把 `truncated` 报给前端。
_VAULT_VIEW_MAX_CHARS = 200_000


def _vault_tree(
    entries: list[Any], *, has_readme: bool, readme_chars: int = 0, native: bool = False
) -> list[dict[str, Any]]:
    """把文件清单折成一棵目录树（给界面逐层点开）。

    在**服务端**建树而不是让前端自己折：顺序只有一份定义（结果稳定）、
    接口可测，而且前端只管渲染。

    通用地按 `/` 分层 —— 现在 vault 规范只允许两级（`sections/x.md`），
    但树是递归的，所以哪天允许更深也不会白写。
    """
    root: dict[str, Any] = {}
    for entry in entries:
        if str(entry.path) == README_NAME:
            continue  # README 由下面显式加（它要带 is_readme 标记与固定位置）
        node = root
        parts = str(entry.path).split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {}).setdefault("__children__", {})
        node[parts[-1]] = {
            "type": "file",
            "path": entry.path,
            "title": entry.title,
            "kind": entry.kind,
            "chars": entry.chars,
            "summary": entry.summary,
            "name": getattr(entry, "name", ""),
            "tags": list(getattr(entry, "tags", ()) or ()),
            "created": getattr(entry, "created", ""),
            "last_updated": getattr(entry, "last_updated", ""),
            "place": getattr(entry, "place", ""),
            # 生成物 / 观察层 / 日志 / 旁路导出：**原生模式**下界面只读。
            # 旧 vault 保持原来的"README 可编辑"行为（ADR-070）。
            "editable": (not native)
            or not (
                entry.path in GENERATED_RELS
                or entry.path
                in (
                    INDEX_NAME,
                    LOG_NAME,
                    OBSERVATIONS_NAME,
                    MEMORY_INSTRUCTION_NAME,
                    SECTIONS_CONFIG,
                )
                or entry.path.startswith("sections/archived_month/")
            ),
            "is_generated": native and entry.path in GENERATED_RELS,
        }

    def render(node: dict[str, Any]) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        dirs: list[dict[str, Any]] = []
        for name, value in node.items():
            if name == "__children__":
                continue
            if isinstance(value, dict) and "__children__" in value:
                children = render(value["__children__"])
                dirs.append(
                    {
                        "type": "dir",
                        "name": name,
                        "children": children,
                        "files": sum(
                            1
                            for item in _walk_tree(children)
                            if item.get("type") == "file"
                        ),
                    }
                )
            else:
                files.append(value)
        # 目录在前、文件在后，各自按名字排 —— 与文件管理器一致，结果稳定
        dirs.sort(key=lambda item: item["name"])
        files.sort(key=lambda item: item["path"])
        return dirs + files

    out: list[dict[str, Any]] = []
    if has_readme:
        out.append(
            {
                "type": "file",
                "path": README_NAME,
                "title": "README",
                "kind": "index",
                # 真实的字符数（正文长度），不是占位 0 —— 界面上会显示这个数，
                # 写死一个假的数字比不显示更糟
                "chars": readme_chars,
                "summary": _t("web.vault.readme_summary"),
                "is_readme": True,
                "is_generated": native,
                "editable": not native,
            }
        )
    out.extend(render(root))
    return out


def _walk_tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for node in nodes:
        yield node
        if node.get("type") == "dir":
            yield from _walk_tree(node.get("children") or [])


def build_vault_view(settings: Settings) -> dict[str, Any]:
    """背景档案那一页要的一切：目录树、README 预算、结构校验、还有多少待整理。"""
    store = _vault_store(settings)
    report = store.check()
    readme, _ = store.readme_text()
    # **README 不算"档案"**：与 `check()["files"]` / `count_files()` / 重置预览同一口径
    # （它是索引、是投影）。`list_files()` 会把 README 一起列出来，所以在这里滤掉 ——
    # 否则界面上"档案 5 份"和这个列表的长度会对不上（这种"一个东西两个数字"
    # 已经栽过一次：见 reset.py 的 vault_files）。
    entries = [entry for entry in store.list_files() if entry.path != README_NAME]
    existing_paths = {entry.path for entry in entries}
    # 原生 vault 的根级文件（index/log/observations/memory_instruction）也要能点开看。
    # observations 已经由 list_files 带出来；这里补上其余几个。
    for rel, title, kind in (
        (INDEX_NAME, "index", "generated"),
        (LOG_NAME, "log", "log"),
        (MEMORY_INSTRUCTION_NAME, "memory_instruction", "aux"),
    ):
        if rel in existing_paths:
            continue
        path = settings.vault_dir / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        _front, body, _warn = parse_frontmatter(text)
        entries.append(
            VaultEntry(
                path=rel,
                title=title,
                kind=kind,
                chars=len(body),
                summary=body.strip().replace("\n", " ")[:60],
            )
        )
    entries.sort(key=lambda entry: entry.path)
    has_readme = (settings.vault_dir / README_NAME).is_file()
    # README 的**真实**字符数（正文，不含 frontmatter）。读它是对的：
    # 这个数字会显示在树上，而占位 0 会让"README 有 183 字"和"0 字"同时出现在一屏里。
    readme_chars = int(report.get("readme_total_chars") or 0)
    if has_readme and not readme_chars:
        try:
            _front, readme_body, _warn = parse_frontmatter(
                (settings.vault_dir / README_NAME).read_text(encoding="utf-8")
            )
            readme_chars = len(readme_body.strip())
        except (OSError, UnicodeDecodeError):
            readme_chars = 0  # 读不出来就不编一个数字（界面上显示 0 也算如实）
    memory = open_memory(settings)
    try:
        pending = summarize_pending(
            pending_sessions(settings.sessions_dir, memory.cursor_map())
        )
    finally:
        memory.close()
    return {
        "path": display_path(settings.vault_dir),
        "check": report,
        "pending": pending,
        "readme": readme,
        "native": store.is_native(),
        "manifest": report.get("manifest"),
        "raw_files": report.get("raw_files", 0),
        # 逐层点开用（目录树）；`files` 是平铺清单，保留给"按名字找"的场景
        "tree": _vault_tree(
            entries,
            has_readme=has_readme,
            readme_chars=readme_chars,
            native=store.is_native(),
        ),
        "files": [entry.to_dict() for entry in entries],
    }


class VaultConflict(Exception):
    """409：东西在你看的时候变了。

    三种情形共用它：内容被别人改过（保存/删除时指纹对不上）、新建时重名、
    整理正在运行。分开成三个异常没有收益 —— 调用方对它们的处理完全一样
    （把话说清楚，让人刷新看一眼再决定），而 `payload` 可以带更具体的材料。
    """

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload


def _file_hash(path: Path) -> str:
    """文件内容的指纹（前 16 位十六进制）。不存在就是空串。

    用途只有一个：**防止"你看着旧内容保存，把别人刚写的覆盖掉"**（ADR-070）。
    界面载入时拿到它，保存时带回来；对不上就拒一次，让人先看清再决定。
    这不是安全机制（本机单用户），是**防手滑** —— 而"手滑覆盖掉模型刚整理的档案"
    正是这个功能最容易造成的损失。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def build_vault_file_view(settings: Settings, raw_path: str) -> dict[str, Any]:
    """一个档案的全文 + 元信息 + 邻域（它指向谁、谁指向它）。

    **只读**：读一个不存在的档案只会 404，不会顺手创建（同 `--vault-init`
    与重置预览的原则）。路径校验全部交给 `VaultStore.read()`：
    它自己会拒绝 `..`、绝对路径、盘符、三层路径、设备名等等。
    """
    store = _vault_store(settings)
    canonical = store.resolve_friendly(raw_path)
    file = store.read(canonical, max_chars=_VAULT_VIEW_MAX_CHARS)
    front = file.frontmatter
    neighbor = store.neighborhood(canonical)
    return {
        "path": file.path,
        "title": front.title or front.name or Path(file.path).stem,
        "name": front.name,
        "kind": front.kind or (front.tags[0] if front.tags else ""),
        "relation": front.relation,
        "status": front.status,
        "created": front.created,
        "last_updated": front.last_updated,
        "updated": front.updated,
        "place": front.place,
        "tags": list(front.tags),
        "aliases": list(front.aliases),
        "sources": list(front.sources),
        "extra": dict(front.extra),
        "body": file.body,
        "chars": file.chars,
        "total_chars": file.total_chars,
        "truncated": file.truncated,
        "warnings": list(file.warnings),
        "links": neighbor["links"],
        "backlinks": neighbor["backlinks"],
        "related": neighbor["related"],
        # 界面上"这个文件是从哪来的"：README 是索引，不是整理出来的档案；
        # 生成物/观察层/日志只读。
        "is_readme": file.path == README_NAME,
        "is_generated": store.is_native() and file.path in GENERATED_RELS,
        "editable": (not store.is_native())
        or not (
            file.path in GENERATED_RELS
            or file.path
            in (
                INDEX_NAME,
                LOG_NAME,
                OBSERVATIONS_NAME,
                MEMORY_INSTRUCTION_NAME,
                SECTIONS_CONFIG,
            )
            or file.path.startswith("sections/archived_month/")
        ),
        # 保存时带回来，用于"这份档案在你编辑期间被改过"的检查
        "hash": _file_hash(settings.vault_dir / file.path),
    }


def save_vault_file(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """界面上的"新建 / 编辑"。**与整理工具走同一个 store 方法**（ADR-070）。

    这里刻意不做任何路径或内容的校验 —— 那全在 `VaultStore.save_note()` 里，
    与模型写档案时用的是同一份实现（含大小写撞名、单文件上限、设备名、两级路径）。
    网页层只负责：翻译成 HTTP 状态码、检查"内容有没有被人改过"、把结构校验结果带回去。
    """
    store = _vault_store(settings)
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise ValueError(_t("web.error.missing_path_field"))
    body = str(payload.get("body") or "")
    mode = str(payload.get("mode") or "update").strip().lower()
    if mode not in ("create", "update", "auto"):
        raise ValueError(_t("web.error.bad_mode", mode=repr(mode)))

    canonical = store.resolve_friendly(raw_path)
    path = store.path_of(canonical)
    exists = path.is_file()

    # 新建时撞名：**409 而不是 400**。"名字不合法"是输入问题（改个名字再来），
    # "已经有这份档案了"是状态冲突（界面该提示"打开它看看"）—— 要用户做的事不一样。
    # 只差大小写的那种撞名由 store 抛 VaultPathError（信息更具体），仍然是 400。

    # "你看着的版本"对不对？对不上就拒一次，并把**当前内容**一起回给界面，
    # 让人看清改了什么再决定（静默覆盖是这一层最该防的事）。
    expect = str(payload.get("expect_hash") or "").strip()
    if expect and exists:
        current = _file_hash(path)
        if current != expect:
            stale = build_vault_file_view(settings, canonical)
            raise VaultConflict(
                _t("web.error.vault_changed", path=canonical), stale
            )

    def _list(key: str) -> tuple[str, ...]:
        value = payload.get(key)
        if isinstance(value, list):
            return tuple(str(item).strip() for item in value if str(item).strip())
        if isinstance(value, str) and value.strip():
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return ()

    front = Frontmatter(
        name=str(payload.get("name") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        kind=str(payload.get("kind") or "").strip(),
        relation=str(payload.get("relation") or "").strip(),
        status=str(payload.get("status") or "").strip(),
        place=str(payload.get("place") or "").strip(),
        tags=_list("tags"),
        aliases=_list("aliases"),
    )
    # **界面把某个框清空 = 要抹掉那个字段。** 合并语义默认是"空 = 保持原样"，
    # 不显式说一声的话，用户清掉 `status: 进行中` 之后文件里还留着它，
    # 而模型下一轮还会读到（这是内容正确性问题，不是外观问题）。
    # 判据是"这个键**在请求里出现过**且为空" —— 界面每次都会带上全部字段，
    # 所以"出现过且为空"就等于用户把它抹了。模型侧不走这里，行为不受影响。
    clear = [
        key
        for key in ("name", "title", "kind", "relation", "status", "place", "tags", "aliases")
        if key in payload and not front.to_dict().get(key)
    ]
    result = store.save_note(canonical, body, front, mode=mode, clear=clear)
    if store.is_native():
        store.rebuild_generated()
    view = build_vault_file_view(settings, result["path"])
    view["action"] = result["action"]
    view["check"] = store.check()
    return view


def delete_vault_file(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """界面上的"删除"。同样走 store 的唯一实现（README 不能删这条也在里面）。"""
    store = _vault_store(settings)
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise ValueError(_t("web.error.missing_path_field"))
    canonical = store.resolve_friendly(raw_path)
    path = store.path_of(canonical)

    expect = str(payload.get("expect_hash") or "").strip()
    if expect and path.is_file() and _file_hash(path) != expect:
        raise VaultConflict(
            _t("web.error.vault_changed_before_delete", path=canonical)
        )

    # 删之前先记下"谁指向它"：删完这些链接就变成"还没写"了，界面该说一声
    dangling = [item["path"] for item in store.neighborhood(canonical)["backlinks"]]
    removed = store.delete_file(canonical)
    if store.is_native():
        store.rebuild_generated()
    return {"ok": True, "path": removed, "dangling": dangling, "check": store.check()}


def tool_store(settings: Settings) -> ToolStore:
    return ToolStore(settings.data_dir / TOOLS_FILE)


def language_store(settings: Settings) -> LanguageStore:
    """界面语言那个运行期配置（`data/settings.json`，ADR-085）。

    **每次现建**：语言是运行期可改的，缓存一份实例迟早会读到旧值
    （同 `limit_for()` 那条注释的理由）。
    """
    return LanguageStore(
        settings.data_dir / SETTINGS_FILE,
        default=settings.language,
        # 单轮最大输出的**种子**来自 .env；`data/settings.json` 里的值优先（ADR-101）
        output_default=settings.max_output_tokens,
    )


def _parse_model_limits(raw: Any) -> dict[str, int]:
    """解析"按模型覆盖上下文上限"。

    接受两种写法：字典 `{model: tokens}`，或多行文本 `model=tokens`（每行一条）。
    界面用的是后者 —— 比让人填 JSON 友好得多。
    """
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        items = list(raw.items())
    else:
        items = []
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            items.append((key, value))
    for key, value in items:
        name = str(key).strip()
        # 单位写法与读档案是**同一条规则**（`parse_token_limit`）：`1M` = 1000000、
        # `200k` = 200000。看不懂的值照旧跳过 —— 空名字也跳过：它永远匹配不上
        # 任何模型，存进去只会变成一条"看着像配置、其实永不生效"的东西。
        parsed = parse_token_limit(value)
        if name and parsed is not None:
            out[name] = parsed
    return out


def _clean_surrogates(value: Any) -> Any:
    """把 JSON 里**不成对的代理字符**换成 `?`（递归处理 dict / list / str）。

    `\\ud800` 这种字符串是合法 JSON —— JS 的 `JSON.stringify` 会照原样生成
    （在输入法/剪贴板里把一个 emoji 从中间切断，就能得到它）。Python 的
    `json.loads` 收得下，但之后写事件日志、发 SSE 都要做 `str.encode("utf-8")`，
    那时会抛 `UnicodeEncodeError: surrogates not allowed`：一条消息就能把整轮打崩，
    而且日志里连"用户说了什么"都没有。在这里一次换掉，后面所有路径都不用再想它。
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
        return value
    if isinstance(value, dict):
        return {_clean_surrogates(key): _clean_surrogates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_surrogates(item) for item in value]
    return value


def _parse_model_labels(raw: Any) -> dict[str, str]:
    """解析"模型的显示名"（`{model: 显示名}`，或多行文本 `model=显示名`）。

    形状与 `_parse_model_limits` 故意一模一样：界面上它们是同一张表格的两列，
    所以前端顺手把两列都按同一个形状发上来，后端也就不该有第二套解析规则。

    **空值丢掉**：一条空字符串会让卡片上那一格变成空白（看起来像"没有名字"），
    而真正想要的是"退回模型 id"（见 `ProviderProfile.display_name`）。
    """
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        items = list(raw.items())
    else:
        items = []
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            items.append((key, value))
    for key, value in items:
        name = str(key).strip()
        text = str(value if value is not None else "").strip()
        if name and text:
            out[name] = text
    return out


def forward_event(kind: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """把 AgentLoop 的 UI 事件翻译成给浏览器的 SSE 帧。

    `prompt` 事件被刻意丢弃：它包含完整提示词，是调试用的，
    每轮都推给前端既浪费带宽也没人看（CLI 的 --show-prompt 才是它的主场）。
    """
    if kind == "reasoning":
        return ("reasoning", {"text": data.get("text") or ""})
    if kind == "text":
        return ("text", {"text": data.get("text") or ""})
    if kind == "step":
        return (
            "step",
            {"step": data.get("step"), "estimated_tokens": data.get("estimated_tokens")},
        )
    if kind == "tool_call":
        call = data.get("call")
        return ("tool_call", {"name": call.name, "arguments": call.arguments})
    if kind == "tool_result":
        call = data.get("call")
        result = data.get("result")
        return (
            "tool_result",
            {
                "name": call.name,
                "ok": bool(result.ok),
                "display": result.display,
                "result": result.payload,
            },
        )
    if kind == "notice":
        return ("notice", {"level": data.get("level") or "info", "text": data.get("text") or ""})
    if kind == "compact":
        # 上下文压缩：前端要显式告诉用户"旧对话被摘要替代了"
        return (
            "compact",
            {
                "summary": data.get("summary") or "",
                "folded": data.get("folded") or 0,
                "covered": data.get("covered") or 0,
                "usage": data.get("usage") or {},
            },
        )
    if kind == "memory":
        # 记忆是全量注入的，平时没什么可报的；只在"有内容没进来"时提醒
        omitted = int(data.get("omitted") or 0)
        if not omitted:
            return None
        return (
            "memory",
            {
                "count": data.get("count") or 0,
                "limit": data.get("limit") or 0,
                "omitted": omitted,
            },
        )
    if kind == "turn_end":
        outcome = data.get("outcome")
        return (
            "turn_end",
            {
                "text": outcome.text,
                "steps": outcome.steps,
                "stop_reason": outcome.stop_reason,
                "error": outcome.error,
                "elapsed": round(outcome.elapsed, 2),
                "usage": outcome.usage.to_dict(),
                # 实时流这一侧的「分叉」按钮靠它定位分叉点（历史回放走 turn_seq）
                "user_seq": getattr(outcome, "user_seq", None),
            },
        )
    return None


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------


def _parse_http_authority(value: str) -> tuple[str, int] | None:
    """解析 HTTP 的 `host[:port]`；不合法返回 None，全程不做 DNS。

    本服务只有 HTTP，所以省略的端口就是 80。IPv6 必须按 HTTP 语法放在方括号里；
    不能把带多个冒号的字符串当成 `name:port` 随便切开。端口也必须真的是当前
    监听端口，否则 `Host: localhost:别的端口` 不能拿来冒充这次请求的目标。
    """
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        return None

    if text.startswith("["):
        closing = text.find("]")
        if closing < 0:
            return None
        hostname = text[1:closing]
        remainder = text[closing + 1 :]
        if not hostname or "%" in hostname:
            # 回环地址不需要 zone id；不认得的形式直接拒，避免各平台解释不同。
            return None
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return None
        if not isinstance(address, ipaddress.IPv6Address):
            return None
        normalized = address.compressed
        has_port = bool(remainder)
        port_text = remainder[1:] if remainder.startswith(":") else ""
        if remainder and not remainder.startswith(":"):
            return None
    else:
        if "[" in text or "]" in text or text.count(":") > 1:
            return None
        hostname, separator, raw_port = text.partition(":")
        if not hostname:
            return None
        has_port = bool(separator)
        port_text = raw_port if separator else ""
        normalized = hostname.lower()
        if normalized == "localhost.":
            normalized = "localhost"
        if normalized != "localhost":
            try:
                address = ipaddress.ip_address(normalized)
            except ValueError:
                return None
            if not isinstance(address, ipaddress.IPv4Address):
                return None
            normalized = address.compressed

    if has_port and not port_text:
        return None
    if port_text:
        if not re.fullmatch(r"[0-9]+", port_text):
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    else:
        port = 80
    return normalized, port


def _host_allowed_for_bind(host_header: str, bound_host: str, bound_port: int) -> bool:
    """默认回环服务只认回环 Host；显式非回环监听保持原来的托管行为。

    DNS 重绑定的关键不是 Origin 有没有和 Host 对上——攻击者可以让两者都叫
    `evil.example`——而是 Host 本身是不是这个本机服务允许的名字。这里只接受
    字面量回环 IP 与 `localhost`，绝不解析域名，所以结果不随 DNS 答案变化。
    """
    bind_name = bound_host.strip().lower()
    if bind_name == "localhost.":
        bind_name = "localhost"
    if bind_name != "localhost":
        try:
            bind_address = ipaddress.ip_address(bind_name.split("%", 1)[0])
        except ValueError:
            # `server_address` 正常已经是解析后的 IP。真出现未知形状时宁可拒绝，
            # 也不能把"不知道绑在哪"当成可以绕过本机 Host 闸的理由。
            return False
        if not bind_address.is_loopback:
            # `--host 0.0.0.0` / 局域网地址是既有显式对外托管契约；这一层不为它
            # 偷偷加一份回环白名单（原有的 Origin / Sec-Fetch-Site 闸仍然生效）。
            return True

    parsed = _parse_http_authority(host_header)
    if parsed is None or parsed[1] != bound_port:
        return False
    hostname = parsed[0]
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class HarnessHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = PRODUCT_SLUG
    sys_version = ""
    timeout = 120

    # -- 基础工具 ----------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            print(f"[web] {fmt % args}", flush=True)

    def _read_json(self) -> dict[str, Any] | None:
        """读 JSON 请求体。**读不出来时返回 `None`**（调用方要回 400）。

        为什么不返回空字典：那样"JSON 坏了"看起来就和"你没填这个字段"一模一样，
        **症状指向错误的方向**。实测踩过一次 —— 用 PowerShell 写的 body 文件带了 BOM，
        服务端报的是"要指定一个语言"，而真正的原因是 JSON 解析失败；
        照那条报错去查，会一直查调用方为什么没传值。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}          # 真的没带 body：这是"空请求"，不是"坏请求"
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return _clean_surrogates(payload)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        self._response_started = True
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self) -> None:
        self._response_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.end_headers()
        # 没有 Content-Length 的 HTTP/1.1 响应必须靠连接关闭来界定结尾
        self.close_connection = True

    def _sse(self, event: str, payload: dict[str, Any]) -> None:
        frame = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def _safe_sse(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self._sse(event, payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _make_emitter(self):
        """把 emit 事件写进 SSE 流；写不进去 = 客户端没了，抛 ClientGone 中止本轮。"""

        def emit(kind: str, **data: Any) -> None:
            forwarded = forward_event(kind, data)
            if forwarded is None:
                return
            try:
                self._sse(forwarded[0], forwarded[1])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                raise ClientGone(str(exc)) from exc

        return emit

    # -- 静态文件 ----------------------------------------------------

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        root = WEB_DIR.resolve()
        target = (root / path.lstrip("/")).resolve()
        # is_relative_to 比字符串前缀比较可靠：能挡住 ../ 穿越，也不会被同前缀目录骗过
        if not target.is_relative_to(root) or not target.is_file():
            self._send_json({"error": _t("web.error.not_found_static", path=path)}, 404)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_vault_raw(self, query: dict[str, list[str]]) -> None:
        """`GET /api/vault/raw?path=raw/source_<n>/<img>`：原生 vault 的图片素材。

        路径校验全部走 `VaultStore.read_raw()`（只允许 `raw/source_<n>/<图片>`
        或 `.meta.json`，不许穿越/盘符/符号链接），web 层不复制那套规则。
        """
        raw_path = (query.get("path") or [""])[0]
        if not raw_path.strip():
            self._send_json({"error": _t("web.error.missing_path")}, 400)
            return
        try:
            body, canonical = _vault_store(self.settings).read_raw(raw_path)
        except FileNotFoundError:
            self._send_json(
                {"error": _t("web.error.no_vault_file", path=raw_path)}, 404
            )
            return
        except VaultPathError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        content_type = mimetypes.guess_type(canonical)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 素材可能被导入/替换；不缓存，避免界面看到旧图。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_attachment(self, query: dict[str, list[str]]) -> None:
        """`GET /api/attachments`：把一张已落盘的图片原样发回去（契约 §7.2）。

        不能用 `_send_json`（那是 JSON）：这是字节流。头必须手写，其中
        `Content-Length` 不是可选项 —— HTTP/1.1 的长连接靠它界定响应边界，
        少了它的后果是浏览器把下一张图的响应拼到这一张后面。

        路径校验**全部复用 attachments 层**（`resolve_path`：会话 id 白名单 +
        内容哈希 hex 白名单 + 扩展名白名单 + `is_relative_to` 复查，两道闸都在那里）。
        在 web 层再抄一套的话，两套迟早走样 —— 而走样的那一套就是漏洞。
        白名单里没有 SVG/HTML，所以"用图片接口发一段能执行的脚本"这件事不存在。
        """
        session_id = (query.get("session") or [""])[0]
        note_id = (query.get("id") or [""])[0]
        ext = (query.get("ext") or [""])[0]
        try:
            path = attachments.resolve_path(self.settings, session_id, note_id, ext)
        except attachments.AttachmentError as exc:
            # 非法参数 == 输入问题（400），和"文件没了"（404）是两回事
            self._send_json({"error": str(exc)}, 400)
            return
        if not path.is_file():
            self._send_json({"error": _t("web.error.attachment_gone")}, 404)
            return
        try:
            body = attachments.read_bytes(path)
        except OSError as exc:
            self._send_json(
                {"error": _t("web.error.attachment_read_failed", error=exc)}, 500
            )
            return
        content_type = attachments.EXT_MIME[
            str(ext or "").strip().lower().lstrip(".")
        ]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 内容嗅探的兜底：即使哪天白名单被改宽，也不许浏览器把这段字节当别的类型执行
        self.send_header("X-Content-Type-Options", "nosniff")
        # 落盘名是内容哈希 → 同一个 URL 的字节永不改变，可以放心长期缓存。
        # private：本机服务的图不该被任何共享缓存留存。
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        # 每条请求重新判断：一条 keep-alive 连接上会复用同一个 handler 实例。
        self._response_started = False
        parts = urlsplit(self.path)
        try:
            rejection = self._host_rejection()
            if rejection is not None:
                self._send_json({"error": rejection[1]}, rejection[0])
                return
            if parts.path.startswith("/api/"):
                self._api_get(parts.path, parse_qs(parts.query))
            else:
                self._serve_static(parts.path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成响应而不是空连接
            self._fail(exc)

    def do_POST(self) -> None:  # noqa: N802
        self._response_started = False
        parts = urlsplit(self.path)
        try:
            # Host 闸与 POST 同源闸都在路由和读 JSON 之前；DNS 重绑定不能再靠
            # "Origin 与恶意 Host 文字相同"混进来，GET 也一样要先过这一关。
            rejection = self._host_rejection() or self._post_rejection()
            if rejection is not None:
                # **先把这个请求的 body 读完再回包**：两道闸在 `_read_json()` 之前，
                # 所以被拒的请求体还没被读过。直接回包并关连接时，客户端可能还在写 body，
                # 于是它看到的不是我们那句 400，而是"连接被重置" ——
                # 实测表现：同一条断言单独跑通过、全量跑偶发失败。
                # 读干净再回，报错才是可靠的。
                self._drain_body()
                self._send_json({"error": rejection[1]}, rejection[0])
                return
            # 上传走自己的读体路径：它必须**先看 Content-Length 再决定读不读**
            # （见 `_handle_attachment_upload`），而下面这个 `_read_json()` 是无上限的。
            if parts.path == "/api/attachments":
                self._handle_attachment_upload()
                return
            payload = self._read_json()
            if payload is None:
                self._send_json({"error": i18n.t("web.bad_json")}, 400)
                return
            self._api_post(parts.path, payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _drain_body(self) -> None:
        """把手上的请求体读完丢掉（拒绝请求时用，见 `do_POST`）。

        **分块读**：`rfile.read(length)` 会为 200 MB 的请求体一次性分配 200 MB，
        而这条路径恰恰是为"这个请求太大/不合法，我要拒了它"准备的 ——
        拒绝一个巨大的请求时把自己撑爆，那就白拒了。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = max(0, length)
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(remaining, _DRAIN_CHUNK_BYTES))
            except OSError:
                return
            if not chunk:
                return
            remaining -= len(chunk)

    def _host_rejection(self) -> tuple[int, str] | None:
        """回环监听时，在任何路由动作之前拒绝不是本机名字的 Host。"""
        bound = self.server.server_address  # type: ignore[attr-defined]
        bound_host, bound_port = str(bound[0]), int(bound[1])
        hosts = self.headers.get_all("Host") or []
        # HTTP/1.1 只能有一个 Host。两个头就算第一个是回环，也不能让不同的
        # 下游各自挑一个解释；非回环托管则由 `_host_allowed_for_bind` 原样放行。
        host = hosts[0].strip() if len(hosts) == 1 else ""
        if _host_allowed_for_bind(host, bound_host, bound_port):
            return None
        shown_host = host or _t("web.error.host_unknown")
        expected = f"127.0.0.1:{bound_port} / localhost:{bound_port} / [::1]:{bound_port}"
        # 复用现有跨站文案，避免只在某一种界面语言里新增一句未经翻译的错误。
        return 403, _t(
            "web.error.cross_site_origin",
            origin=f"Host: {shown_host}",
            host=expected,
        )

    def _post_rejection(self) -> tuple[int, str] | None:
        """改状态的 POST 先过两道闸；返回 (状态码, 原因) 表示要拒绝。

        **第一道：同源。** 浏览器**跨站**发 POST 时一定会带 `Origin`
        （`<form>`、`fetch`、XHR 都一样），新浏览器还会带 `Sec-Fetch-Site`。
        本服务默认无鉴权又跑在 127.0.0.1 上，于是任意网页都能悄悄
        POST `/api/reset`，把全部会话、记忆、接口配置和 API key 抹掉 ——
        在浏览器看来那只是个"简单请求"，服务器连预检都看不到。
        两边都没有这两个头 = 脚本 / CLI（curl、urllib、本项目测试），照旧放行：
        那些调用方本来就能直接读文件，拦它们没有安全收益，只会让工具变难用。

        **第二道：Content-Type。** 表单能造出的 Content-Type 只有
        urlencoded / multipart / text/plain；带自定义 Content-Type 的跨站请求
        会先触发 CORS 预检，而我们根本不回预检，浏览器自己就会拦下来。
        所以"必须 application/json"等于：**网页表单打不到这些接口**。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            host = (self.headers.get("Host") or "").strip()
            # 比 netloc（scheme://host:port 里的 host:port），不是比整串
            if urlsplit(origin).netloc.lower() != host.lower():
                return 403, _t(
                    "web.error.cross_site_origin",
                    origin=origin,
                    host=host or _t("web.error.host_unknown"),
                )
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return 403, _t("web.error.cross_site_fetch")

        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return 400, _t("web.error.need_json_content_type")
        return None

    def _fail(self, exc: Exception) -> None:
        """把一个没被处理的异常变成用户看得见的东西。

        **SSE 已经开始之后不能再走 `_send_json`**：那一刻 200 + `text/event-stream`
        的响应头已经在线上了，再写一个 500 就变成"HTTP 响应套在事件流正文里"，
        而前端只认 `event:`/`data:` 帧 —— 于是它什么也看不到，只看到回答被截断。
        这就是"错误被吞掉"的最坏形态：出错的其实是服务端，用户以为是网络。
        所以这里改成发一条 `notice` 帧（前端本来就会把它显示成一条错误提示）。
        """
        message = f"{type(exc).__name__}: {exc}"
        if getattr(self, "_response_started", False):
            self._safe_sse("notice", {"level": "error", "text": message})
            self.close_connection = True
            return
        try:
            self._send_json({"error": message}, 500)
        except Exception:  # noqa: BLE001 - 连回包都失败就只能断开
            self.close_connection = True

    # -- 会话 id -----------------------------------------------------

    def _session_file(self, path: str, action: str) -> tuple[str, Path] | None:
        """解析 `/api/sessions/<id>/<action>`，失败时**已经回过包**并返回 None。

        id 用"整段"来取（`path[len(prefix):-len('/action')]`）而不是
        `path.split("/")[3]`：后者遇到 `/api/sessions/a/b/settings` 只会看到
        `a`，把 `a/b` 这个非法 id 悄悄截成合法的 `a` —— 分段本身就是校验点。
        """
        prefix = "/api/sessions/"
        # `action` 自带前导斜杠（"/title"），所以切到 `-len(action)` 就正好剩下 id。
        session_id = path[len(prefix) : -len(action)]
        session_path = _session_path(self.settings, session_id)
        if session_path is None:
            self._send_json(
                {
                    "error": _t(
                        "web.error.bad_session_id",
                        session_id=_short_session_id(session_id),
                    )
                },
                400,
            )
            return None
        if not session_path.is_file():
            self._send_json(
                {"error": _t("web.error.no_session", session_id=session_id)}, 404
            )
            return None
        return session_id, session_path

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        settings = self.settings

        if path == "/api/setup/status":
            manager = self.server.venv_setup  # type: ignore[attr-defined]
            payload = manager.snapshot() if manager is not None else {
                "enabled": False,
                "state": "ready",
                "can_chat": True,
                "full_venv": False,
            }
            # 第一道页面在读取其它应用配置之前，就能据此选择正确语言。
            payload["language"] = language_store(settings).language()
            self._send_json(payload)
            return

        manager = self.server.venv_setup  # type: ignore[attr-defined]
        if manager is not None and not manager.allows_chat():
            self._send_json(
                {**manager.snapshot(), "error": "setup_in_progress", "code": "setup_in_progress"},
                503,
            )
            return

        if path == "/api/config":
            store = provider_store(settings)
            default = store.defaults()
            self._send_json(
                {
                    # 生效的模型来自"当前选中的接口"，不再是全局唯一的那一个
                    "model": default.get("model") or "",
                    "reasoning_effort": default.get("effort") or "",
                    "data_dir": display_path(settings.data_dir),
                    # 记忆是**全量注入**的，上限在"设置 → 记忆"里改（ADR-057/058）
                    "memory_mode": _t("web.note.memory_mode"),
                    "has_key": any(p.configured for p in store.providers()),
                    "default": default,
                    "provider_count": len(store.providers()),
                    # 会话里的「联网搜索」开关能不能用，取决于总开关与后端配置
                    "web_search_available": tool_store(settings).web().search_available,
                    "web_enabled": tool_store(settings).web().enabled,
                    # 界面语言（ADR-085）：前端启动时就知道该显示哪一种
                    "language": language_store(settings).language(),
                    # 时区（`time` 工具用的偏移，小时）：前端「设置 → 常规」要显示当前值
                    "timezone": language_store(settings).timezone(),
                    # 单轮最大输出（token；-1 = 不限）：前端「设置 → 常规」要显示当前值
                    "max_output_tokens": language_store(settings).max_output_tokens(),
                    # 版本号（读 `docs/version.txt`，**不是代码里的常量**）：
                    # 侧栏那一行显示它，于是"你跑的是哪个版本"永远答得出来。
                    "version": version(),
                }
            )
            return

        if path == "/api/settings/language":
            self._send_json(language_store(settings).public_state())
            return

        if path == "/api/prompts":
            # 「设置 → 提示词」那一页：我们到底跟模型说了什么、模型手里有哪些工具。
            # **只读**：不写盘、不联网、不调模型（连记忆库都不会被建出来 ——
            # 见 harness/prompt_view.py 的模块头）。所以这里不需要接口配置。
            self._send_json(describe_prompts(settings))
            return

        if path == "/api/providers":
            store = provider_store(settings)
            self._send_json(store.public_state())
            return

        if path == "/api/sessions":
            self._send_json(
                {
                    "sessions": [
                        {
                            "id": info.session_id,
                            "title": info.title,
                            "turns": info.turns,
                            "updated_at": info.updated_at,
                        }
                        for info in list_sessions(settings.sessions_dir)
                    ]
                }
            )
            return

        if path.startswith("/api/sessions/"):
            # 整段取 id 并校验：`/api/sessions/a/b` 里的 "a/b" 必须被看见并拒绝，
            # 不能被 `rsplit("/", 1)[-1]` 悄悄截成 "b"。
            session_id = path[len("/api/sessions/") :]
            if _session_path(settings, session_id) is None:
                self._send_json(
                    {
                        "error": _t(
                            "web.error.bad_session_id",
                            session_id=_short_session_id(session_id),
                        )
                    },
                    400,
                )
                return
            self._send_json(build_session_view(settings, session_id))
            return

        if path == "/api/memories":
            self._send_json(build_memory_view(settings))
            return

        if path == "/api/vault":
            self._send_json(build_vault_view(settings))
            return

        if path == "/api/vault/file":
            # 单个档案的全文（点开看）。查询参数而不是路径段：档案路径里有 `/`
            # 和中文，塞进 URL 路径要处理两层编码，而校验逻辑已经全在 VaultStore 里，
            # 这里只负责把它的三种结果翻译成状态码。
            raw_path = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
            if not raw_path.strip():
                self._send_json({"error": _t("web.error.missing_path")}, 400)
                return
            try:
                self._send_json(build_vault_file_view(settings, raw_path))
            except FileNotFoundError:
                self._send_json(
                    {"error": _t("web.error.no_vault_file", path=raw_path)}, 404
                )
            except VaultPathError as exc:
                self._send_json({"error": str(exc)}, 400)
            return

        if path == "/api/vault/raw":
            self._serve_vault_raw(query)
            return

        if path == "/api/dreams":
            runs = DreamRunStore(settings.dreams_dir).list()
            for run in runs:
                diff = run.get("diff") or {}
                run["changed"] = sum(
                    len(diff.get(key) or []) for key in ("added", "changed", "removed")
                )
                # 列表不需要整份差异（可能很长），详情接口再给
                run.pop("diff", None)
                run.pop("check", None)
            self._send_json({"runs": runs})
            return

        if path.startswith("/api/dreams/"):
            run_id = path.rsplit("/", 1)[-1]
            run = DreamRunStore(settings.dreams_dir).get(run_id)
            if run is None:
                self._send_json(
                    {"error": _t("web.error.no_dream_run", run_id=run_id)}, 404
                )
                return
            # 已经没有 can_undo 这回事了（ADR-065：不留快照、不可回滚）
            self._send_json(run)
            return

        if path == "/api/attachments":
            # 图片字节流（契约 §7.2）。**独立于 JSON 接口**：它自己发头、发字节，
            # 连缓存策略都与别的接口相反（那些是 no-store，这个是 immutable）。
            self._serve_attachment(query)
            return

        if path == "/api/usage":
            session_id = (query.get("session") or [""])[0]
            memory_dir = settings.sessions_dir
            if session_id:
                # 只读接口也不能拼路径：`?session=..\..\x` 能把别人的日志读成用量
                session_path = _session_path(settings, session_id)
                if session_path is None:
                    self._send_json(
                        {
                            "error": _t(
                                "web.error.bad_session_id",
                                session_id=_short_session_id(session_id),
                            )
                        },
                        400,
                    )
                    return
                paths = [session_path]
            else:
                paths = sorted(memory_dir.glob("*.jsonl"))
            labels = {p.id: p.label for p in provider_store(settings).providers()}
            grand = Usage()
            by_provider: dict[str, Usage] = {}
            per_session: list[dict[str, Any]] = []
            for session_path in paths:
                if not session_path.is_file():
                    continue
                # session_usage 会把压缩那一次的开销也算进来
                total = session_usage(session_path)
                grand.add(total)
                # 按供应商分家：一个会话中途换过接口也要算到对应那家头上
                for provider, usage in session_usage_by_provider(session_path).items():
                    by_provider.setdefault(provider, Usage()).add(usage)
                per_session.append({"id": session_path.stem, "usage": total.to_dict()})
            self._send_json(
                {
                    "total": grand.to_dict(),
                    "providers": [
                        {
                            "provider": provider,
                            "label": labels.get(provider) or provider,
                            "usage": usage.to_dict(),
                        }
                        for provider, usage in sorted(
                            by_provider.items(),
                            key=lambda item: -item[1].total_tokens,
                        )
                    ],
                    "sessions": per_session,
                }
            )
            return

        if path == "/api/reset":
            # 只读预览：让"要删多少"在界面上看得见
            self._send_json(preview_reset(settings))
            return

        if path == "/api/tools":
            self._send_json(tool_store(settings).public_state())
            return

        self._send_json({"error": _t("web.error.unknown_api", path=path)}, 404)

    def _handle_vault_write(self, path: str, payload: dict[str, Any]) -> None:
        """界面上的档案新建/编辑/删除（ADR-070）。

        几种失败必须**分开报**，因为它们要用户做的事不一样：
        400 路径/内容不合法（改名字、改内容）、404 没有这份档案（可能刚被删）、
        409 东西变了或整理正在跑（刷新再看一眼）。
        """
        settings = self.settings
        # 整理与手改**不并发**：一次整理会按它开跑时读到的内容重写档案，
        # 你在这期间的手改会被它覆盖掉，而且没有任何提示。等几秒比丢一次编辑便宜。
        #
        # **必须真拿到锁**，不能只 `locked()` 看一眼：检查与写入之间隔着一次
        # 上下文切换，整理可以正好在那个缝里开跑 —— 于是这次手改照样被覆盖。
        # 拿到之后**在发响应之前放掉**（同 `_handle_dream` 那条规矩：客户端收到
        # "成功"就会立刻发下一个请求，锁还没放会让它吃一个假的 409）。
        lock = self.server.dream_lock  # type: ignore[attr-defined]
        if not lock.acquire(blocking=False):
            self._send_json({"error": _t("web.error.vault_busy")}, 409)
            return
        status, body = 200, {}
        try:
            try:
                if path.endswith("/delete"):
                    body = delete_vault_file(settings, payload)
                else:
                    body = save_vault_file(settings, payload)
            except VaultConflict as exc:
                # 一定要带上 error 字段：payload 是更具体的材料（比如当前最新内容），
                # 而 str(exc) 是给人看的那句话，两者都要有
                status = 409
                body = {"error": str(exc), **(exc.payload or {})}
            except VaultExistsError as exc:
                # "已经有这份档案了" → 409：界面该提示"打开它看看"，而不是让人改名字。
                # 带上 `path`（**规范化之后**的那个）：界面要拿它去打开那一份，
                # 而"裸名字"这种写法（`"小林"`）前端补不出规范路径 —— 让前端去猜契约
                # 就是埋一个以后一定会踩的坑（前端实测提过这一条）。
                status = 409
                body = {
                    "error": str(exc),
                    "exists": True,
                    "path": _vault_store(self.settings).resolve_friendly(
                        str(payload.get("path") or "")
                    ),
                }
            except FileNotFoundError:
                status = 404
                body = {
                    "error": _t(
                        "web.error.vault_file_gone", path=payload.get("path")
                    )
                }
            except ValueError as exc:
                # 路径/名字/大小写撞名/单文件上限 —— 文案直接用 vault 自己那句人话
                # （`VaultPathError` 是 `ValueError` 的子类，所以一条就够）
                status = 400
                body = {"error": str(exc)}
            except OSError as exc:
                status = 500
                body = {"error": _t("web.error.vault_write_failed", error=exc)}
        finally:
            lock.release()
        self._send_json(body, status)


    def _api_post(self, path: str, payload: dict[str, Any]) -> None:
        settings = self.settings

        if path.startswith("/api/setup/"):
            manager = self.server.venv_setup  # type: ignore[attr-defined]
            if manager is None:
                self._send_json({"error": "setup_disabled", "code": "setup_disabled"}, 404)
                return
            try:
                if path == "/api/setup/start":
                    if payload.get("consent") is not True:
                        raise SetupError("consent_required")
                    result = manager.start_install(payload.get("mirror") or "")
                    status = 202
                elif path == "/api/setup/skip":
                    result = manager.skip()
                    status = 200
                elif path == "/api/setup/pause":
                    result = manager.pause()
                    status = 200
                elif path == "/api/setup/resume":
                    result = manager.resume()
                    status = 200
                elif path == "/api/setup/cancel":
                    result = manager.cancel()
                    status = 202
                else:
                    self._send_json({"error": _t("web.error.unknown_api", path=path)}, 404)
                    return
            except SetupError as exc:
                self._send_json(
                    {
                        **manager.snapshot(),
                        "error": exc.detail or exc.code,
                        "code": exc.code,
                    },
                    exc.http_status,
                )
                return
            self._send_json(result, status)
            return

        manager = self.server.venv_setup  # type: ignore[attr-defined]
        if manager is not None and not manager.allows_chat():
            self._send_json(
                {**manager.snapshot(), "error": "setup_in_progress", "code": "setup_in_progress"},
                503,
            )
            return

        if path == "/api/reset":
            # 破坏性且不可逆，所以只接受 POST（一个误点的链接或刷新不该触发它）；
            # 同源 + Content-Type 两道闸在 do_POST 里已经过了。
            try:
                result = perform_reset(settings)
            except UnsafeDataDirError as exc:
                # data_dir 指向了不该删的地方（项目根 / 家目录 / 盘根……）：
                # 这是**配置错误**而不是服务器坏了，所以用 400 把人话交给用户。
                # 此时 reset.py 的守卫保证磁盘上一个字节都没动。
                self._send_json({"error": str(exc)}, 400)
                return
            self.server.forget_sessions()  # 旧会话的锁没有意义了
            self._send_json({"ok": True, **result})
            return

        if path == "/api/tools/save":
            store = tool_store(settings)
            store.save_web(payload)
            self._send_json(store.public_state())
            return

        if path == "/api/tools/test":
            # 界面上的"试一下"：跑一次真实搜索，当场看到后端通不通。
            # 不经过模型，所以不花 LLM 钱。
            query = str(payload.get("query") or "").strip()
            config = tool_store(settings).web()
            if not query:
                self._send_json({"error": _t("web.error.no_query")}, 400)
                return
            if not config.search_available:
                self._send_json(
                    {"error": _t("web.error.search_backend_down")}, 400
                )
                return
            # count 是**别处传进来的 JSON 数字**：不数字就直接 400，
            # 不能让它抛 ValueError（那会变成 500 + 一句 Python 原文）。
            try:
                count = int(payload.get("count") or 3)
            except (TypeError, ValueError):
                self._send_json({"error": _t("web.error.bad_count")}, 400)
                return
            try:
                results, backend, note = run_search(
                    query,
                    count=count,
                    backend=config.search_backend,
                    api_key=config.tavily_api_key,
                    timeout=config.search_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 把失败原因原样给用户看
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)
                return
            self._send_json(
                {
                    "backend": backend,
                    "note": note,
                    "results": [item.to_dict() for item in results],
                }
            )
            return

        if path == "/api/chat":
            self._handle_chat(payload)
            return

        if path == "/api/dream":
            self._handle_dream(payload)
            return

        if path.startswith("/api/providers"):
            self._handle_providers(path, payload)
            return

        if path.startswith("/api/sessions/") and path.endswith("/settings"):
            resolved_session = self._session_file(path, "/settings")
            if resolved_session is None:
                return
            session_id, session_path = resolved_session
            # 只追加一条事件，不改写历史 —— 与 ADR-006 的"事件溯源"一致：
            # 换了模型这件事本身也是历史的一部分，重放时能看到当时用的是哪家。
            log = EventLog(session_path, session_id)
            try:
                log.append(
                    "session_settings",
                    provider=str(payload.get("provider") or ""),
                    model=str(payload.get("model") or ""),
                    effort=str(payload.get("effort") or ""),
                    label=str(payload.get("label") or ""),
                )
            finally:
                log.close()
            self._send_json({"ok": True})
            return

        if path.startswith("/api/sessions/") and path.endswith("/tools"):
            resolved_session = self._session_file(path, "/tools")
            if resolved_session is None:
                return
            session_id, session_path = resolved_session
            # 同样是追加一条事件：会话级的工具开关也是历史的一部分
            latest = session_tools(session_path)
            # 两个键**一条事件一起写**（缺一个的话读端会把它当成"没记录过"，
            # 默认值就可能与用户刚设的那个不一致）。请求里没送的字段保持原值。
            if payload.get("web_search") is None:
                web_search = bool(latest.get("web_search", False))
            else:
                web_search = bool(payload.get("web_search"))
            if payload.get("multimodal") is None:
                multimodal = bool(latest.get("multimodal", True))
            else:
                multimodal = bool(payload.get("multimodal"))
            log = EventLog(session_path, session_id)
            try:
                log.append(
                    "session_tools", web_search=web_search, multimodal=multimodal
                )
            finally:
                log.close()
            self._send_json(
                {"ok": True, "web_search": web_search, "multimodal": multimodal}
            )
            return

        if path.startswith("/api/sessions/") and path.endswith("/title"):
            resolved_session = self._session_file(path, "/title")
            if resolved_session is None:
                return
            session_id, session_path = resolved_session
            log = EventLog(session_path, session_id)
            try:
                log.append("session_title", title=str(payload.get("title") or "").strip())
            finally:
                log.close()
            self._send_json({"ok": True, "title": session_display_title(session_path)})
            return

        if path.startswith("/api/sessions/") and path.endswith("/delete"):
            resolved_session = self._session_file(path, "/delete")
            if resolved_session is None:
                return
            session_id, session_path = resolved_session
            # 正在生成的会话不能删：循环还握着那个文件的句柄，
            # 在 Windows 上会变成一句看不懂的 PermissionError。
            # 必须真的拿锁：`locked()` 与删除之间 chat 可以开跑，检查通过并不能
            # 保证删除时仍然没人写。拿到后覆盖整个删除操作，并在响应前释放（ADR-077）。
            lock = self.server.session_lock(session_id)  # type: ignore[attr-defined]
            if not lock.acquire(blocking=False):
                self._send_json({"error": _t("web.error.session_busy_delete")}, 409)
                return
            try:
                deleted = delete_session(session_path)
                # 删会话 = 连同它的附件目录一起删（契约 §2 / ADR-074）。
                # 刻意**不碰** `events.delete_session()` 的语义：那个函数的文档写着
                # "只做这一件事"，那条边界是被 ADR 保护过的 —— 扩展放在调用方这一层。
                removed = 0
                warning = ""
                try:
                    removed = attachments.delete_session_attachments(settings, session_id)
                except attachments.AttachmentError as exc:
                    # 会话已经删了，附件却没删掉：如实说出来，绝不假装干净。
                    # （静默降级会让"删了会话但图还在磁盘上"这件事永远没人发现。）
                    warning = str(exc)
                response: dict[str, Any] = {
                    "ok": deleted,
                    "session_id": session_id,
                    "attachments": removed,
                }
                if warning:
                    response["warning"] = _t(
                        "web.error.session_attachments_left", error=warning
                    )
            finally:
                lock.release()
            self._send_json(response)
            return

        if path.startswith("/api/sessions/") and path.endswith("/fork"):
            resolved_session = self._session_file(path, "/fork")
            if resolved_session is None:
                return
            session_id, session_path = resolved_session
            # 正在生成的会话不能分叉：循环还握着那个文件的句柄（同 delete 的理由）。
            # 与 delete 一样，非阻塞拿锁才足以消除"检查之后、复制之前"的竞态。
            lock = self.server.session_lock(session_id)  # type: ignore[attr-defined]
            if not lock.acquire(blocking=False):
                self._send_json({"error": _t("web.error.session_busy_fork")}, 409)
                return
            status = 200
            try:
                raw_seq = payload.get("seq")
                try:
                    before_seq = int(raw_seq)
                except (TypeError, ValueError):
                    before_seq = 0
                if before_seq <= 0:
                    status = 400
                    response = {"error": _t("web.error.fork_bad_seq")}
                else:
                    try:
                        new_sid, new_path = fork_session(settings, session_id, before_seq)
                    except ValueError as exc:
                        # fork_session 里的校验失败（seq 不是一条用户消息 / 前面没有上下文）。
                        status = 400
                        response = {"error": str(exc)}
                    else:
                        response = {
                            "ok": True,
                            "session_id": new_sid,
                            "title": session_display_title(new_path),
                        }
            finally:
                lock.release()
            # 所有成功/400 响应都在锁外发：客户端收到后可能立刻发起下一次操作，
            # 不能让它碰到一把实际上已经结束、却还没来得及释放的锁（ADR-077）。
            self._send_json(response, status)
            return

        if path == "/api/vault/file" or path == "/api/vault/file/delete":
            self._handle_vault_write(path, payload)
            return

        if path == "/api/dream/format":
            self._handle_dream_format(payload)
            return

        if path == "/api/memories/limit":
            # 改记忆条数上限。校验用的是与 CLI 共用的那个纯函数（"上限太低"之类）。
            from .memory_limit import MEMORY_FILE, MemoryLimitStore

            memory = open_memory(settings)
            try:
                active = memory.stats()["active"]
                store = MemoryLimitStore(
                    settings.data_dir / MEMORY_FILE, default=settings.memory_limit
                )
                change = store.set(payload.get("max_items"), active_count=active)
                if not change.ok:
                    self._send_json({"error": change.error}, 400)
                    return
                self._send_json(
                    {
                        "ok": True,
                        "limit": store.public_state(active_count=active),
                    }
                )
                # **这个 return 不能省。** 少了它，这个分支会继续往下走完整条
                # `if` 链，最后在同一条连接上**再写出一个 404**（"未知接口"）。
                # 而 `protocol_version = "HTTP/1.1"` 的连接是会被复用的：客户端
                # 把那个多余的响应当成**下一个请求**的回答 —— 界面上的表现是
                # "上限明明改成功了，紧接着的列表刷新报未知接口"。
                # 有测试钉住这一整类问题（tests/test_web.py 的
                # `test_every_route_answers_its_own_request`）。
                return
            finally:
                memory.close()

        if path == "/api/settings/language":
            # 切界面语言（ADR-085）。只改语言，**不动别的设置**。
            # 注意顺序：先落盘、再设进程语言 —— 反过来的话，"存失败"会变成
            # "这次生效了、重启又变回去"，那是最难查的一种不一致。
            store = language_store(settings)
            change = store.set(payload.get("language"))
            if not change.ok:
                self._send_json({"error": change.error}, 400)
                return
            i18n.set_language(change.language)
            self._send_json({"ok": True, **store.public_state()})
            return

        if path == "/api/settings/timezone":
            # 改 `time` 工具的时区偏移（「设置 → 常规」）。只动时区，不碰语言。
            # 不设进程级状态：时区是**下一轮对话**装配 Runtime 时才读的（见 runtime.py）。
            store = language_store(settings)
            result = store.set_timezone(payload.get("timezone"))
            if not result.get("ok"):
                self._send_json({"error": result.get("error")}, 400)
                return
            self._send_json({"ok": True, "timezone": result.get("timezone")})
            return

        if path == "/api/settings/output-limit":
            # 改单轮最大输出（ADR-101）。`-1` = 不限；也接受 `5k` / `1M` 写法。
            # 与语言不同，它是**下一轮对话**装配 Runtime 时才读的，所以生成中也能改。
            store = language_store(settings)
            result = store.set_max_output_tokens(payload.get("max_output_tokens"))
            if not result.get("ok"):
                self._send_json({"error": result.get("error")}, 400)
                return
            self._send_json(
                {"ok": True, "max_output_tokens": result.get("max_output_tokens")}
            )
            return

        if path == "/api/prompts/update":
            # 保存一条用户覆盖（契约 `docs/14-editable-prompts.md` §4.2）。
            # **只动** `payload["language"]` 那一门 —— 另一门由 prompts 层
            # "读-改-写整个文件"原样带回去（那是"改中文不影响英文"的实现处）。
            prompt_store.configure(settings.data_dir)
            try:
                result = prompt_store.save(
                    payload.get("language"),
                    payload.get("id"),
                    payload.get("text"),
                    max_chars=settings.prompts_max_chars,
                )
            except prompt_store.PromptError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(result)
            return

        if path == "/api/prompts/revert":
            # 复原：给了 id 复原那一条，没给就复原**这门语言**的全部（契约 §4.3）
            prompt_store.configure(settings.data_dir)
            try:
                result = prompt_store.revert(payload.get("language"), payload.get("id"))
            except prompt_store.PromptError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(result)
            return

        if path in ("/api/memories/update", "/api/memories/delete"):
            memory = open_memory(settings)
            try:
                try:
                    memory_id = int(payload.get("id"))
                except (TypeError, ValueError):
                    self._send_json({"error": _t("web.error.memory_id_not_int")}, 400)
                    return
                if path.endswith("/update"):
                    result = memory.edit(
                        memory_id,
                        content=payload.get("content"),
                        category=payload.get("category"),
                    )
                else:
                    result = memory.delete(
                        memory_id, reason=_t("web.memory.delete_reason")
                    )
            finally:
                memory.close()
            self._send_json(result, 200 if result.get("ok") else 400)
            return

        if path.startswith("/api/dreams/") and path.endswith("/undo"):
            # 回滚已取消（ADR-065：整理是单向门）。返回 404 而不是 400 ——
            # 这个接口**不存在了**，和"参数不对"是两回事（同 ADR-058 删别名接口的先例）。
            self._send_json({"error": _t("web.error.dream_no_undo")}, 404)
            return

        self._send_json({"error": _t("web.error.unknown_api", path=path)}, 404)

    # -- SSE 业务 ----------------------------------------------------

    def _handle_attachment_upload(self) -> None:
        """`POST /api/attachments`（契约 §7.1）。请求体由本函数自己读。

        为什么绕开 `do_POST` 的通用路径：`_read_json()` 是无上限的
        `rfile.read(length)`，而上传是**唯一**一个请求体可能大到上兆字节的接口。
        这里先拿 `Content-Length` 和 `settings.attachment_max_bytes` 比一次，
        超标就立刻 400 并把连接体读干净丢掉（分块），绝不先读进内存再拒绝。
        """
        settings = self.settings
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        limit = int(settings.attachment_max_bytes)
        if length < 0:
            self._send_json({"error": _t("web.error.bad_content_length")}, 400)
            return
        if length > _attachment_body_limit(limit):
            self._drain_body()
            self._send_json(
                {
                    "error": _t(
                        "web.error.attachment_too_big",
                        body_size=_human_size(length),
                        limit=_human_size(limit),
                    )
                },
                400,
            )
            return
        payload = self._read_json()
        # `None` = JSON 坏了（与"没带 body / 空对象"分开报，见 `_read_json` 的注释）。
        # 上传这条路的既有人话文案不变 —— 它已经把"要哪些字段"说清楚了。
        if not payload:
            self._send_json({"error": _t("web.error.attachment_bad_shape")}, 400)
            return

        requested = str(payload.get("session_id") or "").strip()
        if requested:
            # 会话 id 必须过 `_session_path()` 那道白名单：不合法就直接 400，
            # 而且**不碰磁盘**（这个函数自己也不建目录/文件）。
            if _session_path(settings, requested) is None:
                self._send_json(
                    {
                        "error": _t(
                            "web.error.bad_session_id",
                            session_id=_short_session_id(requested),
                        )
                    },
                    400,
                )
                return
            session_id = requested
        else:
            # 空 id = 新会话：只生成一个 id 返回，**不创建 `.jsonl`**
            # （用户加了图却还没发出去，不该凭空多出一个空会话）。
            session_id = new_session_id()

        try:
            data = attachments.decode_base64_data(payload.get("data_base64"))
            record = attachments.store(
                settings,
                session_id,
                data,
                name=str(payload.get("name") or ""),
                declared_mime=str(payload.get("mime") or ""),
            )
        except attachments.AttachmentError as exc:
            result: dict[str, Any] = {"error": str(exc)}
            # `need` 是"缺什么才能做成"的机器可读字段（目前只有 pillow）：
            # 前端据此显示"先跑 run.cmd --setup"，而不是让用户对着一句"缺少依赖"发愣。
            if exc.need:
                result["need"] = exc.need
            if exc.warnings:
                result["warnings"] = list(exc.warnings)
            self._send_json(result, 400)
            return
        self._send_json(_upload_result(session_id, record))

    def _handle_chat(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        settings = self.settings
        # 附件由前端给 `[{id, ext, name}]`；**这里只做形状检查**，
        # 真正"这张图在不在、多大、什么格式"由 attachments.records_for 从磁盘读。
        raw_attachments = payload.get("attachments")
        if raw_attachments in (None, ""):
            wanted: list[Any] = []
        elif isinstance(raw_attachments, list):
            wanted = list(raw_attachments)
        else:
            self._send_json({"error": _t("web.error.attachments_not_array")}, 400)
            return
        # 老规则"内容不能为空"放宽为"文字与图片至少有一个"（契约 §7.3）：
        # 只贴图不说话是合法用法，不该把这样的用户挡在外面；但两个都空仍然要拒。
        if not text and not wanted:
            self._send_json({"error": _t("web.error.empty_message")}, 400)
            return

        # 会话 id 来自请求体，**必须校验**：这里直接拼
        # `sessions_dir / f"{id}.jsonl"` 并创建/追加文件（见 Runtime.open_session），
        # 所以一个带 ".." 与路径分隔符的 id 会在数据目录外面写出一个真事件日志。
        # 注意：空 id 是**合法**输入，含义是"开一个新会话"（前端新对话时
        # 送的就是 null/空串），所以空值走 new_session_id()，非法值才 400。
        requested = str(payload.get("session_id") or "").strip()
        if requested:
            session_path = _session_path(settings, requested)
            if session_path is None:
                self._send_json(
                    {
                        "error": _t(
                            "web.error.bad_session_id",
                            session_id=_short_session_id(requested),
                        )
                    },
                    400,
                )
                return
            session_id = requested
        else:
            session_id = new_session_id()
            session_path = _session_path(settings, session_id)
            if session_path is None:  # 理论上不可能：id 是我们自己生成的
                self._send_json(
                    {"error": _t("web.error.internal_bad_session_id")}, 500
                )
                return
        # 附件的校验与读盘**必须在拿锁之前**：前端给的是 `{id, ext, name}`，
        # 真正的路径由 attachments.records_for 在该会话目录里重新解析并读尺寸 ——
        # 前端给的任何路径/尺寸一概不用（改一个请求体就能让日志记下一张不存在的图，
        # 那是这类接口最典型的坏法）。失败一律 400，且一张图都不会进日志。
        try:
            records = attachments.records_for(settings, session_id, wanted)
        except attachments.AttachmentError as exc:
            result: dict[str, Any] = {"error": str(exc)}
            if exc.need:
                result["need"] = exc.need
            self._send_json(result, 400)
            return
        lock = self.server.session_lock(session_id)  # type: ignore[attr-defined]
        if not lock.acquire(blocking=False):
            self._send_json({"error": _t("web.error.session_busy_chat")}, 409)
            return

        # **每一个 return 路径都必须放开锁**：这里改成了"try 里随便 return、
        # finally 统一 release"。原来的写法在 `resolve_provider` 抛 ProviderError
        # 时是直接 return 的，而那条路径不经过 except，锁就永久留在手里
        # （之后这个会话每次发消息都是"正在生成中"，只能重启服务）。
        done_payload: dict[str, Any] | None = None
        try:
            # 会话内直接可调：前端把选中的接口/模型/档位随消息一起送上来。
            # 没送就用这个会话上次存的，再没有就用全局默认。
            stored = _session_generation(session_path)
            # 「联网搜索」开关：前端送来就用它（新会话还没落盘，只能这样传），
            # 否则用这个会话上次存的，再没有就默认**关**。
            stored_tools = session_tools(session_path)
            if payload.get("web_search") is None:
                web_search = bool(stored_tools.get("web_search", False))
            else:
                web_search = bool(payload.get("web_search"))
            # 「多模态」开关（契约 §13.1）：前端送来就用它，否则用这个会话上次存的，
            # 再没有就 **默认开** —— 它等于上一轮的行为，不该因为加了个开关
            # 就悄悄改变既有会话的行为。
            if payload.get("multimodal") is None:
                multimodal = bool(stored_tools.get("multimodal", True))
            else:
                multimodal = bool(payload.get("multimodal"))
            modality = "image" if multimodal else "text"
            try:
                resolved = resolve_provider(
                    settings,
                    provider_id=str(payload.get("provider") or stored.get("provider") or ""),
                    model=str(payload.get("model") or stored.get("model") or ""),
                    effort=str(payload.get("effort") or stored.get("effort") or ""),
                )
            except ProviderError as exc:
                self._send_json({"error": str(exc)}, 400)
                return

            # -- 契约 §13.8：发送前的闸（在写任何事件、起任何 SSE 之前）--------
            # 为什么必须在发出去之前判：实测 v4-pro 收到图片**不报错**（HTTP 200），
            # 只是把图静默丢掉然后答"无法确定" —— 等 API 报错的判据永远不会触发。
            # 判据与"历史里有图"的提醒共用 providers.store.vision_support 这一处实现。
            # **这不是 fallback**：不改模态、不换模型、不重试，只是把话说到位。
            vision, vision_source = vision_support(
                resolved.profile, resolved.choice.model
            )
            if records and modality == "image" and vision is False:
                self._send_json(
                    {
                        "error": vision_refusal_message(
                            resolved.choice.model, vision_source
                        ),
                        "need": "vision",
                    },
                    400,
                )
                return

            # -- 惰性起 SSE ---------------------------------------------------
            # 为什么不在最前面无条件 `_start_sse()`：OCR 引擎不可用时必须回一个
            # **干净的 400 JSON**（契约 §13.2），而 HTTP 状态行一旦写出去就再也改不了。
            # 第一帧真要发出去的那一刻才起流，于是"引擎不可用"（它发生在任何
            # emit 之前）仍然留得住 400 这条路，而每张图之前的进度 notice 照样实时。
            sse_started = False

            def begin_sse() -> None:
                nonlocal sse_started
                if sse_started:
                    return
                sse_started = True
                self._start_sse()
                self._sse("session", {"session_id": session_id})
                self._sse("generation", resolved.choice.to_dict())
                # `tools` 帧要**同时**回两个开关（契约 §13.1）
                self._sse(
                    "tools", {"web_search": web_search, "multimodal": multimodal}
                )

            def emit(kind: str, **data: Any) -> None:
                forwarded = forward_event(kind, data)
                if forwarded is None:
                    return
                try:
                    # 起流的握手帧也在 try 里面：客户端已经跑了的话，这里失败要变成
                    # ClientGone（本轮中止），而不是一个从 OCR 里冒出来的裸 OSError。
                    if not sse_started:
                        begin_sse()
                    self._sse(forwarded[0], forwarded[1])
                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    ConnectionAbortedError,
                    OSError,
                ) as exc:
                    raise ClientGone(str(exc)) from exc

            # -- 契约 §13.2：OCR 时机 -----------------------------------------
            # **拿到附件记录之后、`loop.run()` 之前**，而且**只在多模态关着时**跑。
            # 开着多模态还去 OCR 是白烧 CPU 与时间（实测单张 1.5~3.4s），
            # 图片本来就要原样发出去。
            if records and modality == "text":
                local_ocr = _load_ocr()
                try:
                    records = local_ocr.fill_records(
                        records, data_dir=settings.data_dir, emit=emit
                    )
                except local_ocr.OcrUnavailable as exc:
                    if not sse_started:
                        # 一帧都没发过 → 还能干净地回 400。**一个事件都不写**：
                        # 不留"用户消息已经进日志、但图没发出去"的半截状态。
                        self._send_json(ocr_unavailable_payload(exc), 400)
                    else:
                        # 极端情形：引擎在第 1 张之后才不可用，而 SSE 已经起了
                        # （HTTP 状态行已经写出去，回不了 400 了）。如实报出来并
                        # 收尾 —— 同样**没有写任何事件**。
                        emit(
                            "notice",
                            level="error",
                            text=ocr_unavailable_payload(exc)["error"],
                        )
                        done_payload = {
                            "session_id": session_id,
                            "stop_reason": "ocr_unavailable",
                            "text": "",
                        }
                    return

            begin_sse()
            with Runtime(
                resolved.settings,
                session_id,
                provider=resolved.provider,
                generation=resolved.choice,
                web_search=web_search,
            ) as runtime:
                # 开关变了（或新会话第一次）就记一条，刷新后前端才能恢复。
                # **两个键一起写**：只写一个的话，读端会把另一个当成"没记录过"，
                # 于是下一次请求对着默认值走，用户刚设的那个开关就悄悄失效了。
                if stored_tools.get("web_search") != web_search or stored_tools.get(
                    "multimodal", True
                ) != multimodal:
                    runtime.log.append(
                        "session_tools", web_search=web_search, multimodal=multimodal
                    )
                # 把这次的选择记进事件日志：刷新页面后下拉框才能恢复到同一个模型
                if stored.get("model") != resolved.choice.model or stored.get(
                    "provider"
                ) != resolved.choice.provider_id:
                    runtime.log.append(
                        "session_settings",
                        provider=resolved.choice.provider_id,
                        model=resolved.choice.model,
                        effort=resolved.choice.effort,
                        label=resolved.choice.label,
                    )
                runtime.loop.emit = emit
                # 汇总 notice 必须在 `run()` **之前**发出去：客户端先看到"发生了什么"
                # （图片没有发出去、发的是 OCR 文字），再看到模型的流式输出（§13.7）。
                if records and modality == "text":
                    emit("notice", level="info", text=ocr_summary_notice(records))
                # 契约 §13.8 末条：这一条**没有附件**，但历史里有图，而模型被判"不支持"
                # —— 照常发送，只在前面提一句。不拒绝、不改写历史、不自动改模态。
                # `conversation()` 只在这一条真的可能触发时才读（它有代价：重放整份日志）。
                if not records and vision is False:
                    count = history_image_count(runtime.log)
                    if count:
                        emit(
                            "notice",
                            level="warn",
                            text=history_image_notice(resolved.choice.model, count),
                        )
                outcome = runtime.loop.run(text, attachments=records, modality=modality)
            done_payload = {
                "session_id": session_id,
                "stop_reason": outcome.stop_reason,
                "text": outcome.text,
            }
        except ClientGone:
            # 用户在浏览器里点了"停止"：本轮已经写进日志的 partial 结果保留
            done_payload = None
            self._safe_sse(
                "notice",
                {"level": "warn", "text": _t("web.error.client_gone_turn")},
            )
        finally:
            lock.release()
        # **终帧在释放会话锁之后发**（同 `_handle_dream_format` 的规矩）：
        # 这一帧告诉界面"本轮结束了"，界面收到它就会立刻允许下一次发送、
        # 也允许删这个会话 —— 而那两个动作都会被同一把锁挡住。
        # 一边握着锁一边宣告"跑完了"，用户就会看到一句假的"这个会话正在生成中"。
        # 发送本身不碰会话文件（`Runtime` 已经关了），所以放在锁外是安全的。
        if done_payload is not None:
            self._safe_sse("done", done_payload)

    def _handle_providers(self, path: str, payload: dict[str, Any]) -> None:
        store = provider_store(self.settings)
        try:
            if path == "/api/providers/save":
                profile_id = str(payload.get("id") or "").strip()
                existing = store.get(profile_id)
                preset = get_preset(str(payload.get("preset") or ""))
                if existing is None and preset is not None:
                    profile = ProviderProfile.from_preset(preset)
                elif existing is not None:
                    profile = existing
                else:
                    profile = ProviderProfile(id=profile_id, label=profile_id, base_url="")

                if payload.get("label") is not None:
                    profile.label = str(payload["label"]).strip() or profile.id
                if payload.get("base_url") is not None:
                    profile.base_url = str(payload["base_url"]).strip()
                if payload.get("api_key"):
                    # 留空表示"不改动已有 key"，避免前端拿不到完整 key 时把它清掉
                    profile.api_key = str(payload["api_key"]).strip()
                if payload.get("thinking_options") is not None:
                    raw = payload["thinking_options"]
                    items = raw if isinstance(raw, list) else str(raw).split(",")
                    profile.thinking_options = [
                        str(x).strip() for x in items if str(x).strip()
                    ]
                if payload.get("models") is not None:
                    raw = payload["models"]
                    items = raw if isinstance(raw, list) else str(raw).split(",")
                    profile.models = [str(x).strip() for x in items if str(x).strip()]
                if payload.get("vision_models") is not None:
                    # 「支持图片的模型」名单（契约 §13.8）。界面上是一行一个/逗号分隔，
                    # 空串 = 清空 = **未知**（不再拦），而不是"谁都不支持"。
                    raw = payload["vision_models"]
                    items = raw if isinstance(raw, list) else str(raw).split(",")
                    profile.vision_models = [str(x).strip() for x in items if str(x).strip()]
                if payload.get("send_reasoning_content") is not None:
                    profile.send_reasoning_content = bool(payload["send_reasoning_content"])
                if payload.get("supports_tools") is not None:
                    profile.supports_tools = bool(payload["supports_tools"])
                if payload.get("notes") is not None:
                    profile.notes = str(payload["notes"]).strip()
                if payload.get("type") is not None:
                    profile.type = (
                        "anthropic" if str(payload["type"]).strip() == "anthropic" else "openai"
                    )
                if payload.get("model_limits") is not None:
                    profile.model_limits = _parse_model_limits(payload["model_limits"])
                if payload.get("model_labels") is not None:
                    # 显示名（可选）：只影响下拉框与卡片上的标题，不参与任何决策，
                    # 所以空表 = 全部退回模型 id，不是"清空成空名字"。
                    profile.model_labels = _parse_model_labels(payload["model_labels"])

                store.upsert(profile)
                self._send_json({"ok": True, "state": store.public_state()})
                return

            if path == "/api/providers/default":
                default = store.set_defaults(
                    provider=payload.get("provider"),
                    model=payload.get("model"),
                    effort=payload.get("effort"),
                )
                self._send_json({"ok": True, "default": default})
                return

            if path == "/api/providers/dream":
                # 整理（dream）专用的模型选择（ADR-103）。空 provider + 空 model
                # = **清掉它、跟随全局默认**（界面上那一项叫「跟随默认」）。
                #
                # 校验放在这里而不是 store 里：store 与 `set_defaults` 一样"只存不判"。
                # 但这条路的输入来自界面下拉框，**存进去的东西必须是能用的** ——
                # 存一个不存在的接口，下次整理会在离原因很远的地方报错。
                provider_id = str(payload.get("provider") or "").strip()
                model = str(payload.get("model") or "").strip()
                if not provider_id and not model:
                    dream = store.set_dream_defaults(
                        provider="", model="", effort=""
                    )
                    self._send_json({"ok": True, "dream": dream})
                    return
                if not provider_id:
                    self._send_json(
                        {"error": _t("web.error.dream_provider_required")}, 400
                    )
                    return
                profile = store.get(provider_id)
                if profile is None:
                    self._send_json(
                        {
                            "error": _t(
                                "web.error.no_provider_profile", profile_id=provider_id
                            )
                        },
                        404,
                    )
                    return
                if not profile.configured:
                    # 没有 key 的接口不会出现在下拉框里，但手写请求可以送到这里。
                    # 存下去只会让下次整理失败，所以现在就拒。
                    self._send_json(
                        {
                            "error": _t(
                                "web.error.provider_not_configured",
                                label=profile.label,
                            )
                        },
                        400,
                    )
                    return
                if not model:
                    self._send_json(
                        {"error": _t("web.error.dream_model_required")}, 400
                    )
                    return
                if profile.models and model not in profile.models:
                    # 列表里的模型才是界面能选到的；不在列表里说明请求是手写的
                    # （或者接口档案刚被改过）。不认它，别到真实请求时才炸。
                    self._send_json(
                        {
                            "error": _t(
                                "web.error.dream_model_unknown",
                                model=model,
                                label=profile.label,
                            )
                        },
                        400,
                    )
                    return
                dream = store.set_dream_defaults(
                    provider=profile.id, model=model, effort=""
                )
                self._send_json({"ok": True, "dream": dream})
                return

            if path == "/api/providers/fetch-models":
                profile_id = str(payload.get("id") or "").strip()
                profile = store.get(profile_id)
                if profile is None:
                    self._send_json(
                        {
                            "error": _t(
                                "web.error.no_provider_profile", profile_id=profile_id
                            )
                        },
                        404,
                    )
                    return
                models = fetch_models(profile.base_url, profile.api_key)
                store.update_models(profile_id, models)
                self._send_json({"ok": True, "models": models})
                return

            if path == "/api/providers/delete":
                profile_id = str(payload.get("id") or "").strip()
                store.remove(profile_id)
                self._send_json({"ok": True, "state": store.public_state()})
                return
        except ProviderError as exc:
            self._send_json({"error": str(exc)}, 400)
            return

        self._send_json({"error": _t("web.error.unknown_api", path=path)}, 404)

    def _handle_dream(self, payload: dict[str, Any]) -> None:
        """SSE：跑一次整理。锁在这一层拿/放，流程在 `_run_dream`。"""
        # **明确拒绝 dry_run**，而不是忽略它（ADR-072）。
        # 这一条很重要：一个还缓存着旧页面的浏览器会 POST `dry_run: true` 并**以为
        # 不会改动任何东西**；如果我们只是把那个字段丢掉，它就变成了一次**真整理** ——
        # 用户以为在预演、实际在写档案。宁可报错。
        if "dry_run" in payload:
            self._send_json({"error": _t("web.error.dream_dry_run")}, 400)
            return
        lock = self.server.dream_lock  # type: ignore[attr-defined]
        if not lock.acquire(blocking=False):
            self._send_json({"error": _t("web.error.dream_busy")}, 409)
            return
        done: dict[str, Any] | None = None
        try:
            # `_run_dream` 只把终帧的**载荷**交回来，不发它（见下面）。
            done = self._run_dream(payload)
        finally:
            lock.release()
        # **终帧在锁之外发**（与 `_handle_dream_format` 同一条规矩）。
        # 客户端收到 dream_done 就会立刻认为"整理结束、可以动档案了"，
        # 紧接着去写 `/api/vault/file` —— 而那个接口用 `lock.locked()` 判断。
        # 一边握着整理锁、一边宣告"跑完了"，用户就会看到一句假的"整理正在运行"。
        if done is not None:
            self._safe_sse("dream_done", done)

    def _handle_dream_format(self, payload: dict[str, Any]) -> None:
        """格式化并初始化 dream 层（ADR-072）。**不可逆**，所以要确认词。"""
        from .dream import dream_format_confirm, format_dream

        # 确认词**现取**：它跟着界面语言走（中文「格式化」/ 英文 `format`），
        # 前端从接口拿这个词、不硬编码，所以换语言时它也跟着换。
        confirm_word = dream_format_confirm()
        if str(payload.get("confirm") or "").strip() != confirm_word:
            self._send_json(
                {
                    "error": _t("web.error.dream_confirm_word", word=confirm_word),
                    "confirm_word": confirm_word,
                },
                400,
            )
            return
        lock = self.server.dream_lock  # type: ignore[attr-defined]
        if not lock.acquire(blocking=False):
            self._send_json({"error": _t("web.error.dream_busy_format")}, 409)
            return
        result: dict[str, Any] | None = None
        error: str | None = None
        # 锁一拿到就进 try：连 `open_memory()` 抛异常也要能放开锁。
        # 否则一次失败就会把整理锁永远握在手里 —— 之后每次格式化/整理都 409。
        try:
            memory = open_memory(self.settings)
            try:
                result = format_dream(self.settings, memory)
            except ValueError as exc:  # 目录不在数据目录里（防手滑的那道检查）
                error = str(exc)
            finally:
                memory.close()
        finally:
            lock.release()
        # **结果必须在临界区之外发出去。**
        # 先 `_send_json(200)` 再在 finally 里释放锁的话，客户端读到 200 的那一刻
        # 服务端线程未必已经执行到 finally —— 紧接着的下一个请求（再格式化一次、
        # 或马上去保存档案）会拿到一句无中生有的"整理正在运行"。
        # 它只在请求**背靠背**时出现（中间插一句 print 就没了），所以藏了很久。
        if error is not None:
            self._send_json({"error": error}, 400)
            return
        self._send_json(result if result is not None else {})

    def _run_dream(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """跑一次整理：过程帧直接推给客户端，**终帧的载荷返回给调用方**。

        为什么不在这里直接发 `dream_done`：调用方（`_handle_dream`）握着整理锁，
        而"宣告完成"必须在释放锁之后才允许到达客户端（同 `_handle_dream_format`）。
        返回 None 表示"没有终帧要发"（提前用 JSON 报错，或客户端已经断开）。
        """
        settings = self.settings
        mode = "apply"
        raw_limit = payload.get("limit")
        try:
            limit = int(raw_limit) if raw_limit else None
        except (TypeError, ValueError):
            limit = None

        # **先看有没有活干，再去解析接口**（CLI 就是这个顺序）：没有待整理的记录
        # 却因为"没配接口"报错，会把用户引到一个不相干的设置页。
        memory = open_memory(settings)
        try:
            pending = summarize_pending(
                pending_sessions(settings.sessions_dir, memory.cursor_map())
            )
        finally:
            memory.close()
        if not pending["sessions"]:
            self._start_sse()
            self._sse("dream_start", {"run_id": "", "mode": mode, "pending": pending})
            return {
                "run_id": "",
                "mode": mode,
                "changed": 0,
                "skipped": True,
                "sessions": [],
                "advanced": {},
                "summary": _t("ui.dream.nothing_pending"),
                "diff": {},
                "check": {},
                "stop_reason": "nothing_pending",
                "usage": {},
            }

        # 整理用哪个模型（ADR-103）：**默认由 Dream 页的设置决定**，CLI 与 Web
        # 共用同一份。payload 里仍接受 provider/model 作为**一次性覆盖** ——
        # 旧页面（缓存着旧 app.js 的那种）会带上来；把这两个字段悄悄丢掉等于
        # 在用户不知情的情况下换了个整理模型。新前端已经不再发它们。
        override_provider = str(payload.get("provider") or "").strip()
        override_model = str(payload.get("model") or "").strip()
        try:
            if override_provider or override_model:
                resolved = resolve_provider(
                    settings,
                    provider_id=override_provider,
                    model=override_model,
                    effort=str(payload.get("effort") or ""),
                )
            else:
                resolved = resolve_dream_provider(settings)
        except ProviderError as exc:
            self._send_json({"error": str(exc)}, 400)
            return None

        memory = open_memory(settings)
        try:
            runner = DreamRunner(
                provider=resolved.provider,
                # 用**折算过的** settings：context_window 已按本轮选中的模型解析，
                # 否则 dream 的窗口保证会拿兜底的 200k 去算（ADR-100）。
                settings=resolved.settings,
                memory=memory,
                vault=VaultStore(
                    settings.vault_dir,
                    read_max_chars=settings.vault_read_max_chars,
                    write_max_chars=settings.vault_write_max_chars,
                    max_files=settings.vault_max_files,
                    name_max_chars=settings.vault_name_max_chars,
                    readme_token_budget=settings.vault_readme_token_budget,
                ),
                emit=None,  # 下面替换成 SSE
                max_sessions=limit,
                generation=resolved.choice.to_dict(),
            )
            self._start_sse()
            self._sse(
                "dream_start",
                {
                    "run_id": runner.run_id,
                    "mode": mode,
                    "pending": {
                        "sessions": len(runner.pending),
                        "events": sum(item.new_events for item in runner.pending),
                        "detail": [item.to_dict() for item in runner.pending],
                    },
                    "generation": resolved.choice.to_dict(),
                },
            )
            runner.loop.emit = self._make_emitter()
            outcome = runner.run()
            return {
                "run_id": outcome.run_id,
                "mode": outcome.mode,
                "changed": outcome.changed,
                "skipped": outcome.skipped,
                "sessions": outcome.sessions,
                "advanced": outcome.advanced,
                "summary": outcome.summary,
                "diff": outcome.diff,
                "check": outcome.check,
                "stop_reason": outcome.stop_reason,
                "usage": outcome.usage.to_dict(),
                # 这一轮用的是哪个模型（ADR-103）：运行记录里已经有一份，
                # 终帧也带上，界面要在"整理完成"那一刻说清是谁做的。
                "generation": outcome.generation,
            }
        except ClientGone:
            # 客户端没了：这一轮的结果已经写在磁盘上，但**没有人在听**，
            # 所以不返回终帧（返回了也只是往一个关掉的连接里写）。
            self._safe_sse(
                "notice", {"level": "warn", "text": _t("web.error.client_gone_dream")}
            )
            return None
        finally:
            memory.close()


# --------------------------------------------------------------------------
# 服务器
# --------------------------------------------------------------------------


class HarnessServer(ThreadingHTTPServer):
    daemon_threads = True

    # `SO_REUSEADDR` 在两个平台上的语义**不一样**，而 Windows 上的那个会咬人：
    #
    #   Unix   —— 允许绑一个还处于 TIME_WAIT 的地址（重启不用等）。两个**在听**的
    #             套接字仍然不能占同一个端口，所以开着它是安全的。
    #   Windows —— 它是"允许抢占/重复绑定"：实测两个都设了它的进程能**同时**绑住
    #             同一个端口，谁收到请求看运气。
    #
    # 2026-10-16 就被这个骗过一次：一个自检脚本打到了另一个实例上，两边的界面语言
    # 互相覆盖，看起来像"设置自己变了"。所以 Windows 上关掉它 —— 第二次启动会拿到
    # 真正的 EADDRINUSE，`serve()` 再把那句话讲清楚。
    # （实测关掉之后"关掉再立刻重启"依然成功，所以没付出重启变难的代价。）
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], handler: type[HarnessHandler]) -> None:
        super().__init__(address, handler)
        self.settings: Settings = Settings.load()
        self.verbose = True
        self.dream_lock = threading.Lock()
        # 只有 start.cmd 的引导模式会放入 manager；普通 --web 不改变既有启动行为。
        self.venv_setup: VenvSetupManager | None = None
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def session_lock(self, session_id: str) -> threading.Lock:
        """同一个会话同时只允许一轮生成，否则两份日志会交错写坏历史。"""
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def forget_sessions(self) -> None:
        """恢复出厂设置之后调用：旧会话的锁已经没有任何意义了。"""
        with self._guard:
            self._locks.clear()


def is_elevated() -> bool:
    """当前进程是不是以**提升的权限**在跑（Windows 上就是"以管理员身份运行"）。

    其它平台恒为 False：这里要处理的是一段 Windows 特有的行为（见 `launch_browser`），
    别的系统上"提权"不是一个会传染给浏览器的概念。

    探测失败一律当作"普通权限"：**这个判断只影响怎么打开浏览器，不该有能力拦住启动**。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def launch_browser(url: str, *, elevated: bool | None = None) -> tuple[bool, str]:
    """把界面在浏览器里打开，返回 `(成功了吗, 给人看的一句话)`。

    这里两条规则都是**实测踩出来的**，不是预防性设计：

    1. **提权窗口里不能用 `os.startfile`。**
       `webbrowser.open()` 在 Windows 上走 `windows-default` → `os.startfile`
       → ShellExecute，而 ShellExecute 会**拿着调用者的安全令牌**去拉起浏览器 ——
       于是浏览器也被抬成管理员权限。Edge/Chrome 又有"启动增强"和
       "关闭后继续运行后台扩展"，那个管理员实例会活很久很久；
       等你下次从**普通**窗口再启动一次，系统就弹
       「Microsoft Edge 未响应，因为现有实例正在以提升的权限运行，是否要用普通权限重启？」
       —— **症状出现在原因之后很久**，所以看起来像"随机抽风"。
       降权的正规做法是让 **Explorer** 去转交这个 URL：Explorer 自己是普通权限，
       由它开口，浏览器就不会被抬权。
    2. **失败必须说出来 —— 但要清楚它能覆盖到什么程度。**
       `webbrowser.open()` 内部是一条**回退链**（Windows 上是
       `windows-default` → 找到的浏览器 exe），它对"第一个启动器失败"是**静默换下一个**的：
       实测拿一个没有关联程序的协议去开，它照样返回 `True` ——
       因为它在 `os.startfile` 失败之后，直接拿 `msedge.exe` 把那个 URL 又开了一次。
       所以：

       * 返回 `True` 只能说明"**至少有一个启动器没报错**"，**不保证**页面真的显示出来了；
       * 系统级弹框（就是那条"现有实例正在以提升的权限运行"）在这里**探测不到** ——
         它不是任何一次调用的返回值，是 Windows 在交接 URL 时弹的。

       结论：**这一层是"整条链都打不开"（例如机器上根本没装浏览器）的兜底，
       不是权限问题的解药**。权限问题只能靠**预防**（别在提权窗口里启动，见上面第 1 条）。
    """
    if elevated is None:
        elevated = is_elevated()
    # `explorer.exe` 只有 Windows 有；别的平台即便"elevated"也走普通路径
    if elevated and os.name == "nt":
        try:
            subprocess.Popen(
                ["explorer.exe", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return False, f"浏览器没有自动打开（{exc}）"
        return True, "已请 Explorer 用普通权限打开"
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - 打不开浏览器绝不能让 Web 端起不来
        return False, f"浏览器没有自动打开（{exc}）"
    if not opened:
        return False, "浏览器没有自动打开（系统里没有可用的默认浏览器）"
    return True, ""


def _open_ui_in_browser(url: str) -> None:
    """`serve()` 在后台线程里调它。打不开就**明说**，并把地址再给一遍。"""
    ok, message = launch_browser(url)
    if not ok:
        print(f"  ! {message}。请手动打开：{url}", flush=True)


def port_is_taken(host: str, port: int) -> bool:
    """这个端口上是不是已经有人在听。**不带 `SO_REUSEADDR`** 地试绑一次来判定。

    为什么不用 connect 去探：① 有的进程绑了端口但不听；② 更要紧的是，
    "另有一个 harness 占着这个端口"这件事在 Windows 上**只有普通 bind 测得出来**
    —— 两个都设了 `SO_REUSEADDR` 的进程能同时绑住同一个端口（见 `HarnessServer`
    那段注释），connect 打过去只会得到"通"，然后你不知道会接到谁。

    `port=0` 表示"随便挑一个空闲端口"，永远不算被占用。
    """
    if not port:
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        probe.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
    verbose: bool = True,
    guide_venv: bool = False,
) -> int:
    # **先确认端口是空的，再动手**（ADR-080：能预防就别只提示）。
    # 以前这里直接构造服务器：Windows 上两个实例能同时绑住 8787，
    # 于是"我明明改了设置，界面却没变"这种问题会查很久 —— 其实你连的是另一个实例。
    if port_is_taken(host, port):
        print(f"{PRODUCT_NAME} · Web 端", flush=True)
        print(f"  ! 端口 {port} 上已经有一个服务在听了，所以**不启动第二个**。", flush=True)
        print(f"    换一个端口：run.cmd --web --port {port + 1}", flush=True)
        print(
            "    或者先关掉那一个 —— 它可能是另一个窗口里的 harness，也可能是别的程序"
            "（想知道是谁：netstat -ano | findstr :%d）" % port,
            flush=True,
        )
        return 1
    try:
        server = HarnessServer((host, port), HarnessHandler)
    except OSError as exc:  # 探测与真正绑定之间被别的进程抢了（或在别的平台上被拒）
        print(f"{PRODUCT_NAME} · Web 端", flush=True)
        print(f"  ! 端口 {port} 绑不上：{exc}", flush=True)
        print(f"    换一个端口：run.cmd --web --port {port + 1}", flush=True)
        return 1
    # 界面语言（ADR-085）：进程一起来就按配置设一次，之后每次构造提示词都现取
    # （所以"设置里改一下"下一轮就生效，不需要重启）。
    i18n.apply_settings(settings)
    settings = replace(settings, language=i18n.current())
    server.settings = settings
    server.verbose = verbose
    # port=0 由操作系统在 bind 时选空闲端口；之后必须读真实边界，不能再拿 0 拼 URL。
    bound_port = int(server.server_address[1])  # type: ignore[attr-defined]
    url = f"http://{host}:{bound_port}/"
    if guide_venv:
        server.venv_setup = VenvSetupManager(ROOT)  # type: ignore[attr-defined]
        # 先完成 bind，再检查 import/pip：检查慢时浏览器也已经有加载页可看。
        server.venv_setup.begin()  # type: ignore[attr-defined]

    print(f"{PRODUCT_NAME} · Web 端", flush=True)
    print(f"  {settings.describe()}", flush=True)
    print(f"  界面：{url}", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("  ! 正在监听非本机地址：记忆库与聊天记录对网络可见，请确认这是你要的", flush=True)
    # 提权窗口要**当场提醒**：这一次看起来一切正常，坏处是下一次用普通窗口启动时
    # 才爆出来（那个系统弹框问的是"要不要用普通权限重启现有实例"）。见 launch_browser。
    if open_browser and is_elevated():
        print(
            "  ! 这个窗口是管理员权限：直接开浏览器会把它也抬成管理员，\n"
            "    之后用普通窗口启动就会弹「现有实例正在以提升的权限运行」。\n"
            "    已改用 Explorer 以普通权限打开（想彻底没这回事，用普通窗口跑 run.cmd 即可）。",
            flush=True,
        )
    print("  Ctrl+C 停止", flush=True)

    if open_browser:
        # 0.6 秒是让"界面：…"那几行先落到终端上，免得被浏览器抢在前面
        threading.Timer(0.6, lambda: _open_ui_in_browser(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        manager = getattr(server, "venv_setup", None)
        if manager is not None:
            manager.shutdown()
        server.server_close()
    return 0
