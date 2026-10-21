"""本地 OCR 的测试。

分三层，**每一层都不依赖上一层**，这样"无依赖模式"（系统 Python，没装
rapidocr-onnxruntime）下也照样能跑掉大半：

1. **纯逻辑**（假引擎）：`fill_records` 的编排 —— 每张图之前发 notice、
   单张失败不影响其它图、引擎不可用时抛 `OcrUnavailable` 且一个事件都不写、
   返回的是新列表（不改调用方那份记录）、串行化的锁真的在排队。
2. **错误消息**：引擎的原话要留着，但**绝对路径必须去掉** ——
   它会被写进 `user_message` 事件的 `ocr.error`，而事件日志要能拷给别人。
3. **真引擎**（`skipUnless(ocr.available())`）：现画一张中英数混排的图断言关键片段；
   纯色空图断言 `text == ""`（"没识别到文字"≠"失败"的正面证明）；
   两个线程同时识别同一张图不炸、结果一致。

字体从 Windows 的字体目录（环境变量 `WINDIR`）里找，**找不到就 skip** ——
绝不写死某台机器的字体路径（可移植性守卫会按行扫绝对路径，注释也算）。
"""

from __future__ import annotations

import base64
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from harness import attachments, ocr
from harness.ocr import OcrError, OcrResult, OcrUnavailable, fill_records

from .helpers import test_settings

# 一张真的 1×1 PNG（不依赖 Pillow 就能构造），用来当"存在的图片文件"。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

HAS_ENGINE = ocr.available()
HAS_PILLOW = attachments.pillow_available()

# 真引擎用例：识别本身要几秒，只留必要的那几条。
needs_real_engine = unittest.skipUnless(
    HAS_ENGINE and HAS_PILLOW,
    "这条用例需要 Pillow + rapidocr-onnxruntime（用 .venv 里的解释器跑才有）",
)


def _font(size: int):
    """从系统字体目录里找一个能写中文的字体。找不到返回 None（调用方 skip）。"""
    from PIL import ImageFont

    root = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or ""
    if not root:
        return None
    fonts = Path(root) / "Fonts"
    if not fonts.is_dir():
        return None
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf"):
        candidate = fonts / name
        if not candidate.is_file():
            continue
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return None


def font_available() -> bool:
    """字体在不在。没装 Pillow 时返回 False（调用方会跳过整类用例）。"""
    if not HAS_PILLOW:
        return False
    try:
        return _font(44) is not None
    except Exception:  # noqa: BLE001 - 字体库出任何问题都当作"画不出图"
        return False


