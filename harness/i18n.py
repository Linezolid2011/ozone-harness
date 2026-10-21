"""界面语言（zh / en）：一处开关，两套文案。契约见 `docs/12-i18n.md`。

## 为什么是"当前语言"这个全局状态

模型侧的文案散落在二十来个模块里（系统提示、工具描述、工具回给模型的文本、
发给界面的 notice 与错误）。而这是**单用户本机程序**：语言是应用级的，
不存在"同一进程里两轮对话用不同语言"这种需求。

把这些文字全部改成"每个函数多一个 `lang` 参数"，要把二十个模块的签名都改一遍，
收益却只是那份我们不需要的能力。所以：

* 模块级 `current()` + 显式 `set_language()`；
* 进程启动时按配置设一次（`apply_settings()`），界面切换时再设一次；
* 测试用 `use("en")` 这个上下文管理器，用完自动还原。

代价（写进 ADR-085）：一个进程里只有一个语言，切换发生在两轮之间。

## 两套文案放哪

* **长模板**（系统提示、整理提示、压缩提示）：`harness/prompts/{zh,en}.py` 的模块级常量
  —— 五十行的提示词塞进字典会没法读；
* **短文案**（工具描述/参数/结果、界面提示与错误）：按子系统成对的
  `<子系统>_text_{zh,en}.py`，每个只导出 `LANG` 与 `MESSAGES`，用 key 查。

## 缺翻译时怎么办

`t()` 在**当前语言**里找不到 key 就退回中文；两边都没有才抛 `KeyError`。
退回是刻意的：一条界面文案漏翻，不该让整个对话挂掉。
但这种事必须**不可能悄悄发生** —— `tests/test_i18n.py` 会断言两个语言的 key 集合完全一致，
并且英文输出里**一个汉字都没有**（那才是"漏翻"的唯一可靠判据）。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from .atomic_io import write_text_atomic

LANGUAGES: tuple[str, ...] = ("zh", "en")
DEFAULT_LANGUAGE = "zh"

# 时区（`time` 工具用的偏移，单位：小时）。运行期可改，存在 `data/settings.json`。
# 现在只收**整数小时**（-12 ~ +14）：半时区（如 +5:30）以后需要再加。
DEFAULT_TIMEZONE = 8.0
TIMEZONE_MIN = -12
TIMEZONE_MAX = 14

#: `data/settings.json` —— 运行期可改的设置（语言是第一个，将来还会有别的）
SETTINGS_FILE = "settings.json"

# 成对的短文案模块。**没导入成功的会记进 `_missing_modules`**，
# 而守卫测试断言那个列表是空的 —— 于是"新加了一个 catalog 但忘了接进来"会当场变红。
_CATALOG_MODULES: tuple[str, ...] = (
    "harness.tools.text_zh",
    "harness.tools.text_en",
    "harness.ui_text_zh",
    "harness.ui_text_en",
    "harness.net.text_zh",
    "harness.net.text_en",
    "harness.provider_text_zh",
    "harness.provider_text_en",
)

# 认得出来的写法。认不出来的一律退回默认值（环境变量是种子，写坏了不该让程序起不来）。
_ALIASES: dict[str, str] = {
    "zh": "zh", "cn": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh",
    "chinese": "zh", "中文": "zh", "简体中文": "zh",
    "en": "en", "en-us": "en", "en-gb": "en", "english": "en", "英文": "en", "英语": "en",
}

_catalogs: dict[str, dict[str, str]] = {code: {} for code in LANGUAGES}
# key → 语言 → 定义它的模块集合。用来发现"同一个 key 被两处定义了"
# （那种情况下谁生效取决于导入顺序，是最难查的一类文案 bug）。
_owners: dict[str, dict[str, set[str]]] = {}
_missing_modules: list[str] = []
_loaded = False
_current = DEFAULT_LANGUAGE


# --------------------------------------------------------------------------
# 语言本身
# --------------------------------------------------------------------------


def normalize(value: Any) -> str:
    """任意输入 → `"zh"` / `"en"`。认不出来就退回默认值。**纯函数**。"""
    if isinstance(value, str):
        text = value.strip().lower().replace("_", "-")
        if text in _ALIASES:
            return _ALIASES[text]
        # `zh-Hans-CN` 这类：取第一段再试一次
        head = re.split(r"[-.]", text, maxsplit=1)[0]
        if head in _ALIASES:
            return _ALIASES[head]
    return DEFAULT_LANGUAGE


def current() -> str:
    return _current


def set_language(value: Any) -> str:
    """切换当前语言，返回规范化后的值。"""
    global _current
    _current = normalize(value)
    return _current


@contextlib.contextmanager
def use(value: Any) -> Iterator[str]:
    """临时切语言（测试用），退出时还原 —— 包括块里抛异常的情况。"""
    global _current
    previous = _current
    set_language(value)
    try:
        yield _current
    finally:
        _current = previous


def apply_settings(settings: Any) -> str:
    """按运行期配置设一次语言。**进程启动时必须调**（CLI 与 Web 各一次）。

    顺序是"运行期文件优先、`.env` 里的当默认"（同 ADR-057 记忆上限那套）：
    `Settings.language` 是种子，`data/settings.json` 才是用户改过的值。

    顺带把**提示词的用户覆盖**也装上（`prompts.configure`）：它和语言一样是
    进程级配置，而这里正是"每个入口都已经在调"的那一处 —— 与其让 CLI 与 Web
    各自记得多调一次（迟早漏一个，ADR-072 的教训），不如只留一个入口。
    `import` 放在函数里是因为 i18n 是底层、prompts 依赖它（延迟到调用时不成环）。
    """
    store = LanguageStore(Path(settings.data_dir) / SETTINGS_FILE, default=settings.language)
    language = set_language(store.language())
    from . import prompts

    prompts.configure(settings.data_dir)
    return language


# --------------------------------------------------------------------------
# 文案
# --------------------------------------------------------------------------


def _load_catalogs() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in _CATALOG_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            _missing_modules.append(name)
            continue
        language = normalize(getattr(module, "LANG", ""))
        mapping = getattr(module, "MESSAGES", None)
        if not isinstance(mapping, Mapping):
            _missing_modules.append(name)
            continue
        register(language, mapping, origin=name)


def register(language: Any, mapping: Mapping[str, str], *, origin: str = "") -> None:
    """登记一批文案。同一 key 被两处定义会被记下来（`duplicate_keys()` 报出来）。

    **不设 `_loaded`**：这个函数也可能被直接调用，设了就会让 `_load_catalogs()`
    提前返回、那几个 catalog 模块永远不被导入。
    """
    code = normalize(language)
    for key, text in mapping.items():
        _catalogs[code][key] = str(text)
        if origin:
            _owners.setdefault(key, {}).setdefault(code, set()).add(origin)


def catalog(language: Any = None) -> dict[str, str]:
    """某语言的完整 key → 文案（尚未登记的模块会现加载）。**只读副本**。"""
    _load_catalogs()
    return dict(_catalogs[normalize(language) if language else _current])


def keys(language: Any = None) -> set[str]:
    return set(catalog(language))


def duplicate_keys() -> dict[str, list[str]]:
    """一个 key 在同一语言下被多处定义 → 报出来。守卫测试用。"""
    _load_catalogs()
    found: dict[str, list[str]] = {}
    for key, per_language in _owners.items():
        for code, origins in per_language.items():
            if len(origins) > 1:
                found[key] = sorted(f"{code}:{origin}" for origin in origins)
    return found


def missing_modules() -> list[str]:
    """没接进来的 catalog 模块（守卫测试断言它是空的）。"""
    _load_catalogs()
    return list(_missing_modules)


def t(key: str, /, **kwargs: Any) -> str:
    """查一条文案。当前语言没有就退回中文；两边都没有才抛 `KeyError`。

    `KeyError` 是刻意的：漏翻是**开发期的错**，守卫测试会先拦住它；
    真跑起来时更该看见一个明确的键名，而不是一句空白。
    """
    _load_catalogs()
    text = _catalogs[_current].get(key)
    if text is None:
        text = _catalogs[DEFAULT_LANGUAGE].get(key)
    if text is None:
        raise KeyError(f"没有这条文案：{key!r}（语言 {_current}）")
    if kwargs:
        return text.format(**kwargs)
    return text


def bundle(language: Any = None) -> ModuleType:
    """当前语言的长模板模块（`harness.prompts.zh` / `.en`）。"""
    code = normalize(language) if language else _current
    return importlib.import_module(f".prompts.{code}", __package__)


# --------------------------------------------------------------------------
# 运行期设置（data/settings.json）
# --------------------------------------------------------------------------


class LanguageChange:
    """一次"改语言"的结果。失败时 `error` 是给人看的一句话。"""

    def __init__(self, ok: bool, language: str = "", error: str = "") -> None:
        self.ok = ok
        self.language = language
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": self.ok, "language": self.language}
        if self.error:
            data["error"] = self.error
        return data


def _coerce_timezone(value: Any) -> float | None:
    """把任意输入折成合法的时区偏移（整数小时）；不合法返回 None。

    只收**整数小时**（半时区以后要再加）：`5.5` / `"abc"` / 越界值一律 None。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，别把 True 当成 1
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None
    if number != int(number):
        return None
    if not (TIMEZONE_MIN <= number <= TIMEZONE_MAX):
        return None
    return number


