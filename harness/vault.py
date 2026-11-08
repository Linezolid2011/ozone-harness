"""Dream vault：模型可以"自己走进去找"的那一层背景档案（M7）。

## 它在两个记忆系统里的位置

    memory instructions        用户明说 → 每条每轮都注入 → 只由对话模型写 → 可信度最高
    dream vault（本模块）       后台自动巩固 → 只有 README 每轮注入，
                              细节靠模型用工具进来读 → 可信度次之

两套东西即使同在 `data/vault/` 也**必须分所有权**：整理者的写权限不能伸进
"用户明确要求记住"的 `memory_instruction.md`，那等于让记录者随时能重写历史。
独立记忆由 MemoryStore 写，Dream 档案由 VaultStore 写，互不改写。

## README 是投影，原始记录才是真相来源

vault 里的东西全是从会话记录（append-only JSONL）压缩出来的**派生物**。
所以 vault 整个删掉是可以重建的 —— 这与 ADR-006 是同一个哲学：
**投影可以丢，真相来源不能丢。** 这也正是敢让模型自由写 vault 的底气。

## 写入口只有一个，而且校验在 store 层

对话 agent 的读取在 `tools/vault_tools.py`，ADR-111 的受约束编辑在
`tools/vault_edit_tools.py`；dream agent 走 `tools/dream_tools.py`。三者最终都必须走
`VaultStore` 的写入口 —— **路径与权限校验在 store 层实现，不在工具层**，
所以新增调用方不可能只靠工具描述绕过它。

## 一条贯穿全模块的规则：路径必须自己归一化，并且 fail-closed

与 ADR-052 同源：**不要指望平台替你检查。** 这里是模型第一次能写"文件路径"
（原生 memory_instruction.md 则由 MemoryStore 单独校验与写入），
而我们在 Windows 上，于是多出一整类平台特有的坑：

* `NUL` / `CON` / `COM1` 这些**设备名**：`open()` 打开的不是文件而是设备，
  写进去的内容**静默消失**，读出来是空的。这不是理论问题，是 Windows 的既有行为。
* 同一个目录里 `小林.md` 与 `小林 .md`、`Foo.md` 与 `foo.md` 在大小写不敏感的
  文件系统上是**同一个文件**，于是"两个实体"会在磁盘上互相覆盖。
* 文件名里的 `: * ? " < > |`、结尾的点和空格：Windows 直接非法或静默改写。
* `../`、绝对路径、盘符、UNC、以及**指向 vault 外面的符号链接**。

处理办法和 ADR-052 一样：**只接受我们自己完全理解的规范形式，其余一律拒绝**，
并且拒绝时给出可操作的说明（模型看得懂，能自己改对），而不是猜它想说什么。
"""
from __future__ import annotations

import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .i18n import t
from .llm_types import estimate_tokens
from .textsim import bigrams, dice

README_NAME = "README.md"
SECTION_DIR = "sections"
ENTITY_DIR = "entities"
# 原生 Kimi vault 的根级文件（A/B 对齐，ADR-104）。
INDEX_NAME = "index.md"
LOG_NAME = "log.md"
OBSERVATIONS_NAME = "observations.md"
MEMORY_INSTRUCTION_NAME = "memory_instruction.md"
SECTIONS_CONFIG = "sections.yaml"
RAW_DIR = "raw"
ARCHIVE_DIR = "archived_month"
ARCHIVE_FILE = "this_month.md"

# 根级**可读**的 md。`README/index/log/observations/memory_instruction` 都是
# 原生导出里会出现的文件：前四个在 UI 里可看（README/index 是生成物，
# log/observations 由 dream 侧维护）；`memory_instruction` 是 Kimi 的旁路导出，
# 只读、不注入。写入权限由上层（工具/UI）的 generated/reserved 守卫决定。
ROOT_MD_FILES: tuple[str, ...] = (
    README_NAME,
    INDEX_NAME,
    LOG_NAME,
    OBSERVATIONS_NAME,
    MEMORY_INSTRUCTION_NAME,
)
# 生成物：模型与网页界面都**不能直接写**，只能由 harness 重建。
GENERATED_RELS: frozenset[str] = frozenset(
    {
        README_NAME,
        INDEX_NAME,
        "sections/earlier_months.md",
    }
)

# 会话编辑工具的固定禁区（ADR-111）。与 dream 整理器分开：会话可以维护
# observations 与旧 vault 的 README，但永远不能直接改清单、索引、整理日志或独立记忆。
# raw/ 与月份归档由 _assert_editable / _assert_session_directory 排除。
SESSION_READONLY_RELS: frozenset[str] = frozenset(
    {INDEX_NAME, LOG_NAME, MEMORY_INSTRUCTION_NAME, SECTIONS_CONFIG}
)


def _path_key(path: str) -> str:
    """路径保护的比较键：大小写不敏感文件系统上必须按实际等价关系比较。"""
    return path.casefold() if _CASE_INSENSITIVE_FS else path


def _is_link_or_reparse(path: Path) -> bool:
    """是否是不能安全当作普通文件/目录遍历的节点。

    Windows junction 的 `os.path.islink()` 是 False，但递归遍历会真的走进目标目录。
    因此同时识别 pathlib junction 与 Win32 reparse point；无法 lstat 时 fail-closed。
    """
    if os.path.islink(path):
        return True
    if os.name != "nt":
        return False
    try:
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and getattr(info, "st_file_attributes", 0) & reparse)

# 原生 section 的固定清单（顺序 = README 注入顺序，ADR-104）。
# 标题保持原生英文：这些是**内容里的结构标题**，不是界面文案；中英界面下都一样，
# 免得同一份档案在切语言时被改名。
DEFAULT_SECTION_SPECS: tuple[tuple[str, str], ...] = (
    ("sections/personal_context.md", "Personal Context"),
    ("sections/work_context.md", "Work Context"),
    ("sections/this_month.md", "This Month"),
    ("sections/earlier_months.md", "Earlier Months"),
    ("sections/taste.md", "Taste & Preferences"),
    ("sections/memory_tips.md", "Memory Tips"),
)
DEFAULT_INJECT_CHAR_CAP = 6000
# README/index 的正文按“字符”计（cap）而 `chars` 元数据按 **UTF-8 字节** 记 ——
# 原生导出实测 `chars: 9356` 对正文 4376 字符 / 9358 字节，见 ADR-104。

# raw/ 只读素材：`raw/source_<ref>/<file>`，只允许图片与同名 `.meta.json`。
RAW_SOURCE_RE = re.compile(r"^source_[0-9]+$")
RAW_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "webp", "gif"})
RAW_META_SUFFIX = ".meta.json"


# 程序自己写的占位 README 带的标记。**由程序写、由程序认** ——
# 不去正文里找"还没有整理过任何对话"这句话（那是靠字符串匹配，改一个字就失效）。
PLACEHOLDER_STATUS = "placeholder"

# 大小写不敏感的文件系统（Windows 与默认配置的 macOS）。
# Linux 上 `Foo.md` 与 `foo.md` 可以合法并存，所以那边的撞名检查要关掉，否则是误伤。
_CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"

# 原生/整理器的两个 canonical 顶层目录（小写）。会话另可用自定义目录，
# 但这两个名字仍有固定 section、归档与禁止整体删除等特殊规则。
_TOP_DIRS = (SECTION_DIR, ENTITY_DIR)

# Windows 保留设备名。**判断的是第一个点之前的那一段**：
# `NUL.md` 与 `NUL` 等价，都是设备。
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)

_ILLEGAL_CHARS = frozenset('<>:"|?*')  # 斜杠在切分阶段就处理掉了

_WIKILINK = re.compile(r"(?<!!)\[\[([^\[\]]{1,160})\]\]")

_FRONTMATTER_FENCE = "---"


class VaultPathError(ValueError):
    """路径不是 vault 里的合法位置。

    继承 `ValueError` 是刻意的（与 `UnsafeDataDirError` 同样理由）：
    调用方只要把"模型给错了路径"当成一类普通的参数错误处理，
    不必认识一个新异常类型。
    """


class VaultExistsError(VaultPathError):
    """要**新建**的档案已经存在了。

    单独一个类型，因为它的处置与"路径不合法"不同：路径不合法是**输入**问题
    （改个名字再来），而"已经有了"是**状态**冲突 —— 网页界面该据此提示
    "打开它看看"，接口该回 **409** 而不是 400（ADR-070）。

    注意它**不覆盖**"只差大小写"那种撞名：那一种仍然走 `VaultPathError`，
    因为该说的是"这跟已有的某份档案是同一个文件"（信息量更大）。
    """


# --------------------------------------------------------------------------
# 路径归一化（纯函数，可离线单测）
# --------------------------------------------------------------------------


def normalize_rel_path(
    raw: str, *, name_max_chars: int = 40, allow_nested: bool = False
) -> str:
    """把模型给的一个路径字符串，规范化成 vault 内的相对路径；不合法就抛异常。

    默认仍只接受原生/旧 vault 的规范形式（ADR-104）。会话编辑工具传入
    `allow_nested=True` 时，额外接受根级与自定义目录下的非保护 Markdown，以及
    sections/entities 的嵌套 Markdown；这是**会话工具的显式能力扩展**，
    不会改变整理器与导入器的既有路径合同。

        README.md / index.md / log.md / observations.md / memory_instruction.md
        sections.yaml
        sections/<名字>.md（会话模式允许中间目录）
        sections/archived_month/<YYYY-MM>/this_month.md
        entities/<名字>.md（会话模式允许中间目录）

    图片素材走 `normalize_raw_path()`（`raw/source_<ref>/...`），不在这里。
    这个函数**不碰文件系统**，所以它的每一条规则都能被离线测试逐条钉住。
    """
    if not isinstance(raw, str):
        raise VaultPathError(t("ui.vault.path_not_string", type=type(raw).__name__))
    text = raw.strip().lstrip("\ufeff")
    if not text:
        raise VaultPathError(t("ui.vault.path_empty"))
    if "\x00" in text:
        raise VaultPathError(t("ui.vault.path_nul"))

    # Windows 风格的写法归一化成同一种分隔符，再统一校验。
    # 这一步是"友好"而不是"放水"：`\` 与 `/` 在这里都只是分隔符，
    # 归一化之后仍然要过下面每一条规则。
    text = text.replace("\\", "/")
    # `./name.md` 是显式根级写法；归一化时去掉前缀，权限永远只看 canonical，
    # 因此 `./memory_instruction.md` 不会变成绕过所有权的另一条路径。
    while text.startswith("./"):
        text = text[2:]

    if text.startswith("/"):
        raise VaultPathError(t("ui.vault.path_absolute", raw=repr(raw)))
    if re.match(r"^[A-Za-z]:", text):
        raise VaultPathError(t("ui.vault.path_drive", raw=repr(raw)))
    if text.startswith("~"):
        raise VaultPathError(t("ui.vault.path_tilde", raw=repr(raw)))

    parts = text.split("/")
    if any(part == "" for part in parts):
        raise VaultPathError(t("ui.vault.path_empty_segment", raw=repr(raw)))

    if len(parts) == 1:
        # 根级文件是**固定白名单**：README 折叠大小写；其余原生文件也折叠成
        # 规范写法（Windows 上 `README.md` / `readme.md` 是同一个文件）。
        lowered = parts[0].lower()
        if lowered == README_NAME.lower():
            return README_NAME
        for known in (*ROOT_MD_FILES, SECTIONS_CONFIG):
            if lowered == known.lower():
                return known
        if allow_nested:
            # 会话模式允许根级自定义 Markdown；固定的原生/记忆文件上面已归一化，
            # 权限清单会在 store 层再次拦截，而不是靠这里放行即拥有写权限。
            _check_name(parts[0], name_max_chars=name_max_chars, raw=raw)
            return parts[0]
        raise VaultPathError(
            t(
                "ui.vault.path_unknown_location",
                section_dir=SECTION_DIR,
                entity_dir=ENTITY_DIR,
                raw=repr(raw),
            )
        )

    directory = parts[0]
    if directory != directory.lower():
        raise VaultPathError(
            t(
                "ui.vault.path_bad_top",
                top_dirs=" / ".join(_TOP_DIRS),
                dir=repr(directory),
            )
        )

    if allow_nested and directory not in _TOP_DIRS:
        # 第一段也必须检查：`./memory_instruction.md` 这类别名若在归一化时
        # 留下 `./`，后续按 canonical 字符串做的保护清单就会被绕过。
        _check_dir_name(directory, name_max_chars=name_max_chars, raw=raw)
        for segment in parts[1:-1]:
            _check_dir_name(segment, name_max_chars=name_max_chars, raw=raw)
        _check_name(parts[-1], name_max_chars=name_max_chars, raw=raw)
        return "/".join(parts)

    if allow_nested and directory in _TOP_DIRS:
        # 先保留原生月份归档的精确校验；其它嵌套路径逐段检查，不能只查文件名。
        # 否则 `entities/NUL/x.md` 会在 Windows 上穿过一个设备名目录。
        if directory == SECTION_DIR and len(parts) > 2 and parts[1] == ARCHIVE_DIR:
            if (
                len(parts) == 4
                and _ARCHIVE_MONTH_RE.match(parts[2])
                and parts[3] == ARCHIVE_FILE
            ):
                return f"{SECTION_DIR}/{ARCHIVE_DIR}/{parts[2]}/{ARCHIVE_FILE}"
            raise VaultPathError(
                t(
                    "ui.vault.path_bad_archive",
                    raw=repr(raw),
                    archive_dir=ARCHIVE_DIR,
                    file=ARCHIVE_FILE,
                )
            )
        for segment in parts[1:-1]:
            _check_dir_name(segment, name_max_chars=name_max_chars, raw=raw)
        _check_name(parts[-1], name_max_chars=name_max_chars, raw=raw)
        return "/".join(parts)

    if directory == SECTION_DIR:
        if len(parts) == 2:
            _check_name(parts[1], name_max_chars=name_max_chars, raw=raw)
            return f"{SECTION_DIR}/{parts[1]}"
        # 只有一个特例：月份归档 `sections/archived_month/YYYY-MM/this_month.md`。
        if (
            len(parts) == 4
            and parts[1] == ARCHIVE_DIR
            and _ARCHIVE_MONTH_RE.match(parts[2])
            and parts[3] == ARCHIVE_FILE
        ):
            return f"{SECTION_DIR}/{ARCHIVE_DIR}/{parts[2]}/{ARCHIVE_FILE}"
        raise VaultPathError(
            t(
                "ui.vault.path_bad_archive",
                raw=repr(raw),
                archive_dir=ARCHIVE_DIR,
                file=ARCHIVE_FILE,
            )
        )

    if directory == ENTITY_DIR:
        if len(parts) != 2:
            raise VaultPathError(
                t(
                    "ui.vault.path_too_deep",
                    top_dirs="/".join(_TOP_DIRS),
                    raw=repr(raw),
                )
            )
        _check_name(parts[1], name_max_chars=name_max_chars, raw=raw)
        return f"{ENTITY_DIR}/{parts[1]}"

    raise VaultPathError(
        t(
            "ui.vault.path_bad_top",
            top_dirs=" / ".join(_TOP_DIRS),
            dir=repr(directory),
        )
    )


