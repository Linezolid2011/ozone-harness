"""Dream vault：模型可以"自己走进去找"的那一层背景档案（M7）。

## 它在两个记忆系统里的位置

    memory instructions        用户明说 → 每条每轮都注入 → 只由对话模型写 → 可信度最高
    dream vault（本模块）       后台自动巩固 → 只有 README 每轮注入，
                              细节靠模型用工具进来读 → 可信度次之

两套东西**必须是两个存储**：把整理者的写权限伸进"用户明确要求记住"的那张表，
等于让记录者随时能重写历史。所以 vault 是目录，memory 是 SQLite，互不相干。

## README 是投影，原始记录才是真相来源

vault 里的东西全是从会话记录（append-only JSONL）压缩出来的**派生物**。
所以 vault 整个删掉是可以重建的 —— 这与 ADR-006 是同一个哲学：
**投影可以丢，真相来源不能丢。** 这也正是敢让模型自由写 vault 的底气。

## 写入口只有一个，而且校验在 store 层

本模块给对话 agent 的只有"读"（`tools/vault_tools.py`）。
写 vault 的只有 dream agent，但它也必须走 `VaultStore.write_file()` ——
**路径校验在 store 层实现，不在工具层**，所以将来任何新增的写入口都不可能绕过它。

## 一条贯穿全模块的规则：路径必须自己归一化，并且 fail-closed

与 ADR-052 同源：**不要指望平台替你检查。** 这里是模型第一次能写"文件路径"
（以前它只能写 SQLite 里的行，每次写入都过 `MemoryStore` 的校验函数），
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

# 程序自己写的占位 README 带的标记。**由程序写、由程序认** ——
# 不去正文里找"还没有整理过任何对话"这句话（那是靠字符串匹配，改一个字就失效）。
PLACEHOLDER_STATUS = "placeholder"

# 大小写不敏感的文件系统（Windows 与默认配置的 macOS）。
# Linux 上 `Foo.md` 与 `foo.md` 可以合法并存，所以那边的撞名检查要关掉，否则是误伤。
_CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"

# 只允许这两层，而且**只允许这两个名字**（小写）。
# 扁平结构和"深度恰好为 2"一起，顺手灭掉了嵌套目录带来的大半路径问题。
_TOP_DIRS = (SECTION_DIR, ENTITY_DIR)

# Windows 保留设备名。**判断的是第一个点之前的那一段**：
# `NUL.md` 与 `NUL` 等价，都是设备。
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)

_ILLEGAL_CHARS = frozenset('<>:"|?*')  # 斜杠在切分阶段就处理掉了

_WIKILINK = re.compile(r"\[\[([^\[\]]{1,80})\]\]")

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


def normalize_rel_path(raw: str, *, name_max_chars: int = 40) -> str:
    """把模型给的一个路径字符串，规范化成 vault 内的相对路径；不合法就抛异常。

    接受且只接受三种形式：

        README.md
        sections/<名字>.md
        entities/<名字>.md

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
        # README 只有一个，而且 Windows 上 `readme.md` / `README.MD` 本来就是
        # 同一个文件，所以这里**折叠**成规范写法；实体文件则要求严格小写后缀
        # （见 _check_name）—— 那边有几百个文件，宽松会带来"两个实体互相覆盖"。
        if parts[0].lower() == README_NAME.lower():
            return README_NAME
        raise VaultPathError(
            t(
                "ui.vault.path_unknown_location",
                section_dir=SECTION_DIR,
                entity_dir=ENTITY_DIR,
                raw=repr(raw),
            )
        )

    if len(parts) > 2:
        raise VaultPathError(
            t(
                "ui.vault.path_too_deep",
                top_dirs="/".join(_TOP_DIRS),
                raw=repr(raw),
            )
        )

    directory, name = parts
    if directory != directory.lower() or directory not in _TOP_DIRS:
        raise VaultPathError(
            t(
                "ui.vault.path_bad_top",
                top_dirs=" / ".join(_TOP_DIRS),
                dir=repr(directory),
            )
        )
    _check_name(name, name_max_chars=name_max_chars, raw=raw)
    return f"{directory}/{name}"


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


def normalize_name(raw: str, *, name_max_chars: int = 40) -> str:
    """把实体名规范化（NFC），用于**比较**（反链、撞名检查）。

    不做大小写折叠：Windows 上大小写不敏感，但 Linux 上敏感，
    所以撞名检查单独做（见 `find_name_collisions`），而不是在这里悄悄折叠。
    """
    return unicodedata.normalize("NFC", raw.strip())