class SettingsStore:
    """`data/settings.json` 的读写：界面语言 + 时区（`time` 工具用的偏移）。

    文件不存在时用种子值，**不主动创建文件**。读操作不创建文件这条与
    `MemoryLimitStore`、`preview_reset()`、`VaultStore` 一致：界面上"看一眼"
    不能改磁盘。文件只在**真正保存**时才写出来。
    """

    def __init__(
        self,
        path: Path,
        *,
        default: str = DEFAULT_LANGUAGE,
        timezone_default: float = DEFAULT_TIMEZONE,
    ) -> None:
        self.path = Path(path)
        self._default = normalize(default)
        self._timezone_default = float(timezone_default)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            # `utf-8-sig`：**容忍 BOM**。这个文件是给人手改的（记事本在旧版本里
            # 默认就写 UTF-8 + BOM），而带 BOM 的 JSON 用普通 utf-8 读会直接
            # `json.JSONDecodeError` —— 症状是"设置改了却好像没生效"，
            # 静默退回默认值，查起来毫无线索（我自己就被它坑过一次：
            # 用 PowerShell 的 `-Encoding utf8` 写文件，写出来是带 BOM 的）。
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def language(self) -> str:
        """当前生效的语言（**只读**）。坏值退回默认值，不报错。"""
        return normalize(self._data.get("language", self._default))

    def timezone(self) -> float:
        """当前生效的时区偏移（**只读**）。坏值退回默认值，不报错。"""
        value = _coerce_timezone(self._data.get("timezone", self._timezone_default))
        return self._timezone_default if value is None else value

    def set(self, value: Any) -> LanguageChange:
        """改成新语言。取值不认识时**明确报错**（这条是设置界面那条路，要严格）。"""
        if not isinstance(value, str) or not value.strip():
            return LanguageChange(False, error=t("ui.language.need_value"))
        code = normalize(value)
        if code not in LANGUAGES or not _recognized(value):
            return LanguageChange(
                False,
                error=t("ui.language.unknown", value=str(value)[:40],
                        choices=" / ".join(LANGUAGES)),
            )
        self._data["language"] = code
        self.save()
        return LanguageChange(True, language=code)

    def set_timezone(self, value: Any) -> dict[str, Any]:
        """改成新时区偏移。取值不合法时**明确报错**（设置界面那条路，要严格）。"""
        number = _coerce_timezone(value)
        if number is None:
            return {
                "ok": False,
                "error": t("ui.timezone.bad_value", value=str(value)[:40]),
            }
        self._data["timezone"] = number
        self.save()
        return {"ok": True, "timezone": number}

    def save(self) -> None:
        # 原子写：半截文件读不出来时，读端会把它当成"没有配置"（见 atomic_io 的说明）。
        write_text_atomic(
            self.path, json.dumps(self._data, ensure_ascii=False, indent=2)
        )

    def public_state(self) -> dict[str, Any]:
        """给前端的一份：语言与时区的当前值、可选清单、以及**默认**是哪个。"""
        return {
            "language": self.language(),
            "languages": list(LANGUAGES),
            "default": self._default,
            "timezone": self.timezone(),
            "timezone_default": self._timezone_default,
            "timezone_min": TIMEZONE_MIN,
            "timezone_max": TIMEZONE_MAX,
        }


