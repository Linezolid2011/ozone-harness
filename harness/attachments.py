"""图片附件的落盘层：嗅探、校验、缩放、原子写入、引用解析、清理。

这个模块**不认识 HTTP**（请求解析、状态码、响应 JSON 都在 web.py），
它只回答一个问题：一段字节能不能作为图片附件留住、留在哪、怎么再读回来。
把这条边界划出来，是因为下面几件事都必须在同一处做，而且只能有一份实现：

* **格式只按内容判断**。前端声明的 MIME 和文件名都不可信：把 `.html` 改成 `.png`
  再声明 `image/png` 是零成本的，而我们最终会把这段字节 base64 成 data URL 交给
  模型接口。所以 `ext` 由魔数嗅探决定，声明的 MIME 只用来记一条不一致的 warning。
* **绝不接受 SVG**。它不是位图，可以带脚本；一旦被浏览器按 `image/svg+xml` 渲染，
  就等于在界面这个 origin 上执行外部内容。所以白名单里没有它，而且任何情况下都不加。
* **Pillow 只负责"能不能解开"和"有多大"**，不负责判定格式 —— 判定格式是嗅探的事。
  两者都要：嗅探挡住"根本不是图片"，Pillow 挡住"魔数对但内容坏/伪装"。
* **Pillow 缺失时明确失败**，不降级成"原样落盘"。没被解开过的字节可能是任何东西，
  把未验证的内容存下来再发给模型，比让用户去装个依赖危险得多。

路径与命名的两条硬规矩（web.py 的 GET 接口和这里共用同一套闸）：

1. 一切路径都必须 `is_relative_to` 复查。`note_id` 只允许 `[0-9a-f]{16,64}`、
   `ext` 只允许白名单，用户文件名**任何情况下都不参与拼路径**（只用于显示）；
2. 落盘文件名就是内容哈希（sha256 前 16 位 + 真实扩展名），所以同一份内容重复上传
   天然幂等到同一个文件 —— 这也是可以用 `immutable` 缓存头的前提。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from .config import Settings
from .i18n import t
from .llm_types import ImagePart

# 只有这四种。**GIF 在这里是因为官方支持，不是因为它是好格式**；
# SVG 永远不在此列（理由见模块文档）。
ALLOWED_EXTS = ("png", "jpg", "webp", "gif")
EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

ATTACHMENTS_DIRNAME = "attachments"

# 与 web.py 的 SESSION_ID_PATTERN 保持一致。**刻意在这里重写一份而不是 import web**：
# web 是 HTTP 层（会 import 本模块），反向依赖会成环；而且这层是最后一道闸，
# 它该自己说了算 —— web 哪天放宽了，这里也不能跟着放宽。
SESSION_ID_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$")
NOTE_ID_PATTERN = re.compile(r"^[0-9a-f]{16,64}$")

# 用户文件名的显示上限。事件日志是"一行一条 JSON"，一个几百 KB 的文件名能把
# grep 变得没法用，所以显示字段也要有闸。
_NAME_MAX_CHARS = 120

# JPEG/WebP 重编码质量（只在"必须缩放"或"必须降体积"时才用得上）。
_QUALITY = 92
_FALLBACK_QUALITY = 90


class AttachmentError(Exception):
    """上传/引用被拒绝的统一异常。message 是给用户看的那句人话。

    `need` 目前只有 "pillow" 一个取值：前端看到它就把提示换成
    "先跑 run.cmd --setup"，而不是让用户对着一句"缺少依赖"发愣。
    `warnings` 是**失败之前**就已经确认要告诉用户的事（目前只有"声明的 MIME
    与内容不一致"），成功路径的 warning 走返回字典里的 `warnings` 字段。
    """

    def __init__(
        self,
        message: str,
        *,
        need: str = "",
        warnings: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.need = need
        self.warnings = tuple(warnings)


# --------------------------------------------------------------------------
# 依赖探测
# --------------------------------------------------------------------------


def pillow_available() -> bool:
    """Pillow 在不在。**单独一个函数是为了让测试能 patch 它**。

    直接 `import PIL` 并捕获 ImportError 也可以，但那样测试没法模拟"这台机器没装"，
    而"缺依赖时不许静默降级"正是最需要被钉住的一条。
    """
    try:
        import PIL.Image  # noqa: F401
    except Exception:  # noqa: BLE001 - 任何 import 失败都当作"没有 Pillow"
        return False
    return True


def _pillow_requirement() -> AttachmentError:
    return AttachmentError(t("ui.attachment.need_pillow"), need="pillow")


# --------------------------------------------------------------------------
# 魔数嗅探
# --------------------------------------------------------------------------


def sniff_ext(data: bytes) -> str | None:
    """按**文件内容**判断图片格式，认不出来返回 None。

    刻意不看文件名、不看前端声明的 MIME、也不看 `imghdr`（那个模块已被移除）
    —— 改名和改声明都是零成本的，而"我们按内容判断"这句话必须能兑现。

    顺序上先判 PNG/GIF/JPEG 这几个固定长度的签名，再判 WebP 这种"RIFF 容器 +
    偏移处再验一次"的格式：只看 `RIFF` 会把音频/视频也放进来。
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# --------------------------------------------------------------------------
# 路径闸
# --------------------------------------------------------------------------