# --------------------------------------------------------------------------
# frontmatter：一个刻意做小的 YAML 子集
# --------------------------------------------------------------------------


@dataclass
class Frontmatter:
    """实体/段落的元信息。

    **刻意不用 PyYAML**：这里只需要"标量 + 字符串列表"，而 frontmatter 的内容
    有一半是模型生成的 —— 用一个完整的 YAML 解析器去接模型生成的文本，
    等于把一个规范里全是边角（锚点、标签、类型推断、`no` 被解析成 False……）
    的东西请进核心路径。这个子集小到能一眼看完，出错方式也就那么几种。

    `extra` 保留我们不认识的键，**原样回写**：用户手改 vault 时加的
    `pinned: true` 之类不能被我们悄悄吃掉。
    """

    title: str = ""
    kind: str = ""
    relation: str = ""
    status: str = ""
    updated: str = ""
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key in ("title", "kind", "relation", "status", "updated"):
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
        """这个元信息里所有"可能被别的文件引用"的名字（用于反链匹配）。"""
        out = [self.title, *self.aliases]
        return [normalize_name(name) for name in out if name.strip()]


_LIST_KEYS = {"tags", "aliases", "sources"}


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
    """极简解析：`key: value`，以及紧跟着的 `- item` 缩进列表。"""
    fields: dict[str, Any] = {}
    warnings: list[str] = []
    current: str | None = None
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is None:
                warnings.append(
                    t("ui.vault.frontmatter_list_without_key", line=stripped[:40])
                )
                continue
            bucket = fields.get(current)
            if not isinstance(bucket, list):
                bucket = []
                fields[current] = bucket
            bucket.append(_unquote(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            warnings.append(t("ui.vault.frontmatter_unparsed", line=stripped[:40]))
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            warnings.append(t("ui.vault.frontmatter_empty_key", line=stripped[:40]))
            continue
        current = key
        if value == "":
            fields[key] = ""  # 可能是标量，也可能是后面跟着的列表
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = (
                [_unquote(part.strip()) for part in inner.split(",") if part.strip()]
                if inner
                else []
            )
        else:
            fields[key] = _unquote(value)
    return fields, warnings


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


def _fields_to_frontmatter(fields: dict[str, Any]) -> Frontmatter:
    known: dict[str, Any] = {}
    extra: dict[str, str] = {}
    for key, value in fields.items():
        if key in ("title", "kind", "relation", "status", "updated") and not isinstance(
            value, list
        ):
            known[key] = str(value)
        elif key in _LIST_KEYS:
            if isinstance(value, list):
                known[key] = tuple(str(item) for item in value if str(item))
            elif value:
                known[key] = (str(value),)
        else:
            extra[key] = ", ".join(str(item) for item in value) if isinstance(
                value, list
            ) else str(value)
    return Frontmatter(extra=extra, **known)


def dump_frontmatter(front: Frontmatter) -> str:
    """规范化输出。**只写非空字段**，顺序固定 —— 于是同一个元信息永远得到同一段字节，
    模型改一个字就只有那一个字变（对 diff 与人工 review 都友好）。
    """
    lines = [_FRONTMATTER_FENCE]
    for key in ("title", "kind", "relation", "status", "updated"):
        value = getattr(front, key)
        if value:
            lines.append(f"{key}: {_quote_if_needed(value)}")
    for key in ("tags", "aliases", "sources"):
        values = getattr(front, key)
        if values:
            rendered = ", ".join(_quote_if_needed(item) for item in values)
            lines.append(f"{key}: [{rendered}]")
    for key, value in front.extra.items():
        lines.append(f"{key}: {value}")
    lines.append(_FRONTMATTER_FENCE)
    return "\n".join(lines)


def _quote_if_needed(value: str) -> str:
    if value == "" or value != value.strip() or any(
        ch in value for ch in ":#[]\"'\n"
    ):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "")
            .replace("\n", "\\n")  # 换行必须转义：frontmatter 是**按行**解析的
        )
        return f'"{escaped}"'
    return value


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
        return self.frontmatter.title or Path(self.path).stem


