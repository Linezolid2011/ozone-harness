"""图片附件落盘层测试：嗅探、缩放、去重、上限、路径闸、清理。

全部在临时数据目录里跑（`test_settings`），**绝不碰真实的 data/**。

Pillow 相关用例在没装 Pillow 的环境里会 skip 而不是报错 —— 本项目的
"无依赖模式"（系统 Python，只跑纯标准库那些）必须照样能全绿。
"""

from __future__ import annotations

import base64
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from harness import attachments
from harness.attachments import (
    ALLOWED_EXTS,
    EXT_MIME,
    AttachmentError,
    count_files,
    decode_base64_data,
    delete_session_attachments,
    gc_apply,
    gc_report,
    load_parts,
    read_bytes,
    records_for,
    resolve_path,
    session_dir,
    sniff_ext,
    store,
)
from harness.events import EventLog
from harness.llm_types import ImagePart

from .helpers import test_settings

HAS_PILLOW = attachments.pillow_available()
needs_pillow = unittest.skipUnless(
    HAS_PILLOW, "这条用例需要 Pillow（用 .venv 里的解释器跑才有）"
)

# 一张真的 1×1 PNG（不依赖 Pillow 就能构造），用于"路径/引用"这类不需要解码的用例。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

HEX_ID = "a1b2c3d4e5f60718"


def make_image_bytes(width: int, height: int, fmt: str = "PNG", **kwargs) -> bytes:
    """用 Pillow 造一张纯色图并编码成字节（只在 needs_pillow 的用例里调用）。"""
    from PIL import Image

    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), (120, 30, 200))
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


class TempDataCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(self.tmp)

    def session_id(self) -> str:
        return "s1"


# --------------------------------------------------------------------------
# 魔数嗅探
# --------------------------------------------------------------------------


