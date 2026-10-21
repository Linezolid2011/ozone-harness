"""测试包。全部离线运行，不访问网络、不消耗 API 额度。

## 顺手把临时目录钉在一个确定的地方

原因是一个真实的环境怪癖：这台机器上 `TMPDIR` / `TEMP` / `TMP` 三个变量
**都指向存在的目录，但都写不进去**（受限环境下 `PermissionError`）——
于是 `tempfile.gettempdir()` 一路 fallback 到候选表的最后一项 `os.getcwd()`，
也就是**项目根**。后果是测试的临时目录散落在仓库里。

（附带一个观察：临时目录清理失败时会留下来 —— 例如某个用例报错后
sqlite 句柄没关，`TemporaryDirectory` 就删不掉它，于是仓库里多出一个
装着泄漏 `memory.db` 的目录。这不只是"脏"，它会掩盖"到底哪个测试没清理干净"。）

虽然 `.gitignore` 里有 `tmp*/`、而且大部分情况下会自己清理，
但"测试的临时文件出现在仓库里"本身就不该发生：
`data/` 正是恢复出厂设置会**整目录删掉**的地方，而"跑测试"绝不该碰它。

所以这里在**任何测试模块被导入之前**先把 `tempfile.tempdir` 定下来：
优先用系统临时目录（**真的写一个文件探测**，不是看环境变量在不在），
实在不行才退到项目里的 `.tmp-tests/`（已 gitignore）。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _writable(path: Path) -> bool:
    """真的写一个文件再删掉 —— 只看环境变量会被"指向不存在目录"骗过去。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{uuid.uuid4().hex[:6]}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _pick_temp_root() -> Path:
    """挑一个**真能写**的地方放测试的临时目录。

    只看环境变量会被"指向不存在/不可写的目录"骗过去，所以每个候选都真写一个文件试试。

    **刻意不写死"系统临时目录"兜底**（原来有一个 `C:` 盘下的 Windows 临时目录）：
    它不只是"依赖某台机器"，在 Linux/macOS 上那是个**合法文件名** ——
    `mkdir(parents=True)` 会真的在当前目录下建出一个叫这个名字的目录，然后拿它当临时根。
    而 Windows 上 `%TEMP%`/`%TMP%` 正常都存在；真都没有时，项目内的 `.tmp-tests/`
    是更可靠的兜底（它已经被 .gitignore 忽略）。

    （这段注释也刻意不写出那个路径的字面形式 —— `tests/test_paths.py` 的守卫
    按行扫绝对路径，评论里的字面量一样会被拦下来。）
    """
    for candidate in (os.environ.get("TEMP"), os.environ.get("TMP")):
        if candidate and _writable(Path(candidate)):
            return Path(candidate)
    fallback = _PROJECT_ROOT / ".tmp-tests"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


tempfile.tempdir = str(_pick_temp_root())
