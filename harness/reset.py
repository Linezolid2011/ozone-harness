"""恢复出厂设置：抹掉全部运行时数据，让程序回到"首次运行"。

这是整个项目里**唯一一个破坏性且不可逆**的操作，所以设计上刻意保守：

* 只动 `data_dir` 里的东西（对话历史 / 记忆 / 背景档案 / 整理运行记录 / 接口配置）。
  **接口的 key 也在这里**，所以重置能把 key 一起抹掉 —— 而这正是重点：
  本项目不预置任何 key，也没有"主 key"藏在别处当出厂配置，
  所以"抹掉 data/"就等于"回到一台什么都没配过的机器"；
* **它同时把 dream 层格式化并初始化**（ADR-065）：`data/vault/`、`data/dreams/`
  连同记忆库里的整理进度（`dream_cursor`）一起没 —— 下一次 `--dream` 从零开始，
  就像从没整理过。这条现在是**刻意的**（不是副作用）：整理是单向门，
  所以"回到出厂"必须包含"把档案与进度一起清掉"，
  否则会留下一个"档案还在、进度说整理过了"的中间状态。
  注意它**不会替你造一个空 vault 骨架**：出厂状态里本来就没有那个目录
  （`--vault-init` 才建骨架），"看一眼不改磁盘"这条原则对重置同样成立；
* 删除前先**把目录改名挪走**再做删除。直接逐个删的话，
  中途失败会留下一个"删了一半"的数据目录，程序看起来像坏了；
  改名是原子的，新目录立刻可用，删不掉的东西也留在明处（会如实上报）；
* 统计（preview）走**只读**路径：不构造 ProviderStore（那会顺手建文件）、
  以 `mode=ro` 打开 SQLite（那会在库不存在时凭空建一个空库）；
* 动手之前先问一句"这个 data_dir 真的只是个数据目录吗"（见
  `assert_safe_data_dir`）。`data_dir` 来自 `HARNESS_DATA_DIR` 环境变量，
  手滑写成 `.` 就等于指向项目根 —— 那一下会把源码与 `.env` 里的真 key 一起删掉。
  这类事故没有"撤销"，所以宁可直接拒绝执行。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .attachments import attachments_root
from .config import ROOT, Settings, display_path
from .i18n import t


class UnsafeDataDirError(ValueError):
    """data_dir 指向了不该被整目录删掉的位置。

    继承 `ValueError` 是刻意的：调用方（CLI / Web）只需要把这条**配置错误**
    和"路径本身写坏了"（比如 `with_name()` 对盘根抛的 ValueError）当成同一类
    用户错误处理，不必认识两个异常类型。
    """


def _resolved(path: Path) -> Path:
    """尽量给出真实绝对路径；resolve 失败（坏盘符 / 超长路径 / 没有 home）也要能比较。

    `resolve()` 在 Windows 上会碰文件系统，遇到非法路径会抛 OSError，
    少数环境（没有 home 目录）还会抛 RuntimeError。这里退化成 absolute()：
    比较的准确度差一点，但至少不会让"守卫自己"成为崩溃点。
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def data_dir_problem(path: Path | str) -> str | None:
    """这个路径能不能当"可以被抹掉的数据目录"？不行就返回原因（人话）。**纯函数**。

    为什么要专门做这件事：`perform_reset()` 是整个项目里唯一会 rmtree 的地方，
    而它拿到的是 `settings.data_dir`（来自 `HARNESS_DATA_DIR`）。
    一个手滑：

        HARNESS_DATA_DIR=.              → 项目根，源码 + .env 里的真 key 全没
        HARNESS_DATA_DIR=<用户主目录>    → 整个主目录没
        HARNESS_DATA_DIR=<盘根>         → with_name() 直接抛 ValueError，接不住

    这三种都没有"撤销"，所以只能在动手之前挡住。抽成纯函数是为了能单测 ——
    真拿项目根去跑一遍 perform_reset 来验证，本身就是一场事故。
    """
    try:
        candidate = Path(path).expanduser()
    except (TypeError, ValueError) as exc:
        return t("ui.reset.unresolvable", error=exc)
    if not str(candidate).strip():
        return t("ui.reset.empty_path")

    real = _resolved(candidate)

    # 1) 文件系统根（盘根，例如当前盘符的顶层）。它既是"删了就删掉整块盘"，
    #    也恰好是 `with_name()` 会抛 ValueError 的那个输入 —— 在这里挡住，
    #    后面就只剩"正常目录"这一种可能。
    anchor = real.anchor
    if real.parent == real or (anchor and real == Path(anchor)):
        return t("ui.reset.fs_root", path=real)

    # 2) 项目根，以及项目根的任何上级。源码、web/、tests/、.env 都在这一层里。
    #    `HARNESS_DATA_DIR=.` 会解析成项目根，正落在这个分支。
    root = _resolved(Path(ROOT))
    if real == root or root.is_relative_to(real):
        return t("ui.reset.project_dir", path=real)

    # 3) 用户主目录，以及它的任何上级（例如各平台放家目录的那一层）。
    #    **不断言"家目录的子目录"**：临时目录、`~/harness-data` 都在家目录下面，
    #    那些是正常用法 —— 只有"删掉家目录本身"才是灾难。
    try:
        home = _resolved(Path.home())
    except (RuntimeError, OSError):
        home = None
    if home is not None and (real == home or home.is_relative_to(real)):
        return t("ui.reset.home_dir", path=real)

    # 4) 兜底：长得就不像数据目录 —— 里面直接躺着源码目录或 .env。
    #    这一条不依赖 ROOT 判断，所以"项目被整个复制到别处"时同样管用。
    if (real / "harness").is_dir():
        return t("ui.reset.has_source", path=real)
    if (real / ".env").is_file():
        return t("ui.reset.has_env", path=real)

    # 5) 存在但不是目录（手滑填到一个文件上）：改名/删除都没有意义。
    if real.exists() and not real.is_dir():
        return t("ui.reset.not_a_dir", path=real)

    return None