class SniffTests(unittest.TestCase):
    def test_recognises_the_four_supported_formats(self) -> None:
        self.assertEqual(sniff_ext(b"\x89PNG\r\n\x1a\n" + b"rest"), "png")
        self.assertEqual(sniff_ext(b"\xff\xd8\xff\xe0" + b"rest"), "jpg")
        self.assertEqual(sniff_ext(b"GIF89a" + b"rest"), "gif")
        self.assertEqual(sniff_ext(b"GIF87a" + b"rest"), "gif")
        riff = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 "
        self.assertEqual(sniff_ext(riff), "webp")

    def test_unknown_content_is_rejected(self) -> None:
        self.assertIsNone(sniff_ext(b"hello, I am not an image"))
        self.assertIsNone(sniff_ext(b""))
        self.assertIsNone(sniff_ext(b"<!DOCTYPE html><html></html>"))
        self.assertIsNone(sniff_ext(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"))

    def test_riff_container_must_actually_say_webp(self) -> None:
        """只看 `RIFF` 会把音频也放进来，所以偏移 8 处还要再验一次。"""
        self.assertIsNone(sniff_ext(b"RIFF\x24\x00\x00\x00WAVEfmt "))
        self.assertIsNone(sniff_ext(b"RIFF\x24\x00\x00\x00"))

    def test_truncated_headers_do_not_crash(self) -> None:
        for blob in (b"\x89PNG", b"\xff\xd8", b"GIF8", b"RIFF\x00\x00"):
            self.assertIsNone(sniff_ext(blob))


# --------------------------------------------------------------------------
# 落盘：格式判定
# --------------------------------------------------------------------------


@needs_pillow
class StoreFormatTests(TempDataCase):
    def test_text_file_renamed_to_png_is_rejected(self) -> None:
        """**格式按内容判断**：改个扩展名、再声明 image/png 也不认。"""
        with self.assertRaises(AttachmentError) as ctx:
            store(
                self.settings,
                self.session_id(),
                b"just some text pretending to be an image",
                name="photo.png",
                declared_mime="image/png",
            )
        self.assertIn("内容", str(ctx.exception))
        self.assertFalse(session_dir(self.settings, self.session_id()).exists())

    def test_html_and_svg_disguised_as_images_are_rejected(self) -> None:
        for name, blob in (
            ("page.html.png", b"<!DOCTYPE html><html><script>x</script></html>"),
            ("vector.svg.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(AttachmentError):
                    store(self.settings, self.session_id(), blob, name=name)

    def test_png_magic_with_garbage_body_is_rejected_by_pillow(self) -> None:
        """魔数对但内容坏：嗅探通过，由 Pillow 拦下，而且报错要带原话。"""
        with self.assertRaises(AttachmentError) as ctx:
            store(
                self.settings,
                self.session_id(),
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
                name="broken.png",
            )
        self.assertIn("解不开", str(ctx.exception))
        self.assertFalse(session_dir(self.settings, self.session_id()).exists())

    def test_declared_mime_mismatch_becomes_a_warning(self) -> None:
        data = make_image_bytes(40, 40)
        record = store(
            self.settings,
            self.session_id(),
            data,
            name="shot.png",
            declared_mime="image/jpeg",
        )
        self.assertEqual(record["mime"], "image/png")
        self.assertTrue(record["warnings"], "声明与内容不一致必须说出来，不能静默接受")
        self.assertIn("image/jpeg", record["warnings"][0])

    def test_matching_mime_has_no_warning(self) -> None:
        record = store(
            self.settings,
            self.session_id(),
            make_image_bytes(40, 40),
            name="shot.png",
            declared_mime="image/png",
        )
        self.assertEqual(record["warnings"], [])


# --------------------------------------------------------------------------
# 原子写：同进程并发也不能共享临时文件
# --------------------------------------------------------------------------


class AtomicWriteTests(TempDataCase):
    def test_concurrent_writers_use_independent_temp_files(self) -> None:
        path = self.tmp / "attachments" / "s1" / "same.png"
        path.parent.mkdir(parents=True)
        barrier = threading.Barrier(2)
        sources: list[str] = []
        sources_lock = threading.Lock()
        synchronized: set[str] = set()
        real_replace = attachments.os.replace

        def replace_together(src, dst):
            name = Path(src).name
            with sources_lock:
                sources.append(name)
                first_attempt = name not in synchronized
                synchronized.add(name)
            # 只同步每个临时文件的第一次 replace。Windows 允许生产代码在短暂
            # PermissionError 后重试；重试若再等同一个 barrier 反而会把它弄坏。
            if first_attempt:
                barrier.wait(timeout=10)
            return real_replace(src, dst)

        with mock.patch.object(attachments.os, "replace", side_effect=replace_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(attachments._atomic_write, path, b"same")
                    for _ in range(2)
                ]
                for future in futures:
                    future.result(timeout=10)

        self.assertEqual(path.read_bytes(), b"same")
        self.assertEqual(len(set(sources)), 2, "两个写入仍然共享了临时文件名")
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])


# --------------------------------------------------------------------------
# 落盘：缩放、去重、上限
# --------------------------------------------------------------------------


@needs_pillow
class StoreScalingTests(TempDataCase):
    def test_large_image_is_scaled_down_with_aspect_ratio_kept(self) -> None:
        data = make_image_bytes(4000, 4000)
        record = store(self.settings, self.session_id(), data, name="big.png")
        self.assertLessEqual(
            record["stored_width"] * record["stored_height"],
            self.settings.attachment_max_pixels,
        )
        self.assertLessEqual(
            max(record["stored_width"], record["stored_height"]),
            self.settings.attachment_max_side,
        )
        self.assertAlmostEqual(
            record["stored_width"] / record["stored_height"], 1.0, places=2
        )
        self.assertEqual((record["width"], record["height"]), (4000, 4000))

        path = self.tmp / record["file"]
        from PIL import Image

        with Image.open(path) as image:
            self.assertEqual(image.size, (record["stored_width"], record["stored_height"]))

    def test_tall_image_is_limited_by_pixels_not_side(self) -> None:
        """4000×2000：像素超限、单边没超 —— 长宽比必须保持。"""
        record = store(
            self.settings, self.session_id(), make_image_bytes(4000, 2000), name="wide.png"
        )
        self.assertLessEqual(
            record["stored_width"] * record["stored_height"],
            self.settings.attachment_max_pixels,
        )
        self.assertAlmostEqual(
            record["stored_width"] / record["stored_height"], 2.0, places=2
        )

    def test_extreme_side_is_scaled_even_when_pixel_count_is_small(self) -> None:
        """单边超 8192 的细长图：像素没超也要缩（官方硬限制）。"""
        record = store(
            self.settings, self.session_id(), make_image_bytes(9000, 40), name="thin.png"
        )
        self.assertLessEqual(
            max(record["stored_width"], record["stored_height"]),
            self.settings.attachment_max_side,
        )

    def test_small_image_is_stored_byte_for_byte(self) -> None:
        """小图**不放大、不重编码**：存进去多少字节，落盘就是多少字节。"""
        data = make_image_bytes(200, 200)
        record = store(self.settings, self.session_id(), data, name="small.png")
        self.assertEqual((record["stored_width"], record["stored_height"]), (200, 200))
        self.assertEqual(record["stored_bytes"], len(data))
        self.assertEqual(record["bytes"], len(data))
        self.assertEqual(read_bytes(self.tmp / record["file"]), data)

    def test_same_content_twice_reuses_one_file(self) -> None:
        data = make_image_bytes(64, 64)
        first = store(self.settings, self.session_id(), data, name="a.png")
        second = store(self.settings, self.session_id(), data, name="b.png")
        self.assertEqual(first["file"], second["file"])
        self.assertEqual(first["id"], second["id"])
        directory = session_dir(self.settings, self.session_id())
        self.assertEqual([p.name for p in directory.iterdir()], [f"{first['id']}.png"])

    def test_animated_gif_scaled_warns_but_static_gif_does_not(self) -> None:
        from PIL import Image

        frames = [Image.new("RGB", (9000, 40), (i * 40, 0, 0)) for i in range(3)]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
        record = store(self.settings, self.session_id(), buffer.getvalue(), name="anim.gif")
        self.assertTrue(any("动图" in text for text in record["warnings"]), record["warnings"])

        static = store(
            self.settings, self.session_id(), make_image_bytes(9000, 40, "GIF"), name="still.gif"
        )
        self.assertEqual(static["warnings"], [])

    def test_name_is_display_only_and_sanitised(self) -> None:
        """用户文件名里塞路径/控制字符：只洗成显示串，绝不参与拼路径。"""
        record = store(
            self.settings,
            self.session_id(),
            make_image_bytes(32, 32),
            name="../../evil\r\nname with\nnewlines.png",
        )
        self.assertNotIn("/", record["name"])
        self.assertNotIn("\\", record["name"])
        self.assertNotIn("\n", record["name"])
        self.assertNotIn("\r", record["name"])
        # 落盘名仍然是内容哈希 + 真实扩展名
        self.assertEqual(Path(record["file"]).name, f"{record['id']}.png")

    def test_name_falls_back_when_it_is_all_control_characters(self) -> None:
        record = store(
            self.settings, self.session_id(), make_image_bytes(16, 16), name="\x00\x01\x02"
        )
        self.assertEqual(record["name"], "image.png")


@needs_pillow
class StoreLimitTests(TempDataCase):
    def test_over_max_bytes_is_rejected_with_the_limit_in_the_message(self) -> None:
        settings = test_settings(self.tmp, attachment_max_bytes=10)
        with self.assertRaises(AttachmentError) as ctx:
            store(settings, "s1", make_image_bytes(32, 32), name="big.png")
        self.assertIn("上限", str(ctx.exception))
        self.assertFalse(session_dir(settings, "s1").exists())

    def test_stored_bytes_limit_falls_back_to_webp_then_gives_up(self) -> None:
        """编码后超标：先退 WebP，还超标就明确拒绝（不能悄悄存个超大文件）。"""
        data = make_image_bytes(4000, 4000)
        tiny = test_settings(self.tmp, attachment_stored_max_bytes=1)
        with self.assertRaises(AttachmentError) as ctx:
            store(tiny, "s1", data, name="big.png")
        self.assertIn("落盘上限", str(ctx.exception))

    def test_webp_fallback_changes_the_stored_extension(self) -> None:
        """退到 WebP 时扩展名必须跟着变 —— 否则 GET 会按错的白名单发 Content-Type。"""
        import os

        from PIL import Image

        # 噪声图：PNG 压不动（≈4.3 MB），WebP q90 明显更小 —— 正好卡在两者之间
        # 设一个落盘上限，逼出降级链。用 os.urandom 而不是纯色，纯色两种格式都极小。
        image = Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3))
        png_buffer = io.BytesIO()
        image.save(png_buffer, format="PNG")
        webp_buffer = io.BytesIO()
        image.save(webp_buffer, format="WEBP", quality=90)
        png_len = len(png_buffer.getvalue())
        webp_len = len(webp_buffer.getvalue())
        self.assertLess(webp_len, png_len, "前提：噪声图的 WebP 必须比 PNG 小")

        settings = test_settings(self.tmp, attachment_stored_max_bytes=webp_len + 16)
        record = store(settings, "s1", png_buffer.getvalue(), name="noise.png")

        self.assertEqual(record["mime"], "image/webp")
        self.assertEqual(Path(record["file"]).suffix, ".webp")
        self.assertLessEqual(record["stored_bytes"], settings.attachment_stored_max_bytes)
        self.assertTrue(any("WebP" in text for text in record["warnings"]), record["warnings"])


