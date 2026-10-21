"""配置文件的 **BOM** 容忍（ADR-085 的副产品，但它是独立的一类 bug）。

## 为什么值得一个专门的测试文件

`data/` 下那几份 JSON（`providers.json` / `tools.json` / `memory.json` / `settings.json`）
都是**给人手改**的。而 Windows 记事本在旧版本里默认写 **UTF-8 + BOM**，
PowerShell 的 `Set-Content -Encoding utf8`（5.1）同样带 BOM。

带 BOM 的 JSON 用普通 `utf-8` 去 `json.loads` 会直接 `JSONDecodeError`，
而这些读取点又**全部吞掉异常退回默认值**（那是有意的：坏配置不该让程序起不来）。
两者一叠加就是最糟的组合：**用户改了配置、程序当没看见、而且一声不吭** ——
`providers.json` 那一条尤其致命（表现是"我的接口和 key 全都不见了"）。

所以这几个读取点统一用 `utf-8-sig`（有 BOM 就剥掉，没有就是普通 utf-8）。

## 怎么发现它的

我自己写验收脚本时用 PowerShell 的 `-Encoding utf8` 落了一份 `settings.json`，
然后**看截图**发现界面还是中文 —— 语言设置被静默忽略了。
（同上一次：`_read_json` 那条也是 BOM 引起的，那次症状是"要指定一个语言"。）
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import i18n
from harness.memory_limit import MEMORY_FILE, MemoryLimitStore
from harness.net.config import TOOLS_FILE, ToolStore
from harness.providers.store import ProviderStore

from .helpers import test_settings

BOM = "\ufeff"


def _write_with_bom(path: Path, payload: dict) -> None:
    """按记事本那种方式写：UTF-8 + BOM。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BOM + json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class BomToleranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_language_store_reads_a_bom_file(self) -> None:
        path = self.root / i18n.SETTINGS_FILE
        _write_with_bom(path, {"language": "en"})
        self.assertEqual(i18n.LanguageStore(path).language(), "en")

    def test_memory_limit_store_reads_a_bom_file(self) -> None:
        path = self.root / MEMORY_FILE
        _write_with_bom(path, {"max_items": 7})
        self.assertEqual(MemoryLimitStore(path).limit(), 7)

    def test_provider_store_reads_a_bom_file(self) -> None:
        """最要命的一条：这份文件里是接口配置与 API key。"""
        settings = test_settings(self.root)
        _write_with_bom(
            settings.data_dir / "providers.json",
            {
                "default": {"provider": "deepseek", "model": "deepseek-flash"},
                "providers": [
                    {"id": "deepseek", "label": "DeepSeek", "api_key": "sk-test",
                     "base_url": "https://api.deepseek.com", "models": ["deepseek-flash"]}
                ],
            },
        )
        store = ProviderStore(settings.data_dir / "providers.json")
        self.assertEqual([p.id for p in store.providers()], ["deepseek"])
        self.assertTrue(store.providers()[0].configured, "带 BOM 时 key 不该被当成空的")

    def test_tool_store_reads_a_bom_file(self) -> None:
        settings = test_settings(self.root)
        _write_with_bom(
            settings.data_dir / TOOLS_FILE,
            {"web": {"enabled": False, "search_backend": "tavily", "search_count": 3}},
        )
        web = ToolStore(settings.data_dir / TOOLS_FILE).web()
        self.assertFalse(web.enabled)
        self.assertEqual(web.search_backend, "tavily")
        self.assertEqual(web.search_count, 3)

    def test_reset_preview_counts_providers_in_a_bom_file(self) -> None:
        """重置预览也得用同一口径，否则它会把"2 个接口"报成 0 个。"""
        from harness.reset import preview_reset

        settings = test_settings(self.root)
        _write_with_bom(
            settings.data_dir / "providers.json",
            {"providers": [{"id": "a"}, {"id": "b"}]},
        )
        self.assertEqual(preview_reset(settings)["providers"], 2)

    def test_a_plain_file_still_reads_fine(self) -> None:
        """没有 BOM 的老文件不能因此变坏（`utf-8-sig` 对它们就是普通 utf-8）。"""
        path = self.root / i18n.SETTINGS_FILE
        path.write_text(json.dumps({"language": "en"}), encoding="utf-8")
        self.assertEqual(i18n.LanguageStore(path).language(), "en")


if __name__ == "__main__":
    unittest.main()
