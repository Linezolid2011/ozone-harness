"""长模板：提示词这类"五十行一段"的文案，按语言各放一个模块。

为什么不做成 `MESSAGES` 字典里的一个 key：提示词有几十行、还有多段小标题，
塞进字典之后既没法读也没法 diff。**它们是代码**，所以按模块放。

两个模块必须暴露**同名同占位符**的常量；`tests/test_i18n.py` 会逐条查：
公开名一致、`{}` 占位符集合一致、用假值 `format()` 都能成功、而且英文里没有汉字。

代码里一律 `bundle().SYSTEM_PROMPT`（**不要** import 具体某个语言模块，
否则英文模式会拿到中文）；测试要断言内置原文时才 `from harness.prompts.zh import ...`。

## 用户覆盖（ADR-088，契约 `docs/14-editable-prompts.md`）

界面上可以改这些提示词，改动存在 `data/prompts.json`，**按语言分家**：

    {"version": 1, "zh": {"SYSTEM_PROMPT": "……"}, "en": {}}

`bundle()` 返回的不再是语言模块，而是**"内置 + 覆盖"的生效副本**（只读名字空间）。
这样做的关键收益：`context.py` / `dream.py` / `loop.py` 早就是
`from .prompts import bundle`，所以**没有任何调用点需要改** —— 覆盖自动对
对话、压缩、整理三条路径全部生效。

`i18n.bundle()` 保留为"拿语言模块"的**底层原语**（不叠加覆盖）。
应用代码不许直接调它：有一条扫源码的守卫盯着（否则那条路径会静默绕过用户覆盖）。
"""

from __future__ import annotations

import json
import os
import string
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from .. import i18n
from ..i18n import t

LANGUAGES = i18n.LANGUAGES

# 覆盖存在数据目录里的这个文件（跟着 `data/` 一起被"恢复出厂设置"抹掉，
# 这正是"重置能复原提示词"的实现方式 —— 不需要另写一份复原代码）。
OVERRIDES_FILE = "prompts.json"
OVERRIDES_VERSION = 1

# 单条覆盖的长度上限（字符）。`Settings.prompts_max_chars` 会覆盖它；
# 这个默认值只给"没有 settings 的调用方"（测试、直接调 API 的脚本）用。
# 为什么要上限：提示词每轮都进上下文，一条几万字的 SYSTEM_PROMPT 会把
# 上下文预算吃光，而那不是用户想要的"改一下"。
MAX_CHARS_DEFAULT = 20000

# 不是提示词的名字（`LANG` 是语言代码）。
_SKIP = frozenset({"LANG"})


class PromptError(ValueError):
    """保存/复原被拒绝。消息是**给人看的一句话**，直接回给界面。"""


# --------------------------------------------------------------------------
# 进程级状态
#
# 为什么是全局的：`bundle()` 在热路径里被调用（组系统提示、渲染历史、压缩），
# 而那些地方**拿不到 settings**。语言也是同一套做法（`i18n.apply_settings`）。
# 单用户本机程序，一个进程只有一个数据目录，所以这是刻意的取舍。
# --------------------------------------------------------------------------

_data_dir: Path | None = None
_cache: dict[str, Any] = {"signature": None, "overrides": {}, "error": ""}
_effective: dict[str, Any] = {}


def configure(data_dir: Any) -> None:
    """告诉这一层数据目录在哪。幂等；`None` 表示"不加载任何覆盖"（内置）。"""
    global _data_dir
    new_dir = Path(data_dir) if data_dir is not None else None
    if new_dir != _data_dir:
        _data_dir = new_dir
        _cache["signature"] = None  # 换了目录 → 下次一定重载
        _effective.clear()


def overrides_path() -> Path | None:
    """覆盖文件的位置（没 configure 过就是 `None`）。"""
    return None if _data_dir is None else Path(_data_dir) / OVERRIDES_FILE


def state() -> dict[str, Any]:
    """给界面用：两门语言各改了哪些、文件坏没坏。**只读**。"""
    _ensure_loaded()
    overrides = _cache["overrides"]
    return {
        "overrides": {
            language: sorted((overrides.get(language) or {}).keys())
            for language in LANGUAGES
        },
        "error": _cache["error"],
    }


# --------------------------------------------------------------------------
# 读 / 写文件
# --------------------------------------------------------------------------


def _signature(path: Path | None) -> Any:
    """文件指纹（`mtime_ns + size`）：**手工编辑这个文件也要立刻生效**。

    只比较"存在与否"是不够的 —— 用户可能正开着编辑器改它。
    """
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _parse(raw: Any) -> tuple[dict[str, dict[str, str]], str]:
    """把文件内容整成 `{language: {name: text}}`。坏结构一律 fail-soft。"""
    if not isinstance(raw, Mapping):
        return {}, t("ui.prompts.err.store_shape")
    out: dict[str, dict[str, str]] = {}
    for language in LANGUAGES:
        section = raw.get(language)
        if section is None:
            continue
        if not isinstance(section, Mapping):
            return {}, t("ui.prompts.err.store_section", language=language)
        cleaned: dict[str, str] = {}
        for name, text in section.items():
            if isinstance(name, str) and isinstance(text, str):
                cleaned[name] = text
        out[language] = cleaned
    return out, ""