class MaxCountTests(TempDataCase):
    """张数上限不依赖 Pillow：records_for 先数张数，再谈文件与解码。"""

    def test_over_max_count_is_rejected(self) -> None:
        limit = self.settings.attachment_max_count
        wanted = [{"id": HEX_ID, "ext": "png", "name": "x.png"} for _ in range(limit + 1)]
        with self.assertRaises(AttachmentError) as ctx:
            records_for(self.settings, "s1", wanted)
        self.assertIn(str(limit), str(ctx.exception))

    def test_exactly_the_limit_is_not_rejected_for_the_count(self) -> None:
        """刚好到上限时不该被"张数"拦下（后面会因为文件不存在而报别的错）。"""
        wanted = [{"id": HEX_ID, "ext": "png"}] * self.settings.attachment_max_count
        with self.assertRaises(AttachmentError) as ctx:
            records_for(self.settings, "s1", wanted)
        self.assertNotIn("最多", str(ctx.exception))


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------


class PathSafetyTests(TempDataCase):
    def test_session_id_must_match_the_pattern(self) -> None:
        for bad in ("../evil", "..", "a/b", "", "s 1", ".hidden", "x" * 65):
            with self.subTest(session_id=bad):
                with self.assertRaises(AttachmentError):
                    session_dir(self.settings, bad)

    def test_note_id_must_be_hex_of_the_right_length(self) -> None:
        for bad in ("../" + "a" * 16, "a" * 15, "z" * 16, "", "a" * 65, "a1b2/../../x"):
            with self.subTest(note_id=bad):
                with self.assertRaises(AttachmentError):
                    resolve_path(self.settings, "s1", bad, "png")

    def test_ext_must_be_in_the_whitelist(self) -> None:
        for bad in ("svg", "html", "js", "", "png.html", "../png"):
            with self.subTest(ext=bad):
                with self.assertRaises(AttachmentError):
                    resolve_path(self.settings, "s1", HEX_ID, bad)

    def test_valid_inputs_resolve_under_the_session_directory(self) -> None:
        path = resolve_path(self.settings, "s1", HEX_ID.upper(), ".PNG")
        self.assertEqual(path, session_dir(self.settings, "s1") / f"{HEX_ID}.png")
        self.assertTrue(path.is_relative_to(attachments.attachments_root(self.settings)))

    def test_whitelist_never_gains_svg(self) -> None:
        self.assertNotIn("svg", ALLOWED_EXTS)
        self.assertEqual(set(ALLOWED_EXTS), set(EXT_MIME))