_ARCHIVE_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def normalize_raw_path(raw: str, *, name_max_chars: int = 120) -> str:
    """把 `raw/source_<ref>/<文件名>` 归一化；只允许图片与同名 `.meta.json`。

    raw 是**只读素材**，不是模型能写的档案：它只由导入 / 显式的图片工具写入。
    所以这里单独一套规则，不把 `.md` 的白名单混进来。
    """
    if not isinstance(raw, str):
        raise VaultPathError(t("ui.vault.path_not_string", type=type(raw).__name__))
    text = raw.strip().lstrip("\ufeff")
    if not text:
        raise VaultPathError(t("ui.vault.path_empty"))
    if "\x00" in text:
        raise VaultPathError(t("ui.vault.path_nul"))
    text = text.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("~"):
        raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    parts = text.split("/")
    if len(parts) != 3 or any(part == "" for part in parts):
        raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    if parts[0] != RAW_DIR or not RAW_SOURCE_RE.match(parts[1]):
        raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    _check_raw_name(parts[2], name_max_chars=name_max_chars, raw=raw)
    return f"{RAW_DIR}/{parts[1]}/{parts[2]}"


def _check_raw_name(name: str, *, name_max_chars: int, raw: str) -> None:
    if name in (".", "..") or name != name.strip():
        raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    if any(ch in _ILLEGAL_CHARS or ord(ch) < 32 for ch in name):
        raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    if name.endswith("."):
        raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    lowered = name.lower()
    if lowered.endswith(RAW_META_SUFFIX):
        image_name = name[: -len(RAW_META_SUFFIX)]
        if not image_name or image_name.split(".")[0].rstrip(" .").lower() in _RESERVED_NAMES:
            raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
        ext = image_name.rsplit(".", 1)[-1].lower() if "." in image_name else ""
        if ext not in RAW_IMAGE_EXTS:
            raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    else:
        ext = lowered.rsplit(".", 1)[-1] if "." in lowered else ""
        if ext not in RAW_IMAGE_EXTS:
            raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
        if name.split(".")[0].rstrip(" .").lower() in _RESERVED_NAMES:
            raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))
    if len(name) > name_max_chars:
        raise VaultPathError(t("ui.vault.raw_bad_path", raw=repr(raw)))



def _check_name(name: str, *, name_max_chars: int, raw: str) -> None:
    """单个文件名的规则。名字是**没有目录成分**的一段。"""
    if name in (".", ".."):
        raise VaultPathError(t("ui.vault.name_dot", name=repr(name)))
    if name != name.strip():
        raise VaultPathError(t("ui.vault.name_whitespace", name=repr(name)))
    if any(ch in _ILLEGAL_CHARS or ord(ch) < 32 for ch in name):
        raise VaultPathError(t("ui.vault.name_illegal_chars", name=repr(name)))
    if name.endswith("."):
        raise VaultPathError(t("ui.vault.name_dot_suffix", name=repr(name)))
    if not name.endswith(".md") or name == ".md":
        raise VaultPathError(t("ui.vault.name_needs_md", name=repr(name)))

    stem = name.split(".")[0]
    # **先剥掉尾部的空格和点再比对**：Windows 忽略 `NUL .md` 里点号前的空格，
    # 把它同样解析成设备 —— 只比对 `split(".")[0]` 会让这几种写法漏过去，
    # 落地时才抛一个模型看不懂的 FileExistsError。
    stripped = stem.rstrip(" .")
    if stripped.lower() in _RESERVED_NAMES:
        # 这条不是洁癖：`open("entities/NUL.md", "w")` 在 Windows 上打开的是
        # 空设备，写入被丢弃、读回是空的 —— 看起来"写成功了"，其实什么都没发生。
        raise VaultPathError(t("ui.vault.name_reserved", stem=repr(stem)))
    if stripped != stem:
        # `x .md` 与 `x.md` 在 NTFS 上**是两个文件**（不像设备名那样被改写），
        # 但它们在人眼里一模一样 —— 同一个实体会被写成两份，孤儿检查也会报两次。
        # 与"文件名首尾不能有空白"同一条理由：看得出是同一个就别让它们并存。
        raise VaultPathError(
            t("ui.vault.name_trailing_dot", stem=stripped, name=repr(name))
        )
    if len(name) > name_max_chars:
        raise VaultPathError(
            t(
                "ui.vault.name_too_long",
                length=len(name),
                limit=name_max_chars,
                name=repr(name),
            )
        )


def _check_dir_name(name: str, *, name_max_chars: int, raw: str) -> None:
    """会话可建目录的单段名称。与文件名共用 Windows 的 fail-closed 规则。"""
    if name in (".", ".."):
        raise VaultPathError(t("ui.vault.name_dot", name=repr(name)))
    if name != name.strip():
        raise VaultPathError(t("ui.vault.name_whitespace", name=repr(name)))
    if any(ch in _ILLEGAL_CHARS or ord(ch) < 32 for ch in name):
        raise VaultPathError(t("ui.vault.name_illegal_chars", name=repr(name)))
    if name.endswith("."):
        raise VaultPathError(t("ui.vault.name_dot_suffix", name=repr(name)))
    if name.lower().endswith(".md"):
        # 目录也叫 `x.md` 会让删除入口分不清"文件还是目录"，没有实际收益。
        raise VaultPathError(t("ui.vault.dir_markdown_suffix", path=repr(raw)))
    if name.split(".")[0].rstrip(" .").lower() in _RESERVED_NAMES:
        raise VaultPathError(
            t("ui.vault.name_reserved", stem=repr(name.split(".")[0]))
        )
    if len(name) > name_max_chars:
        raise VaultPathError(
            t(
                "ui.vault.name_too_long",
                length=len(name),
                limit=name_max_chars,
                name=repr(name),
            )
        )


def normalize_session_dir_path(raw: str, *, name_max_chars: int = 40) -> str:
    """会话工具可操作的 vault 内目录，包括自定义顶层目录与嵌套目录。

    这是独立于 Markdown 文件白名单的入口：目录没有扩展名，且允许一个末尾 `/`。
    raw/、月份归档与顶层 sections/entities 的删除保护由 store 层执行。
    """
    if not isinstance(raw, str):
        raise VaultPathError(t("ui.vault.path_not_string", type=type(raw).__name__))
    text = raw.strip().lstrip("\ufeff")
    if not text:
        raise VaultPathError(t("ui.vault.path_empty"))
    if "\x00" in text:
        raise VaultPathError(t("ui.vault.path_nul"))
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.endswith("/"):
        text = text[:-1]
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("~"):
        raise VaultPathError(t("ui.vault.path_absolute", raw=repr(raw)))
    parts = text.split("/")
    if any(part == "" for part in parts):
        raise VaultPathError(t("ui.vault.path_empty_segment", raw=repr(raw)))
    if parts[0] != parts[0].lower():
        raise VaultPathError(
            t("ui.vault.path_bad_top", top_dirs=" / ".join(_TOP_DIRS), dir=repr(parts[0]))
        )
    if len(parts) > 8:
        # 目录深度也要有界：这是模型可调用的写工具，不是通用文件系统。
        raise VaultPathError(t("ui.vault.dir_too_deep", path=repr(raw)))
    segments = parts if parts[0] not in _TOP_DIRS else parts[1:]
    for segment in segments:
        _check_dir_name(segment, name_max_chars=name_max_chars, raw=raw)
    return "/".join(parts)


def normalize_name(raw: str, *, name_max_chars: int = 40) -> str:
    """把实体名规范化（NFC），用于**比较**（反链、撞名检查）。

    不做大小写折叠：Windows 上大小写不敏感，但 Linux 上敏感，
    所以撞名检查单独做（见 `find_name_collisions`），而不是在这里悄悄折叠。
    """
    return unicodedata.normalize("NFC", raw.strip())


# --------------------------------------------------------------------------
# sections.yaml：原生 vault 的 section 清单（ADR-104）
#
# 原生格式刻意很小，不需要 PyYAML：
#
#     inject_char_cap: 6000
#     sections:
#       - path: sections/personal_context.md
#         title: Personal Context
#
# 未知字段/坏行 fail-soft（数据质量问题），但**不会**静默丢 section：
# 无法解析的路径会变成一条警告并由 `check()` 报出来。
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionSpec:
    path: str
    title: str


@dataclass(frozen=True)
class SectionsManifest:
    inject_char_cap: int = DEFAULT_INJECT_CHAR_CAP
    sections: tuple[SectionSpec, ...] = ()

    def paths(self) -> tuple[str, ...]:
        return tuple(spec.path for spec in self.sections)

    def title_for(self, path: str) -> str:
        for spec in self.sections:
            if spec.path == path:
                return spec.title
        return Path(path).stem


def default_manifest() -> SectionsManifest:
    return SectionsManifest(
        inject_char_cap=DEFAULT_INJECT_CHAR_CAP,
        sections=tuple(SectionSpec(path, title) for path, title in DEFAULT_SECTION_SPECS),
    )


def parse_sections_manifest(text: str) -> tuple[SectionsManifest, list[str]]:
    """解析原生 `sections.yaml`。坏行降级成警告（同 frontmatter 的 fail-soft）。"""
    warnings: list[str] = []
    cap = DEFAULT_INJECT_CHAR_CAP
    raw_specs: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    def _set(spec: dict[str, str], key: str, value: str) -> None:
        key = key.strip()
        if key in ("path", "title"):
            spec[key] = _unquote(value.strip())
        else:
            warnings.append(t("ui.vault.manifest_bad", line=f"{key}: {value}"[:60]))

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "inject_char_cap":
                try:
                    parsed = int(value)
                    cap = max(0, parsed)
                except (TypeError, ValueError):
                    warnings.append(t("ui.vault.manifest_bad", line=stripped[:60]))
            elif key == "sections":
                if value:
                    warnings.append(t("ui.vault.manifest_bad", line=stripped[:60]))
            else:
                warnings.append(t("ui.vault.manifest_bad", line=stripped[:60]))
            current = None
            continue

        if stripped.startswith("- "):
            item = stripped[2:].strip()
            current = {"path": "", "title": ""}
            raw_specs.append(current)
            if ":" in item:
                sub_key, _, sub_value = item.partition(":")
                _set(current, sub_key, sub_value)
            elif item:
                warnings.append(t("ui.vault.manifest_bad", line=stripped[:60]))
            continue

        if ":" in stripped and current is not None:
            sub_key, _, sub_value = stripped.partition(":")
            _set(current, sub_key, sub_value)
            continue

        warnings.append(t("ui.vault.manifest_bad", line=stripped[:60]))

    specs: list[SectionSpec] = []
    for raw_spec in raw_specs:
        raw_path = raw_spec.get("path", "").strip()
        if not raw_path:
            warnings.append(t("ui.vault.manifest_bad", line="(missing path)"))
            continue
        try:
            canonical = normalize_rel_path(raw_path, allow_nested=True)
        except VaultPathError as exc:
            warnings.append(t("ui.vault.manifest_bad", line=f"{raw_path}: {exc}"[:90]))
            continue
        if not canonical.startswith(f"{SECTION_DIR}/"):
            warnings.append(t("ui.vault.manifest_bad", line=raw_path[:60]))
            continue
        title = raw_spec.get("title", "").strip() or Path(canonical).stem
        specs.append(SectionSpec(canonical, title))
    if not specs:
        return default_manifest(), warnings
    return SectionsManifest(inject_char_cap=cap, sections=tuple(specs)), warnings


def dump_sections_manifest(manifest: SectionsManifest) -> str:
    lines = [f"inject_char_cap: {int(manifest.inject_char_cap)}", "sections:"]
    for spec in manifest.sections:
        lines.append(f"  - path: {spec.path}")
        lines.append(f"    title: {_quote_if_needed(spec.title)}")
    return "\n".join(lines) + "\n"



# --------------------------------------------------------------------------
# frontmatter：一个刻意做小的 YAML 子集
# --------------------------------------------------------------------------


@dataclass
class Frontmatter:
    """实体/段落的元信息（ADR-104：兼容原生 Kimi frontmatter）。

    **刻意不用 PyYAML**：只需要"标量 / 字符串列表 / 一层嵌套映射"这个子集，
    而 frontmatter 有一半是模型生成的 —— 用一个完整 YAML 解析器去接它，
    等于把锚点、标签、类型推断（`no` → False）这些边角请进核心路径。

    两套字段并存：
      * 原生 v2：`name / tags / created / last_updated / relation / status / date / place`
        （`date` 与任意未知键留在 `extra`，**原样保留结构**）；
      * 旧版兼容：`title / kind / relation(字符串) / status / updated / aliases / sources`。

    `extra` 保存**所有**未知键（标量、列表、嵌套映射都行），原样回写：
    用户手改的 `pinned: true` 或原生的 `date: {2026_edition: ...}` 都不能被吃掉。
    """

    name: str = ""
    title: str = ""
    kind: str = ""
    relation: str = ""
    status: str = ""
    created: str = ""
    last_updated: str = ""
    updated: str = ""
    place: str = ""
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key in (
            "name",
            "title",
            "kind",
            "relation",
            "status",
            "created",
            "last_updated",
            "updated",
            "place",
        ):
            value = getattr(self, key)
            if value:
                data[key] = value
        for key in ("tags", "aliases", "sources"):
            value = getattr(self, key)
            if value:
                data[key] = list(value)
        data.update(self.extra)
        return data

    def names(self) -> list[str]:
        """所有"可能被别的文件引用"的名字（用于反链 / 双链解析）。

        原生 vault 的实体名是 slug（`name`）；旧档案用 `title`/`aliases`。
        三个都算，解析时按精确匹配。
        """
        out = [self.name, self.title, *self.aliases]
        return [normalize_name(name) for name in out if name.strip()]