def _is_relative_to(path: Path, root: Path) -> bool:
    """Path.is_relative_to 的薄封装（统一在这里写，免得哪天有人漏了这一步）。"""
    try:
        return path.is_relative_to(root)
    except (ValueError, OSError):
        return False


def attachments_root(settings: Settings) -> Path:
    """`data/attachments/`。所有附件路径都必须落在这个目录里。"""
    return Path(settings.data_dir) / ATTACHMENTS_DIRNAME


def session_dir(settings: Settings, session_id: str) -> Path:
    """某个会话的附件目录（不创建）。

    两道闸：会话 id 必须匹配 `SESSION_ID_PATTERN`，且拼出来的路径必须在
    `data/attachments/` 之下。前者已经排除了 `..`，后者是"万一正则哪天被改宽"
    的兜底 —— 路径拼接这种事，宁可多检查一次。
    """
    sid = str(session_id or "")
    if not SESSION_ID_PATTERN.fullmatch(sid):
        raise AttachmentError(t("ui.attachment.bad_session_id"))
    root = attachments_root(settings)
    directory = root / sid
    if not _is_relative_to(directory, root):
        raise AttachmentError(t("ui.attachment.session_path_escaped"))
    return directory


def resolve_path(settings: Settings, session_id: str, note_id: str, ext: str) -> Path:
    """由 (会话, 内容哈希, 扩展名) 拼出附件路径，并逐项校验。

    这里**只拼路径、只校验**，不管文件在不在：判断存在性是调用方的事
    （读图时不存在是 404 或"请重新选择"，不是参数错误）。
    """
    directory = session_dir(settings, session_id)
    ident = str(note_id or "").strip().lower()
    if not NOTE_ID_PATTERN.fullmatch(ident):
        raise AttachmentError(t("ui.attachment.bad_note_id"))
    suffix = str(ext or "").strip().lower().lstrip(".")
    if suffix not in ALLOWED_EXTS:
        raise AttachmentError(
            t(
                "ui.attachment.unsupported_ext",
                ext=suffix or t("ui.attachment.empty_value"),
                allowed="、".join(ALLOWED_EXTS),
            )
        )
    path = directory / f"{ident}.{suffix}"
    if not _is_relative_to(path, directory) or path.parent != directory:
        raise AttachmentError(t("ui.attachment.path_escaped"))
    return path


def _safe_name(raw: Any, ext: str) -> str:
    """把用户文件名洗成**只能显示**的字符串。

    三件事：只留最后一段（两种分隔符都算，别把用户的目录结构显示出来）、
    去掉控制字符（一个换行就能把"一行一条 JSON"的事件日志搅乱、也能污染终端）、
    限长。**洗完之后依然绝不参与拼路径** —— 落盘名是内容哈希。
    """
    text = str(raw or "")
    text = text.replace("\\", "/").split("/")[-1]
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    text = " ".join(text.split())
    if len(text) > _NAME_MAX_CHARS:
        text = text[: _NAME_MAX_CHARS]
    return text or f"image.{ext}"


