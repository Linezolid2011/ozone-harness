"""原生 vault zip 导入（A）的安全与兼容测试。"""

from __future__ import annotations

import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from harness.vault import VaultStore
from harness.vault_import import VaultImportError, import_vault_zip

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
JPG = b"\xff\xd8\xff" + b"fake-jpg-body"


def _make_zip(path: Path, files: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            archive.writestr(name, data)
    return path


NATIVE_FILES: dict[str, bytes | str] = {
    "README.md": (
        "---\nuser_id: local-x\ncreated: \"2026-08-31\"\nchars: 1\n---\n\n## Personal Context\n\nhello\n"
    ),
    "index.md": "# Vault Index\n",
    "sections.yaml": (
        "inject_char_cap: 6000\n"
        "sections:\n"
        "  - path: sections/personal_context.md\n"
        "    title: Personal Context\n"
    ),
    "sections/personal_context.md": "Hello [[project_photon]].",
    "sections/archived_month/2026-09/this_month.md": (
        "---\nmonth: 2026-09\n---\n\n# 2026-09\n\nSeptember.\n"
    ),
    "entities/project_photon.md": (
        "---\nname: project_photon\ntags:\n  - project\ncreated: \"2026-09-17\"\n"
        "last_updated: \"2026-09-17\"\n---\n\n# project_photon\n\nBody.\n"
    ),
    "raw/source_1/pic.png": PNG,
    "raw/source_1/pic.png.meta.json": '{"source_ref": 1}',
    "memory_instruction.md": "# Memory instructions\n",
}


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.zip = _make_zip(self.tmp / "vault.zip", NATIVE_FILES)
        self.store = VaultStore(self.tmp / "vault")

    def test_imports_a_native_vault(self) -> None:
        result = import_vault_zip(self.zip, self.store)
        self.assertTrue(result["ok"])
        self.assertTrue(result["native"])
        self.assertEqual(result["files"], len(NATIVE_FILES))
        self.assertEqual(result["raw"], 2)
        self.assertTrue((self.store.root / "sections.yaml").is_file())
        self.assertEqual(
            self.store.read("sections/archived_month/2026-09/this_month.md").title,
            "this_month",
        )
        self.assertEqual(self.store.raw_files()[0]["path"], "raw/source_1/pic.png")
        self.assertEqual(self.store.read_raw("raw/source_1/pic.png")[0], PNG)
        self.assertEqual(
            self.store.read("entities/project_photon.md").frontmatter.name,
            "project_photon",
        )

    def test_refuses_a_non_empty_vault_unless_overwrite(self) -> None:
        (self.store.root).mkdir(parents=True)
        (self.store.root / "README.md").write_text("old", encoding="utf-8")
        with self.assertRaises(VaultImportError):
            import_vault_zip(self.zip, self.store)
        result = import_vault_zip(self.zip, self.store, overwrite=True)
        self.assertTrue(result["ok"])

    def test_rejects_non_zip(self) -> None:
        path = self.tmp / "not.zip"
        path.write_text("not a zip", encoding="utf-8")
        with self.assertRaises(VaultImportError):
            import_vault_zip(path, self.store)

    def test_rejects_traversal_and_unknown_root(self) -> None:
        for name in ("../evil.md", "sections/../../evil.md", "notes.txt"):
            with self.subTest(name=name):
                bad = _make_zip(self.tmp / "bad.zip", {name: "x"})
                with self.assertRaises(VaultImportError):
                    import_vault_zip(bad, self.store)

    def test_rejects_bad_raw_images(self) -> None:
        for name, payload in (
            ("raw/source_1/pic.png", b"not an image"),
            ("raw/source_1/pic.jpg", PNG),      # 扩展名与内容不符
            ("raw/source_1/pic.png", JPG),
        ):
            with self.subTest(name=name, payload=payload[:4]):
                bad = _make_zip(self.tmp / "bad.zip", {name: payload})
                with self.assertRaises(VaultImportError):
                    import_vault_zip(bad, self.store)

    def test_rejects_symlink_entries(self) -> None:
        bad = self.tmp / "symlink.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            info = zipfile.ZipInfo("entities/link.md")
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "target")
        with self.assertRaises(VaultImportError):
            import_vault_zip(bad, self.store)

    def test_rejects_exact_duplicate_paths(self) -> None:
        bad = self.tmp / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("entities/same.md", "first")
                archive.writestr("entities/same.md", "second")
        with self.assertRaises(VaultImportError):
            import_vault_zip(bad, self.store)
        self.assertFalse(self.store.root.exists())

    def test_copy_failure_leaves_existing_vault_unchanged(self) -> None:
        self.store.root.mkdir(parents=True)
        (self.store.root / "README.md").write_text("old readme", encoding="utf-8")
        (self.store.root / "entities").mkdir()
        (self.store.root / "entities" / "old.md").write_text("keep", encoding="utf-8")
        before = {
            path.relative_to(self.store.root): path.read_bytes()
            for path in self.store.root.rglob("*")
            if path.is_file()
        }

        def copy_then_fail(source, destination, **kwargs) -> None:
            target = Path(destination)
            target.mkdir(parents=True, exist_ok=True)
            (target / "README.md").write_text("partial", encoding="utf-8")
            # 旧库没有清单；只有复制暂存的新档案时才模拟写完后的失败。
            if (Path(source) / "sections.yaml").is_file():
                raise OSError("simulated commit failure")

        with mock.patch("harness.vault_import.shutil.copytree", side_effect=copy_then_fail):
            with self.assertRaisesRegex(OSError, "simulated commit failure"):
                import_vault_zip(self.zip, self.store, overwrite=True)
        after = {
            path.relative_to(self.store.root): path.read_bytes()
            for path in self.store.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_skips_system_junk(self) -> None:
        files = dict(NATIVE_FILES)
        files["__MACOSX/._README.md"] = b"junk"
        files[".DS_Store"] = b"junk"
        archive = _make_zip(self.tmp / "junk.zip", files)
        result = import_vault_zip(archive, self.store)
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])
        self.assertFalse((self.store.root / ".DS_Store").exists())


if __name__ == "__main__":
    unittest.main()