_SCALAR_KEYS = (
    "name",
    "title",
    "kind",
    "relation",
    "status",
    "created",
    "last_updated",
    "updated",
    "place",
)
_LIST_KEYS = {"tags", "aliases", "sources"}
# 输出顺序：尽量贴近原生（name/tags/created/last_updated/relation/status/date/place），
# 旧字段排在后面，读起来也仍然是"同一份文件只有一种规范字节"。
_DUMP_ORDER = (
    "name",
    "title",
    "kind",
    "tags",
    "created",
    "last_updated",
    "relation",
    "status",
    "place",
    "updated",
    "aliases",
    "sources",
)


def parse_frontmatter(text: str) -> tuple[Frontmatter, str, list[str]]:
    """返回 (元信息, 正文, 警告列表)。

    **这里刻意 fail-soft，而不是像路径那样 fail-closed**，两者不矛盾：

    * 路径是**动作**（要落到磁盘的哪个位置），猜错会写到不该写的地方 → 一律拒绝；
    * frontmatter 是**数据**（vault 里一个文件长什么样），一个坏文件不该让整个
      vault 读不出来。手工编辑、编辑器换行、半截写入都会造成坏 frontmatter，
      这些都不是攻击，而是"数据质量"，所以降级成"正文照读 + 一条警告"。

    真正不可信的那类输入（网页内容）在别处就被挡住了，不会靠这里兜。
    """
    warnings: list[str] = []
    body = text.lstrip("\ufeff")
    if not body.startswith(_FRONTMATTER_FENCE):
        return Frontmatter(), body, warnings

    lines = body.splitlines()
    # 第一行必须是单独的一行 `---`
    if lines[0].strip() != _FRONTMATTER_FENCE:
        return Frontmatter(), body, warnings

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            end = index
            break
    if end is None:
        warnings.append(t("ui.vault.frontmatter_unterminated"))
        return Frontmatter(), body, warnings

    fields, problems = _parse_fields(lines[1:end])
    warnings.extend(problems)
    rest = "\n".join(lines[end + 1 :]).lstrip("\n")
    return _fields_to_frontmatter(fields), rest, warnings