def _read(path: Path | None) -> tuple[dict[str, dict[str, str]], str]:
    if path is None or not path.is_file():
        return {}, ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        # fail-soft：一个坏文件绝不能让程序起不来（ADR-064 那条）。
        # 但也**不能静默** —— 界面上会显示这句话，用户才知道自己在看内置文案。
        return {}, t("ui.prompts.err.store_read", kind=type(exc).__name__)
    return _parse(raw)


def _ensure_loaded() -> None:
    path = overrides_path()
    signature = _signature(path)
    if signature == _cache["signature"]:
        return
    overrides, error = _read(path)
    _cache["signature"] = signature
    _cache["overrides"] = overrides
    _cache["error"] = error
    _effective.clear()


def _write(overrides: Mapping[str, Mapping[str, str]]) -> None:
    """整份写回（原子）。**空的就删文件** —— "没有自定义"不该留下一个空壳。

    写完必须把**内存缓存也一起更新**（而不是只把签名置空）：文件被删掉时
    `_signature()` 也是 `None`，而 `_ensure_loaded()` 见到"签名没变"就会早退 ——
    那样缓存里那份**旧覆盖会复活**，表现成"刚点了复原，提示词还是我改的那段"。
    （这个 bug 是冒烟测第一遍就撞上的：保存 → 复原 → 跑一遍，内容没回来。）
    """
    path = overrides_path()
    if path is None:
        raise PromptError(t("ui.prompts.err.no_data_dir"))
    cleaned = {
        language: dict(overrides.get(language) or {})
        for language in LANGUAGES
        if overrides.get(language)
    }
    if not cleaned:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        payload: dict[str, Any] = dict(cleaned)
        payload["version"] = OVERRIDES_VERSION
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, path)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
    # 缓存 = 刚刚写下去的那份（而不是"留个空签名等下次重读"）
    _cache["overrides"] = cleaned
    _cache["error"] = ""
    _cache["signature"] = _signature(path)
    _effective.clear()


def _load_fresh() -> dict[str, dict[str, str]]:
    """**重新读一遍文件**再改 —— 另一门语言的覆盖必须原样带过去。"""
    overrides, _error = _read(overrides_path())
    return overrides


# --------------------------------------------------------------------------
# 生效版本
# --------------------------------------------------------------------------


def _names(language: str) -> set[str]:
    """这个语言的内置提示词名字集合。"""
    module = i18n.bundle(language)
    return {name for name in dir(module) if name.isupper()} - _SKIP


def effective(language: str | None = None) -> Any:
    """**生效版本**：内置 + 用户覆盖（只读名字空间）。"""
    code = i18n.normalize(language) if language else i18n.current()
    _ensure_loaded()
    # 用 `get` 而不是 `in` + 下标：web 是多线程的，另一条线程可能刚
    # `_effective.clear()`（保存/复原之后）—— 那会在两步之间把这条读炸。
    cached = _effective.get(code)
    if cached is not None:
        return cached
    module = i18n.bundle(code)
    values = {
        name: getattr(module, name) for name in dir(module) if name.isupper()
    }
    for name, text in (_cache["overrides"].get(code) or {}).items():
        if name in values:  # 文件里多余的键（比如旧版本删掉的名字）忽略
            values[name] = text
    bundle_object = SimpleNamespace(**values)
    _effective[code] = bundle_object
    return bundle_object


def builtin(language: str | None = None) -> Any:
    """**内置原文**（不叠加覆盖）—— 给"复原"与"看看原来是什么样"用。

    单独开这个口子，是为了让应用代码永远不必直接碰 `i18n.bundle()`
    （那条路会静默绕过用户覆盖）；有守卫测试盯着这件事。
    """
    code = i18n.normalize(language) if language else i18n.current()
    return i18n.bundle(code)


def bundle(language: str | None = None) -> Any:
    """取某个语言的**生效提示词**（默认当前语言）。"""
    return effective(language)


# --------------------------------------------------------------------------
# 校验 / 保存 / 复原
# --------------------------------------------------------------------------


def _placeholders(text: str) -> set[str]:
    """文本里的 `{name}` 占位符。解析不了就抛 `PromptError`（保存时拒绝）。"""
    try:
        return {
            field
            for _literal, field, _spec, _conv in string.Formatter().parse(text)
            if field
        }
    except ValueError as exc:
        raise PromptError(t("ui.prompts.err.bad_template", error=exc)) from exc


