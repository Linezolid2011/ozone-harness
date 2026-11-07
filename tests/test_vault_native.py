"""原生 vault（A/B 对齐）的单元测试：路径、frontmatter、清单与生成物。

这些用例只碰一次性临时目录，不联网、不花钱。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.vault import (
    Frontmatter,
    INDEX_NAME,
    OBSERVATIONS_NAME,
    README_NAME,
    SECTIONS_CONFIG,
    SectionSpec,
    SectionsManifest,
    VaultPathError,
    VaultStore,
    default_manifest,
    dump_frontmatter,
    dump_sections_manifest,
    normalize_raw_path,
    normalize_rel_path,
    parse_frontmatter,
    parse_sections_manifest,
)


class NativePathTests(unittest.TestCase):
    def test_accepts_native_paths(self) -> None:
        self.assertEqual(
            normalize_rel_path("sections/archived_month/2026-09/this_month.md"),
            "sections/archived_month/2026-09/this_month.md",
        )
        self.assertEqual(normalize_rel_path("index.md"), INDEX_NAME)
        self.assertEqual(normalize_rel_path("observations.md"), OBSERVATIONS_NAME)
        self.assertEqual(normalize_rel_path("sections.yaml"), SECTIONS_CONFIG)
        self.assertEqual(
            normalize_raw_path("raw/source_11/icais2026_pic1.jpg"),
            "raw/source_11/icais2026_pic1.jpg",
        )
        self.assertEqual(
            normalize_raw_path("raw/source_11/icais2026_pic1.jpg.meta.json"),
            "raw/source_11/icais2026_pic1.jpg.meta.json",
        )

    def test_rejects_other_depth_and_raw_shapes(self) -> None:
        for raw in (
            "sections/archived_month/2026-13/this_month.md",
            "sections/archived_month/2026-09/other.md",
            "sections/foo/2026-09/this_month.md",
            "raw/source_x/a.png",
            "raw/source_1/a.txt",
            "raw/source_1/../a.png",
            "raw/source_1/a.png/extra",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(VaultPathError):
                    if raw.startswith("raw/"):
                        normalize_raw_path(raw)
                    else:
                        normalize_rel_path(raw)


class NativeFrontmatterTests(unittest.TestCase):
    def test_native_fields_and_nested_values_round_trip(self) -> None:
        text = (
            "---\n"
            "name: project_photon\n"
            "tags:\n"
            "  - project\n"
            "  - photography\n"
            "created: \"2026-09-17\"\n"
            "last_updated: \"2026-09-17\"\n"
            "relation:\n"
            "  - classmate_of: user\n"
            "date:\n"
            "  2026_edition: \"2026-10-18\"\n"
            "place: Beijing\n"
            "---\n\nbody\n"
        )
        front, body, warnings = parse_frontmatter(text)
        self.assertEqual(warnings, [])
        self.assertEqual(front.name, "project_photon")
        self.assertEqual(front.created, "2026-09-17")
        self.assertEqual(front.last_updated, "2026-09-17")
        self.assertEqual(front.place, "Beijing")
        self.assertEqual(front.tags, ("project", "photography"))
        self.assertEqual(front.extra["relation"], [{"classmate_of": "user"}])
        self.assertEqual(front.extra["date"], {"2026_edition": "2026-10-18"})
        dumped = dump_frontmatter(front)
        back, _, warnings2 = parse_frontmatter(dumped + "\nbody\n")
        self.assertEqual(warnings2, [])
        self.assertEqual(back.to_dict(), front.to_dict())
        self.assertIn("relation:\n  - classmate_of: user", dumped)
        self.assertIn("date:\n  2026_edition: \"2026-10-18\"", dumped)

    def test_unknown_nested_key_is_preserved(self) -> None:
        front, _, _ = parse_frontmatter(
            "---\ntitle: x\ncustom:\n  a: b\n---\n正文\n"
        )
        self.assertEqual(front.extra["custom"], {"a": "b"})

    def test_list_of_multi_key_mappings_round_trips(self) -> None:
        front = Frontmatter(
            extra={"relation": [{"classmate_of": "user", "since": "2026"}]}
        )
        dumped = dump_frontmatter(front)
        back, _, warnings = parse_frontmatter(dumped + "\nbody\n")
        self.assertEqual(warnings, [])
        self.assertEqual(back.to_dict(), front.to_dict())
        self.assertEqual(dump_frontmatter(back), dumped)


class ManifestTests(unittest.TestCase):
    def test_parse_and_dump_native_manifest(self) -> None:
        text = (
            "inject_char_cap: 6000\n"
            "sections:\n"
            "  - path: sections/personal_context.md\n"
            "    title: Personal Context\n"
            "  - path: sections/taste.md\n"
            "    title: Taste & Preferences\n"
        )
        manifest, warnings = parse_sections_manifest(text)
        self.assertEqual(warnings, [])
        self.assertEqual(manifest.inject_char_cap, 6000)
        self.assertEqual(
            [spec.path for spec in manifest.sections],
            ["sections/personal_context.md", "sections/taste.md"],
        )
        self.assertEqual(manifest.sections[1].title, "Taste & Preferences")
        again, _ = parse_sections_manifest(dump_sections_manifest(manifest))
        self.assertEqual(again, manifest)

    def test_bad_lines_degrade_to_warnings(self) -> None:
        manifest, warnings = parse_sections_manifest(
            "inject_char_cap: nope\nsections:\n  - path: ../evil.md\n"
        )
        self.assertEqual(manifest, default_manifest())
        self.assertTrue(warnings)


class NativeVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "vault"
        self.store = VaultStore(self.root)
        self.store.init_skeleton()

    def _write(self, rel: str, body: str, front: Frontmatter | None = None) -> None:
        self.store.save_note(rel, body, front or Frontmatter())

    def _use(self, *specs: tuple[str, str]) -> None:
        """只保留这次用例关心的 section（否则默认 6 个里缺文件会被 check 报出来）。"""
        self.store.write_manifest(
            SectionsManifest(
                inject_char_cap=6000,
                sections=tuple(SectionSpec(path, title) for path, title in specs),
            )
        )

    def test_skeleton_creates_manifest_and_placeholder_readme(self) -> None:
        self.assertTrue(self.store.is_native())
        self.assertTrue((self.root / SECTIONS_CONFIG).is_file())
        self.assertFalse(self.store.has_content())
        self.assertEqual(self.store.readme_text(), ("", False))

    def test_init_does_not_convert_a_legacy_vault(self) -> None:
        legacy_root = self.root.parent / "legacy"
        (legacy_root / "sections").mkdir(parents=True)
        (legacy_root / "sections" / "old.md").write_text("old", encoding="utf-8")
        legacy = VaultStore(legacy_root)
        legacy.init_skeleton()
        self.assertFalse((legacy_root / SECTIONS_CONFIG).exists())
        self.assertFalse(legacy.is_native())

    def test_path_without_md_is_resolved_for_native_links(self) -> None:
        self._write("sections/personal_context.md", "body")
        self._write("entities/project_photon.md", "body")
        self.assertEqual(
            self.store.resolve_friendly("sections/personal_context"),
            "sections/personal_context.md",
        )
        self.assertEqual(
            self.store.resolve_friendly("entities/project_photon"),
            "entities/project_photon.md",
        )

    def test_generated_readme_index_and_earlier_months(self) -> None:
        self._use(
            ("sections/personal_context.md", "Personal Context"),
            ("sections/this_month.md", "This Month"),
            ("sections/earlier_months.md", "Earlier Months"),
        )
        self._write(
            "entities/project_photon.md",
            "Body [[class_5]] and ![[raw/source_1/pic.png]]. [^6]",
            Frontmatter(name="project_photon", tags=("project", "photography")),
        )
        self._write("sections/personal_context.md", "Context body [[project_photon]].")
        self._write("sections/this_month.md", "# This Month\n\nOctober body.")
        self.store.write_file(
            "sections/archived_month/2026-09/this_month.md",
            "---\nmonth: 2026-09\n---\n\n# 2026-09\n\nSeptember body.",
        )
        (self.root / "raw/source_1").mkdir(parents=True)
        (self.root / "raw/source_1/pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

        result = self.store.rebuild_generated(today="2026-11-07", last_dream="2026-11-07")
        self.assertTrue(result["rebuilt"])
        readme = self.store.read(README_NAME)
        self.assertIn("## Personal Context", readme.body)
        self.assertIn("## This Month", readme.body)
        self.assertIn("## 2026-09", readme.body)
        self.assertEqual(readme.body.count("## This Month"), 1)
        self.assertIn("project_photon", readme.body)
        # `chars` 是正文的 UTF-8 字节数（原生实测口径）
        self.assertEqual(
            readme.frontmatter.extra.get("chars"),
            str(len(readme.body.encode("utf-8"))),
        )
        self.assertEqual(
            readme.frontmatter.extra.get("sections"),
            ["personal_context", "this_month", "earlier_months"],
        )
        index = self.store.read(INDEX_NAME)
        self.assertIn("[[entities/project_photon|project_photon]]", index.body)
        self.assertIn(
            "[[sections/archived_month/2026-09/this_month|2026-09]]", index.body
        )
        earlier = self.store.read("sections/earlier_months.md")
        self.assertIn("## 2026-09", earlier.body)
        self.assertEqual(self.store.check()["problems"], [])

    def test_generated_files_are_read_only(self) -> None:
        for rel in (README_NAME, INDEX_NAME, "sections/earlier_months.md"):
            with self.subTest(rel=rel):
                with self.assertRaises(VaultPathError):
                    self.store.save_note(rel, "nope")

    def test_cannot_delete_a_manifest_section(self) -> None:
        self._write("sections/personal_context.md", "body")
        with self.assertRaises(VaultPathError):
            self.store.delete_file("sections/personal_context.md")
        # 非固定 section / entity 可以删
        self._write("entities/x.md", "body")
        self.assertEqual(self.store.delete_file("entities/x.md"), "entities/x.md")

    def test_custom_section_is_added_to_manifest_on_rebuild(self) -> None:
        self._use(("sections/personal_context.md", "Personal Context"))
        self._write("sections/personal_context.md", "context")
        self._write("sections/custom.md", "custom body")
        self.store.rebuild_generated(today="2026-11-07")
        manifest = self.store.manifest()
        self.assertIn("sections/custom.md", manifest.paths())
        self.assertIn("## custom", self.store.read(README_NAME).body)

    def test_char_cap_truncates_and_reports(self) -> None:
        self.store.write_manifest(
            SectionsManifest(
                inject_char_cap=240,
                sections=(SectionSpec("sections/personal_context.md", "Personal Context"),),
            )
        )
        self._write(
            "sections/personal_context.md",
            "\n\n".join("### 小节" + "字" * 80 for _ in range(8)),
        )
        self.store.rebuild_generated(today="2026-11-07")
        text, truncated = self.store.readme_text()
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 240)
        self.assertIn("截断", text)

    def test_stale_generated_files_are_reported(self) -> None:
        self._use(("sections/personal_context.md", "Personal Context"))
        self._write("sections/personal_context.md", "one")
        self.store.rebuild_generated(today="2026-11-07")
        self.assertEqual(self.store.check()["problems"], [])
        self._write("sections/personal_context.md", "two")
        problems = self.store.check()["problems"]
        self.assertTrue(any("README" in item for item in problems), problems)

    def test_raw_assets_are_readable_and_missing_references_reported(self) -> None:
        self._use(("sections/personal_context.md", "Personal Context"))
        (self.root / "raw/source_1").mkdir(parents=True)
        (self.root / "raw/source_1/pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self._write("sections/personal_context.md", "![[raw/source_1/pic.png]]")
        self.store.rebuild_generated(today="2026-11-07")
        self.assertEqual(self.store.raw_files()[0]["path"], "raw/source_1/pic.png")
        data, canonical = self.store.read_raw("raw/source_1/pic.png")
        self.assertEqual(canonical, "raw/source_1/pic.png")
        self.assertTrue(data)
        self.assertEqual(self.store.check()["problems"], [])

        self._write("sections/personal_context.md", "![[raw/source_1/gone.png]]")
        self.store.rebuild_generated(today="2026-11-07")
        self.assertTrue(
            any("gone.png" in item for item in self.store.check()["problems"])
        )


if __name__ == "__main__":
    unittest.main()