# --------------------------------------------------------------------------
# Pillow 缺失
# --------------------------------------------------------------------------


class PillowMissingTests(TempDataCase):
    def test_store_refuses_without_writing_anything(self) -> None:
        with mock.patch.object(attachments, "pillow_available", return_value=False):
            with self.assertRaises(AttachmentError) as ctx:
                store(self.settings, "s1", TINY_PNG, name="x.png")
        self.assertEqual(ctx.exception.need, "pillow")
        # 错误信息要给出可执行的下一步，而不是一句"缺少依赖"
        self.assertIn("setup", str(ctx.exception))
        self.assertIn("requirements.txt", str(ctx.exception))
        # **什么都没落盘**：连目录都不该建
        self.assertFalse((self.tmp / "attachments").exists())

    def test_records_for_refuses_without_pillow(self) -> None:
        with mock.patch.object(attachments, "pillow_available", return_value=False):
            with self.assertRaises(AttachmentError) as ctx:
                records_for(self.settings, "s1", [{"id": HEX_ID, "ext": "png"}])
        self.assertEqual(ctx.exception.need, "pillow")

    def test_pillow_available_is_a_real_probe(self) -> None:
        self.assertIsInstance(attachments.pillow_available(), bool)


# --------------------------------------------------------------------------
# 引用解析与重放
# --------------------------------------------------------------------------