def _normalize_mime(value: Any) -> str:
    """归一化声明的 MIME：有的浏览器给 JPEG 发的是 `image/jpg`（非标准但常见）。"""
    text = str(value or "").strip().lower()
    if text == "image/jpg":
        return "image/jpeg"
    return text


def _human_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.0f} KiB"
    return t("ui.size.bytes", size=size)


# --------------------------------------------------------------------------
# 读取（带缓存）
# --------------------------------------------------------------------------


@lru_cache(maxsize=32)
def _read_cached(path_text: str, mtime_ns: int, size: int) -> bytes:
    return Path(path_text).read_bytes()


def read_bytes(path: Path) -> bytes:
    """读附件字节，带一个小 LRU。

    为什么需要缓存：历史里的图片**每一步都要重发**，也就是每步都要把同一张图
    重新 base64 一遍 —— 不缓存就是每步几十兆的重复磁盘 IO。

    缓存键带上 (mtime, size) 而不是只用路径：附件目录里的文件理论上永不修改
    （内容寻址），但测试和手工修复会覆盖文件，只用路径做键就能读到过期内容，
    那是最难查的一类 bug。多花一次 stat 换掉这个可能，很划算。
    """
    target = Path(path)
    info = target.stat()
    return _read_cached(str(target), info.st_mtime_ns, info.st_size)


def data_url(path: Path, ext: str) -> str:
    """把附件读成 data URL（发给模型接口用）。**绝不能进事件日志**（契约 §5）。"""
    mime = EXT_MIME.get(str(ext or "").lower().lstrip("."), "application/octet-stream")
    return f"data:{mime};base64," + base64.b64encode(read_bytes(path)).decode("ascii")