def assert_safe_data_dir(path: Path | str) -> None:
    """不合法就抛 `UnsafeDataDirError`。**必须在任何破坏性动作之前调用**。"""
    problem = data_dir_problem(path)
    if problem:
        raise UnsafeDataDirError(problem)


def _tree_stats(path: Path) -> tuple[int, int]:
    """(文件数, 总字节)。"""
    if not path.is_dir():
        return 0, 0
    files = 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            files += 1
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return files, total


def _memory_counts(db_path: Path) -> dict[str, int]:
    """只读地数一下记忆库。库不存在就返回全 0，**绝不创建它**。"""
    empty = {"memories": 0, "deleted": 0, "ops": 0}
    if not db_path.is_file():
        return empty
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        with connection:
            counts = dict(empty)
            try:
                counts["memories"] = connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE status = 'active'"
                ).fetchone()[0]
                counts["deleted"] = connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE status != 'active'"
                ).fetchone()[0]
            except sqlite3.Error:
                pass
            try:
                counts["ops"] = connection.execute(
                    "SELECT COUNT(*) FROM memory_ops"
                ).fetchone()[0]
            except sqlite3.Error:
                pass
            return counts
    finally:
        connection.close()


def _dream_run_count(dreams_dir: Path) -> int:
    """整理运行过几次。记录是 `data/dreams/dream-*.json`（不再进数据库）。"""
    if not dreams_dir.is_dir():
        return 0
    return sum(1 for path in dreams_dir.glob("dream-*.json") if path.is_file())


def _cursor_count(db_path: Path) -> int:
    """**只读**地数一下整理进度有几条。库不存在/表不存在都返回 0，绝不创建。

    不能图省事用 `open_memory()`：那会 `CREATE TABLE IF NOT EXISTS`，
    于是"打开设置页看一眼"就把记忆库建出来了 —— 预览必须只读
    （这条有测试钉着，第一版就是被它抓出来的）。
    """
    if not db_path.is_file():
        return 0
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM dream_cursor").fetchone()[0]
        )
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def prompt_override_count(settings: Settings) -> int:
    """有多少条提示词被用户改过（两门语言加起来）。**只读**。

    它也算"重置会失去的东西"：`data/prompts.json` 就在 `data/` 里，
    恢复出厂设置把它一起抹掉 —— 这正是"重置能复原提示词（中英都复原）"的
    实现方式（不需要另写一份复原代码）。抽出来是为了让预览能如实报出这个数字。
    """
    from . import prompts

    path = Path(settings.data_dir) / prompts.OVERRIDES_FILE
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(raw, dict):
        return 0
    total = 0
    for language in prompts.LANGUAGES:
        section = raw.get(language)
        if isinstance(section, dict):
            total += len(section)
    return total


def dream_state(settings: Settings) -> dict[str, Any]:
    """dream 层现在有什么：档案数、运行次数、整理进度条数。**全程只读**。

    **重置会把它们全部清掉**（ADR-065：格式化并初始化）。抽成一个函数是为了：
    界面上能如实报出"要失去什么"，测试里也能直接断言"重置后它确实空了"。
    """
    from .vault import VaultStore

    return {
        "vault_files": VaultStore(settings.vault_dir).count_files(),
        "dream_runs": _dream_run_count(settings.dreams_dir),
        "cursor_sessions": _cursor_count(settings.memory_db_path),
    }