class LoadPartsTests(TempDataCase):
    def _write(self, relative: str, payload: bytes = TINY_PNG) -> Path:
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_round_trips_a_record_into_an_image_part(self) -> None:
        relative = f"attachments/s1/{HEX_ID}.png"
        self._write(relative)
        parts = load_parts(
            self.tmp,
            [
                {
                    "id": HEX_ID,
                    "file": relative,
                    "name": "image.png",
                    "mime": "image/png",
                    "stored_width": 1300,
                    "stored_height": 783,
                }
            ],
        )
        self.assertEqual(len(parts), 1)
        self.assertIsInstance(parts[0], ImagePart)
        self.assertEqual(parts[0].path, self.tmp / relative)
        self.assertEqual((parts[0].width, parts[0].height), (1300, 783))
        self.assertEqual(parts[0].mime, "image/png")

    def test_falls_back_to_the_original_dimensions_when_stored_ones_are_missing(self) -> None:
        relative = f"attachments/s1/{HEX_ID}.png"
        self._write(relative)
        parts = load_parts(
            self.tmp, [{"id": HEX_ID, "file": relative, "width": 640, "height": 480}]
        )
        self.assertEqual((parts[0].width, parts[0].height), (640, 480))

    def test_path_escape_raises_instead_of_reading_outside(self) -> None:
        """**fail closed**：越界路径只可能来自被改过的日志，绝不能照着它读文件。"""
        for bad in (
            "../../secret.png",
            "attachments/../../secret.png",
            "/etc/passwd.png",
        ):
            with self.subTest(file=bad):
                with self.assertRaises(AttachmentError):
                    load_parts(self.tmp, [{"id": HEX_ID, "file": bad}])

    def test_non_whitelisted_extension_raises(self) -> None:
        with self.assertRaises(AttachmentError):
            load_parts(
                self.tmp,
                [{"id": HEX_ID, "file": f"attachments/s1/{HEX_ID}.svg"}],
            )

    def test_missing_file_is_skipped_so_the_session_still_opens(self) -> None:
        """用户手工删掉一张图，不该让整个会话从此打不开（可用性优先）。"""
        parts = load_parts(
            self.tmp,
            [{"id": HEX_ID, "file": f"attachments/s1/{HEX_ID}.png"}],
        )
        self.assertEqual(parts, [])


# --------------------------------------------------------------------------
# base64 解码
# --------------------------------------------------------------------------


class DecodeTests(unittest.TestCase):
    def test_decodes_standard_and_urlsafe_alphabet(self) -> None:
        raw = b"\xfb\xff\x00binary"
        self.assertEqual(decode_base64_data(base64.b64encode(raw).decode()), raw)
        self.assertEqual(
            decode_base64_data(base64.urlsafe_b64encode(raw).decode()), raw
        )

    def test_accepts_a_full_data_url_and_whitespace(self) -> None:
        raw = TINY_PNG
        encoded = "data:image/png;base64," + base64.b64encode(raw).decode()
        self.assertEqual(decode_base64_data(encoded), raw)
        self.assertEqual(decode_base64_data(encoded[:40] + "\n" + encoded[40:]), raw)

    def test_broken_base64_is_reported_in_chinese(self) -> None:
        for bad in ("not base64!!!", "@@@@", "AAAA!", ""):
            with self.subTest(value=bad):
                with self.assertRaises(AttachmentError) as ctx:
                    decode_base64_data(bad)
                self.assertIn("base64", str(ctx.exception))


# --------------------------------------------------------------------------
# 记录构造
# --------------------------------------------------------------------------


@needs_pillow
class RecordsForTests(TempDataCase):
    def test_reads_the_truth_from_disk_and_ignores_frontend_extras(self) -> None:
        stored = store(self.settings, "s1", make_image_bytes(300, 200), name="cat.png")
        records = records_for(
            self.settings,
            "s1",
            [
                {
                    "id": stored["id"],
                    "ext": "png",
                    "name": "用户改的名字.png",
                    # 前端多给的字段一律不用（磁盘才是真相）
                    "width": 99999,
                    "file": "../../etc/passwd",
                }
            ],
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record["width"], record["height"]), (300, 200))
        self.assertEqual(record["name"], "用户改的名字.png")
        self.assertEqual(record["file"], stored["file"])
        self.assertEqual(record["stored_bytes"], (self.tmp / stored["file"]).stat().st_size)

    def test_missing_file_is_rejected_at_send_time(self) -> None:
        """上传那一刻能报错就要报错 —— 重放时才可以降级跳过。"""
        with self.assertRaises(AttachmentError) as ctx:
            records_for(self.settings, "s1", [{"id": HEX_ID, "ext": "png"}])
        self.assertIn("不存在", str(ctx.exception))


