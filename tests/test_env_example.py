"""`.env.example` 与 `config.py` 的一致性守卫。

## 为什么要这条

这份文件是"用户唯一会照着改的东西"，而它已经漂过一次：里面有 3 个**早就删掉**的
dream 旋钮（`DREAM_MAX_MEMORIES` / `MAX_INSIGHTS` / `SIMILARITY_THRESHOLD`，
ADR-053/059 之后不再被读取），同时**漏掉**了 9 个正在生效的（整段 vault、
整理读记录的两项、以及新加的 `PROMPTS_MAX_CHARS`）。

漂移的后果与"过时的自我否定"是同一类：**用户以为有个旋钮，改了却没有任何效果**，
而这比"没有这个旋钮"更难查 —— 他会怀疑自己的配置没生效、怀疑程序读错了文件。

判据只用**实际读取点**（`env, "HARNESS_X"` 那种调用）与**实际赋值行**
（`.env.example` 里顶格的 `HARNESS_X=`），所以注释里提到旧名字不算数
（这份文件刻意保留了一段"已删除"的说明）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from harness.config import ROOT

# `config.py` 里真正被读取的键：`_int(env, "HARNESS_X", ...)` / `_int_range(env, "HARNESS_X"...)`
_READ = re.compile(r'env,\s*"(HARNESS_[A-Z0-9_]+)"')
# `.env.example` 里**生效**的赋值行（顶格、不被 # 注释掉）
_ASSIGNED = re.compile(r"^(HARNESS_[A-Z0-9_]+)=", re.MULTILINE)
# 提到的都算"文档化了"：包括被注释掉的示例（`# HARNESS_LANGUAGE=zh`）——
# 那两个是"用户按需取消注释"的写法，不是漏写。
_MENTIONED = re.compile(r"^#?\s*(HARNESS_[A-Z0-9_]+)=", re.MULTILINE)


class EnvExampleTests(unittest.TestCase):
    def test_every_read_key_is_documented_and_vice_versa(self) -> None:
        config = (ROOT / "harness" / "config.py").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        read = set(_READ.findall(config))
        documented = set(_ASSIGNED.findall(example))
        mentioned = set(_MENTIONED.findall(example))

        missing = sorted(read - mentioned)
        dead = sorted(documented - read)
        self.assertEqual(
            missing,
            [],
            "这些旋钮在 config.py 里会被读取，却在 .env.example 里一个字都没提：\n  "
            + "\n  ".join(missing),
        )
        self.assertEqual(
            dead,
            [],
            "这些旋钮写在 .env.example 里（且是生效的赋值行），但 config.py 根本不读它 —— "
            "用户改了不会有任何效果（要留就挪到「已删除」那段注释里）：\n  "
            + "\n  ".join(dead),
        )

    def test_the_scan_itself_finds_something(self) -> None:
        """"两边都空"也是一种通过 —— 所以先确认扫描确实扫到了东西。"""
        config = (ROOT / "harness" / "config.py").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertGreater(len(_READ.findall(config)), 20)
        self.assertGreater(len(_ASSIGNED.findall(example)), 20)
        self.assertIn("HARNESS_PROMPTS_MAX_CHARS", _ASSIGNED.findall(example))


if __name__ == "__main__":
    unittest.main()