def _checked_language(language: Any) -> str:
    code = str(language or "").strip().lower()
    if code not in LANGUAGES:
        # 不认识的取值**明确报错**，不许悄悄当成默认语言（ADR-085 的教训：
        # "我写了 Japanese" 静默变成"切到了中文"，看起来成功、其实要求被丢了）。
        raise PromptError(
            t("ui.prompts.err.language", language=language, languages="、".join(LANGUAGES))
        )
    return code


def _checked_name(language: str, name: Any) -> str:
    text = str(name or "").strip()
    names = _names(language)
    if text not in names:
        raise PromptError(
            t("ui.prompts.err.no_such_id", name=name, count=len(names))
        )
    return text


def inspect(language: Any, name: Any, text: Any, *, max_chars: int = MAX_CHARS_DEFAULT) -> dict[str, Any]:
    """保存前的校验结果（也是界面能拿来预检的那份逻辑）。**不写任何东西**。"""
    code = _checked_language(language)
    item = _checked_name(code, name)
    body = str(text or "")
    if not body.strip():
        raise PromptError(t("ui.prompts.err.empty"))
    if len(body) > int(max_chars):
        raise PromptError(
            t("ui.prompts.err.too_long", chars=len(body), max=int(max_chars))
        )
    wanted = _placeholders(body)
    default = str(getattr(i18n.bundle(code), item))
    allowed = _placeholders(default)
    extra = sorted(wanted - allowed)
    if extra:
        # 这一类**最危险**：保存时看不出问题，下一轮对话发请求时才炸
        # （`str.format` 遇到不认识的字段会 KeyError）。
        raise PromptError(
            t(
                "ui.prompts.err.unknown_placeholder",
                names="、".join(f"{{{name}}}" for name in extra),
            )
        )
    missing = sorted(allowed - wanted)
    warnings: list[str] = []
    if missing:
        warnings.append(
            t(
                "ui.prompts.warn.missing_placeholder",
                names="、".join(f"{{{name}}}" for name in missing),
            )
        )
    return {
        "language": code,
        "id": item,
        "text": body,
        "default_text": default,
        "same_as_default": body.strip() == default.strip(),
        "warnings": warnings,
        "missing": missing,
    }


def save(
    language: Any,
    name: Any,
    text: Any,
    *,
    max_chars: int = MAX_CHARS_DEFAULT,
) -> dict[str, Any]:
    """保存一条覆盖。返回给界面的那份结果（含 `stored` 与 `warnings`）。"""
    checked = inspect(language, name, text, max_chars=max_chars)
    code, item = checked["language"], checked["id"]
    # **读-改-写整个文件**：另一门语言的内容原样带回去，一个字都不动。
    overrides = _load_fresh()
    section = dict(overrides.get(code) or {})
    if checked["same_as_default"]:
        # 与内置逐字相同 → 不存成覆盖，并把可能存在的旧覆盖删掉。
        # 否则会留下一份"和内置一样、却把内置冻结了"的记录：将来内置改进了，
        # 这一条不会跟进，而用户以为自己没改过任何东西。
        stored = False
        section.pop(item, None)
    else:
        stored = True
        section[item] = checked["text"]
    if section:
        overrides[code] = section
    else:
        overrides.pop(code, None)
    _write(overrides)
    return {
        "ok": True,
        "language": code,
        "id": item,
        "stored": stored,
        "overridden": stored,
        "warnings": checked["warnings"],
    }


def revert(language: Any, name: Any = None) -> dict[str, Any]:
    """复原：给了 `name` 就复原那一条，否则复原**这门语言的全部**。

    **只碰这一门语言**（`_load_fresh()` 读进来的另一门原样写回）——
    用户明确要求"改中文不影响英文"，这条是它的实现处。
    """
    code = _checked_language(language)
    overrides = _load_fresh()
    section = dict(overrides.get(code) or {})
    if name is None:
        cleared = len(section)
        overrides.pop(code, None)
    else:
        item = _checked_name(code, name)
        cleared = 1 if item in section else 0
        section.pop(item, None)
        if section:
            overrides[code] = section
        else:
            overrides.pop(code, None)
    if cleared:
        _write(overrides)
    # 没改过就**不写文件**：幂等，而且不让 mtime 无谓地动
    # （"另一门语言没被动过"那种逐字节比对的测试也靠这一点）。
    _ensure_loaded()
    return {"ok": True, "language": code, "cleared": cleared, "overridden": False}


__all__ = [
    "LANGUAGES",
    "MAX_CHARS_DEFAULT",
    "OVERRIDES_FILE",
    "PromptError",
    "bundle",
    "builtin",
    "configure",
    "effective",
    "inspect",
    "overrides_path",
    "revert",
    "save",
    "state",
]
