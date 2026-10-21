"""本地 OCR：多模态关掉时，只把图片里的**文字**发给模型。

## 它在这个项目里的位置

「多模态」开着时图片按契约 §4 直接发给模型；关着时这一层接手：把每张图识别成文字，
文字进 `user_message` 事件的 `ocr` 字段（契约 §13.3），此后重放、压缩、整理档案
都只认这份**已经落盘的文字**，不会再识别第二次 —— 同一张图识别两次可能给出
不一样的结果，而事件日志是"当时到底发生了什么"的唯一真相（§13.6）。

## 三个刻意的取舍

**一、延迟 import + 进程内引擎单例。**
`import rapidocr_onnxruntime` 会连带拉起 onnxruntime 与 opencv，构造 `RapidOCR()`
还要加载模型（本机实测 1.2~2.3s）。这笔钱只在**第一次**真的要 OCR 时付一次：
不 OCR 的会话一秒都不该为它买单，而同一个进程里的第二、第三张图也不该重付。

**二、串行化：宁可排队，也不并发。**
web 服务是多线程的（`ThreadingHTTPServer`）。实测（2026-10-14，本机，960×520 文字图，
4 线程同时识别同一张图）：**无锁也能跑完**，4/4 成功、结果一致、进程没崩，12.5s；
加锁串行 15.1s，同样 4/4 成功。也就是说引擎内部本来就在串行化（4 张 ≈ 4×3.1s），
加锁只多花约 20%。
**没有观察到崩溃或错乱，但我们仍然串行化**：一次通过不等于线程安全（投机性调度、
版本变化、缓存状态都可能让"这次没事"变成"下次有事"），而这 20% 换的是确定性；
何况 OCR 本来就是 CPU 密集，多开线程只是互相抢核。
所以用模块级 `Lock` 老老实实排队 —— 这是一条**防御性**选择，不是"已知会崩"。

**三、识别不到文字 = 空串，不是失败。**
`text == ""` 表示"这张图里确实没有文字"（纯色图、纯风景照），
与"识别失败了"（抛 `OcrError`）是**两件不同的事**：上层要靠这个区分
"没内容可发"和"该告诉用户出问题了"。所以一张都没识别到时返回 `""`，
绝不返回 `None`、也绝不返回"未识别"这种字符串（那会变成发给模型的正文）。

## 已经实测过的（2026-10-14，本机 .venv，rapidocr-onnxruntime 1.2.3）

| 场景 | 耗时 |
| --- | --- |
| 引擎加载 | 1.2 ~ 2.3s |
| 900×360 现画中文图（首次 / 之后） | 3.9s / 1.8s |
| 落盘上限那一版（1300×1300，6 行中英混排） | 3.7 ~ 3.9s |
| 真实照片（750×503 猎豹） | 1.4s |
| 4000×4000（16 MP）直接送引擎 | **17.6s** |

最后一行是 `_MAX_OCR_PIXELS` 存在的原因（见 `_clamped_input`）。

## 诚实的局限

它**不是"什么都认得出"**：真实照片上那行很小的水印 "Baidu百科" 被识别成
"Baldu百科"（Ba 认成了 Ba，d 认成了 l）。小字号、低对比、艺术字、旋转文字都会掉准确率，
手写体基本没戏。所以界面上必须让用户能看见"模型到底收到了什么"（契约 §13.7），
不要让用户以为关掉多模态和开着一样好。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from .i18n import t

# 引擎的发行名（importlib.metadata 里的名字）。装的是 rapidocr-onnxruntime==1.2.3，
# 但**代码不写死版本号**：换个版本照样能用，engine 字段如实记当时用的那一个。
_ENGINE_NAME = "rapidocr-onnxruntime"

# 送进引擎前的**总像素**上限。为什么是总像素而不是"长边"：
# 长边限制会直接毁掉长截图/细长图里的小字 —— 一张 8192×206 的横向截图本来只有
# 1.69 MP、识别很快，按长边一夹就变成 2048×52，字全糊了。
#
# 4.0 MP 这个数字是**刻意选在落盘上限（1.69 MP）之上**的：
# 附件落盘时已经缩到 ≤1.69 MP（契约 §6），所以走正常流程的图片
# **永远不会**被这里二次缩放、一个像素都不动；它只是给"直接拿 recognize 读一张
# 16 MP 原图"这种情况兜底 —— 实测 16 MP 要 17.6s，落在 web 请求里既拖住
# 排队的所有图、又可能撞上请求超时；夹到 4 MP 后实测 5.7s，识别结果不变。
_MAX_OCR_PIXELS = 4_000_000

# 引擎串行化用的锁。实测无锁并发 4 线程也能跑完且结果一致（见模块文档"二"），
# 所以这是**防御性**的：一次通过不等于线程安全，而排队只多花约 20%。
_ENGINE_LOCK = threading.Lock()
_ENGINE: Any = None

# 引擎返回的 result 是 list[[box, text, score], ...]，一张都没识别到时是 None。
# 解析时按"坏数据不许把整张图判成失败"处理：形状不对的条目直接跳过。
Emit = Callable[..., None]


class OcrUnavailable(RuntimeError):
    """OCR 引擎根本不可用（没装、import 不了、模型加载失败）。

    与 `OcrError` 的区别是**整条路都不通**，而不是"这一张图出问题"：
    调用方（web 层）据此直接 400 拒绝，**不静默降级**（契约 §13.2）——
    绝不能把"OCR 不可用"当成一段文字发给模型。
    """


class OcrError(RuntimeError):
    """这一张图识别失败。消息里带引擎的原话（"为什么失败"比"失败了"有用）。"""


@dataclass(frozen=True)
class OcrResult:
    """一次识别的结果。

    `text` 是按行拼接的文本（`"\\n".join`），**空串表示真的没识别到文字**
    （不是 None、不是"未识别"），上层要靠它区分"没文字"与"失败"。
    `chars` 是这份文本的字符数（含行间换行），`seconds` 是本次识别的墙钟秒数。
    """

    text: str
    chars: int
    seconds: float
    engine: str

    def to_dict(self) -> dict[str, Any]:
        """§13.3 里 `ocr` 字段的形状（写进 `user_message` 事件的那一份）。"""
        return {
            "engine": self.engine,
            "seconds": self.seconds,
            "chars": self.chars,
            "text": self.text,
        }


# --------------------------------------------------------------------------
# 引擎探测与获取
# --------------------------------------------------------------------------


def available() -> bool:
    """OCR 引擎在不在。**单独一个函数是为了让测试能 patch 它**。

    与 `attachments.pillow_available()` 同一个套路：直接 try/except import 也行，
    但那样测试没法模拟"这台机器没装"，而"引擎不可用时不许静默降级"
    正是最需要被钉住的一条。

    注意它只验证"import 得动"，不验证"模型能加载"（那要 1~2s）——
    真正的加载失败由 `_engine()` 抛 `OcrUnavailable`，同样走 400 那条路。
    """
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001 - 任何 import 失败都当作"没有引擎"
        return False
    return True


def engine_info() -> str:
    """引擎标识，例如 `rapidocr-onnxruntime 1.2.3`。

    写进事件的 `ocr.engine`（契约 §13.3）：出问题时"当时用的哪个引擎"是必需线索。
    取不到版本号就只报名字 —— **不许因为查版本失败就把整次识别判成失败**。
    """
    version = ""
    try:
        from importlib.metadata import version as _distribution_version

        version = _distribution_version(_ENGINE_NAME)
    except Exception:  # noqa: BLE001 - 版本号只是线索，取不到不影响识别
        version = ""
    return f"{_ENGINE_NAME} {version}".strip()


def _engine() -> Any:
    """拿到进程内唯一的引擎实例（第一次会付加载的 1~2s）。

    加载**放在锁里**：两个线程同时进门时，只有一个真的去加载模型，
    另一个等它加载完直接复用 —— 否则会同时构造两个引擎（内存翻倍、启动更慢）。
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:  # 等锁期间别人已经加载好了
            return _ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable(
                t(
                    "ui.ocr.engine_not_installed",
                    engine=_ENGINE_NAME,
                    error=f"{type(exc).__name__}: {exc}",
                )
            ) from exc
        try:
            _ENGINE = RapidOCR()
        except Exception as exc:  # noqa: BLE001 - 模型文件缺失/损坏也会在这里炸
            raise OcrUnavailable(
                t(
                    "ui.ocr.engine_load_failed",
                    engine=_ENGINE_NAME,
                    error=f"{type(exc).__name__}: {exc}",
                )
            ) from exc
    return _ENGINE