# 向后兼容别名：旧代码 / 测试仍叫它 `LanguageStore`（它是设置 store 的第一个名字）。
LanguageStore = SettingsStore


def _recognized(value: str) -> bool:
    """`normalize` 会把认不出来的都折成默认值 —— 设置界面需要区分
    "你写的就是默认那个"和"你写的我根本不认识"。所以这里单独判一次。"""
    text = value.strip().lower().replace("_", "-")
    if text in _ALIASES:
        return True
    return re.split(r"[-.]", text, maxsplit=1)[0] in _ALIASES


def language_for(data_dir: Path, default: str = DEFAULT_LANGUAGE) -> str:
    """当前生效的语言。**每个需要它的地方都走这里**（同 `limit_for` 的理由：
    运行期可改的值散落各处自己读文件，迟早有一处读到旧的）。"""
    return SettingsStore(Path(data_dir) / SETTINGS_FILE, default=default).language()


def timezone_for(data_dir: Path, default: float = DEFAULT_TIMEZONE) -> float:
    """当前生效的时区偏移（小时）。`time` 工具从这里拿时区 ——
    运行期可改，改完下一轮对话（重新装配 Runtime）就生效。"""
    return SettingsStore(
        Path(data_dir) / SETTINGS_FILE, timezone_default=default
    ).timezone()