def _provider_count(path: Path) -> int:
    """有多少个已配置的接口。**不构造 ProviderStore**（那会顺手把文件种出来）。"""
    if not path.is_file():
        return 0
    try:
        # `utf-8-sig`：与 ProviderStore 同口径（那份文件可能带 BOM），
        # 否则重置预览会把"有 2 个接口"报成 0 个。
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return 0
    providers = data.get("providers")
    return len(providers) if isinstance(providers, list) else 0


def preview_reset(settings: Settings) -> dict[str, Any]:
    """重置会删掉什么。前端拿它显示具体数字，让"要删多少"看得见。"""
    sessions, _ = _tree_stats(settings.sessions_dir)
    # 背景档案（dream vault）也在 data_dir 里，所以重置会把它们**格式化**
    # （ADR-065）。**必须如实报出来**：这是"不偷偷删东西"的一部分。
    #
    # 口径与 `VaultStore.check()["files"]` / `--vault` 一致：**README 不算档案**
    # （它是索引）。早先这里数是"vault 目录下所有文件"，于是同一个东西有两个数字
    # —— `/api/reset` 说 2 个文件、`/api/vault` 说 1 份档案，迟早有人读错。
    state = dream_state(settings)
    counts = _memory_counts(settings.memory_db_path)
    # 占用空间直接量整个数据目录：以前是"把几项加起来"，每加一种新数据
    # （先是 vault、后来是运行记录）就有一次忘记加的机会。量目录不会漏。
    _, total_bytes = _tree_stats(settings.data_dir)
    # 图片附件：整目录 rmtree 天然包含 `data/attachments/`，但预览原先**没数它** ——
    # 于是会出现"预览说 0 个文件、其实删掉了 20 张图"。这里如实报数量与占用。
    # 口径固定成 attachments 层认定的那个目录（`attachments_root`），
    # 免得两处各算一次、给出两个数字（同一个东西两个数字已经栽过一次）。
    attachment_files, attachment_bytes = _tree_stats(attachments_root(settings))
    return {
        # 给界面显示用相对形式（在项目内时），别把某台机器的盘符烙进截图里
        "data_dir": display_path(settings.data_dir),
        "sessions": sessions,
        "vault_files": state["vault_files"],
        "providers": _provider_count(settings.data_dir / "providers.json"),
        "memories": counts["memories"],
        "deleted_memories": counts["deleted"],
        "memory_ops": counts["ops"],
        # dream 层的两个数字（ADR-065：重置会把它们格式化）：档案数 + 运行次数。
        # `cursor_sessions` 不放进界面（它是进度，用户看不到也不关心），
        # 但测试会用它确认"整理进度确实被清空了"。
        "dream_runs": state["dream_runs"],
        "cursor_sessions": state["cursor_sessions"],
        # 图片附件（`bytes` 已经含它们的占用，这两项是"其中有多少是图"）
        "attachment_files": attachment_files,
        "attachment_bytes": attachment_bytes,
        # 用户改过的提示词条数（两门语言合计）。重置会连它一起抹掉 ——
        # 这就是"恢复出厂设置能复原提示词（中英都复原）"。要如实报出来。
        "prompt_overrides": prompt_override_count(settings),
        "bytes": total_bytes,
    }


def _remove_tree(path: Path) -> bool:
    """尽力删除；返回是否**完全**删掉了。"""
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def perform_reset(settings: Settings) -> dict[str, Any]:
    """真正执行。返回删掉了什么、以及有没有删不掉的残留。

    **第一件事永远是 `assert_safe_data_dir()`**：先把"这个路径是不是真的
    只是个数据目录"问清楚。不合法就抛 `UnsafeDataDirError`，此时磁盘上
    一个字节都没动过。
    """
    data_dir = Path(settings.data_dir)
    assert_safe_data_dir(data_dir)

    before = preview_reset(settings)
    leftover: str | None = None

    if data_dir.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            staging = data_dir.with_name(f"{data_dir.name}.deleting-{stamp}")
        except ValueError as exc:
            # `with_name()` 对"没有名字的路径"（盘根、UNC 共享根）会抛 ValueError。
            # 上面的守卫本应已经拦住这些输入，这里再兜一次：宁可这次不重置，
            # 也不能让异常带着**尚未清空**的目录状态溜出去。
            raise UnsafeDataDirError(
                t("ui.reset.no_dir_name", path=data_dir)
            ) from exc
        try:
            data_dir.rename(staging)
        except OSError:
            # 改名失败（例如有句柄占着）就退回直接删，至少别把流程卡死。
            # 注意：能走到这里，说明 data_dir 已经通过了上面的安全校验 ——
            # "有句柄占着"正是"它确实是个正在用的数据目录"的旁证。
            if not _remove_tree(data_dir):
                raise
            staging = None
        data_dir.mkdir(parents=True, exist_ok=True)
        if staging is not None and not _remove_tree(staging):
            leftover = str(staging)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)

    return {"deleted": before, "data_dir": display_path(data_dir), "leftover": leftover}