def decode_base64_data(text: str) -> bytes:
    """解开前端 `data_base64` 字段。

    单独放在这里，是为了"坏 base64 的报错"和其余附件错误**是同一句人话**，
    而不是让 web 层各自捕获 `binascii.Error` 再拼一句英文异常。
    标准与 URL-safe 两种字母表都收（有的客户端会发后者），但**都开 validate**：
    开了才会把"掺了别的东西"当场报出来，而不是静默丢掉字符再拿一段乱码去嗅探。
    """
    raw = str(text or "").strip()
    if not raw:
        raise AttachmentError(t("ui.attachment.empty_data"))
    if raw.startswith("data:"):
        # 容忍前端直接贴完整的 data URL
        _, _, raw = raw.partition(",")
    raw = "".join(raw.split())
    if not raw:
        raise AttachmentError(t("ui.attachment.empty_data"))
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        pass
    try:
        return base64.b64decode(raw, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError(t("ui.attachment.bad_base64", error=exc)) from exc


# --------------------------------------------------------------------------
# 缩放与编码
# --------------------------------------------------------------------------


def _scaled_size(
    width: int, height: int, max_pixels: int, max_side: int
) -> tuple[int, int] | None:
    """需要缩放就返回目标尺寸，否则返回 None。

    判据与契约一致：像素超限**或**单边超限。小图**绝不放大** —— 放大只会让
    编码后的字节更大、画质更差，对模型没有半点好处（模型侧自己要放大是它的事）。
    """
    if width <= 0 or height <= 0:
        return None
    if width * height <= max_pixels and max(width, height) <= max_side:
        return None
    scale = min(1.0, (max_pixels / (width * height)) ** 0.5, max_side / max(width, height))
    for _ in range(16):
        target_w = max(1, int(width * scale))
        target_h = max(1, int(height * scale))
        if target_w * target_h <= max_pixels and max(target_w, target_h) <= max_side:
            return target_w, target_h
        # 浮点误差让取整后刚好越界时，退一点点重算 —— 等比是先验条件，
        # 不能靠单独砍某一维来"凑合法"（那会把图片拉变形）。
        scale *= 0.999
    return None


def _encode(image: Any, ext: str, *, quality: int = _QUALITY) -> tuple[bytes, str]:
    """按目标格式编码，返回 (字节, 实际扩展名)。

    JPEG 不允许 alpha 通道，带透明度的图直接存 JPEG 会抛错，所以先转 RGB
    （透明区域变黑是可接受的，因为这条路径只在"必须缩放的有损格式"上走）。
    GIF 只支持调色板/灰度，其他模式统一转 RGB 再交给 Pillow 量化。
    """
    buffer = io.BytesIO()
    if ext == "png":
        image.save(buffer, format="PNG")
        return buffer.getvalue(), "png"
    if ext == "jpg":
        frame = image if image.mode in ("RGB", "L") else image.convert("RGB")
        frame.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue(), "jpg"
    if ext == "webp":
        image.save(buffer, format="WEBP", quality=quality)
        return buffer.getvalue(), "webp"
    if ext == "gif":
        frame = image if image.mode in ("P", "L") else image.convert("RGB")
        frame.save(buffer, format="GIF")
        return buffer.getvalue(), "gif"
    raise AttachmentError(
        t("ui.attachment.unsupported_ext", ext=ext, allowed="、".join(ALLOWED_EXTS))
    )


def _shrink(
    image: Any, ext: str, limit: int, warnings: list[str]
) -> tuple[bytes, str, list[str]]:
    """落盘字节超限时的降级链：按原格式重编码 → 退 WebP → 仍然超就拒绝。

    **不静默存一个超限文件**：落盘字节上限是硬约束（GET、重放、以及每次发给
    模型都要把这份 base64 读一遍），"太大了但先存着"只会把问题推到更贵的地方。
    """
    encoded, encoded_ext = _encode(image, ext)
    if len(encoded) <= limit:
        return encoded, encoded_ext, warnings
    fallback, fallback_ext = _encode(image, "webp", quality=_FALLBACK_QUALITY)
    if len(fallback) > limit:
        raise AttachmentError(
            t(
                "ui.attachment.still_too_big",
                size=_human_bytes(len(encoded)),
                limit=_human_bytes(limit),
            )
        )
    warnings.append(t("ui.attachment.webp_fallback"))
    return fallback, fallback_ext, warnings


def _prepare(settings: Settings, data: bytes, ext: str) -> dict[str, Any]:
    """验证 + 按需缩放 + 编码，返回落盘所需的全部事实。

    返回的 `stored_ext` 可能和传入的 ext 不同：尺寸或字节超限时会退回 WebP，
    而"文件内容是什么格式"必须和文件名/Content-Type 一致，否则 GET 接口会按
    错误的白名单映射发 Content-Type。
    """
    from PIL import Image  # 延迟导入：没装 Pillow 时本模块其余部分照样能 import

    try:
        # verify() 只做完整性检查，做完这个 Image 对象就不能再用（Pillow 的规定），
        # 所以下面必须重新 open 一次才能读尺寸/缩放。
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = int(image.size[0]), int(image.size[1])
            animated = int(getattr(image, "n_frames", 1) or 1) > 1
            warnings: list[str] = []
            limit = int(settings.attachment_stored_max_bytes)
            target = _scaled_size(
                width,
                height,
                int(settings.attachment_max_pixels),
                int(settings.attachment_max_side),
            )
            if target is None and len(data) <= limit:
                # 不需要缩放、也不超字节上限：**原字节落盘**。重编码一次既费时
                # 又让画质变差，还会让"上传的和发给模型的不是同一张图"没必要地发生。
                return {
                    "width": width,
                    "height": height,
                    "stored": data,
                    "stored_ext": ext,
                    "stored_width": width,
                    "stored_height": height,
                    "warnings": warnings,
                }
            if target is None:
                # 尺寸没问题、但原始字节超过落盘上限：这条路径也必须压。
                # 契约把它和"缩放"分开写，但两者落到的都是同一个硬约束。
                if ext == "gif" and animated:
                    warnings.append(t("ui.attachment.gif_compressed_first_frame"))
                stored, stored_ext, warnings = _shrink(image, ext, limit, warnings)
                return {
                    "width": width,
                    "height": height,
                    "stored": stored,
                    "stored_ext": stored_ext,
                    "stored_width": width,
                    "stored_height": height,
                    "warnings": warnings,
                }
            target_w, target_h = target
            if ext == "gif" and animated:
                # 缩放动图 = 只留第一帧。静默把动图变成静帧是不能接受的，
                # 所以这件事必须作为 warning 回到用户眼前（契约 §6.4）。
                warnings.append(t("ui.attachment.gif_first_frame"))
            resized = image.resize((target_w, target_h), Image.LANCZOS)
            stored, stored_ext = _encode(resized, ext)
            if len(stored) > limit:
                stored, stored_ext, warnings = _shrink(resized, ext, limit, warnings)
            return {
                "width": width,
                "height": height,
                "stored": stored,
                "stored_ext": stored_ext,
                "stored_width": target_w,
                "stored_height": target_h,
                "warnings": warnings,
            }
    except AttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow 的异常种类很多，统一转成人话
        # 带上 Pillow 的原话："为什么失败"比"失败了"有用得多（截断？伪装？色深不支持？）
        raise AttachmentError(
            t(
                "ui.attachment.decode_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        ) from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    """先写同目录临时文件，再 os.replace 原子替换。

    同目录是必须的（跨盘 replace 不是原子的，会退化成复制）；临时文件名带 pid，
    两个进程同时上传同一张图时不会互相踩。写坏了顶多留下一个 `.tmp`，
    **绝不会留下半张能读到的图片**。
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise AttachmentError(t("ui.attachment.write_failed", error=exc)) from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


def store(
    settings: Settings,
    session_id: str,
    data: bytes,
    *,
    name: str,
    declared_mime: str = "",
) -> dict[str, Any]:
    """校验并落盘一张图，返回事件里要记的那条附件记录（契约 §7.1 的 attachment）。

    顺序是刻意的：**所有会失败的检查都排在写盘之前**，所以任何一次拒绝都不会
    留下半个文件（"Pillow 缺失时没有落盘任何文件"是有测试钉住的）。
    """
    if not pillow_available():
        raise _pillow_requirement()

    payload = bytes(data or b"")
    if not payload:
        raise AttachmentError(t("ui.attachment.empty_image"))

    limit = int(settings.attachment_max_bytes)
    if len(payload) > limit:
        raise AttachmentError(
            t(
                "ui.attachment.too_big",
                size=_human_bytes(len(payload)),
                limit=_human_bytes(limit),
            )
        )

    ext = sniff_ext(payload)
    if ext is None:
        raise AttachmentError(t("ui.attachment.not_an_image"))

    warnings: list[str] = []
    declared = _normalize_mime(declared_mime)
    if declared and declared != EXT_MIME[ext]:
        warnings.append(
            t(
                "ui.attachment.mime_mismatch",
                declared=declared_mime,
                actual=EXT_MIME[ext],
            )
        )

    prepared = _prepare(settings, payload, ext)
    warnings.extend(prepared["warnings"])
    stored = prepared["stored"]
    stored_ext = prepared["stored_ext"]

    digest = hashlib.sha256(payload).hexdigest()[:16]
    directory = session_dir(settings, session_id)
    path = directory / f"{digest}.{stored_ext}"
    if not _is_relative_to(path, directory) or path.parent != directory:
        raise AttachmentError(t("ui.attachment.path_escaped"))
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AttachmentError(t("ui.attachment.mkdir_failed", error=exc)) from exc

    # 文件名就是内容哈希 → 同一份数据重复上传天然指向同一个文件。
    # **已有就不重写**：内容寻址的文件一经写入不再变化，重写只会让正在读它的
    # 请求（浏览器缓存、重放）撞上"文件被替换"这类没必要的竞态。
    if not path.is_file():
        _atomic_write(path, stored)

    return {
        "id": digest,
        "file": path.relative_to(settings.data_dir).as_posix(),
        "name": _safe_name(name, stored_ext),
        "mime": EXT_MIME[stored_ext],
        "bytes": len(payload),
        "width": prepared["width"],
        "height": prepared["height"],
        "stored_bytes": len(stored),
        "stored_width": prepared["stored_width"],
        "stored_height": prepared["stored_height"],
        # 成功路径的 warning 只能挂在这里（签名冻结，没有第二个返回通道）：
        # 例如"声明的类型和内容不一致""动图只留了第一帧"。
        "warnings": warnings,
    }


def records_for(
    settings: Settings, session_id: str, wanted: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把前端在 `/api/chat` 里给的 `[{id, ext, name}]` 变成事件里的附件记录。

    **磁盘是唯一真相**：id/ext 必须能在该会话目录里找到真实文件，尺寸重新从磁盘读，
    前端只提供 `name` 这个显示字段（契约 §7.3）。前端给的任何路径、尺寸、MIME
    一概不用 —— 否则改一个请求体就能让事件日志记下一张并不存在的图。

    原始尺寸（`bytes/width/height`）在这里等于落盘尺寸：上传那一刻才知道原图多大，
    而对话时前端只回传 id/ext/name。历史上显示的会是"实际发给模型的那一版"，
    这也正是用户在历史气泡里看到的那张图。
    """
    items = list(wanted or ())
    if not items:
        return []
    limit = int(settings.attachment_max_count)
    if len(items) > limit:
        raise AttachmentError(
            t("ui.attachment.too_many", limit=limit, count=len(items))
        )
    if not pillow_available():
        raise _pillow_requirement()

    from PIL import Image  # 延迟导入，理由同 _prepare

    records: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise AttachmentError(t("ui.attachment.item_not_object", index=index))
        note_id = str(item.get("id") or "").strip().lower()
        ext = str(item.get("ext") or "").strip().lower().lstrip(".")
        path = resolve_path(settings, session_id, note_id, ext)
        if not path.is_file():
            raise AttachmentError(
                t(
                    "ui.attachment.missing_on_disk",
                    index=index,
                    note_id=note_id,
                    ext=ext,
                )
            )
        if not _is_relative_to(path, attachments_root(settings)):
            raise AttachmentError(t("ui.attachment.path_escaped"))
        try:
            with Image.open(path) as image:
                width, height = int(image.size[0]), int(image.size[1])
        except Exception as exc:  # noqa: BLE001
            raise AttachmentError(
                t("ui.attachment.dimensions_failed", index=index, error=exc)
            ) from exc
        size = path.stat().st_size
        records.append(
            {
                "id": note_id,
                "file": path.relative_to(settings.data_dir).as_posix(),
                "name": _safe_name(item.get("name"), ext),
                "mime": EXT_MIME[ext],
                "bytes": size,
                "width": width,
                "height": height,
                "stored_bytes": size,
                "stored_width": width,
                "stored_height": height,
            }
        )
    return records


# --------------------------------------------------------------------------
# 重放（把事件里的记录还原成 ImagePart）
# --------------------------------------------------------------------------


def load_parts(data_dir: Path, records: Iterable[dict[str, Any]]) -> list[ImagePart]:
    """把事件里的 `attachments` 记录还原成挂在 user 消息上的 ImagePart。

    两种坏情况**故意区别对待**：

    * 记录里的路径越出附件目录、或扩展名不在白名单里 → **抛异常**（fail closed）。
      这只可能来自被改过的日志，而"照着一个越界路径去读文件"是我们唯一不能接受的；
    * 文件不在了（用户手工删了） → **跳过**，不抛。会话日志是唯一真相来源，
      一张图丢了不该让整个会话从此打不开 —— 这与"悬空 tool_calls 必须补桩"
      是同一个取舍：可用性优先，但只在"信息已经不可恢复"的时候让路。
    """
    root = (Path(data_dir) / ATTACHMENTS_DIRNAME).resolve()
    parts: list[ImagePart] = []
    for record in records or ():
        if not isinstance(record, dict):
            continue
        relative = str(record.get("file") or "")
        if not relative:
            continue
        pure = PurePosixPath(relative.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise AttachmentError(t("ui.attachment.replay_path_escaped", path=relative))
        ext = pure.suffix.lstrip(".").lower()
        if ext not in ALLOWED_EXTS:
            raise AttachmentError(
                t(
                    "ui.attachment.ext_not_allowed",
                    ext=ext or t("ui.attachment.empty_value"),
                    path=relative,
                )
            )
        path = Path(data_dir) / Path(*pure.parts)
        if not _is_relative_to(path.resolve(), root):
            raise AttachmentError(t("ui.attachment.replay_path_escaped", path=relative))
        if not path.is_file():
            continue
        note_id = str(record.get("id") or pure.stem)
        parts.append(
            ImagePart(
                path=path,
                mime=str(record.get("mime") or "") or EXT_MIME[ext],
                name=_safe_name(record.get("name"), ext),
                width=_as_int(record.get("stored_width"), record.get("width")),
                height=_as_int(record.get("stored_height"), record.get("height")),
                id=note_id,
            )
        )
    return parts


def _record_ext(file_value: Any) -> str:
    """从事件里的 `file` 字段取出扩展名（小写、不带点）。取不到返回空串。"""
    pure = PurePosixPath(str(file_value or "").replace("\\", "/"))
    return pure.suffix.lstrip(".").lower()


def copy_records_for_fork(
    settings: Settings,
    src_session_id: str,
    dst_session_id: str,
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把一批附件记录复制到**新会话**的目录，并把 `file` 改写指向副本。

    分叉会话要"和原会话彻底独立"（连删掉原会话都不能影响它），所以引用的图
    必须跟着复制一份 —— 事件里那个 `file` 指向的是原会话目录，原样带过去
    会在"原会话被删（连带附件目录一起删）"时变成悬空引用。

    设计取舍：**源文件缺失时保留原 `file`**（用户手删过图）。重放层
    `load_parts` 对"文件不存在"本就是跳过，这里不必因为一张丢了的图让整个
    分叉失败 —— 可用性优先，只在"路径越界/扩展名非法"时才 fail-closed。
    """
    out: list[dict[str, Any]] = []
    for record in records or ():
        item = dict(record) if isinstance(record, dict) else {}
        note_id = str(item.get("id") or "").strip().lower()
        ext = _record_ext(item.get("file"))
        if not note_id or not ext:
            out.append(item)
            continue
        try:
            # resolve_path 会重做一遍 id / 扩展名 / 路径越界的校验（纵深防御）：
            # 这里读的毕竟是从日志里来的东西。
            src = resolve_path(settings, src_session_id, note_id, ext)
            dst = resolve_path(settings, dst_session_id, note_id, ext)
        except AttachmentError:
            out.append(item)
            continue
        if not src.is_file():
            out.append(item)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.is_file():
                shutil.copyfile(src, dst)
            item["file"] = dst.relative_to(settings.data_dir).as_posix()
        except OSError:
            # 复制失败也保留原 file：最坏是以后图丢了，但不会把会话弄坏。
            pass
        out.append(item)
    return out


def _as_int(*candidates: Any) -> int:
    for value in candidates:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


# --------------------------------------------------------------------------
# 统计与清理
# --------------------------------------------------------------------------


def count_files(settings: Settings) -> int:
    """`data/attachments/` 下一共有多少个文件。

    给"恢复出厂设置"的预览用：它原本只数会话/记忆那几样，附件是整目录删除的，
    不数进来就会报出"重置 0 个文件、其实删掉了 20 张图"。
    """
    root = attachments_root(settings)
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def delete_session_attachments(settings: Settings, session_id: str) -> int:
    """删掉一个会话的附件目录，返回删掉的文件数。

    这是对"删会话只删会话文件"那条老边界的**显式扩展**（契约 §2 / ADR-074）：
    附件只被它自己的会话引用，会话没了它们就是永远读不到的孤儿。
    路径同样要过两道闸，绝不能出现"参数写错就删掉整个 data/"。
    """
    directory = session_dir(settings, session_id)
    root = attachments_root(settings)
    if not _is_relative_to(directory, root) or directory.parent != root:
        raise AttachmentError(t("ui.attachment.refuse_delete"))
    if not directory.is_dir():
        return 0
    files = sum(1 for path in directory.rglob("*") if path.is_file())
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise AttachmentError(t("ui.attachment.delete_failed", error=exc)) from exc
    return files


def _all_files(settings: Settings) -> set[str]:
    root = attachments_root(settings)
    if not root.is_dir():
        return set()
    data_dir = Path(settings.data_dir)
    return {
        path.relative_to(data_dir).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def _referenced_files(settings: Settings) -> set[str]:
    """所有会话事件里引用过的附件路径（相对 data_dir 的 posix 形式）。

    只认 `attachments[].file` 这一个字段 —— 它是事件日志里唯一的引用通道，
    接口 URL、显示名都不算（那些是派生的）。
    """
    # 延迟导入：events 需要本模块的 load_parts，顶层互相 import 会成环。
    from .events import read_events

    referenced: set[str] = set()
    sessions_dir = Path(settings.sessions_dir)
    if not sessions_dir.is_dir():
        return referenced
    for session_file in sessions_dir.glob("*.jsonl"):
        for record in read_events(session_file):
            for item in record.get("attachments") or ():
                if not isinstance(item, dict):
                    continue
                value = str(item.get("file") or "").strip()
                if value:
                    referenced.add(PurePosixPath(value.replace("\\", "/")).as_posix())
    return referenced


def gc_report(settings: Settings) -> dict[str, Any]:
    """报告 `data/attachments/` 下没有任何事件引用的文件（**只报告，不删**）。

    为什么会有孤儿：加了图却没发出去（目录建了、事件没写）、去重之后"删掉一条
    引用"并不等于"那个文件没人用了"（可能还有别的会话/消息引用着它）。
    所以清理只能反过来做：先扫全部引用，再删没人认领的。
    """
    files = _all_files(settings)
    referenced = _referenced_files(settings)
    orphans = sorted(files - referenced)
    data_dir = Path(settings.data_dir)
    orphan_bytes = 0
    total_bytes = 0
    for relative in files:
        try:
            size = (data_dir / relative).stat().st_size
        except OSError:
            continue
        total_bytes += size
        if relative in set(orphans):
            orphan_bytes += size
    return {
        "orphans": orphans,
        "orphan_bytes": orphan_bytes,
        "total_files": len(files),
        "total_bytes": total_bytes,
        "referenced": len(referenced),
    }


def gc_apply(settings: Settings) -> dict[str, Any]:
    """真删孤儿文件。删不掉的留在 `leftovers`/`failed` 里如实上报，不静默跳过。"""
    report = gc_report(settings)
    root = attachments_root(settings)
    data_dir = Path(settings.data_dir)
    deleted: list[str] = []
    deleted_bytes = 0
    leftovers: list[str] = []
    failed: list[dict[str, str]] = []
    for relative in report["orphans"]:
        target = data_dir / PurePosixPath(relative)
        if not _is_relative_to(target.resolve(), root.resolve()):
            failed.append(
                {"file": relative, "error": t("ui.attachment.orphan_path_escaped")}
            )
            continue
        try:
            size = target.stat().st_size
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            failed.append({"file": relative, "error": str(exc)})
            continue
        deleted.append(relative)
        deleted_bytes += size
    # 空目录也顺手收掉（用户加了图又没发，会留下一个空会话目录）。
    # 有文件留下的目录原样保留，免得把"删不掉"伪装成"清干净了"。
    if root.is_dir():
        for directory in sorted(root.iterdir(), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                try:
                    directory.rmdir()
                except OSError as exc:
                    leftovers.append(str(directory.relative_to(data_dir)))
                    failed.append({"file": str(directory.relative_to(data_dir)), "error": str(exc)})
    return {
        "deleted": deleted,
        "deleted_bytes": deleted_bytes,
        "leftovers": leftovers,
        "failed": failed,
        "orphans": report["orphans"],
        "total_files": report["total_files"],
        "total_bytes": report["total_bytes"],
    }