def draw_text_png(lines: list[str], *, size: tuple[int, int] = (900, 360)) -> bytes:
    """现画一张白底黑字的 PNG（中英数混排）。"""
    from PIL import Image, ImageDraw

    font = _font(44)
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    step = max(20, (size[1] - 40) // max(1, len(lines)))
    for index, line in enumerate(lines):
        draw.text((40, 20 + index * step), line, font=font, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def draw_blank_png(*, size: tuple[int, int] = (900, 360)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeEngine:
    """假引擎：只为测 `fill_records` 的编排逻辑，不加载真模型。

    返回值形状照抄引擎契约：`(list[[box, text, score], ...], elapsed)`。
    它同时记录"同一时刻有几个在跑"，用来钉住 `recognize` 里的那把锁真的在排队。
    """

    def __init__(self, text: str = "识别到的文字", *, delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        self.calls = 0
        self.max_concurrent = 0
        self._concurrent = 0
        self._lock = threading.Lock()

    def __call__(self, payload):
        with self._lock:
            self.calls += 1
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            if self.delay:
                time.sleep(self.delay)
            # 文本**不随调用次数变化**：并发用例要断言"两个线程结果一致"。
            # 形状与引擎一致：`[[box, text, score], ...]`（外面还有一层"每行一条"）。
            box = [[0, 0], [10, 0], [10, 5], [0, 5]]
            return ([[box, self.text, 0.99]], 0.001)
        finally:
            with self._lock:
                self._concurrent -= 1


def fake_engine(engine: FakeEngine):
    """把 `available` 与 `_engine` 换成假引擎的上下文管理器（每次都要新建）。"""
    return _FakeEnginePatch(engine)


class _FakeEnginePatch:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self._patches: list = []

    def __enter__(self) -> FakeEngine:
        self._patches = [
            mock.patch.object(ocr, "available", return_value=True),
            mock.patch.object(ocr, "_engine", return_value=self.engine),
        ]
        for patch in self._patches:
            patch.start()
        return self.engine

    def __exit__(self, *exc: object) -> bool:
        for patch in reversed(self._patches):
            patch.stop()
        return False


class OcrCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(self.tmp)
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **data) -> None:
        self.events.append((kind, data))

    def notices(self, level: str | None = None) -> list[str]:
        return [
            str(data.get("text") or "")
            for kind, data in self.events
            if kind == "notice" and (level is None or data.get("level") == level)
        ]

    def seed(self, name: str, *, session: str = "s1", payload: bytes = TINY_PNG) -> dict:
        path = self.tmp / "attachments" / session / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {
            "id": Path(name).stem,
            "file": f"attachments/{session}/{name}",
            "name": name,
            "mime": "image/png",
        }

    def missing(self, name: str = "deadbeefdeadbeef.png") -> dict:
        """一条指向**不存在文件**的附件记录（模拟文件被手工删掉/路径坏）。"""
        return {
            "id": Path(name).stem,
            "file": f"attachments/s1/{name}",
            "name": name,
            "mime": "image/png",
        }


# --------------------------------------------------------------------------
# fill_records 的编排逻辑（假引擎，任何环境都能跑）
# --------------------------------------------------------------------------


class FillRecordsTests(OcrCase):
    def test_every_image_gets_a_notice_and_an_ocr_field(self) -> None:
        engine = FakeEngine("第一行")
        records = [self.seed("a" * 16 + ".png"), self.seed("b" * 16 + ".png")]
        with fake_engine(engine):
            filled = fill_records(records, data_dir=self.tmp, emit=self.emit)

        self.assertEqual(len(filled), 2)
        for item in filled:
            self.assertIn("ocr", item)
            self.assertEqual(item["ocr"]["engine"], ocr.engine_info())
            self.assertGreaterEqual(item["ocr"]["seconds"], 0.0)
            self.assertIn("第一行", item["ocr"]["text"])
            self.assertEqual(item["ocr"]["chars"], len(item["ocr"]["text"]))
        # 每张图**之前**都要先说一声，界面不能静默卡住（契约 §13.2）
        self.assertEqual(self.notices(), ["正在识别图片文字…"] * 2)
        self.assertEqual(engine.calls, 2)

    def test_empty_text_is_recorded_as_an_empty_string(self) -> None:
        """一张都没识别到 → `text` 是空串，**不是** None、也不是"未识别"。"""
        records = [self.seed("9" * 16 + ".png")]
        with fake_engine(FakeEngine("")):
            filled = fill_records(records, data_dir=self.tmp, emit=self.emit)
        self.assertEqual(filled[0]["ocr"]["text"], "")
        self.assertEqual(filled[0]["ocr"]["chars"], 0)
        self.assertNotIn("error", filled[0]["ocr"])

    def test_records_are_not_mutated(self) -> None:
        """返回新列表；调用方手里那份事件记录不许被这个函数悄悄改写。"""
        original = self.seed("c" * 16 + ".png")
        with fake_engine(FakeEngine()):
            filled = fill_records([original], data_dir=self.tmp, emit=self.emit)
        self.assertNotIn("ocr", original)
        self.assertIsNot(filled[0], original)

    def test_one_bad_image_does_not_stop_the_others(self) -> None:
        engine = FakeEngine("好的那张")
        records = [self.seed("d" * 16 + ".png"), self.missing(), self.seed("e" * 16 + ".png")]
        with fake_engine(engine):
            filled = fill_records(records, data_dir=self.tmp, emit=self.emit)

        self.assertIn("好的那张", filled[0]["ocr"]["text"])
        # 失败的那张记 error，**不写半截 text**（空 text 有明确含义：真的没识别到字）
        self.assertIn("error", filled[1]["ocr"])
        self.assertNotIn("text", filled[1]["ocr"])
        self.assertIn("好的那张", filled[2]["ocr"]["text"])
        # 失败必须看得见：一条 warn，并且它说的是"识别失败"而不是泛泛的提示
        warned = self.notices("warn")
        self.assertEqual(len(warned), 1)
        self.assertIn("识别失败", warned[0])
        self.assertIn("deadbeefdeadbeef.png", warned[0])
        # 三张图各有一次"正在识别"：失败的那张也是**先说了再失败**的
        self.assertEqual(len(self.notices("info")), 3)
        self.assertEqual(engine.calls, 2)

    def test_empty_records_do_not_touch_the_engine(self) -> None:
        with mock.patch.object(ocr, "available", return_value=False):
            self.assertEqual(fill_records([], data_dir=self.tmp, emit=self.emit), [])
        self.assertEqual(self.events, [])

    def test_unavailable_engine_raises_and_writes_nothing(self) -> None:
        """引擎不可用 = 直接拒绝：不静默降级、不写任何事件（契约 §13.2）。"""
        records = [self.seed("f" * 16 + ".png")]
        with mock.patch.object(ocr, "available", return_value=False):
            with self.assertRaises(OcrUnavailable) as caught:
                fill_records(records, data_dir=self.tmp, emit=self.emit)
        self.assertEqual(self.events, [])
        self.assertIn("多模态", str(caught.exception))

    def test_engine_load_failure_is_reported_before_any_notice(self) -> None:
        """装上了但加载不起来（模型文件缺/坏）也必须走 400 那条路，且不留半截状态。"""
        records = [self.seed("1" * 16 + ".png")]
        with mock.patch.object(ocr, "available", return_value=True), mock.patch.object(
            ocr, "_engine", side_effect=OcrUnavailable("模型加载失败")
        ):
            with self.assertRaises(OcrUnavailable):
                fill_records(records, data_dir=self.tmp, emit=self.emit)
        self.assertEqual(self.events, [])

    def test_bad_path_is_refused_and_recorded(self) -> None:
        engine = FakeEngine()
        record = {"id": "x" * 16, "file": "../outside.png", "name": "outside.png"}
        with fake_engine(engine):
            filled = fill_records([record], data_dir=self.tmp, emit=self.emit)
        self.assertIn("error", filled[0]["ocr"])
        self.assertEqual(engine.calls, 0)

    def test_absolute_file_field_is_refused(self) -> None:
        engine = FakeEngine()
        record = {"id": "y" * 16, "file": "/etc/passwd", "name": "passwd"}
        with fake_engine(engine):
            filled = fill_records([record], data_dir=self.tmp, emit=self.emit)
        self.assertIn("error", filled[0]["ocr"])
        self.assertEqual(engine.calls, 0)


class AvailabilityTests(OcrCase):
    def test_available_is_false_when_the_import_fails(self) -> None:
        """引擎"没装"这一条必须能被模拟 —— 否则"缺依赖时不静默降级"就没法钉住。

        `sys.modules[name] = None` 会让 `import name` 抛 ImportError，
        正是"这台机器没装"的效果。
        """
        with mock.patch.dict(sys.modules, {"rapidocr_onnxruntime": None}):
            self.assertFalse(ocr.available())

    def test_engine_info_never_raises_and_names_the_engine(self) -> None:
        info = ocr.engine_info()
        self.assertTrue(info)
        self.assertTrue(info.startswith("rapidocr-onnxruntime"))
        with mock.patch.dict(sys.modules, {"rapidocr_onnxruntime": None}):
            # 引擎不在时也要能报出一个名字（它是给"当时用了哪个引擎"留线索的）
            self.assertTrue(ocr.engine_info())


# --------------------------------------------------------------------------
# 错误消息：原话留着，绝对路径必须去掉
# --------------------------------------------------------------------------


class ErrorMessageTests(OcrCase):
    def test_engine_message_keeps_the_reason_but_drops_the_path(self) -> None:
        target = self.tmp / "attachments" / "s1" / ("a" * 16 + ".png")
        exc = RuntimeError(f"cannot identify image file {target}")
        text = ocr._safe_message(exc, target)
        # 原话（为什么失败）必须留着
        self.assertIn("RuntimeError", text)
        self.assertIn("cannot identify image file", text)
        # 但这台机器的盘符与目录名绝不能跟着进事件日志（ADR-042）
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(str(target), text)
        self.assertIn(target.name, text)

    def test_recognize_on_a_missing_file_is_an_ocr_error(self) -> None:
        """文件不在 = 这一张失败，**不是**"没识别到文字"，也不该去加载引擎。"""
        missing = self.tmp / "attachments" / "s1" / "nope.png"
        with mock.patch.object(ocr, "_engine") as engine:
            with self.assertRaises(OcrError):
                ocr.recognize(missing)
        engine.assert_not_called()

    def test_recognize_wraps_engine_failures_with_the_original_words(self) -> None:
        target = self.tmp / "attachments" / "s1" / ("b" * 16 + ".png")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(TINY_PNG)

        def boom(payload):
            raise ValueError(f"onnxruntime exploded on {target}")

        with mock.patch.object(ocr, "available", return_value=True), mock.patch.object(
            ocr, "_engine", return_value=boom
        ):
            with self.assertRaises(OcrError) as caught:
                ocr.recognize(target)
        message = str(caught.exception)
        self.assertIn("onnxruntime exploded", message)
        self.assertNotIn(str(target), message)


# --------------------------------------------------------------------------
# 串行化：两个线程同时识别
# --------------------------------------------------------------------------


class ConcurrencyTests(OcrCase):
    def test_lock_serializes_the_threads(self) -> None:
        """锁真的在排队：假引擎里记下"同一时刻有几个在跑"，它必须始终是 1。

        这条**不是**在说"不加锁会崩" —— 实测无锁并发也能跑完（见 ocr 模块文档）。
        只是要钉住"我们确实串行化了"这件事，别哪天被人顺手把锁删掉。
        """
        engine = FakeEngine("并发结果", delay=0.1)
        records = [self.seed("2" * 16 + ".png")]
        with fake_engine(engine):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(fill_records, records, data_dir=self.tmp)
                    for _ in range(4)
                ]
                filled = [future.result(timeout=60) for future in futures]

        results = [item[0]["ocr"]["text"] for item in filled]
        self.assertEqual(len(set(results)), 1, "并发下结果必须一致")
        self.assertEqual(engine.calls, 4)
        self.assertEqual(engine.max_concurrent, 1, "锁没生效：两个线程同时在调引擎")


# --------------------------------------------------------------------------
# 真引擎（没装 rapidocr 时 skip）
# --------------------------------------------------------------------------


@unittest.skipUnless(font_available(), "这台机器的字体目录里找不到可用的中文字体")
class RealEngineTests(OcrCase):
    @needs_real_engine
    def test_reads_chinese_english_and_digits(self) -> None:
        """现画一张图，断言识别出的**关键片段**。

        刻意只断言"稳定出现"的片段：同一张图上 `CNY` 这类三个大写字母有时会被
        认成 `CNV`（实测），"第三行：验证换行与标点"也可能被拆成两段 ——
        那是 OCR 的固有抖动，把它写进断言只会得到一条时红时绿的测试。
        """
        lines = [
            "多模态模式已关闭",
            "只识别图片中的文字",
            "Order #A-2026-1014 total 128.50 CNY",
            "第三行：验证换行与标点，句号。",
        ]
        path = self.tmp / "sample.png"
        path.write_bytes(draw_text_png(lines))
        result = ocr.recognize(path)

        self.assertIsInstance(result, OcrResult)
        for fragment in (
            "多模态模式已关闭",
            "只识别图片中的文字",
            "Order #A-2026-1014",
            "128.50",
            "第三行",
            "验证换行与标点",
        ):
            self.assertIn(fragment, result.text, f"没识别出 {fragment!r}：{result.text!r}")
        self.assertEqual(result.chars, len(result.text))
        self.assertGreater(result.seconds, 0.0)
        self.assertTrue(result.engine.startswith("rapidocr-onnxruntime"))
        self.assertEqual(result.to_dict()["text"], result.text)

    @needs_real_engine
    def test_blank_image_returns_an_empty_string_not_an_error(self) -> None:
        """**"没识别到文字" ≠ "失败"** —— 这是上层区分两种情况的唯一依据。"""
        path = self.tmp / "blank.png"
        path.write_bytes(draw_blank_png())
        result = ocr.recognize(path)
        self.assertEqual(result.text, "")
        self.assertEqual(result.chars, 0)
        self.assertGreater(result.seconds, 0.0)

    @needs_real_engine
    def test_two_threads_read_the_same_image(self) -> None:
        """并发下不抛异常、结果一致。**不断言"锁防止了崩溃"**（不加锁实测也没崩）。"""
        path = self.tmp / "threads.png"
        path.write_bytes(draw_text_png(["并发识别测试", "Second line 2026"]))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ocr.recognize, path) for _ in range(2)]
            # 宽松超时：真引擎单张要几秒，这里只防"卡死"，不量性能。
            results = [future.result(timeout=180) for future in futures]

        self.assertEqual(results[0].text, results[1].text)
        self.assertIn("并发识别测试", results[0].text)

    @needs_real_engine
    def test_fill_records_runs_the_real_engine(self) -> None:
        record = self.seed("3" * 16 + ".png", payload=draw_text_png(["真实引擎跑一遍"]))
        filled = fill_records([record], data_dir=self.tmp, emit=self.emit)
        self.assertNotIn("error", filled[0]["ocr"])
        self.assertIn("真实引擎", filled[0]["ocr"]["text"])
        self.assertEqual(self.notices(), ["正在识别图片文字…"])


if __name__ == "__main__":
    unittest.main()