# --------------------------------------------------------------------------
# 统计与清理
# --------------------------------------------------------------------------


class GcTests(TempDataCase):
    def _seed(self) -> tuple[Path, Path]:
        """造一个被会话引用的文件 + 一个孤儿文件。"""
        log = EventLog(self.settings.sessions_dir / "s1.jsonl", "s1")
        self.addCleanup(log.close)
        referenced = self.tmp / "attachments" / "s1" / f"{HEX_ID}.png"
        referenced.parent.mkdir(parents=True, exist_ok=True)
        referenced.write_bytes(TINY_PNG)
        log.append("user_message", text="这是什么动物", attachments=[{"id": HEX_ID, "file": referenced.relative_to(self.tmp).as_posix()}])
        orphan = self.tmp / "attachments" / "s2" / f"{'b' * 16}.png"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(TINY_PNG + b"orphan")
        return referenced, orphan

    def test_count_files_counts_every_attachment(self) -> None:
        self.assertEqual(count_files(self.settings), 0)
        self._seed()
        self.assertEqual(count_files(self.settings), 2)

    def test_report_lists_only_orphans(self) -> None:
        referenced, orphan = self._seed()
        report = gc_report(self.settings)
        self.assertEqual(report["orphans"], [orphan.relative_to(self.tmp).as_posix()])
        self.assertEqual(report["referenced"], 1)
        self.assertEqual(report["total_files"], 2)
        self.assertGreater(report["orphan_bytes"], 0)

    def test_apply_deletes_only_orphans(self) -> None:
        referenced, orphan = self._seed()
        result = gc_apply(self.settings)
        self.assertEqual(result["deleted"], [orphan.relative_to(self.tmp).as_posix()])
        self.assertFalse(orphan.exists())
        self.assertTrue(referenced.exists(), "被引用的文件一个都不能动")
        self.assertEqual(gc_report(self.settings)["orphans"], [])
        # 空掉的会话目录会被收掉，但留着文件的那个目录必须还在
        self.assertFalse(orphan.parent.exists())
        self.assertTrue(referenced.parent.exists())

    def test_apply_rechecks_references_before_deleting(self) -> None:
        """报告生成后才出现的引用也必须保住文件，不能按过期快照 unlink。"""
        _referenced, orphan = self._seed()
        original_report = attachments.gc_report

        def report_then_reference(settings):
            report = original_report(settings)
            log = EventLog(self.settings.sessions_dir / "s2.jsonl", "s2")
            try:
                log.append(
                    "user_message",
                    text="现在引用了这张图",
                    attachments=[
                        {
                            "id": "b" * 16,
                            "file": orphan.relative_to(self.tmp).as_posix(),
                        }
                    ],
                )
            finally:
                log.close()
            return report

        with mock.patch.object(attachments, "gc_report", side_effect=report_then_reference):
            result = gc_apply(self.settings)

        self.assertTrue(orphan.is_file(), "GC 删掉了报告生成后已经被引用的图片")
        self.assertNotIn(orphan.relative_to(self.tmp).as_posix(), result["deleted"])

    def test_report_is_read_only(self) -> None:
        _referenced, orphan = self._seed()
        gc_report(self.settings)
        self.assertTrue(orphan.exists(), "只报告就不许删东西")


class DeleteSessionTests(TempDataCase):
    def test_deletes_the_directory_and_reports_the_count(self) -> None:
        store_dir = session_dir(self.settings, "s1")
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / f"{HEX_ID}.png").write_bytes(TINY_PNG)
        (store_dir / f"{'c' * 16}.png").write_bytes(TINY_PNG)
        self.assertEqual(delete_session_attachments(self.settings, "s1"), 2)
        self.assertFalse(store_dir.exists())

    def test_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(delete_session_attachments(self.settings, "nope"), 0)

    def test_bad_session_id_is_refused(self) -> None:
        with self.assertRaises(AttachmentError):
            delete_session_attachments(self.settings, "../..")


if __name__ == "__main__":
    unittest.main()