# --------------------------------------------------------------------------
# 识别
# --------------------------------------------------------------------------


def _safe_message(exc: BaseException, target: Path) -> str:
    """把引擎异常变成一句人话，并**去掉里面的绝对路径**。

    引擎自带的 `LoadImageError` 会把完整路径写进消息（实测那句形如
    "LoadImageError: <盘符与各级目录>/nope.png does not exist."），而这句话会被写进
    `user_message` 事件的 `ocr.error`。事件日志是要能拷给别人、能贴进 issue 的，
    绝不能带着这台机器的盘符与用户名（ADR-042 / test_paths 的守卫）。
    所以把绝对路径换成文件名，其余原话一字不改 ——"为什么失败"必须留着。
    """
    text = f"{type(exc).__name__}: {exc}"
    for raw, replacement in (
        (str(target), target.name or t("ui.ocr.unknown")),
        (str(target.parent), t("ui.ocr.attachment_dir")),
    ):
        if raw:
            text = text.replace(raw, replacement)
    return text


def _clamped_input(target: Path) -> Any:
    """图片太大就先缩到 `_MAX_OCR_PIXELS` 以内，返回 ndarray；否则原样返回路径。

    正常流程里的图（落盘时已 ≤1.69 MP）**一个像素都不动**，走的就是
    `engine(path)` 这条与实测完全一致的路径；只有"直接拿 recognize 读一张
    超大原图"才会走到缩放分支。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 没装 Pillow 时不缩，交给引擎自己读
        return target
    with Image.open(target) as image:
        width, height = int(image.size[0]), int(image.size[1])
        if width <= 0 or height <= 0 or width * height <= _MAX_OCR_PIXELS:
            return target
        scale = (_MAX_OCR_PIXELS / (width * height)) ** 0.5
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        # 转 RGB：调色板（GIF）与带 alpha 的 PNG 直接喂给引擎会出现通道数不一致。
        resized = image.convert("RGB").resize(size, Image.LANCZOS)
        import numpy as np  # 引擎依赖 numpy，走到这里它一定在

        return np.asarray(resized)


def _lines_of(result: Any) -> list[str]:
    """从引擎返回值里取出文本行。**容错解析**：形状不对的条目直接跳过。

    引擎契约是 `list[[box, text, score], ...]` 或 `None`；但这是外部库的输出，
    一条形状不对的记录不该让整张图被判成"识别失败"（那会让用户看到一句
    莫名其妙的报错，而实际文字已经识别出来了）。空白的行也不收 ——
    拼接后会出现空行，把发给模型的正文弄得很难读。
    """
    lines: list[str] = []
    for item in result or ():
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        text = str(item[1] or "").strip()
        if text:
            lines.append(text)
    return lines


def recognize(path: Path) -> OcrResult:
    """识别一张图片里的文字。**这是唯一真正调用引擎的地方。**

    返回 `OcrResult`；一张都没识别到时 `text == ""`（"没文字"与"失败"必须能区分）。
    失败抛 `OcrError`，消息里带引擎原话（去掉绝对路径，见 `_safe_message`）。
    """
    target = Path(path)
    if not target.is_file():
        raise OcrError(t("ui.ocr.file_missing", name=target.name or t("ui.ocr.unknown")))
    engine = _engine()
    with _ENGINE_LOCK:
        # 计时放在锁**里面**：它量的是"这张图真正花了多久"，
        # 不含在前面排队等别人识别的时间（那个数字对判断引擎快慢没有意义）。
        started = time.monotonic()
        try:
            payload = _clamped_input(target)
            result, _engine_elapsed = engine(payload)
        except OcrError:
            raise
        except Exception as exc:  # noqa: BLE001 - 引擎的异常种类很多，统一转成人话
            raise OcrError(
                t("ui.ocr.recognize_failed", error=_safe_message(exc, target))
            ) from exc
        seconds = time.monotonic() - started
    text = "\n".join(_lines_of(result))
    return OcrResult(
        text=text,
        chars=len(text),
        seconds=round(seconds, 2),
        engine=engine_info(),
    )


# --------------------------------------------------------------------------
# 批量填充附件记录
# --------------------------------------------------------------------------


def _record_path(record: dict[str, Any], data_dir: Path) -> Path:
    """把附件记录里的相对 `file` 还原成本地路径，并复查它没越出数据目录。

    与 `attachments.load_parts` 同一套判断：绝对路径、`..`、以及解析后越界
    一律拒绝。这里是**读文件**的入口，路径必须自己再验一遍，
    不能假设上游一定洗过（事件日志是可以被手工改的）。
    """
    relative = str(record.get("file") or "")
    if not relative:
        raise OcrError(t("ui.ocr.bad_record"))
    pure = PurePosixPath(relative.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise OcrError(t("ui.ocr.path_escaped_attachments", path=relative))
    base = Path(data_dir).resolve()
    resolved = (base / Path(*pure.parts)).resolve()
    try:
        inside = resolved.is_relative_to(base)
    except (ValueError, OSError):
        inside = False
    if not inside:
        raise OcrError(t("ui.ocr.path_escaped_data", path=relative))
    return resolved


def fill_records(
    records: Sequence[dict[str, Any]],
    *,
    data_dir: Path,
    emit: Emit | None = None,
) -> list[dict[str, Any]]:
    """给每条附件记录补上 §13.3 的 `ocr` 字段。

    **返回新列表（里面是新的字典），不改动传进来的记录** —— 调用方原来那份
    事件记录不该被这个函数悄悄改写；哪些图真的识别过、识别出什么，全部由返回值表达。

    行为：
      * `records` 为空 → 直接返回 `[]`（没有图就没有可识别的，连引擎都不碰）；
      * 引擎不可用（没装 / 加载失败）→ 抛 `OcrUnavailable`，**一个事件都不写**：
        契约 §13.2 要求这是"直接拒绝"，不留半截状态，更不能把"OCR 不可用"
        当文字发给模型；
      * 每张图之前先 `emit("notice", level="info", text="正在识别图片文字…")`：
        单张要 1.5~4s，界面不能静默卡住；
      * 单张识别失败 → 那张记 `{"error": "…"}` 继续，**其它图照常识别**
        （一张坏图不该让整条消息发不出去），并补一条 warning notice 让用户看得见；
      * 成功 → 记 `{"engine", "seconds", "chars", "text"}`。
    """
    items = [record for record in (records or ()) if isinstance(record, dict)]
    if not items:
        return []
    if not available():
        raise OcrUnavailable(
            t("ui.ocr.engine_unavailable", engine=_ENGINE_NAME)
        )
    # 提前把引擎拿到手：不可用时要在**发任何 notice、写任何事件之前**就失败，
    # 否则用户会看到一句"正在识别图片文字…"然后什么都没有。
    _engine()

    filled: list[dict[str, Any]] = []
    for index, record in enumerate(items, 1):
        if emit is not None:
            emit("notice", level="info", text=t("ui.ocr.progress"))
        item = dict(record)
        try:
            target = _record_path(record, data_dir)
            item["ocr"] = recognize(target).to_dict()
        except OcrError as exc:
            # 失败的记 error，**不写半截 text**（契约 §13.3）：
            # 空的 text 有明确含义（"真的没识别到文字"），不能被失败复用。
            item["ocr"] = {"error": str(exc)}
            if emit is not None:
                emit(
                    "notice",
                    level="warn",
                    text=t("ui.ocr.one_failed", index=index, error=exc),
                )
        filled.append(item)
    return filled