def _parse_fields(lines: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """解析 `key: value` / 缩进列表 / 一层嵌套映射。

    支持原生实体里那两种新写法：

        relation:
          - classmate_of: user
        date:
          2026_edition: "2026-10-18"

    仍然是**按行**解析，不引入 YAML 的锚点/标签/类型推断。
    """
    entries: list[tuple[int, str]] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        entries.append((indent, line.strip()))

    warnings: list[str] = []
    if not entries:
        return {}, warnings
    fields, index = _parse_mapping(entries, 0, 0, warnings)
    while index < len(entries):
        _indent, content = entries[index]
        warnings.append(t("ui.vault.frontmatter_unparsed", line=content[:40]))
        index += 1
    return fields, warnings


def _is_list_item(content: str) -> bool:
    # 多键字典的列表项会写成裸 `-`；即使写 `- `，上面的 strip 也会把它变成这样。
    return content == "-" or content.startswith("- ")


def _parse_mapping(
    entries: Sequence[tuple[int, str]],
    index: int,
    indent: int,
    warnings: list[str],
) -> tuple[dict[str, Any], int]:
    fields: dict[str, Any] = {}
    while index < len(entries):
        line_indent, content = entries[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            warnings.append(t("ui.vault.frontmatter_unparsed", line=content[:40]))
            index += 1
            continue
        if _is_list_item(content):
            warnings.append(t("ui.vault.frontmatter_list_without_key", line=content[:40]))
            index += 1
            continue
        if ":" not in content:
            warnings.append(t("ui.vault.frontmatter_unparsed", line=content[:40]))
            index += 1
            continue
        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            warnings.append(t("ui.vault.frontmatter_empty_key", line=content[:40]))
            index += 1
            continue
        index += 1
        if value == "":
            # 可能是 `key:` 后面跟列表/嵌套映射，也可能真的就是空值。
            if index < len(entries) and entries[index][0] > line_indent:
                child_indent, child_content = entries[index]
                if _is_list_item(child_content):
                    child, index = _parse_list(entries, index, child_indent, warnings)
                else:
                    child, index = _parse_mapping(entries, index, child_indent, warnings)
                fields[key] = child
            else:
                fields[key] = ""
            continue
        if value.startswith("[") and value.endswith("]"):
            fields[key] = _parse_inline_list(value[1:-1])
        else:
            fields[key] = _unquote(value)
    return fields, index


def _parse_list(
    entries: Sequence[tuple[int, str]],
    index: int,
    indent: int,
    warnings: list[str],
) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(entries):
        line_indent, content = entries[index]
        if line_indent < indent:
            break
        if line_indent != indent or not _is_list_item(content):
            # 不是这个列表的项：留给上层（这是"坏缩进"的边界，不吞掉后面的键）
            break
        item = content[1:].strip()
        index += 1
        if item == "":
            if index < len(entries) and entries[index][0] > line_indent:
                child_indent, child_content = entries[index]
                if _is_list_item(child_content):
                    child, index = _parse_list(entries, index, child_indent, warnings)
                else:
                    child, index = _parse_mapping(entries, index, child_indent, warnings)
                items.append(child)
            else:
                items.append("")
            continue
        if ":" in item:
            key, _, value = item.partition(":")
            key = key.strip()
            value = value.strip()
            if key and value == "" and index < len(entries) and entries[index][0] > line_indent:
                child_indent, child_content = entries[index]
                if _is_list_item(child_content):
                    child, index = _parse_list(entries, index, child_indent, warnings)
                else:
                    child, index = _parse_mapping(entries, index, child_indent, warnings)
                items.append({key: child})
            elif key and value.startswith("[") and value.endswith("]"):
                items.append({key: _parse_inline_list(value[1:-1])})
            elif key:
                items.append({key: _unquote(value)})
            else:
                items.append(_unquote(item))
            continue
        items.append(_unquote(item))
    return items, index


def _parse_inline_list(inner: str) -> list[str]:
    """`[a, "b, c"]` → `["a", "b, c"]`（引号里的逗号不切）。"""
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for char in inner:
        if escaped:
            buf.append(char)
            escaped = False
            continue
        if quote:
            buf.append(char)
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buf.append(char)
            continue
        if char == ",":
            part = "".join(buf).strip()
            if part:
                items.append(_unquote(part))
            buf = []
            continue
        buf.append(char)
    part = "".join(buf).strip()
    if part:
        items.append(_unquote(part))
    return items


def _unquote(value: str) -> str:
    """去掉成对引号，并还原 `_quote_if_needed` 做的转义。

    两边必须成对：只转义不还原的话，一个内容里带引号的值（`别名: "引号\\"在这里"`）
    会在"读出→写回"之后越滚越多反斜杠。

    **转义要一次扫完，不能连着做几次 `str.replace`**：`\\\\n` 这种"原文就是反斜杠加 n"
    的内容，先换 `\\n` 再换 `\\\\` 会把它错当成换行。
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            return _unescape(inner)
        return inner
    return value


def _unescape(inner: str) -> str:
    """单次从左到右扫描，遇到转义序列才消费两个字符。"""
    out: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner):
            following = inner[index + 1]
            if following == "n":
                out.append("\n")
                index += 2
                continue
            if following in ('"', "\\"):
                out.append(following)
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _scalar_text(value: Any) -> str:
    """把标量值变成字符串。布尔/数字保持可读写法，字典/列表由调用方处理。"""
    if isinstance(value, str):
        return value
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _native_meta(text: str) -> dict[str, str]:
    """原生 README 的标量元信息（`last_dream` / `chars` 等在 extra 里）。"""
    front, _, _ = parse_frontmatter(text)
    data = front.to_dict()
    return {
        str(key): _scalar_text(value)
        for key, value in data.items()
        if not isinstance(value, (dict, list))
    }


def _fields_to_frontmatter(fields: dict[str, Any]) -> Frontmatter:
    known: dict[str, str] = {}
    extra: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _SCALAR_KEYS and isinstance(value, str):
            known[key] = value
        elif key in _LIST_KEYS:
            if isinstance(value, list):
                known[key] = tuple(
                    _scalar_text(item).strip()
                    for item in value
                    if _scalar_text(item).strip()
                )
            elif isinstance(value, str) and value:
                known[key] = (value,)
        else:
            # `relation` 的列表形态、`date` 的映射、以及任何不认识的键：
            # 原样保留结构，不拍平成字符串（原生 `date` 就是嵌套映射）。
            extra[key] = value
    return Frontmatter(extra=extra, **known)


def dump_frontmatter(front: Frontmatter) -> str:
    """规范化输出。**只写非空字段**，顺序固定 —— 同一个元信息永远得到同一段字节。"""
    data = front.to_dict()
    lines = [_FRONTMATTER_FENCE]
    written: set[str] = set()
    for key in _DUMP_ORDER:
        if key in data and key not in written:
            _dump_field(lines, key, data[key], 0)
            written.add(key)
        if key == "status" and "date" in data and "date" not in written:
            # 原生把 `date` 放在 `status` 与 `place` 之间；它是嵌套映射，
            # 这里单独排一下，纯粹为了读起来像原生。
            _dump_field(lines, "date", data["date"], 0)
            written.add("date")
    for key, value in data.items():
        if key not in written:
            _dump_field(lines, key, value, 0)
            written.add(key)
    lines.append(_FRONTMATTER_FENCE)
    return "\n".join(lines)


def _dump_field(lines: list[str], key: str, value: Any, indent: int) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            lines.append(f"{pad}{key}: {{}}")
            return
        lines.append(f"{pad}{key}:")
        for sub_key, sub_value in value.items():
            _dump_field(lines, str(sub_key), sub_value, indent + 2)
        return
    if isinstance(value, list):
        if not value:
            lines.append(f"{pad}{key}: []")
            return
        lines.append(f"{pad}{key}:")
        for item in value:
            if isinstance(item, dict):
                if len(item) == 1:
                    sub_key, sub_value = next(iter(item.items()))
                    if isinstance(sub_value, (dict, list)) or sub_value == "":
                        lines.append(f"{pad}  - {sub_key}:")
                        _dump_value(lines, sub_value, indent + 6)
                    else:
                        lines.append(
                            f"{pad}  - {sub_key}: {_quote_if_needed(_scalar_text(sub_value))}"
                        )
                else:
                    lines.append(f"{pad}  -")
                    for sub_key, sub_value in item.items():
                        _dump_field(lines, str(sub_key), sub_value, indent + 4)
            elif isinstance(item, list):
                lines.append(f"{pad}  -")
                _dump_value(lines, item, indent + 4)
            else:
                lines.append(f"{pad}  - {_quote_if_needed(_scalar_text(item))}")
        return
    lines.append(f"{pad}{key}: {_quote_if_needed(_scalar_text(value))}")


def _dump_value(lines: list[str], value: Any, indent: int) -> None:
    """把"没有键的值"渲染出来（列表项里的嵌套映射/列表用）。"""
    if isinstance(value, dict):
        for sub_key, sub_value in value.items():
            _dump_field(lines, str(sub_key), sub_value, indent)
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if len(item) == 1:
                    sub_key, sub_value = next(iter(item.items()))
                    if isinstance(sub_value, (dict, list)) or sub_value == "":
                        lines.append(f"{' ' * indent}- {sub_key}:")
                        _dump_value(lines, sub_value, indent + 4)
                    else:
                        lines.append(
                            f"{' ' * indent}- {sub_key}: {_quote_if_needed(_scalar_text(sub_value))}"
                        )
                else:
                    lines.append(f"{' ' * indent}-")
                    for sub_key, sub_value in item.items():
                        _dump_field(lines, str(sub_key), sub_value, indent + 2)
            elif isinstance(item, list):
                lines.append(f"{' ' * indent}-")
                _dump_value(lines, item, indent + 2)
            else:
                lines.append(f"{' ' * indent}- {_quote_if_needed(_scalar_text(item))}")
        return
    lines.append(f"{' ' * indent}{_quote_if_needed(_scalar_text(value))}")


def _quote_if_needed(value: Any) -> str:
    text = _scalar_text(value)
    # 原生把 ISO 日期写成带引号的字符串；我们也这样，形状更接近。
    iso_date = bool(re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T].*)?$", text))
    if text == "" or text != text.strip() or iso_date or any(
        ch in text for ch in ":#[]{}\"'\n"
    ):
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "")
            .replace("\n", "\\n")  # 换行必须转义：frontmatter 是**按行**解析的
        )
        return f'"{escaped}"'
    return text


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------


@dataclass
class VaultFile:
    path: str
    frontmatter: Frontmatter
    body: str
    chars: int          # 这次**返回**的正文长度
    total_chars: int = 0  # 磁盘上正文的真实长度（截断前的）
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.frontmatter.title or self.frontmatter.name or Path(self.path).stem


@dataclass
class VaultEntry:
    path: str
    title: str
    kind: str
    chars: int
    summary: str
    # 原生 v2 元信息（ADR-104）；旧档案这些字段为空。
    name: str = ""
    tags: tuple[str, ...] = ()
    created: str = ""
    last_updated: str = ""
    place: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "title": self.title, "chars": self.chars}
        if self.kind:
            data["kind"] = self.kind
        if self.name:
            data["name"] = self.name
        if self.tags:
            data["tags"] = list(self.tags)
        if self.created:
            data["created"] = self.created
        if self.last_updated:
            data["last_updated"] = self.last_updated
        if self.place:
            data["place"] = self.place
        if self.summary:
            data["summary"] = self.summary
        return data


def _summary_of(body: str, *, limit: int = 60) -> str:
    """列表里显示的一行摘要：**跳过标题行**，取第一句有内容的话。

    模型写的档案几乎都以 `# 标题` 开头，而标题在列表里已经单独显示了 ——
    摘要再返回一次标题，就成了「小张 [person] 小张」这种废话。
    正文里全是标题时（空壳文件）才退回用标题。
    """
    fallback = ""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        text = re.sub(r"^[-*+]\s+", "", line.lstrip("#").strip()).strip()
        if not text:
            continue
        if line.startswith("#"):
            fallback = fallback or text
            continue
        return text[:limit]
    return fallback[:limit]


def _wikilinks(text: str) -> list[str]:
    """`[[小林]]` / `[[entities/x|显示名]]` 形式的链接目标（顺序、去重）。

    `![[raw/...]]` 是**嵌入**不是链接：排除掉（由 UI 单独渲染成图片）。
    带 `|别名` 的写法只取路径那一半用于解析。
    """
    seen: list[str] = []
    for match in _WIKILINK.findall(text):
        target = match.split("|", 1)[0].strip()
        name = normalize_name(target)
        if name and name not in seen:
            seen.append(name)
    return seen


class VaultStore:
    """vault 的读写入口。**读操作绝不创建任何文件/目录**。

    这条和 `reset.preview_reset()`"走只读路径、不构造会顺手种文件的 store"
    是同一条原则：界面上的"看一眼"不能改变磁盘状态。
    """

    def __init__(
        self,
        root: Path,
        *,
        read_max_chars: int = 4000,
        write_max_chars: int = 12000,
        max_files: int = 400,
        name_max_chars: int = 40,
        readme_token_budget: int = 1200,
    ) -> None:
        self.root = Path(root)
        self.read_max_chars = int(read_max_chars)
        self.write_max_chars = int(write_max_chars)
        self.max_files = int(max_files)
        self.name_max_chars = int(name_max_chars)
        self.readme_token_budget = int(readme_token_budget)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def path_of(self, rel: str) -> Path:
        """把相对路径落到绝对路径，并**再次**确认它真的在 vault 里面。

        归一化已经挡住了 `..` 与绝对路径；这一次是防**符号链接**：
        `vault/entities/x.md` 本身合法，但如果 `entities` 是指向别处的链接，
        归一化看不出来，只有 resolve 之后比较真实前缀才发现得了。
        兜底永远 fail-closed —— 宁可报错，也不要写出去。会话创建的嵌套档案也可读。
        """
        canonical = normalize_rel_path(
            rel, name_max_chars=self.name_max_chars, allow_nested=True
        )
        return self._resolve_canonical(canonical)

    def _resolve_canonical(self, canonical: str) -> Path:
        target = self.root / canonical
        try:
            real_root = self.root.resolve()
            real_target = target.resolve()
        except OSError as exc:  # 坏路径 / 超长路径
            raise VaultPathError(t("ui.vault.resolve_failed", error=exc)) from exc
        if real_target != real_root and not real_target.is_relative_to(real_root):
            raise VaultPathError(
                t("ui.vault.resolved_outside", path=canonical)
            )
        return real_target

    def _resolve_session_path(self, canonical: str) -> Path:
        """会话写/删路径：拒绝任一层的链接、junction 或 reparse point。

        仅检查"最后有没有逃出 root"不够：vault 内的链接也可能把合法别名指向
        memory_instruction / 固定 section。模型不能创建链接，但用户手工放入的链接
        也不能成为权限绕过入口。
        """
        current = self.root
        for part in canonical.split("/"):
            current = current / part
            if _is_link_or_reparse(current):
                raise VaultPathError(t("ui.vault.dir_contains_link", path=canonical))
        return self._resolve_canonical(canonical)

    def resolve_friendly(self, raw: str, *, allow_nested: bool = False) -> str:
        """宽松一点地理解模型给的路径，但**最终仍然只产出规范形式**。

        模型很自然会写 `dream_read("小林")`。这里按固定顺序补目录：
        先根级文件、再 entities、再 sections，最后按原样（README 等）。
        顺序固定 = 结果可预测；每次都返回规范路径，所以模型能学到正确写法。
        `allow_nested` 只由会话编辑与档案读取打开，整理器的写合同保持不变。
        """
        cleaned = str(raw).strip().lstrip("\ufeff").replace("\\", "/")
        explicit_root = cleaned.startswith("./")
        root_request = ""
        try:
            canonical = normalize_rel_path(
                raw, name_max_chars=self.name_max_chars, allow_nested=allow_nested
            )
            if (
                allow_nested
                and "/" not in canonical
                and canonical.endswith(".md")
                and canonical not in ROOT_MD_FILES
            ):
                if explicit_root:
                    # `./same.md` 是根级档案的显式地址，与 entities/same.md 不歧义。
                    return canonical
                # 普通 `foo.md` 先保持 entities/sections 的既有友好解析；
                # 只有都没有时才把它当成一个自定义根级路径。
                root_request = canonical
            else:
                return canonical
        except VaultPathError:
            pass

        lowered = cleaned.lower()
        for known in (*ROOT_MD_FILES, SECTIONS_CONFIG):
            if lowered in (known.lower(), Path(known).stem.lower()):
                return known
        # 已经带了目录但写错大小写之类：先按原样再试一次会掩盖问题，
        # 所以这里只处理"裸名字"，其余情况把原始错误抛出去。
        # 原生链接 `[[sections/taste|Taste]]` 省略 `.md`，读/写入口也接受这种写法。
        if "/" in cleaned and not cleaned.lower().endswith(".md"):
            try:
                return normalize_rel_path(
                    f"{cleaned}.md",
                    name_max_chars=self.name_max_chars,
                    allow_nested=allow_nested,
                )
            except VaultPathError:
                pass
        if "/" in cleaned:
            return normalize_rel_path(
                raw, name_max_chars=self.name_max_chars, allow_nested=allow_nested
            )

        base = cleaned if cleaned.lower().endswith(".md") else f"{cleaned}.md"
        # 扩展名统一成小写：`Foo.MD` 在 Windows 上就是 `Foo.md`，
        # 不归一化的话会拼出 `entities/Foo.MD.md` 这个根本不存在的路径
        # （然后模型得到"没有这个档案"，而它要找的文件其实就在那儿）。
        base = base[:-3] + ".md"
        root_candidate = ""
        root_resolved: Path | None = None
        if allow_nested:
            root_candidate = normalize_rel_path(
                base, name_max_chars=self.name_max_chars, allow_nested=True
            )
            root_resolved = self._resolve_canonical(root_candidate)
        for candidate in (f"{ENTITY_DIR}/{base}", f"{SECTION_DIR}/{base}"):
            try:
                canonical = normalize_rel_path(
                    candidate,
                    name_max_chars=self.name_max_chars,
                    allow_nested=allow_nested,
                )
            except VaultPathError:
                continue
            resolved = self._resolve_canonical(canonical)
            if resolved.is_file():
                if root_resolved is not None and root_resolved.is_file():
                    # 手工放入的同名根级/canonical 档案不能让裸名字删除"猜一个"。
                    raise VaultPathError(
                        t(
                            "ui.vault.ambiguous_name",
                            name=base,
                            paths=f"{root_candidate}, {canonical}",
                        )
                    )
                # 返回**磁盘上的名字**而不是模型写的那个：大小写不敏感的系统上
                # 它们指向同一个文件，但报给模型的名字应当是真实存在的那个
                # （否则模型会把 `entities/FOO.md` 当成规范写法记下来）。
                directory = canonical.rpartition("/")[0]
                return f"{directory}/{resolved.name}"
        if root_resolved is not None and root_resolved.is_file():
            return root_resolved.name
        if root_request:
            # 明确的 `new-name.md` 且没有任何 canonical 同名档案时，才新建根级档案。
            return root_request
        # 都不存在：返回 entities 下的规范形式，让上层的"没有这个文件"更具体
        return normalize_rel_path(
            f"{ENTITY_DIR}/{base}", name_max_chars=self.name_max_chars
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        return self.root.is_dir()

    def has_content(self) -> bool:
        """有没有**可读的档案**？**只读判断，不创建任何东西**。

        判据是"vault 里至少有一个**真实**档案（含自定义根级/目录）"，而**不是**
        "README 在不在"或"生成物在不在"。`sections/earlier_months.md` 是投影，
        空 vault 跑一次 `--vault-rebuild` 会把它写出来；把它算成"有档案"会让
        `runtime` 给模型注入一段只有 "No Earlier Months yet" 的 README。
        理由有两个：

        * `--vault-init` 建的骨架只有一份占位 README。按"README 在不在"判，
          那时会白白注册三个工具、还往上下文里注入一段"这里什么都没有"的 README；
        * README 是**档案的投影**，没有档案时它本来就没有可注入的内容。

        同样刻意**不读文件内容**：这个判断每轮都会做一次（Web 端每轮重建 Runtime），
        读一遍 vault 里每个文件在档案变多之后是纯浪费。所以只做一次目录扫描。
        """
        return any(
            rel != f"{SECTION_DIR}/earlier_months.md"
            for rel in self._iter_rels()
        )

    def list_files(self, prefix: str = "") -> list[VaultEntry]:
        """列出文件（按路径排序，结果稳定）。`prefix` 只用于筛选，不参与解析。"""
        wanted = prefix.strip().strip("/")
        entries = [
            entry
            for entry in self._read_entries(None)
            if not wanted or entry.path.startswith(wanted)
        ]
        return entries[: self.max_files]

    def count_files(self) -> int:
        """**档案**数量（不含 README）。

        口径必须和 `has_content()` 一致：README 是索引、是投影，不是一个"档案"。
        早先这里数的是 `_iter_paths()`（含 README），于是"文件数上限"
        和"有没有档案"用了两个不同的定义 —— 同一个名字两套口径，
        正是那种以后一定会有人读错的东西。
        """
        return len(self._iter_rels())

    # ------------------------------------------------------------------
    # sections.yaml / raw 素材（原生 vault 对齐，ADR-104）
    # ------------------------------------------------------------------

    def manifest_report(self) -> tuple[SectionsManifest, list[str]]:
        """读 `sections.yaml`。不存在时返回默认清单（**只读，不创建文件**）。"""
        path = self.root / SECTIONS_CONFIG
        if not path.is_file():
            return default_manifest(), []
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            return default_manifest(), [t("ui.vault.manifest_read_failed", error=exc)]
        return parse_sections_manifest(text)

    def manifest(self) -> SectionsManifest:
        return self.manifest_report()[0]

    def is_native(self) -> bool:
        """是不是原生对齐的 vault（有 `sections.yaml`）？

        有清单 = README/index/earlier_months 由 harness 生成；
        没有清单 = 旧 vault，保持"模型写 README"的老行为（兼容，不强迁）。
        """
        return (self.root / SECTIONS_CONFIG).is_file()

    def write_manifest(self, manifest: SectionsManifest) -> str:
        return self.write_file(SECTIONS_CONFIG, dump_sections_manifest(manifest))

    def ensure_manifest(self) -> bool:
        """空 vault 第一次整理时建立默认清单。**不迁移旧 vault。**

        旧 vault 已经有 sections/entities、但没有 `sections.yaml` 时，继续保持
        旧模式（模型写 README）；要不要迁移由用户显式决定，不能在一次整理里
        悄悄把 README 的写法换掉。
        """
        if self.is_native():
            return False
        if self._iter_rels():
            return False
        self.write_manifest(default_manifest())
        return True

    def _assert_editable(self, canonical: str, *, session: bool = False) -> None:
        """写权限在 store 层收口；会话与 dream 的允许范围**刻意不同**。

        会话可以维护 observations，也可以重写旧 vault 的 README，但不能碰
        index / sections.yaml / log / memory_instruction。原生 README 与其他
        生成物、月份归档仍然只读。dream 保持 ADR-104 的既有权限，不随会话扩权。
        """
        key = _path_key(canonical)
        if key == _path_key(MEMORY_INSTRUCTION_NAME):
            raise VaultPathError(t("ui.vault.generated_readonly", path=canonical))
        if session and key in {_path_key(path) for path in SESSION_READONLY_RELS}:
            raise VaultPathError(t("ui.vault.session_readonly", path=canonical))
        if session and (key == _path_key(RAW_DIR) or key.startswith(_path_key(RAW_DIR + "/"))):
            raise VaultPathError(t("ui.vault.session_readonly", path=canonical))
        if not self.is_native():
            return
        generated = (
            key in {_path_key(path) for path in GENERATED_RELS}
            or key.startswith(_path_key(f"{SECTION_DIR}/{ARCHIVE_DIR}/"))
        )
        dream_only = key in {
            _path_key(path) for path in (SECTIONS_CONFIG, LOG_NAME, OBSERVATIONS_NAME)
        }
        if generated or (dream_only and not session):
            raise VaultPathError(t("ui.vault.generated_readonly", path=canonical))


    def raw_files(self) -> list[dict[str, Any]]:
        """raw/ 下的图片素材（含同名 `.meta.json` 的路径），按路径排序。"""
        out: list[dict[str, Any]] = []
        for rel in self._iter_raw():
            if rel.lower().endswith(RAW_META_SUFFIX):
                continue
            meta = rel + RAW_META_SUFFIX
            path = self.root / rel
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            out.append(
                {
                    "path": rel,
                    "meta": meta if (self.root / meta).is_file() else "",
                    "bytes": size,
                }
            )
        return out

    def raw_path_of(self, rel: str) -> Path:
        canonical = normalize_raw_path(rel, name_max_chars=self.name_max_chars * 3)
        return self._resolve_canonical(canonical)

    def read_raw(self, rel: str, *, max_bytes: int = 32 * 1024 * 1024) -> tuple[bytes, str]:
        canonical = normalize_raw_path(rel, name_max_chars=self.name_max_chars * 3)
        path = self._resolve_canonical(canonical)
        if not path.is_file():
            raise FileNotFoundError(canonical)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise VaultPathError(t("ui.vault.raw_read_failed", path=canonical, error=exc)) from exc
        if size > max_bytes:
            raise VaultPathError(
                t("ui.vault.raw_too_large", path=canonical, size=size, limit=max_bytes)
            )
        return path.read_bytes(), canonical

    def read(self, rel: str, *, max_chars: int | None = None) -> VaultFile:
        """读一个 md 文件。`max_chars` 只管正文，元信息永远完整给出。"""
        canonical = self.resolve_friendly(rel, allow_nested=True)
        if canonical == SECTIONS_CONFIG:
            raise VaultPathError(t("ui.vault.manifest_not_a_note"))
        path = self._resolve_canonical(canonical)
        if not path.is_file():
            raise FileNotFoundError(canonical)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise VaultPathError(
                t("ui.vault.not_utf8", path=canonical, error=exc)
            ) from exc

        front, body, warnings = parse_frontmatter(text)
        limit = self.read_max_chars if max_chars is None else max(0, int(max_chars))
        total = len(body)
        truncated = total > limit
        if truncated:
            body = body[:limit] + t("ui.vault.read_truncated")
        return VaultFile(
            path=canonical,
            frontmatter=front,
            body=body,
            chars=len(body),
            total_chars=total,
            truncated=truncated,
            warnings=warnings,
        )

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """字面检索：复用 M2 的二元组打分，不引入第二套算法。

        刻意**不设分数门槛**：vault 里文件本来就不多，宁可返回"最像的几个"
        并在结果里带上分数，让模型自己判断相关性 —— 返回空列表会让它以为
        "档案里没有这回事"，那比给几个弱相关的结果更糟。
        """
        grams = bigrams(query or "")
        if not grams:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for rel, path in self._iter_paths():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            front, body, _ = parse_frontmatter(text)
            haystack = " ".join(
                [front.name, front.title, *front.aliases, *front.tags, body]
            )
            score = dice(grams, bigrams(haystack))
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "path": rel,
                        "title": front.title or front.name or Path(rel).stem,
                        "score": round(score, 3),
                        "snippet": _snippet(body, query),
                    },
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1]["path"]))
        return [item for _, item in scored[: max(1, limit)]]

    def neighborhood(self, rel: str, *, limit: int = 12) -> dict[str, list[dict[str, str]]]:
        """这个文件的邻域：它指向谁、谁指向它、以及和它共现的节点。

        图邻接**不建数据库**：每次读的时候机械地扫一遍全库（vault 规模按
        `max_files` 封顶，这个扫描是有界的）。好处是 vault 永远是自洽的 ——
        不像索引那样会和文件内容脱节。代价是 O(文件数)，可接受，而且
        换来的是"用户手改一个文件，邻域立刻正确"。
        """
        canonical = self.resolve_friendly(rel, allow_nested=True)
        index = self._link_index(limit=self.max_files)
        mine = index.get(canonical, {})
        my_links = mine.get("links", [])

        links: list[dict[str, str]] = []
        for name in my_links[:limit]:
            target = self._name_to_path(name, index)
            links.append({"name": name, "path": target or ""})

        backlinks: list[dict[str, str]] = []
        my_names = set(mine.get("names", []))
        for other, info in index.items():
            if other == canonical:
                continue
            # 反链 = **对方指向我**。所以要比的是"对方的 links"与"我的名字"，
            # 而不是两边的名字 —— 后者会把"同名的两个文件"误判成互相引用。
            if set(info.get("links", [])) & my_names:
                backlinks.append({"path": other, "title": info.get("title", "")})

        related: list[dict[str, str]] = []
        my_set = set(my_links)
        for other, info in index.items():
            if other == canonical:
                continue
            shared = my_set & set(info.get("links", []))
            if shared:
                related.append(
                    {
                        "path": other,
                        "title": info.get("title", ""),
                        "shares": "、".join(sorted(shared)[:5]),
                    }
                )

        related.sort(key=lambda item: item["path"])
        backlinks.sort(key=lambda item: item["path"])
        return {
            "links": links[:limit],
            "backlinks": backlinks[:limit],
            "related": related[:limit],
        }

    def readme_text(
        self,
        *,
        token_budget: int | None = None,
        char_cap: int | None = None,
    ) -> tuple[str, bool]:
        """README 的正文，以及"是否因为超预算被截断"。

        **截断必须由 harness 强制**：README 每轮都进上下文，靠提示词求模型
        写短一点是不可靠的 —— 一个啰嗦的 README 会让**此后每一轮**都变贵，
        而模型只在写它的那一次看到代价。

        ADR-104：有 `sections.yaml` 的原生/新 vault 用 **字符 cap**
        （`inject_char_cap`，默认 6000）；没有清单的旧 vault 仍用 token 预算，
        这样升级不会把老档案的注入量突然放大。
        """
        path = self.root / README_NAME
        if not path.is_file():
            return "", False
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return "", False
        front, body, _ = parse_frontmatter(text)
        if front.status.strip().lower() == PLACEHOLDER_STATUS:
            # 程序自己写的占位 README：那不是"整理出来的档案"，一律不注入
            return "", False
        if not body.strip():
            return "", False
        if char_cap is not None:
            return _fit_chars(body.strip(), max(0, int(char_cap)))
        manifest = self.manifest()
        if (self.root / SECTIONS_CONFIG).is_file():
            return _fit_chars(body.strip(), max(0, manifest.inject_char_cap))
        budget = self.readme_token_budget if token_budget is None else int(token_budget)
        return _fit_tokens(body.strip(), budget)

    # ------------------------------------------------------------------
    # 写（dream、会话编辑工具、网页界面与 --vault-init 共用底层入口）
    # ------------------------------------------------------------------

    def write_file(
        self,
        rel: str,
        text: str,
        *,
        allow_nested: bool = False,
        generated: bool = False,
    ) -> str:
        """原子写入。返回规范化的相对路径。

        "先写临时文件再 `os.replace`"不是为了并发（本机单用户），而是为了
        **崩溃安全**：README 是每轮都要读的东西，半截写入会让它一直读到
        半个文件。临时文件放在同一个目录里，所以 `os.replace` 是同一卷内的
        原子改名。
        """
        canonical = normalize_rel_path(
            rel, name_max_chars=self.name_max_chars, allow_nested=allow_nested
        )
        # memory_instruction 只能由 MemoryStore 写；低层原子写接口也不能绕过所有权。
        # Dream 自己的 README/index 等生成物仍可由 harness 写，不能在这里一刀切。
        if canonical == MEMORY_INSTRUCTION_NAME:
            raise VaultPathError(t("ui.vault.generated_readonly", path=canonical))
        path = self._resolve_canonical(canonical)
        payload = text if text.endswith("\n") else text + "\n"
        # write_max_chars 是"模型单份档案"的上限，不该卡住由多份合法档案
        # 汇总出来的 README/index/清单；生成物总量仍受 max_files × 单档案上限约束。
        if not generated and len(payload) > self.write_max_chars:
            raise VaultPathError(
                t(
                    "ui.vault.file_too_long",
                    path=canonical,
                    length=len(payload),
                    limit=self.write_max_chars,
                )
            )
        # 撞名检查**必须无条件跑**，不能只放在"新建"分支里：
        # 在大小写不敏感的系统上，`entities/foo.md` 的 `is_file()` 会因为
        # `entities/Foo.md` 存在而返回真 —— 于是"新建"分支根本不执行，
        # 而 `os.replace` 正会把那份档案悄悄覆盖掉。
        #
        # 比对用的是**归一化后的相对路径**，不是 resolve 之后的名字：
        # Windows 上 `resolve()` 会把名字规范成磁盘上存的那个大小写
        # （`foo.md` → `Foo.md`），拿它去比就永远看不到差异（第一版就这么错）。
        clash = self._case_clash(canonical)
        if clash:
            raise VaultPathError(t("ui.vault.case_clash", path=canonical, clash=clash))
        creating = not path.is_file()
        if not generated and creating and self.count_files() >= self.max_files:
            raise VaultPathError(t("ui.vault.too_many_files", limit=self.max_files))
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:6]}")
        try:
            temp.write_text(payload, encoding="utf-8", newline="\n")
            self._replace(temp, path, canonical)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
        return canonical

    def save_note(
        self,
        rel: str,
        body: str,
        front: Frontmatter | None = None,
        *,
        today: str = "",
        mode: str = "auto",
        clear: Sequence[str] = (),
        session: bool = False,
    ) -> dict[str, Any]:
        """把"正文 + 元信息"写成一份档案。**档案写入的唯一入口。**

        存在的意义是"一条规则只有一个实现"：整理工具（模型）与网页界面（人）
        都往 vault 里写，两边必须服从同一套规矩 —— 合并元信息的方式、`updated` 怎么戳、
        大小写撞名怎么拒、单文件上限是多少。这些全在 `write_file` 与这里，
        两边都不许自己拼一份。

        * `mode="create"`：不存在才写（已存在 → 抛 `VaultExistsError`，避免悄悄覆盖别人的档案）；
        * `mode="update"`：必须已存在（不存在 → `FileNotFoundError`，避免拼错路径反而建出新档案）；
        * `mode="auto"`：存在就更新、不存在就新建（整理工具用的就是这个）。

        **元信息是合并而不是替换**（`merge_frontmatter`）：只覆盖明确给了的字段，
        你手工加过的键（`pinned: true` 之类）与没提到的字段原样保留。
        `updated` 统一戳成 `today`（不传就用今天）。

        `clear` 是**合并语义的一个必要出口**：默认"空值 = 保持原样"，
        所以界面把某个输入框清空时得显式说"这个字段我要抹掉"。
        没有它就会出现"用户在界面上清掉了 status，文件里却还留着旧值"——
        而模型下一轮还会读到那个旧值。**只有 `True` 的字段会被清空**，
        没列进来的字段一律保持（与模型侧的行为一致：它不提就是不改）。
        """
        canonical = self.resolve_friendly(rel, allow_nested=session)
        self._assert_editable(canonical, session=session)
        path = (
            self._resolve_session_path(canonical)
            if session
            else self._resolve_canonical(canonical)
        )
        exists = path.is_file()
        if session and mode == "create" and canonical.startswith(
            (f"{ENTITY_DIR}/", f"{SECTION_DIR}/")
        ):
            root_name = Path(canonical).name
            if self._resolve_session_path(root_name).is_file():
                raise VaultPathError(
                    t(
                        "ui.vault.ambiguous_name",
                        name=root_name,
                        paths=f"{root_name}, {canonical}",
                    )
                )
        # 顺序有讲究：**先**查"只差大小写的兄弟"，再查"已经存在"。
        # 因为 `path.is_file()` 在大小写不敏感的系统上对 `Foo.md`/`foo.md` 都为真，
        # 先报"已经有一份"就把"这跟已有的 foo.md 是同一个文件"这条更有用的信息盖掉了。
        clash = self._case_clash(canonical) if exists else ""
        if clash:
            raise VaultPathError(t("ui.vault.case_clash_no_suffix", path=canonical, clash=clash))
        if mode == "create" and exists:
            raise VaultExistsError(t("ui.vault.exists", path=canonical))
        if mode == "update" and not exists:
            raise FileNotFoundError(canonical)

        today_s = today or time.strftime("%Y-%m-%d")
        incoming = front or Frontmatter()
        # 原生模式戳 `last_updated`；旧模式继续保持 `updated`（已有测试/界面都读它）。
        # 旧档案里已有的另一个字段由 merge 原样保留，不会被删。
        if self.is_native():
            incoming = replace(incoming, last_updated=today_s)
        else:
            incoming = replace(incoming, updated=today_s)
        merged = incoming
        if exists:
            existing_front, _, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            merged = merge_frontmatter(existing_front, incoming)
            merged = _clear_frontmatter_fields(merged, clear)
        if not merged.created:
            merged = replace(merged, created=today_s)
        # 原生实体名默认 = 文件名 stem（slug）；sections 不写 `name`。
        if canonical.startswith(f"{ENTITY_DIR}/") and not merged.name:
            merged = replace(merged, name=Path(canonical).stem)
        # `status: placeholder` 会让 README **永久不被注入**（那是程序给空骨架用的标记）。
        # 原生模式下 README 是生成物、走不到这里；旧模式仍然保留这条守卫。
        if canonical == README_NAME and merged.status.strip().lower() == PLACEHOLDER_STATUS:
            raise VaultPathError(t("ui.vault.readme_placeholder"))
        written = self.write_file(
            canonical,
            dump_frontmatter(merged) + "\n\n" + body.strip() + "\n",
            allow_nested=session,
        )
        return {
            "path": written,
            "action": "updated" if exists else "created",
            "frontmatter": merged.to_dict(),
            "body_chars": len(body.strip()),
        }

    def delete_file(self, rel: str, *, session: bool = False) -> str:
        """删一份档案，返回规范路径。**档案删除的唯一入口。**

        README 不能删（它是索引，应当重写）—— 这条在工具与界面两边都要一样，
        所以放在这里而不是各自的调用点。文件不存在时抛 `FileNotFoundError`
        （调用方要把"没有这份档案"和"路径非法"分开报）。

        原生模式还额外保护：生成物、清单、归档月份、固定 section 不能删
        （删了 README 就会缺一块，而且没有"重建"以外的恢复路径）。
        """
        canonical = self.resolve_friendly(rel, allow_nested=session)
        self._assert_editable(canonical, session=session)
        if _path_key(canonical) == _path_key(README_NAME):
            raise VaultPathError(t("ui.vault.readme_no_delete"))
        protected_sections = (
            {path for path, _title in DEFAULT_SECTION_SPECS}
            if session
            else set(self.manifest().paths())
        )
        if self.is_native() and _path_key(canonical) in {
            _path_key(path) for path in protected_sections
        }:
            raise VaultPathError(t("ui.vault.section_no_delete", path=canonical))
        path = (
            self._resolve_session_path(canonical)
            if session
            else self._resolve_canonical(canonical)
        )
        if not path.is_file():
            raise FileNotFoundError(canonical)
        path.unlink()
        return canonical

    # ------------------------------------------------------------------
    # 会话编辑（ADR-111）：与 dream 写入共用底层校验，但权限清单独立
    # ------------------------------------------------------------------

    def _rebuild_after_session_edit(self) -> dict[str, Any]:
        """会话每次变更后重建生成物；旧 vault 只重建 index，不接管 README。"""
        if self.is_native():
            return self.rebuild_generated()
        self.write_file(INDEX_NAME, self.render_index(), generated=True)
        return {"rebuilt": True, "index_only": True}

    def save_session_note(
        self,
        rel: str,
        body: str,
        front: Frontmatter | None = None,
        *,
        today: str = "",
        mode: str,
    ) -> dict[str, Any]:
        """会话的新建/编辑入口。`mode` 必须是 create 或 update，不接受静默覆盖。"""
        if mode not in ("create", "update"):
            raise ValueError("session vault mode must be create or update")
        result = self.save_note(
            rel, body, front, today=today, mode=mode, session=True
        )
        result["generated"] = self._rebuild_after_session_edit()
        return result

    def _assert_session_directory(self, canonical: str, *, deleting: bool) -> None:
        key = _path_key(canonical)
        archive = _path_key(f"{SECTION_DIR}/{ARCHIVE_DIR}")
        raw = _path_key(RAW_DIR)
        if key == raw or key.startswith(raw + "/"):
            raise VaultPathError(t("ui.vault.session_readonly", path=canonical + "/"))
        if key == archive or key.startswith(archive + "/"):
            raise VaultPathError(t("ui.vault.generated_readonly", path=canonical + "/"))
        if deleting and key in {_path_key(path) for path in _TOP_DIRS}:
            raise VaultPathError(t("ui.vault.dir_no_delete", path=canonical + "/"))

    def create_session_directory(self, rel: str) -> dict[str, Any]:
        """建一个 vault 内目录；已存在就明确返回 existing，不伪装成新建。"""
        canonical = normalize_session_dir_path(rel, name_max_chars=self.name_max_chars)
        self._assert_session_directory(canonical, deleting=False)
        path = self._resolve_session_path(canonical)
        if path.exists() and not path.is_dir():
            raise VaultPathError(t("ui.vault.dir_target_file", path=canonical))
        clash = self._case_clash(canonical)
        if clash:
            raise VaultPathError(
                t("ui.vault.case_clash_no_suffix", path=canonical, clash=clash)
            )
        created = not path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
        return {
            "path": canonical + "/",
            "action": "created" if created else "existing",
            "generated": self._rebuild_after_session_edit() if created else {"rebuilt": False},
        }

    def delete_session_path(self, rel: str, *, recursive: bool = False) -> dict[str, Any]:
        """删除会话可写的文件或目录；精确存在的目录优先于友好文件名解析。

        这样 `entities` 不会被误解成 `entities/entities.md`。递归删除先验证整棵树，
        再动第一个字节，父目录不能成为 README/固定 section/归档保护的旁路。
        """
        raw = str(rel or "").strip()
        canonical_dir = ""
        dir_error: VaultPathError | None = None
        try:
            canonical_dir = normalize_session_dir_path(
                raw, name_max_chars=self.name_max_chars
            )
        except VaultPathError as exc:
            dir_error = exc
        else:
            directory = self._resolve_session_path(canonical_dir)
            if directory.is_dir() or raw.replace("\\", "/").endswith("/"):
                return self._delete_session_directory(
                    canonical_dir, directory, recursive=recursive
                )

        file_error: VaultPathError | None = None
        canonical_file = ""
        try:
            canonical_file = self.resolve_friendly(raw, allow_nested=True)
        except VaultPathError as exc:
            file_error = exc
        else:
            # 即使目标不存在，也要先报"这个路径禁止删"，而不是含混地说不存在。
            self._assert_editable(canonical_file, session=True)
            if _path_key(canonical_file) == _path_key(README_NAME):
                raise VaultPathError(t("ui.vault.readme_no_delete"))
            if self.is_native() and _path_key(canonical_file) in {
                _path_key(path) for path, _title in DEFAULT_SECTION_SPECS
            }:
                raise VaultPathError(
                    t("ui.vault.section_no_delete", path=canonical_file)
                )
            file_path = self._resolve_session_path(canonical_file)
            if file_path.is_file():
                removed = self.delete_file(canonical_file, session=True)
                generated = self._rebuild_after_session_edit()
                return {
                    "path": removed,
                    "kind": "file",
                    "files": 1,
                    "recursive": False,
                    "generated": generated,
                }

        if canonical_dir:
            directory = self._resolve_session_path(canonical_dir)
            return self._delete_session_directory(
                canonical_dir, directory, recursive=recursive
            )
        if canonical_file:
            raise FileNotFoundError(canonical_file)
        raise file_error or dir_error or FileNotFoundError(raw)

    def _delete_session_directory(
        self, canonical_dir: str, directory: Path, *, recursive: bool
    ) -> dict[str, Any]:
        self._assert_session_directory(canonical_dir, deleting=True)
        if not directory.is_dir():
            raise FileNotFoundError(canonical_dir)

        files: list[tuple[Path, str]] = []
        directories: list[Path] = []
        real_root = self.root.resolve()
        if recursive:
            def visit(current: Path) -> None:
                """显式遍历，绝不下入链接/junction；所有检查先于任何 unlink。"""
                for path in sorted(current.iterdir(), key=lambda item: item.name):
                    rel = path.relative_to(real_root).as_posix()
                    if _is_link_or_reparse(path):
                        raise VaultPathError(t("ui.vault.dir_contains_link", path=rel))
                    # 每个节点都再过一次真实路径 containment；即使将来换了遍历
                    # 实现，也不能把 vault 外面的对象列进待删清单。
                    resolved = path.resolve()
                    if resolved != real_root and not resolved.is_relative_to(real_root):
                        raise VaultPathError(t("ui.vault.resolved_outside", path=rel))
                    if path.is_dir():
                        canonical_subdir = normalize_session_dir_path(
                            rel, name_max_chars=self.name_max_chars
                        )
                        self._assert_session_directory(canonical_subdir, deleting=True)
                        visit(path)
                        directories.append(path)  # 子目录先入列，删除时才能先深后浅
                        continue
                    canonical = normalize_rel_path(
                        rel, name_max_chars=self.name_max_chars, allow_nested=True
                    )
                    self._assert_editable(canonical, session=True)
                    if _path_key(canonical) == _path_key(README_NAME):
                        raise VaultPathError(t("ui.vault.readme_no_delete"))
                    if self.is_native() and _path_key(canonical) in {
                        _path_key(section) for section, _title in DEFAULT_SECTION_SPECS
                    }:
                        raise VaultPathError(
                            t("ui.vault.section_no_delete", path=canonical)
                        )
                    files.append((path, canonical))

            visit(directory)
            for path, _rel in files:
                path.unlink()
            for path in directories:
                path.rmdir()
            directory.rmdir()
        else:
            try:
                directory.rmdir()
            except OSError as exc:
                raise VaultPathError(
                    t("ui.vault.dir_not_empty", path=canonical_dir)
                ) from exc
        generated = self._rebuild_after_session_edit()
        return {
            "path": canonical_dir + "/",
            "kind": "directory",
            "files": len(files),
            "recursive": bool(recursive),
            "generated": generated,
        }

    def _case_clash(self, canonical: str) -> str:
        """同一个目录里有没有"只差大小写"的兄弟**档案**？

        Windows / macOS 上 `Foo.md` 与 `foo.md` 是**同一个文件**，所以
        "先写 Foo、再写 foo"会把第一个档案静默销毁 —— 而且事后连证据都没有
        （目录里只剩一个文件，`check()` 也查不出撞名）。
        所以必须在**写之前**就拒绝，而不是事后统计。

        只在大小写不敏感的文件系统上生效：Linux 上这两个文件确实可以并存，
        在那里拦下来是误伤。
        """
        if not _CASE_INSENSITIVE_FS:
            return ""
        directory, _, name = canonical.rpartition("/")
        parent = (self.root / directory) if directory else self.root
        if not parent.is_dir():
            return ""
        folded = name.casefold()
        try:
            siblings = list(parent.iterdir())
        except OSError:
            return ""
        for sibling in siblings:
            # 名字**一模一样**不算撞名（那是正常覆盖同一份档案）
            if sibling.name != name and sibling.name.casefold() == folded:
                return f"{directory}/{sibling.name}" if directory else sibling.name
        return ""

    @staticmethod
    def _replace(temp: Path, path: Path, canonical: str) -> None:
        """`os.replace`，带一点重试，并把 OSError 翻译成看得懂的话。

        为什么要重试：**Web 端是多线程的**。每轮对话构造 Runtime 都会读 README，
        而 dream 可能在另一个线程写它 —— "本机单用户"不等于"没有并发"。
        目标文件短暂被占用时 `os.replace` 会抛 `PermissionError`，
        重试几次基本就过去了；实在不行也要给一句人话，而不是一个 WinError。
        """
        for attempt in range(4):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise VaultPathError(
                        t("ui.vault.target_busy", path=canonical)
                    ) from None
                time.sleep(0.02)
            except OSError as exc:
                raise VaultPathError(
                    t("ui.vault.write_failed", path=canonical, error=exc)
                ) from exc

    def init_skeleton(self) -> list[str]:
        """建立空 vault 的骨架（目录 + `sections.yaml` + 一份占位 README）。

        **不预置任何"内容"**：把一个空骨架写得像档案，会让人分不清
        "这是模型整理的"还是"这是程序编的"。README 的正文由 dream agent 写。
        ADR-104 起同时建 `sections.yaml`（固定 6 个 section 角色 + 注入 cap），
        这样第一次整理就按原生格式生成 README/index。

        占位 README 带 `status: placeholder` 标记，`readme_text()` 认这个标记
        并拒绝注入 —— 否则一旦 vault 有了真档案，注入给模型的会是
        "（还没有整理过任何对话）"这句程序自己写下的话（审查实测到过）。
        """
        created: list[str] = []
        for directory in _TOP_DIRS:
            target = self.root / directory
            if not target.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                created.append(directory + "/")
        # **绝不把已有内容的旧 vault 变成原生模式**：`sections.yaml` 是模式开关，
        # 在 `--vault-init` 里顺手加一份会让下一次保存把模型写的 README 换成生成物。
        # 但仍允许它补齐目录/占位 README（与旧行为一致）。
        if not (self.root / SECTIONS_CONFIG).is_file() and not self.has_content():
            self.write_manifest(default_manifest())
            created.append(SECTIONS_CONFIG)
        if not (self.root / README_NAME).is_file():
            self.write_file(
                README_NAME,
                f"{_FRONTMATTER_FENCE}\nstatus: {PLACEHOLDER_STATUS}\n"
                f"{_FRONTMATTER_FENCE}\n\n"
                + t("ui.vault.placeholder_readme")
                + "\n",
            )
            created.append(README_NAME)
        return created

    # ------------------------------------------------------------------
    # 生成物：README.md / index.md / sections/earlier_months.md（ADR-104）
    #
    # 原生 vault 的这些文件是**投影**：sections 才是 canonical。模型只写
    # sections/entities，harness 在每次整理结束（以及网页界面保存后）重建。
    # ------------------------------------------------------------------

    def _front_of(self, rel: str) -> Frontmatter:
        try:
            path = self._resolve_canonical(normalize_rel_path(rel, name_max_chars=120, allow_nested=True))
            text = path.read_text(encoding="utf-8")
        except (VaultPathError, OSError, UnicodeDecodeError):
            return Frontmatter()
        front, _, _ = parse_frontmatter(text)
        return front

    def _section_body(self, rel: str) -> str:
        """读一个 section 的正文，去掉开头的单个 H1（README 用 `## 标题`）。"""
        try:
            path = self._resolve_canonical(normalize_rel_path(rel, name_max_chars=120, allow_nested=True))
            text = path.read_text(encoding="utf-8")
        except (VaultPathError, OSError, UnicodeDecodeError):
            return ""
        _front, body, _ = parse_frontmatter(text)
        lines = body.lstrip("\n").splitlines()
        if lines and re.match(r"^#\s+", lines[0].strip()):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines.pop(0)
        return "\n".join(lines).strip()

    def render_earlier_months(self) -> str:
        """由 `sections/archived_month/<YYYY-MM>/this_month.md` 拼出 earlier_months。

        新的月份在前（原生只有一份，看不出顺序；按"最近的更相关"排）。
        没有归档时才写 `No Earlier Months yet`（原生那句占位）。
        """
        archive = self.root / SECTION_DIR / ARCHIVE_DIR
        blocks: list[str] = []
        if archive.is_dir():
            months = [
                item
                for item in archive.iterdir()
                if item.is_dir() and _ARCHIVE_MONTH_RE.match(item.name)
            ]
            for month in sorted(months, key=lambda p: p.name, reverse=True):
                body = self._section_body(
                    f"{SECTION_DIR}/{ARCHIVE_DIR}/{month.name}/{ARCHIVE_FILE}"
                )
                if body:
                    blocks.append(f"## {month.name}\n\n{body}")
        return "\n\n".join(blocks) if blocks else "No Earlier Months yet"

    def _top_tags(self, limit: int = 10) -> list[str]:
        counts: dict[str, int] = {}
        for rel in self._iter_rels():
            if not rel.startswith(f"{ENTITY_DIR}/"):
                continue
            for tag in self._front_of(rel).tags:
                counts[tag] = counts.get(tag, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [tag for tag, _count in ordered[:limit]]

    def _readme_parts(self, *, today: str = "", last_dream: str = "") -> tuple[str, str]:
        """返回 (frontmatter 文本, body)。**纯函数**（只读磁盘，不写）。"""
        manifest = self.manifest()
        old = self._front_of(README_NAME)
        today_s = today or time.strftime("%Y-%m-%d")
        created = old.created or _scalar_text(old.extra.get("created", "")) or today_s
        last = (
            last_dream
            or _scalar_text(old.extra.get("last_dream", ""))
            or old.last_updated
            or old.updated
            or today_s
        )
        user_id = _scalar_text(old.extra.get("user_id", "")) or (
            "local-" + uuid.uuid5(uuid.NAMESPACE_URL, str(self.root.resolve())).hex[:12]
        )
        last_avatar = _scalar_text(old.extra.get("last_avatar", "")) or "never"
        tags = list(old.tags) or ["user-profile"]
        section_names = [Path(spec.path).stem for spec in manifest.sections]
        top_tags = self._top_tags()

        blocks: list[str] = []
        for spec in manifest.sections:
            if spec.path == f"{SECTION_DIR}/earlier_months.md":
                body = self.render_earlier_months()
            else:
                try:
                    exists = self._resolve_canonical(spec.path).is_file()
                except VaultPathError:
                    exists = False
                if not exists:
                    continue
                body = self._section_body(spec.path)
            blocks.append(f"## {spec.title}\n\n{body}".rstrip())
        body_text = "\n\n".join(blocks)
        chars = len(body_text.encode("utf-8"))

        lines = [
            _FRONTMATTER_FENCE,
            f"user_id: {user_id}",
            f'created: "{created}"',
            f'last_dream: "{last}"',
            f'last_updated: "{today_s}"',
            f"last_avatar: {last_avatar}",
            f"chars: {chars}",
            "sections:",
            *[f"  - {name}" for name in section_names],
            "tags:",
            *[f"  - {tag}" for tag in tags],
        ]
        if top_tags:
            lines.append("top_tags:")
            lines.extend(f"  - {tag}" for tag in top_tags)
        lines.append(_FRONTMATTER_FENCE)
        return "\n".join(lines), body_text

    def render_readme(self, *, today: str = "", last_dream: str = "") -> str:
        front, body = self._readme_parts(today=today, last_dream=last_dream)
        return front + "\n\n" + body + "\n"

    def render_index(self) -> str:
        """生成 `index.md`：sections / entities / other files。"""
        manifest = self.manifest()
        titles = {spec.path: spec.title for spec in manifest.sections}
        section_rows: list[tuple[str, str]] = []
        entity_rows: list[tuple[str, str]] = []
        other_rows: list[tuple[str, str]] = []
        for rel in self._iter_rels():
            if rel.startswith(f"{SECTION_DIR}/"):
                if rel.startswith(f"{SECTION_DIR}/{ARCHIVE_DIR}/"):
                    title = Path(rel).parent.name
                else:
                    title = titles.get(rel) or Path(rel).stem
                section_rows.append((rel, title))
            elif rel.startswith(f"{ENTITY_DIR}/"):
                front = self._front_of(rel)
                entity_rows.append((rel, front.name or front.title or Path(rel).stem))
            else:
                front = self._front_of(rel)
                other_rows.append((rel, front.title or front.name or Path(rel).stem))
        lines = [
            "# Vault Index",
            "",
            "> Generated from canonical vault files. Relationships are maintained in markdown links and indexed by write tools.",
            "",
            "## Sections",
            "",
        ]
        for rel, title in sorted(section_rows):
            lines.append(f"- [[{rel[:-3]}|{title}]]")
        lines.extend(["", "## Entities", ""])
        for rel, title in sorted(entity_rows):
            lines.append(f"- [[{rel[:-3]}|{title}]]")
        if (self.root / OBSERVATIONS_NAME).is_file():
            other_rows.append((OBSERVATIONS_NAME, "Observations"))
        if other_rows:
            lines.extend(["", "## Other Files", ""])
            for rel, title in sorted(other_rows):
                lines.append(f"- [[{rel[:-3]}|{title}]]")
        return "\n".join(lines) + "\n"

    def rebuild_generated(
        self, *, today: str = "", last_dream: str = ""
    ) -> dict[str, Any]:
        """重建 README / index / earlier_months。**旧 vault 不做迁移。**

        只有原生模式（有 `sections.yaml`）才重建。旧 vault 继续用模型写的
        README，避免一次整理把用户的索引换掉。
        """
        if not self.is_native():
            return {"rebuilt": False, "reason": "legacy"}
        original_manifest = self.manifest()
        earlier_rel = f"{SECTION_DIR}/earlier_months.md"
        required = {path for path, _title in DEFAULT_SECTION_SPECS}
        kept_specs: list[SectionSpec] = []
        for spec in original_manifest.sections:
            try:
                exists = self._resolve_canonical(spec.path).is_file()
            except VaultPathError:
                exists = False
            # 固定 section 缺失要留给 check() 报出来；自定义 section 已被会话
            # 删除时则要从清单移除，否则每次重建都会制造一条永久 stale 记录。
            if exists or spec.path in required or spec.path == earlier_rel:
                kept_specs.append(spec)
        known = {spec.path for spec in kept_specs}
        extra: list[SectionSpec] = []
        for rel in self._iter_rels():
            if (
                rel.startswith(f"{SECTION_DIR}/")
                and not rel.startswith(f"{SECTION_DIR}/{ARCHIVE_DIR}/")
                and rel not in known
                and rel != earlier_rel
            ):
                extra.append(SectionSpec(rel, Path(rel).stem))
                known.add(rel)
        # `earlier_months` 是原生合同里固定的一项；自定义清单漏了它时补上，
        # 否则第一次 rebuild 写了文件、却要等下一次才进 README。
        if earlier_rel not in known:
            extra.append(SectionSpec(earlier_rel, "Earlier Months"))
            known.add(earlier_rel)
        manifest = replace(original_manifest, sections=(*kept_specs, *extra))
        if manifest.sections != original_manifest.sections:
            self.write_file(
                SECTIONS_CONFIG, dump_sections_manifest(manifest), generated=True
            )
        earlier = self.render_earlier_months() + "\n"
        self.write_file(
            f"{SECTION_DIR}/earlier_months.md", earlier, generated=True
        )
        readme = self.render_readme(today=today, last_dream=last_dream)
        self.write_file(README_NAME, readme, generated=True)
        index = self.render_index()
        self.write_file(INDEX_NAME, index, generated=True)
        return {
            "rebuilt": True,
            "readme_chars": len(readme),
            "sections": [spec.path for spec in manifest.sections],
        }

    # ------------------------------------------------------------------
    # 机械校验（README 由 harness 生成；结构必须由机械规则守住）
    # ------------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """读一遍 vault，报告"结构上明显不对"的地方。**只读**。

        原生模式（有 `sections.yaml`）下，README/index/earlier_months 是生成物：
        这里除了原有的坏 frontmatter / 空文件 / 孤儿 / 命名撞车之外，还会核对
        *清单里的 section 是否都有文件*、*生成物是否是最新的*、*raw 引用是否存在*。
        旧 vault（没有清单）保持原来的检查口径，不强行迁移。
        """
        problems: list[str] = []
        warnings: list[str] = []
        if not self.root.is_dir():
            return {
                "exists": False,
                "files": 0,
                "problems": [t("ui.vault.no_vault_dir")],
                "warnings": [],
                "readme_tokens": 0,
                "readme_truncated": False,
            }

        native = self.is_native()
        manifest, manifest_warnings = self.manifest_report()
        warnings.extend(manifest_warnings)

        entries = self._read_entries(None)
        by_path = {entry.path: entry for entry in entries}
        archives = self._iter_rels()
        real_archives = [
            rel for rel in archives if rel != f"{SECTION_DIR}/earlier_months.md"
        ]
        for rel in archives:
            if rel not in by_path:
                problems.append(t("ui.vault.read_failed", path=rel))
        if len(archives) > self.max_files:
            problems.append(
                t(
                    "ui.vault.over_display_limit",
                    count=len(archives),
                    limit=self.max_files,
                )
            )
        problems.extend(self.find_name_collisions())

        # 手工放进来的坏文件名：不走写入路径，所以 `_iter_rels()` 会跳过它们；
        # 校验必须自己扫一遍磁盘，否则"手放一个超长/非法名字"会完全看不见。
        for directory in _TOP_DIRS:
            folder = self.root / directory
            if not folder.is_dir():
                continue
            for current, dirs, files in os.walk(folder, followlinks=False):
                dirs[:] = [
                    name
                    for name in dirs
                    if not os.path.islink(os.path.join(current, name))
                ]
                for name in files:
                    if not name.endswith(".md"):
                        continue
                    full = Path(current) / name
                    if os.path.islink(full):
                        continue
                    rel = full.relative_to(self.root).as_posix()
                    if rel in archives:
                        continue
                    try:
                        normalize_rel_path(
                            rel, name_max_chars=self.name_max_chars, allow_nested=True
                        )
                    except VaultPathError as exc:
                        problems.append(
                            t("ui.vault.bad_file_name", path=rel, error=exc)
                        )

        raw_targets: set[str] = set()
        for rel, path in self._iter_paths():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            front, body, item_warnings = parse_frontmatter(text)
            for warning in item_warnings:
                problems.append(f"{rel}：{warning}")
            if not body.strip() and rel != OBSERVATIONS_NAME:
                if front.title or front.name:
                    problems.append(t("ui.vault.empty_body_with_title", path=rel))
                else:
                    problems.append(t("ui.vault.empty_file", path=rel))
            for target in re.findall(r"!\[\[([^\[\]]{1,200})\]\]", body):
                raw_target = target.split("|", 1)[0].strip()
                raw_targets.add(raw_target)
                try:
                    raw_path = normalize_raw_path(
                        raw_target, name_max_chars=self.name_max_chars * 3
                    )
                except VaultPathError as exc:
                    problems.append(
                        t("ui.vault.raw_missing", path=rel, raw=raw_target, error=exc)
                    )
                    continue
                if not self._resolve_canonical(raw_path).is_file():
                    problems.append(
                        t("ui.vault.raw_missing", path=rel, raw=raw_target, error="missing")
                    )

        readme_body = ""
        readme_injected = ""
        readme_text = ""
        truncated = False
        char_cap = 0
        readme_path = self.root / README_NAME
        if readme_path.is_file():
            try:
                readme_text = readme_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                readme_text = ""
            _front, readme_body, _ = parse_frontmatter(readme_text)
        if native:
            char_cap = manifest.inject_char_cap
            readme_injected, truncated = self.readme_text()
            if archives and not readme_injected:
                if self._readme_is_placeholder():
                    problems.append(t("ui.vault.readme_is_placeholder"))
                else:
                    problems.append(t("ui.vault.readme_missing"))
            if truncated:
                problems.append(
                    t("ui.vault.readme_over_char_budget", budget=char_cap)
                )
            if readme_body and not self._readme_is_placeholder():
                meta = _native_meta(readme_text)
                expected_readme = self.render_readme(
                    today=meta.get("last_updated", "") or time.strftime("%Y-%m-%d"),
                    last_dream=meta.get("last_dream", ""),
                )
                _efront, expected_body, _ = parse_frontmatter(expected_readme)
                stale = expected_body.strip() != readme_body.strip()
                if not stale:
                    stale = meta.get("chars") not in (
                        "",
                        str(len(expected_body.encode("utf-8"))),
                    )
                if not stale:
                    actual_front, _, _ = parse_frontmatter(readme_text)
                    actual_sections = [
                        str(item) for item in (actual_front.extra.get("sections") or [])
                    ]
                    expected_sections = [
                        Path(spec.path).stem for spec in manifest.sections
                    ]
                    stale = actual_sections != expected_sections
                if stale:
                    problems.append(t("ui.vault.readme_stale"))
            if real_archives:
                for spec in manifest.sections:
                    try:
                        exists = self._resolve_canonical(spec.path).is_file()
                    except VaultPathError:
                        exists = False
                    if not exists and not self._readme_is_placeholder():
                        problems.append(t("ui.vault.section_missing", path=spec.path))
                index_path = self.root / INDEX_NAME
                if not index_path.is_file():
                    problems.append(t("ui.vault.index_missing"))
                else:
                    try:
                        actual_index = index_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        actual_index = ""
                    if actual_index != self.render_index():
                        problems.append(t("ui.vault.index_stale"))
                earlier_path = self.root / SECTION_DIR / "earlier_months.md"
                try:
                    actual_earlier = earlier_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    actual_earlier = ""
                if actual_earlier.strip() != self.render_earlier_months().strip():
                    problems.append(t("ui.vault.earlier_months_stale"))
        else:
            readme_injected, truncated = self.readme_text()
            if archives and not readme_injected:
                if self._readme_is_placeholder():
                    problems.append(t("ui.vault.readme_is_placeholder"))
                else:
                    problems.append(t("ui.vault.readme_missing"))
            if truncated:
                problems.append(
                    t("ui.vault.readme_over_budget", budget=self.readme_token_budget)
                )

        # 孤儿：原生模式下看**链接图**（"有没有被 sections/entities 链接到"），
        # 旧模式仍看 README 文字。原生 README 是 sections 的投影，拿它判孤儿
        # 会把"只在正文里被提到、没进 README 的实体"误报。
        if readme_body:
            if native:
                index = self._link_index(limit=self.max_files * 4)
                linked: set[str] = set()
                for info in index.values():
                    linked.update(info.get("links", []))
                for entry in entries:
                    if not entry.path.startswith(f"{ENTITY_DIR}/"):
                        continue
                    names = {
                        normalize_name(entry.name or ""),
                        normalize_name(Path(entry.path).stem),
                    }
                    names.discard("")
                    if not (names & linked):
                        problems.append(t("ui.vault.orphan", path=entry.path))
            else:
                for entry in entries:
                    if not entry.path.startswith(f"{ENTITY_DIR}/"):
                        continue
                    stem = Path(entry.path).stem
                    if stem not in readme_body and entry.path not in readme_body:
                        problems.append(t("ui.vault.orphan", path=entry.path))

        return {
            "exists": True,
            # 口径与 count_files() 一致：README 是索引，不算档案
            "files": len(archives),
            "has_readme": bool(readme_body),
            "readme_tokens": estimate_tokens(readme_injected),
            "readme_chars": len(readme_injected),
            "readme_total_chars": len(readme_body),
            "readme_truncated": truncated,
            "readme_budget": char_cap if native else self.readme_token_budget,
            "readme_char_cap": char_cap,
            "manifest": (
                {
                    "inject_char_cap": manifest.inject_char_cap,
                    "sections": [
                        {"path": spec.path, "title": spec.title}
                        for spec in manifest.sections
                    ],
                }
                if native
                else None
            ),
            "raw_files": len(
                [rel for rel in self._iter_raw() if not rel.endswith(RAW_META_SUFFIX)]
            ),
            "problems": problems,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _read_entries(self, limit: int | None) -> list[VaultEntry]:
        """读一遍被扫到的文件，组装成条目。

        `limit=None` 表示**不截断** —— `check()` 必须用这一档：
        拿"截断后的列表"和"没截断的名单"去比对，会把完全可读的文件
        报成"读取失败"（审查实测到过这个假警报）。
        """
        entries: list[VaultEntry] = []
        for rel, path in self._iter_paths():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            front, body, _ = parse_frontmatter(text)
            entries.append(
                VaultEntry(
                    path=rel,
                    title=front.title or front.name or Path(rel).stem,
                    # 原生实体没有 `kind`，类型靠 tags（person/place/…）；
                    # 旧档案仍优先用它自己的 `kind`。
                    kind=front.kind or (front.tags[0] if front.tags else ""),
                    chars=len(body),
                    summary=_summary_of(body),
                    name=front.name,
                    tags=front.tags,
                    created=front.created,
                    last_updated=front.last_updated or front.updated,
                    place=front.place,
                )
            )
        entries.sort(key=lambda entry: entry.path)
        return entries if limit is None else entries[:limit]

    def _iter_rels(self) -> list[str]:
        """枚举 vault 里**确实位于 vault 内**的档案（全局排序，结果稳定）。

        ADR-104 的月份归档与 ADR-111 的会话嵌套目录都支持。所有条目仍然过
        `_resolve_canonical` —— 符号链接/junction 走出 vault 的一律跳过
        （四个扫描 list / search / neighborhood / check 都走这里）。
        """
        rels: list[str] = []
        if not self.root.is_dir():
            return rels

        def add(rel: str) -> None:
            try:
                canonical = normalize_rel_path(
                    rel, name_max_chars=self.name_max_chars, allow_nested=True
                )
                self._resolve_canonical(canonical)  # 与写入同一道闸
            except VaultPathError:
                return
            rels.append(canonical)

        # 显式排除链接/junction/reparse point；raw 是素材区，不把其中的文本当档案。
        # 根级 README/index/log/observations/memory 由各自通道负责，自定义根级 Markdown
        # 与自定义目录则和 sections/entities 一样进入列表、检索和索引。
        protected_roots = {
            _path_key(path) for path in (*ROOT_MD_FILES, SECTIONS_CONFIG)
        }
        for current, dirs, files in os.walk(self.root, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                name
                for name in dirs
                if not _is_link_or_reparse(current_path / name)
                and not (
                    current_path == self.root
                    and _path_key(name) == _path_key(RAW_DIR)
                )
            )
            for name in sorted(files):
                path = current_path / name
                if not name.endswith(".md") or _is_link_or_reparse(path) or not path.is_file():
                    continue
                rel = path.relative_to(self.root).as_posix()
                if "/" not in rel and _path_key(rel) in protected_roots:
                    continue
                add(rel)

        rels.sort()
        # 硬上限只为"别让一次扫描把机器拖死"存在，比展示上限宽 4 倍；
        # 展示层的上限由 list_files / search 各自去管。
        return rels[: max(1, self.max_files) * 4]

    def _iter_raw(self) -> list[str]:
        """枚举 `raw/source_<ref>/` 下的图片与 `.meta.json`（只读）。"""
        root = self.root / RAW_DIR
        if not root.is_dir():
            return []
        rels: list[str] = []
        for source in sorted(root.iterdir()):
            if not source.is_dir() or not RAW_SOURCE_RE.match(source.name):
                continue
            for path in sorted(source.iterdir()):
                if not path.is_file():
                    continue
                rel = f"{RAW_DIR}/{source.name}/{path.name}"
                try:
                    normalize_raw_path(rel, name_max_chars=self.name_max_chars * 3)
                    self._resolve_canonical(rel)
                except VaultPathError:
                    continue
                rels.append(rel)
        return rels

    def _iter_paths(self) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        # README + observations 是"可读/可搜"的根级文件；index/log/memory_instruction
        # 不进检索与邻域（它们是生成物/日志/旁路导出）。
        for rel in (README_NAME, OBSERVATIONS_NAME):
            path = self.root / rel
            if path.is_file():
                out.append((rel, path))
        for rel in self._iter_rels():
            out.append((rel, self.root / rel))
        return out

    def _link_index(self, *, limit: int) -> dict[str, dict[str, Any]]:
        """一次扫描建好的邻接索引：path -> {title, names, links}。"""
        index: dict[str, dict[str, Any]] = {}
        for rel, path in self._iter_paths()[:limit]:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            front, body, _ = parse_frontmatter(text)
            title = front.title or front.name or Path(rel).stem
            names = front.names() or [normalize_name(title)]
            index[rel] = {
                "title": title,
                "names": names,
                "links": _wikilinks(body),
            }
        return index

    @staticmethod
    def _name_to_path(name: str, index: dict[str, dict[str, Any]]) -> str:
        """双链目标 → 文件路径。名称按 `name/title/alias/文件名` **精确匹配**。

        ADR-104 起还接受两种原生写法：
          * `[[entities/slug|显示名]]` —— 取 `|` 前的路径；
          * `[[sections/archived_month/2026-09/this_month]]` —— 允许省略 `.md`。

        仍然**不做**宽容修正（不折大小写、不去前缀）：拼错就显示成"还没写"，
        而不是悄悄指向一个"差不多"的档案。
        """
        raw = str(name).split("|", 1)[0].strip()
        wanted = normalize_name(raw)
        if "/" in raw:
            candidate = raw if raw.lower().endswith(".md") else f"{raw}.md"
            try:
                canonical = normalize_rel_path(
                    candidate, name_max_chars=120, allow_nested=True
                )
            except VaultPathError:
                return ""
            return canonical if canonical in index else ""
        for rel, info in sorted(index.items()):
            if wanted in set(info.get("names", [])):
                return rel
            if normalize_name(Path(rel).stem) == wanted:
                return rel
        return ""

    def _readme_is_placeholder(self) -> bool:
        """README 是不是**程序自己写的占位**（或被人/模型标成了占位）。

        单独一个方法，因为"占位"与"没有 README"在 `check()` 里要说成两句不同的话：
        后者的后果是"看不到索引"，前者是"永久一个字都不注入"。
        """
        path = self.root / README_NAME
        if not path.is_file():
            return False
        try:
            front, _, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return False
        return front.status.strip().lower() == PLACEHOLDER_STATUS

    def find_name_collisions(self) -> list[str]:
        """在**大小写不敏感**的文件系统上会互相覆盖的文件名。

        这是 Windows 上真实存在的坑：`Foo.md` 与 `foo.md` 是同一个文件，
        模型以为建了两个实体，实际只有一个（后写的把先写的覆盖了）。
        也顺手覆盖"名字只差首尾空白"这一类 —— 它们在路径校验里已经被拒，
        但用户手工放进来的文件不会经过那一层。
        """
        seen: dict[str, str] = {}
        collisions: list[str] = []
        for rel in self._iter_rels():
            key = rel.casefold()
            if key in seen and seen[key] != rel:
                collisions.append(
                    t("ui.vault.name_collision", first=seen[key], second=rel)
                )
            else:
                seen[key] = rel
        return collisions


def _truncation_notice() -> str:
    """README 被预算截断时补上的那句提示（**发给模型**，所以也要能翻）。

    **是函数而不是常量**：文案跟着当前语言走，而语言是运行期可切的
    （`docs/12-i18n.md` §3）—— 模块级常量会在 import 那一刻就定死。
    """
    return t("ui.vault.readme_truncated")


def _fit_tokens(text: str, budget: int) -> tuple[str, bool]:
    """按**段落**截断到预算以内，并且**保证返回的文本真的在预算内**。

    两条容易写错的地方：

    1. `estimate_tokens` 的性质是"中文 1 字 1 token、其余 4 字符 1 token"，
       所以 `len(text)` 永远是 token 数的**上界** —— 按字符数切一定不会超预算。
       第一版在"第一段就超预算"时取了 `budget * 2` 个字符，
       对中文来说那正好是**预算的两倍** —— 于是"上限"在最需要它的那条路径上失效了。
    2. **截断提示本身也要算进预算**。只切正文、最后再补一句提示，
       结果就是"截断了，但总量仍然超" —— 与不截断只差一点。

    预算的最小可用值约为 90 token：截断提示必须留下（模型得知道内容被截了），
    比这更小的预算不予保证。
    """
    if not text:
        return "", False
    if estimate_tokens(text) <= budget:
        return text, False

    notice = _truncation_notice()
    notice_cost = estimate_tokens(notice)
    room = max(40, budget - notice_cost)
    kept: list[str] = []
    for block in (block for block in text.split("\n\n") if block.strip()):
        if estimate_tokens(block) > room:
            # 这一段自己就放不下：一段都不留，改走"按字符切"的兜底
            break
        if kept and estimate_tokens("\n\n".join([*kept, block])) > room:
            break
        kept.append(block)
    if kept:
        return "\n\n".join(kept) + notice, True
    return text[:room] + notice, True


def _fit_chars(text: str, cap: int) -> tuple[str, bool]:
    """按**段落**截断到字符上限以内（原生 `inject_char_cap`）。

    与 `_fit_tokens` 同样的两条约束：
      1. 截断提示本身也算进 cap（否则"截断了但总量还是超"）；
      2. cap 为 0 表示**不设上限**（原生清单里 0 更像"关掉这道闸"）。
    """
    if not text:
        return "", False
    if cap <= 0 or len(text) <= cap:
        return text, False
    notice = _truncation_notice()
    if len(notice) >= cap:
        return text[:cap], True
    room = max(1, cap - len(notice))
    kept: list[str] = []
    for block in (block for block in text.split("\n\n") if block.strip()):
        if len(block) > room:
            break
        if kept and len("\n\n".join([*kept, block])) > room:
            break
        kept.append(block)
    if kept:
        return "\n\n".join(kept) + notice, True
    return text[:room] + notice, True


def _snippet(body: str, query: str) -> str:
    """检索结果里那一行"为什么它被选中"的证据。"""
    chars = {ch for ch in query if ch.strip()}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line and any(ch in line for ch in chars):
            return line[:120]
    return _summary_of(body)


# --------------------------------------------------------------------------
# 差异：整理流水线用到的两个动作
#
# **这里没有"回滚"，也没有"预演"**（ADR-065 / ADR-072）：整理是单向门、改完不可逆，
# 所以不留快照、也不复制副本。留下的只有 `vault_image()`（运行前把 vault 读进**内存**，
# 只用来算"这次改了什么"，进程结束就没了）与 `diff_images()`。
# 要重来就"格式化 dream 层再整理一遍"（`format_dream`）——
# 档案是会话的投影，重做是可行的；代价是再花一次整理的钱。
# --------------------------------------------------------------------------


def merge_frontmatter(existing: Frontmatter, incoming: Frontmatter) -> Frontmatter:
    """把模型这次给的元信息合并到已有元信息上。

    规则是"只覆盖它明确给了的字段"：模型只想改正文时不该把 `tags` 清空，
    而它没提到、但**你手工加过**的键（`pinned: true` 或原生的 `date`）必须原样留着。
    `created` 永远保留第一次写入的值；`last_updated` 由调用方每次戳今天。
    """
    return Frontmatter(
        name=incoming.name or existing.name,
        title=incoming.title or existing.title,
        kind=incoming.kind or existing.kind,
        relation=incoming.relation or existing.relation,
        status=incoming.status or existing.status,
        created=incoming.created or existing.created,
        last_updated=incoming.last_updated or existing.last_updated,
        updated=incoming.updated or existing.updated,
        place=incoming.place or existing.place,
        tags=incoming.tags or existing.tags,
        aliases=incoming.aliases or existing.aliases,
        sources=incoming.sources or existing.sources,
        extra={**existing.extra, **incoming.extra},
    )


def vault_files_on_disk(root: Path) -> list[str]:
    """磁盘上真实存在的 `.md` **普通文件**（不跟随目录链接，也不收符号链接文件）。

    刻意不用 `_iter_rels()`：那个函数服务于"读档案"，会把不合规的名字过滤掉；
    而复制/比较必须把**任何**文件都算上，否则预演报出来的差异会漏东西。

    **符号链接一律不收**（目录或文件）：`copy2` 会跟着文件链接把内容复制进来，
    于是 vault 外面那份文件的内容就变成了副本里的一份普通文件。
    我们不管理链接文件，也不假装管理过。
    """
    root = Path(root)
    if not root.is_dir():
        return []
    found: list[str] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            name for name in dirs if not os.path.islink(os.path.join(current, name))
        ]
        for name in files:
            if not name.endswith(".md") or name.startswith("."):
                continue  # 点开头的是正在写入的临时文件，不该进副本
            full = os.path.join(current, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root)
            found.append(rel.replace(os.sep, "/"))
    found.sort()
    return found


def copy_vault(root: Path, dest: Path) -> list[str]:
    """〔已删除〕这个函数只服务于"预演"，而预演已随 ADR-072 取消。

    留一个会抛错的存根而不是直接删掉，是为了让"还有调用点"这件事**立刻炸出来**
    （比 `NameError`/`ImportError` 更说明问题）。没有任何生产代码调用它。
    """
    raise NotImplementedError(t("ui.vault.copy_cancelled"))


def _clear_frontmatter_fields(front: Frontmatter, keys: Sequence[str]) -> Frontmatter:
    """把明确点名要清空的字段置空（其余原样）。

    只清**认识的字段名**（原生 `name/created/last_updated/place` 也算）。
    `relation` 可能是 `extra` 里的列表形态，点名清它时一并移除；`date` 等
    其它手写键不动 —— "删掉你手写的一个键"是另一件事。
    """
    out = front
    for key in keys:
        name = str(key).strip()
        if name in (
            "name",
            "title",
            "kind",
            "relation",
            "status",
            "created",
            "last_updated",
            "updated",
            "place",
        ):
            out = replace(out, **{name: ""})
            if name in out.extra:
                extra = dict(out.extra)
                extra.pop(name, None)
                out = replace(out, extra=extra)
        elif name in ("tags", "aliases", "sources"):
            out = replace(out, **{name: ()})
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def vault_image(root: Path) -> dict[str, str]:
    """把 vault 读成一份"内存影像"：`相对路径 → 全文`。

    **这是"运行前是什么样"的唯一记录，而且它只在内存里。** ADR-065 取消了回滚，
    所以绝不能落盘成快照（那又是备份了）；但差异总得有个"之前"可比 ——
    在内存里留一份就够了：进程结束它自然就没了，谁也拿不回去。

    内存占用有上界：`vault_max_files × vault_write_max_chars`（默认 400 × 12000 ≈ 4.8MB
    的最坏情况，正常一个 vault 是几百 KB）。
    """
    root = Path(root)
    return {rel: _read_text(root / rel) for rel in vault_files_on_disk(root)}


def diff_images(
    after: Mapping[str, str], before: Mapping[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """比较两份影像：新增 / 改动 / 删除。按路径排序，结果稳定。

    预演与正式运行**用同一个函数**：区别只是"之后"那份影像是
    从一次性副本读的、还是从真 vault 读的。
    """
    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for rel in sorted(after):
        text = after[rel]
        if rel not in before:
            front, body, _ = parse_frontmatter(text)
            added.append(
                {
                    "path": rel,
                    "title": front.title or front.name or Path(rel).stem,
                    "chars": len(body),
                    "summary": _summary_of(body),
                }
            )
        elif before[rel] != text:
            front, body, _ = parse_frontmatter(text)
            changed.append(
                {
                    "path": rel,
                    "chars_before": len(before[rel]),
                    "chars_after": len(text),
                    # 给 `--dream-review` 用：现在没有"改动前"可比了（ADR-065），
                    # 所以至少把**改完之后**长什么样给你一眼。
                    "summary": _summary_of(body),
                }
            )
    for rel in sorted(before):
        if rel not in after:
            removed.append({"path": rel, "chars": len(before[rel])})
    return {"added": added, "changed": changed, "removed": removed}


def file_preview(root: Path, rel: str, *, limit: int = 400) -> str:
    """一份档案现在长什么样（截断）。给 `--dream-review` 看模型写了什么。"""
    text = _read_text(Path(root) / rel)
    if len(text) <= limit:
        return text.strip()
    return text[:limit].strip() + f"\n…（还有 {len(text) - limit} 字符未显示）"