@dataclass
class VaultEntry:
    path: str
    title: str
    kind: str
    chars: int
    summary: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "title": self.title, "chars": self.chars}
        if self.kind:
            data["kind"] = self.kind
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
    """`[[小林]]` 形式的链接目标（保持出现顺序、去重）。"""
    seen: list[str] = []
    for match in _WIKILINK.findall(text):
        name = normalize_name(match)
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
        兜底永远 fail-closed —— 宁可报错，也不要写出去。
        """
        canonical = normalize_rel_path(rel, name_max_chars=self.name_max_chars)
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

    def resolve_friendly(self, raw: str) -> str:
        """宽松一点地理解模型给的路径，但**最终仍然只产出规范形式**。

        模型很自然会写 `dream_read("小林")`。这里按固定顺序补目录：
        先 entities、再 sections、最后按原样（README.md）。
        顺序固定 = 结果可预测；每次都返回规范路径，所以模型能学到正确写法。
        """
        try:
            return normalize_rel_path(raw, name_max_chars=self.name_max_chars)
        except VaultPathError:
            pass

        cleaned = str(raw).strip().lstrip("\ufeff").replace("\\", "/")
        if cleaned.lower() in (README_NAME.lower(), "readme"):
            return README_NAME
        # 已经带了目录但写错大小写之类：先按原样再试一次会掩盖问题，
        # 所以这里只处理"裸名字"，其余情况把原始错误抛出去。
        if "/" in cleaned:
            return normalize_rel_path(raw, name_max_chars=self.name_max_chars)

        base = cleaned if cleaned.lower().endswith(".md") else f"{cleaned}.md"
        # 扩展名统一成小写：`Foo.MD` 在 Windows 上就是 `Foo.md`，
        # 不归一化的话会拼出 `entities/Foo.MD.md` 这个根本不存在的路径
        # （然后模型得到"没有这个档案"，而它要找的文件其实就在那儿）。
        base = base[:-3] + ".md"
        for candidate in (f"{ENTITY_DIR}/{base}", f"{SECTION_DIR}/{base}"):
            try:
                canonical = normalize_rel_path(
                    candidate, name_max_chars=self.name_max_chars
                )
            except VaultPathError:
                continue
            resolved = self._resolve_canonical(canonical)
            if resolved.is_file():
                # 返回**磁盘上的名字**而不是模型写的那个：大小写不敏感的系统上
                # 它们指向同一个文件，但报给模型的名字应当是真实存在的那个
                # （否则模型会把 `entities/FOO.md` 当成规范写法记下来）。
                directory = canonical.rpartition("/")[0]
                return f"{directory}/{resolved.name}"
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

        判据是"sections/ 或 entities/ 里至少有一个文件"，而**不是**"README 在不在"。
        理由有两个：

        * `--vault-init` 建的骨架只有一份占位 README。按"README 在不在"判，
          那时会白白注册三个工具、还往上下文里注入一段"这里什么都没有"的 README；
        * README 是**档案的投影**，没有档案时它本来就没有可注入的内容。

        同样刻意**不读文件内容**：这个判断每轮都会做一次（Web 端每轮重建 Runtime），
        读一遍 vault 里每个文件在档案变多之后是纯浪费。所以只做一次目录扫描。
        """
        return bool(self._iter_rels())

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

    def read(self, rel: str, *, max_chars: int | None = None) -> VaultFile:
        """读一个文件。`max_chars` 只管正文，元信息永远完整给出。"""
        canonical = self.resolve_friendly(rel)
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
                [front.title, *front.aliases, *front.tags, body]
            )
            score = dice(grams, bigrams(haystack))
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "path": rel,
                        "title": front.title or Path(rel).stem,
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
        canonical = self.resolve_friendly(rel)
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

    def readme_text(self, *, token_budget: int | None = None) -> tuple[str, bool]:
        """README 的正文，以及"是否因为超预算被截断"。

        **截断必须由 harness 强制**：README 每轮都进上下文，靠提示词求模型
        写短一点是不可靠的 —— 一个啰嗦的 README 会让**此后每一轮**都变贵，
        而模型只在写它的那一次看到代价。
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
        if front.title and not body.strip():
            return "", False
        budget = self.readme_token_budget if token_budget is None else int(token_budget)
        return _fit_tokens(body.strip(), budget)

    # ------------------------------------------------------------------
    # 写（只有 dream agent 与 --vault-init 会用到）
    # ------------------------------------------------------------------

    def write_file(self, rel: str, text: str) -> str:
        """原子写入。返回规范化的相对路径。

        "先写临时文件再 `os.replace`"不是为了并发（本机单用户），而是为了
        **崩溃安全**：README 是每轮都要读的东西，半截写入会让它一直读到
        半个文件。临时文件放在同一个目录里，所以 `os.replace` 是同一卷内的
        原子改名。
        """
        canonical = normalize_rel_path(rel, name_max_chars=self.name_max_chars)
        path = self._resolve_canonical(canonical)
        payload = text if text.endswith("\n") else text + "\n"
        if len(payload) > self.write_max_chars:
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
        if creating and self.count_files() >= self.max_files:
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
        canonical = self.resolve_friendly(rel)
        path = self._resolve_canonical(canonical)
        exists = path.is_file()
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

        incoming = front or Frontmatter()
        incoming = replace(
            incoming,
            updated=incoming.updated or today or time.strftime("%Y-%m-%d"),
        )
        merged = incoming
        if exists:
            existing_front, _, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            merged = merge_frontmatter(existing_front, incoming)
            merged = _clear_frontmatter_fields(merged, clear)
        # `status: placeholder` 会让 README **永久不被注入**（那是程序给空骨架用的标记）。
        # 这条守卫放这里而不是各调用点：模型与网页界面都往这里写，谁都别想绕过它。
        # （整理工具那边还有一条更早、更贴心的提示，两条都对，是有意的冗余。）
        if canonical == README_NAME and merged.status.strip().lower() == PLACEHOLDER_STATUS:
            raise VaultPathError(t("ui.vault.readme_placeholder"))
        written = self.write_file(
            canonical, dump_frontmatter(merged) + "\n\n" + body.strip() + "\n"
        )
        return {
            "path": written,
            "action": "updated" if exists else "created",
            "frontmatter": merged.to_dict(),
            "body_chars": len(body.strip()),
        }

    def delete_file(self, rel: str) -> str:
        """删一份档案，返回规范路径。**档案删除的唯一入口。**

        README 不能删（它是索引，应当重写）—— 这条在工具与界面两边都要一样，
        所以放在这里而不是各自的调用点。文件不存在时抛 `FileNotFoundError`
        （调用方要把"没有这份档案"和"路径非法"分开报）。
        """
        canonical = self.resolve_friendly(rel)
        if canonical == README_NAME:
            raise VaultPathError(t("ui.vault.readme_no_delete"))
        path = self._resolve_canonical(canonical)
        if not path.is_file():
            raise FileNotFoundError(canonical)
        path.unlink()
        return canonical

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
        """建立空 vault 的骨架（只有目录与一份占位 README）。

        **不预置任何"内容"**：把一个空骨架写得像档案，会让人分不清
        "这是模型整理的"还是"这是程序编的"。README 的正文由 dream agent 写。

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
    # 机械校验（README 由模型写，所以必须有不变式守住它）
    # ------------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """读一遍 vault，报告"结构上明显不对"的地方。**只读**。

        这一套就是"机械装配"退化成"机械校验"之后的产物：我们不替模型写
        README（那是它的活），但要能一眼看出它写漏了、写崩了、写长了。
        """
        problems: list[str] = []
        if not self.root.is_dir():
            return {
                "exists": False,
                "files": 0,
                "problems": [t("ui.vault.no_vault_dir")],
                "readme_tokens": 0,
                "readme_truncated": False,
            }

        entries = self._read_entries(None)
        by_path = {entry.path: entry for entry in entries}
        archives = self._iter_rels()
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

        for rel, path in self._iter_paths():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            front, body, warnings = parse_frontmatter(text)
            for warning in warnings:
                problems.append(f"{rel}：{warning}")
            if not body.strip():
                # **两种情况分开说**。只在"标题也空"时报"空文件"是不够的：
                # 界面（ADR-070）允许存一份"有元信息、正文空"的档案 ——
                # 而那正是"手滑把正文删掉就保存了"的形状，必须看得见。
                # （允许存空是有意的：改到一半先存个草稿是正常需求，所以这里是**报出来**
                # 而不是拒绝写入。）
                if front.title:
                    problems.append(
                        t("ui.vault.empty_body_with_title", path=rel)
                    )
                else:
                    problems.append(t("ui.vault.empty_file", path=rel))
            try:
                normalize_rel_path(rel, name_max_chars=self.name_max_chars)
            except VaultPathError as exc:
                problems.append(
                    t("ui.vault.bad_file_name", path=rel, error=exc)
                )

        readme, truncated = self.readme_text()
        if archives and not readme:
            if self._readme_is_placeholder():
                # 与"真的没有 README"分开说：这一种的后果是**永久不注入**，
                # 而且模型自己就能造成它（写 README 时带上那个标记）
                problems.append(t("ui.vault.readme_is_placeholder"))
            else:
                problems.append(t("ui.vault.readme_missing"))
        if truncated:
            problems.append(
                t(
                    "ui.vault.readme_over_budget",
                    budget=self.readme_token_budget,
                )
            )

        # 孤儿：README 里一个字都没提到的档案文件（模型写了 README 却忘了索引它）
        if readme:
            for entry in entries:
                if entry.path == README_NAME:
                    continue
                stem = Path(entry.path).stem
                if stem not in readme and entry.path not in readme:
                    problems.append(t("ui.vault.orphan", path=entry.path))

        return {
            "exists": True,
            # 口径与 count_files() 一致：README 是索引，不算档案
            "files": len(archives),
            "has_readme": bool(readme),
            "readme_tokens": estimate_tokens(readme),
            "readme_chars": len(readme),
            "readme_truncated": truncated,
            "readme_budget": self.readme_token_budget,
            "problems": problems,
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
                    title=front.title or Path(rel).stem,
                    kind=front.kind,
                    chars=len(body),
                    summary=_summary_of(body),
                )
            )
        entries.sort(key=lambda entry: entry.path)
        return entries if limit is None else entries[:limit]

    def _iter_rels(self) -> list[str]:
        """枚举 vault 里**确实位于 vault 内**的档案（全局排序，结果稳定）。

        两处都是审查实测出来的问题，所以在这里一次收口：

        1. `glob("*.md")` 会**跟着符号链接 / junction 走出去**。于是出现
           "`dream_read` 拒绝、`list_files` / `dream_search` 却把外面的文件读出来"
           这种自相矛盾 —— 四个扫描（list / search / neighborhood / check）
           全走这个函数，所以修这一处就够；
        2. 原来按目录顺序累加再 `[:max_files]`，结果是 **sections 会把 entities
           整体挤到看不见**（而 `read` 反而读得到）。改成先全局排序再截断，
           "谁被丢"至少是可预测的。
        """
        rels: list[str] = []
        if not self.root.is_dir():
            return rels
        for directory in _TOP_DIRS:
            folder = self.root / directory
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.md")):
                if not path.is_file():
                    continue
                rel = f"{directory}/{path.name}"
                try:
                    self._resolve_canonical(rel)  # 与写入同一道闸
                except VaultPathError:
                    continue
                rels.append(rel)
        rels.sort()
        # 硬上限只为"别让一次扫描把机器拖死"存在，比展示上限宽 4 倍；
        # 展示层的上限由 list_files / search 各自去管。
        return rels[: max(1, self.max_files) * 4]

    def _iter_paths(self) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        readme = self.root / README_NAME
        if readme.is_file():
            out.append((README_NAME, readme))
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
            title = front.title or Path(rel).stem
            names = front.names() or [normalize_name(title)]
            index[rel] = {
                "title": title,
                "names": names,
                "links": _wikilinks(body),
            }
        return index

    @staticmethod
    def _name_to_path(name: str, index: dict[str, dict[str, Any]]) -> str:
        """双链名字 → 文件路径。**精确匹配**（标题 / 别名 / 文件名）。

        刻意**不做**宽容处理：不折大小写、不去 `.md`、不剥目录前缀。
        定位 `entities/AI4Science.md` 的写法就是 `[[AI4Science]]` 这一种 ——
        别的写法一律算"这份档案还没写"，界面上会如实显示成虚线的"还没写"，
        而不是悄悄指向一个"差不多"的档案。**可预测比宽容重要**：
        一个拼错的链接如果被默默修正，你就永远学不会它拼错了。
        """
        wanted = normalize_name(name)
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
    而它没提到、但**你手工加过**的键（`pinned: true` 之类）必须原样留着。
    """
    return Frontmatter(
        title=incoming.title or existing.title,
        kind=incoming.kind or existing.kind,
        relation=incoming.relation or existing.relation,
        status=incoming.status or existing.status,
        updated=incoming.updated or existing.updated,
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

    只认**认识的字段名**：`extra` 里的手写键不在这里处理 —— "删掉你手写的一个键"
    是另一件事（现在没有这个需求，硬做容易误删）。
    """
    out = front
    for key in keys:
        name = str(key).strip()
        if name in ("title", "kind", "relation", "status", "updated"):
            out = replace(out, **{name: ""})
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
                    "title": front.title or Path(rel).stem,
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
