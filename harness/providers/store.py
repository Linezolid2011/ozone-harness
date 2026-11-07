"""供应商档案的持久化与预设（多接口支持）。

存在 `data/providers.json`（`data/` 已被 .gitignore 忽略，key 不会进仓库）。

**不预置任何档案，也不从 `.env` 读 key**：没有任何一家供应商是特殊的或主要的，
key 只存在这个文件里。这样"抹掉 data/ 就等于抹掉所有 key"，
恢复出厂设置才有意义（ADR-036）。第一次使用要去 Web 界面的「设置 → 接口」自己加。

模型列表不写死：点「获取模型列表」会去调该供应商的 `/models` 端点自动发现。
写死模型名会随时间过期，而 `/models` 是 OpenAI 兼容接口的通用能力。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from ..atomic_io import write_text_atomic
from ..config import Settings
from ..i18n import t
from ..token_limit import parse_token_limit
from .base import ProviderError

# --------------------------------------------------------------------------
# 预设：给"添加接口"一个起点，用户只需要填自己的 key
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    base_url: str
    models: tuple[str, ...] = ()
    thinking_options: tuple[str, ...] = ()
    send_reasoning_content: bool = False
    notes: str = ""
    type: str = "openai"                 # openai | anthropic
    # 该档案里**支持图片输入**的模型名单（契约 §13.8）。空 = 不声明 = 未知。
    vision_models: tuple[str, ...] = ()


# 上下文窗口没有"按供应商"这一层了：同一家的模型差别太大
# （Kimi 有 8k 也有 256k，DeepSeek 从 64k 到 1M 都有），
# 一个整家共用的数字必然是错的 —— 错的那个方向还会让人以为"配好了"。
# 所以只保留**按模型**覆盖，没设就用这个默认值，界面上能直接改。
DEFAULT_CONTEXT_WINDOW = 200_000

# token 写法（`1000000` / `1M` / `200k`）的解析在 `harness/token_limit.py`：
# 上下文上限与「设置 → 常规」的单轮最大输出共用同一个实现（ADR-093 / ADR-101）。
# 这里把它 re-export 出去：`harness.providers.store.parse_token_limit` 是既有调用点
# 与测试一直在用的名字，不要因为搬家而改别人的 import。


# 注意：除 DeepSeek 外，这里**刻意不预填模型名**。
# 我们不掌握各家当前在售的模型 id，写死只会带来过期与误导 ——
# 点一下「获取模型列表」就能拿到真实的。
#
# `label` / `notes` 一律留空：它们是**界面文案**（"添加接口"里那一排预设），
# 由 `_preset_text()` 在用到它的那一刻从 catalog 现取（`get_preset()` / `presets()`）。
# 语言是运行期可切的，把文案写死在这个模块常量里就等于在 import 那一刻钉死了语言。
PRESETS: tuple[Preset, ...] = (
    Preset(
        id="kimi-code",
        label="",
        base_url="https://api.kimi.com/coding/v1",
        type="anthropic",
    ),
    Preset(
        id="kimi-code-global",
        label="",
        base_url="https://api.kimi.ai/coding/v1",
        type="anthropic",
    ),
    Preset(
        id="moonshot",
        label="",
        base_url="https://api.moonshot.cn/v1",
    ),
    Preset(
        id="moonshot-global",
        label="",
        base_url="https://api.moonshot.ai/v1",
    ),
    Preset(
        id="deepseek",
        label="",
        base_url="https://api.deepseek.com",
        models=("deepseek-flash", "deepseek-v4-pro"),
        thinking_options=("low", "high", "max"),
        send_reasoning_content=True,
        # 只有 flash 是原生多模态：2026-10-14 实测 v4-pro 收到图片**不报错**，
        # 只是静默把图丢掉（prompt_tokens 91 → 99）然后答"无法确定"。
        # 所以这里把名单写死，让发送前的闸拦得住（契约 §13.8）。
        vision_models=("deepseek-flash",),
    ),
    Preset(
        id="zhipu",
        label="",
        base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    Preset(
        id="dashscope",
        label="",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    Preset(
        id="siliconflow",
        label="",
        base_url="https://api.siliconflow.cn/v1",
    ),
    Preset(
        id="openrouter",
        label="",
        base_url="https://openrouter.ai/api/v1",
    ),
    Preset(
        id="openai",
        label="",
        base_url="https://api.openai.com/v1",
    ),
    Preset(
        id="ollama",
        label="",
        base_url="http://127.0.0.1:11434/v1",
    ),
)


def _preset_text(preset: Preset) -> Preset:
    """把预设的 `label` / `notes` 换成**当前语言**的那一份。

    **现取**，不能在 import 时算好：语言是运行期可切的（契约 §3）。
    """
    return replace(
        preset,
        label=t(f"provider.preset.{preset.id}.label"),
        notes=t(f"provider.preset.{preset.id}.notes"),
    )


def presets() -> list[dict[str, Any]]:
    return [asdict(_preset_text(preset)) for preset in PRESETS]


def get_preset(preset_id: str) -> Preset | None:
    for preset in PRESETS:
        if preset.id == preset_id:
            return _preset_text(preset)
    return None


# --------------------------------------------------------------------------
# 档案
# --------------------------------------------------------------------------


@dataclass
class ProviderProfile:
    id: str
    label: str
    base_url: str
    api_key: str = ""
    models: list[str] = field(default_factory=list)
    thinking_options: list[str] = field(default_factory=list)
    send_reasoning_content: bool = False
    supports_tools: bool = True
    notes: str = ""
    type: str = "openai"                                       # openai | anthropic
    # **只有按模型这一层**：`{"deepseek-v4-pro": 1_000_000}`。
    # 没有"按供应商"的整家默认值 —— 同家模型的窗口差别太大，那个数字必然是错的。
    model_limits: dict[str, int] = field(default_factory=dict)
    # 「支持图片输入」的模型名单（契约 §13.8）。语义**只有两种**：
    #   * 非空 = 白名单，名单外的模型一律按**不支持**处理；
    #   * 空   = **未知**（不是"不支持"）：照发，真被 API 拒了走 §13.5。
    # 空与"没这个键"是同一个意思 —— 用户已有的档案**不需要改**。
    vision_models: list[str] = field(default_factory=list)
    # 模型的**显示名**（可选）：`{"deepseek-flash": "DeepSeek V4.1 Flash"}`。
    # 它只影响界面（下拉框、接口卡片那几行的标题），**不参与任何决策** ——
    # 所以缺键、空串、名字后来被删掉，一律退回模型 id 本身。
    # 与 `model_limits` 一样按模型 id 索引，所以 id 改了等于换了个模型（旧名自然失效）。
    model_labels: dict[str, str] = field(default_factory=dict)

    def display_name(self, model: str) -> str:
        """这个模型在界面上叫什么：有显示名就用它，否则就是 id。

        **只有这一个实现** —— 下拉框、接口卡片、默认模型那一栏都从这里取，
        免得"有的地方显示名、有的地方 id"。
        """
        name = (model or "").strip()
        return (self.model_labels.get(name) or "").strip() or name

    def context_window(self, model: str) -> int:
        """这个模型这次能用多大的上下文。

        没设过就是 `DEFAULT_CONTEXT_WINDOW`（200k）。填错不会坏，
        只会让 compact 提前/延后触发 —— 所以这里不做任何"看起来合理"的纠正。
        """
        override = self.model_limits.get((model or "").strip())
        # 档案里可能是 `1000000`，也可能是人手写进去的 `"1M"` —— 同一个解析器。
        value = parse_token_limit(override) or 0
        return value or DEFAULT_CONTEXT_WINDOW

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    def masked_key(self) -> str:
        key = self.api_key.strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "••••••"
        return f"{key[:5]}{t('provider.store.key_mask')}{key[-4:]}"

    def to_public(self) -> dict[str, Any]:
        """给前端的版本：**永不包含完整 key**。"""
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "masked_key": self.masked_key(),
            "has_key": self.configured,
            "models": list(self.models),
            "thinking_options": list(self.thinking_options),
            "send_reasoning_content": self.send_reasoning_content,
            "supports_tools": self.supports_tools,
            "notes": self.notes,
            "type": self.type,
            "model_limits": dict(self.model_limits),
            # 前端要能**读**它（设置页里编辑"支持图片的模型"名单）
            "vision_models": list(self.vision_models),
            # 前端要能**读**它（模型表格里的"显示名"那一列）
            "model_labels": dict(self.model_labels),
        }

    def to_storage(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProviderProfile":
        limits: dict[str, int] = {}
        for key, value in (raw.get("model_limits") or {}).items():
            name = str(key).strip()
            # 手改档案时写 `"1M"` 也认（ADR-093）；看不懂的照旧**跳过** ——
            # 一条坏值不该让整个档案读不出来。界面那条路会当场拦住，
            # 不会走到这里（见 `readModels()`）。
            parsed = parse_token_limit(value)
            if name and parsed is not None:
                limits[name] = parsed
        # 显示名同理：**空值等于没写**（不是"名字叫空串"）。一条空字符串
        # 会让界面上那一格变成空白，看起来像"这个模型没有名字" —— 不如退回 id。
        labels: dict[str, str] = {}
        for key, value in (raw.get("model_labels") or {}).items():
            name = str(key).strip()
            text = str(value or "").strip()
            if name and text:
                labels[name] = text
        # 旧格式里的 context_limit 不在这里读：ProviderStore._load() 会先把它
        # 折算成 model_limits（见那段注释）。这样"改配置放在哪一层"就不会
        # 顺手把用户设置的效果改掉。
        return cls(
            id=str(raw.get("id") or "").strip(),
            label=str(raw.get("label") or raw.get("id") or "").strip(),
            base_url=_LEGACY_BASE_URLS.get(
                str(raw.get("base_url") or "").strip().rstrip("/"),
                str(raw.get("base_url") or "").strip(),
            ),
            api_key=str(raw.get("api_key") or ""),
            models=[str(m) for m in raw.get("models") or [] if str(m).strip()],
            thinking_options=[
                str(t) for t in raw.get("thinking_options") or [] if str(t).strip()
            ],
            send_reasoning_content=bool(raw.get("send_reasoning_content")),
            supports_tools=raw.get("supports_tools", True) is not False,
            notes=str(raw.get("notes") or ""),
            type=str(raw.get("type") or "openai").strip() or "openai",
            model_limits=limits,
            # 缺这个键 = 空 = **未知**（老档案照旧"照发"，契约 §13.8）。
            # 读的时候**只保留非空字符串**：一个 `[""]` 会被当成"声明了白名单但谁都不支持"，
            # 那是把"没填"读成了"全不支持"，方向正好相反。
            vision_models=[
                str(m).strip() for m in raw.get("vision_models") or [] if str(m).strip()
            ],
            model_labels=labels,
        )

    @classmethod
    def from_preset(cls, preset: Preset, api_key: str = "") -> "ProviderProfile":
        return cls(
            id=preset.id,
            label=preset.label,
            base_url=preset.base_url,
            api_key=api_key,
            models=list(preset.models),
            thinking_options=list(preset.thinking_options),
            send_reasoning_content=preset.send_reasoning_content,
            notes=preset.notes,
            type=preset.type,
            vision_models=list(preset.vision_models),
        )


# --------------------------------------------------------------------------
# 「这个模型能不能看图」的能力声明（契约 §13.8）
# --------------------------------------------------------------------------

# 内置的已知模型表：**一次带日期的观测，不是永恒真理**。
# 2026-10-14 用真接口实测 deepseek：
#   * `deepseek-flash` 能看图（问"这是什么动物"答"猎豹"）；
#   * `deepseek-v4-pro` 收到图片**不报错**（HTTP 200），却把图静默丢掉
#     （prompt_tokens 91 → 99，图片只贡献 8 个 token）然后答"无法确定"。
# 正因为它不报错，"等 API 报错再提示用户"永远不会触发，所以判据必须在这里。
# 档案里的声明永远优先于本表（用户能自己纠正，见 vision_support 的来源）。
_BUILTIN_VISION_MODELS: dict[str, bool] = {
    "deepseek-flash": True,
    "deepseek-v4-pro": False,
}

# 判定来源（给错误信息用）：档案声明 > 内置已知表 > 未知。
VISION_SOURCE_PROFILE = "profile"
VISION_SOURCE_BUILTIN = "builtin"
VISION_SOURCE_UNKNOWN = "unknown"


def vision_support(profile: "ProviderProfile", model: str) -> tuple[bool | None, str]:
    """判定 `model` 能不能收图片输入，返回 `(判定, 来源)`。

    三层规则（契约 §13.8），**这是全项目唯一的实现** —— 发送前的闸与
    "历史里有图"的提醒都必须走这里，谁都不许再写第二套：

    1. 档案里 **有** `vision_models`（非空）= 白名单：在名单里 → True，
       不在 → False（来源 `profile`）；
    2. 档案**没声明**（空 / 没这个键）= 未知 → 查内置已知表；
       查得到 → 那个布尔值（来源 `builtin`）；查不到 → `None`（来源 `unknown`）；
    3. `None` = **未知**，不是"不支持"：照发，真被 API 拒了走契约 §13.5。

    "未知就照发"是刻意的：拿 token 数去猜会被我们自己的估算误差淹没
    （文本估算误差 20% 量级，而图片贡献可以低到 8 个 token）。
    """
    name = str(model or "").strip()
    declared = [str(item).strip() for item in (profile.vision_models or []) if str(item).strip()]
    if declared:
        return (name in declared), VISION_SOURCE_PROFILE
    known = _BUILTIN_VISION_MODELS.get(name)
    if known is None:
        return None, VISION_SOURCE_UNKNOWN
    return known, VISION_SOURCE_BUILTIN


def vision_supported(profile: "ProviderProfile", model: str) -> bool | None:
    """`vision_support()` 的判定那一半（只关心"支不支持"时用它）。"""
    return vision_support(profile, model)[0]


def _clean_id(raw: str) -> str:
    return "".join(ch for ch in str(raw).strip().lower() if ch.isalnum() or ch in "-_")


def _migrate_context_limits(raw: dict[str, Any]) -> None:
    """把旧格式的"按供应商上下文上限"折算成新的"按模型覆盖"。**原地改 raw。**

    旧：`providers[i].context_limit = 1000000` —— 对该供应商的**所有**模型生效
    新：`providers[i].model_limits = {"deepseek-v4-pro": 1000000, ...}`

    为什么要迁移而不是直接丢掉旧字段：那是一次**静默的行为变更**。
    用户设的 1M 会悄悄掉回默认值，compact 提前好几倍触发，
    而界面上什么提示都没有 —— 我们改的是"配置放在哪一层"，
    不该顺手把用户设置的效果也改掉。

    折算规则尽量忠实地保留原意：
    * 档案里有模型列表 → 那个旧值**原本就对这些模型全部生效**，逐个挂上去；
    * 没有模型列表，但它正好是默认供应商 → 挂到默认模型上；
    * 都对不上 → 本来就无处生效（那个供应商连模型都没有），丢掉。

    只在内存里改，**不在加载时写盘**（读操作不该有副作用）；
    等用户下次保存时，`to_storage()` 自然就写成了新格式。
    """
    default = raw.get("default") if isinstance(raw.get("default"), dict) else {}
    default_provider = str(default.get("provider") or "").strip()
    default_model = str(default.get("model") or "").strip()

    for item in raw.get("providers") or []:
        if not isinstance(item, dict):
            continue
        legacy = item.pop("context_limit", None)
        if legacy is None:
            continue
        try:
            value = int(legacy)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        existing = item.get("model_limits")
        if isinstance(existing, dict) and existing:
            # 已经有按模型的覆盖：那份更具体，旧的整家值让位
            continue
        models = [str(m).strip() for m in item.get("models") or [] if str(m).strip()]
        if not models and item.get("id") == default_provider and default_model:
            models = [default_model]
        if not models:
            continue
        item["model_limits"] = {model: value for model in models}


# 早期预设把 Kimi Code 的 base_url 写成了没有 /v1 的版本。
# 对**对话**端点其实没影响（Anthropic 客户端会把 /v1 归一化后拼路径），
# 但模型发现是直接拼 `/models` 的，少 /v1 就会打到错的地址 —— 实测确认过：
# 带上 /v1 之后，获取模型列表立刻拿到了 k3 / k3-256k / kimi-for-coding 等真实模型。
# 这两个字符串只可能来自我们自己的预设，所以可以安全地就地纠正。
_LEGACY_BASE_URLS = {
    "https://api.kimi.com/coding": "https://api.kimi.com/coding/v1",
    "https://api.kimi.ai/coding": "https://api.kimi.ai/coding/v1",
}


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------


class ProviderStore:
    """接口档案 + 默认选择。单文件 JSON，读写都很小，直接整体重写。

    **不预置任何接口**：一家都不特殊。全新的机器上这里是空的，
    用户在「设置 → 接口」里自己挑预设、填自己的 key。
    这样"抹掉 data/ 就等于抹掉所有 key"，恢复出厂设置才有意义。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = self._load()

    # -- 读写 --------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            # `utf-8-sig`：容忍 BOM。这个文件里是**接口配置与 API key**，
            # 而它是给人手改的 —— 用记事本存一次就可能带上 BOM，
            # 普通 utf-8 读会 `JSONDecodeError`，于是"用户的接口全都不见了"，
            # 而且没有任何报错线索。一行之差，别省。
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        _migrate_context_limits(raw)
        return raw

    def save(self) -> None:
        # 原子写：半截文件读不出来时，读端会把它当成"没有配置"（见 atomic_io 的说明）。
        write_text_atomic(
            self.path, json.dumps(self._data, ensure_ascii=False, indent=2)
        )

    # -- 查询 --------------------------------------------------------

    def providers(self) -> list[ProviderProfile]:
        return [
            ProviderProfile.from_dict(raw)
            for raw in self._data.get("providers") or []
            if raw.get("id")
        ]

    def get(self, provider_id: str) -> ProviderProfile | None:
        for profile in self.providers():
            if profile.id == provider_id:
                return profile
        return None

    def defaults(self) -> dict[str, str]:
        raw = self._data.get("default") or {}
        return {
            "provider": str(raw.get("provider") or ""),
            "model": str(raw.get("model") or ""),
            "effort": str(raw.get("effort") or ""),
        }

    def dream_defaults(self) -> dict[str, str]:
        """整理（dream）用哪家 / 哪个模型 / 哪个档位。

        **空串 = 跟随 `default`**（没设过就是"用聊天那套默认"）。这样全新
        机器上不需要先配一次 dream：它继承现有的默认选择，行为与加这个设置之前
        完全一致（ADR-103）。

        为什么不放到 `data/settings.json`：那个文件有语言、时区、单轮输出，
        都是"与用哪家接口无关"的偏好；而这里的三个字段与 `default` 是同一类
        东西（选哪家、哪个模型），放在同一个 store 里才能让"接口被删了怎么办"
        只有一处实现（见 `remove()`）。
        """
        raw = self._data.get("dream") or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "provider": str(raw.get("provider") or ""),
            "model": str(raw.get("model") or ""),
            "effort": str(raw.get("effort") or ""),
        }

    def all_models(self) -> list[dict[str, str]]:
        """给前端下拉用：把每家已配置好的模型摊平成一个列表。

        `label` 用**显示名**（没填就是 id）：下拉框是用户唯一会反复看的那一栏，
        那里显示 `deepseek-flash` 还是 `DeepSeek V4.1 Flash` 差别很大；
        而 `model` 永远是真实 id —— 选中的值必须能直接发给接口。
        """
        out: list[dict[str, str]] = []
        for profile in self.providers():
            if not profile.configured:
                continue
            for model in profile.models:
                out.append(
                    {
                        "provider": profile.id,
                        "provider_label": profile.label,
                        "model": model,
                        "model_label": profile.display_name(model),
                        "label": f"{profile.label} · {profile.display_name(model)}",
                    }
                )
        return out

    def public_state(self) -> dict[str, Any]:
        return {
            "providers": [p.to_public() for p in self.providers()],
            "default": self.defaults(),
            # 整理专用的选择（ADR-103）：空串 = 跟随 `default`。前端拿它画
            # Dream 页那个模型选择器；**不在这里替它折叠成生效值** ——
            # "没设过"与"设成了某个值"在界面上是两种状态（前者显示「跟随默认」）。
            "dream": self.dream_defaults(),
            "models": self.all_models(),
            "presets": presets(),
        }

    # -- 修改 --------------------------------------------------------

    def upsert(self, profile: ProviderProfile) -> ProviderProfile:
        profile.id = _clean_id(profile.id)
        if not profile.id:
            raise ProviderError(t("provider.store.id_required"))
        if not profile.base_url:
            raise ProviderError(t("provider.store.base_url_required"))
        # **保存时就校验 scheme**（P0 修复）：`api.deepseek.com/v1` 这种写法在
        # `urllib.request.Request(...)` 里会抛 `ValueError: unknown url type`，
        # 而那个异常既不是 ProviderError、也不是 OSError —— 它会一路穿到 HTTP 层
        # 变成 500（界面上是一句 Python 原文），在对话路径上更糟：整轮直接中断。
        # 与其在三个调用点各兜一次，不如在**写入口**就拒绝，并说清怎么写。
        if urlsplit(profile.base_url).scheme.lower() not in ("http", "https"):
            raise ProviderError(t("provider.store.base_url_scheme"))
        profiles = [p for p in self.providers() if p.id != profile.id]
        profiles.append(profile)
        profiles.sort(key=lambda p: p.label.casefold())
        self._data["providers"] = [p.to_storage() for p in profiles]
        default = self.defaults()
        if not default["provider"]:
            default["provider"] = profile.id
        if not default["model"] and profile.models:
            default["model"] = profile.models[0]
        self._data["default"] = default
        self.save()
        return profile

    def update_key(self, provider_id: str, api_key: str) -> ProviderProfile:
        profile = self.get(provider_id)
        if profile is None:
            raise ProviderError(t("provider.store.unknown", provider_id=provider_id))
        profile.api_key = api_key.strip()
        return self.upsert(profile)

    def update_models(self, provider_id: str, models: Iterable[str]) -> ProviderProfile:
        profile = self.get(provider_id)
        if profile is None:
            raise ProviderError(t("provider.store.unknown", provider_id=provider_id))
        profile.models = [str(m) for m in models if str(m).strip()]
        return self.upsert(profile)

    def remove(self, provider_id: str) -> None:
        profiles = [p for p in self.providers() if p.id != provider_id]
        self._data["providers"] = [p.to_storage() for p in profiles]
        default = self.defaults()
        if default["provider"] == provider_id:
            default = {"provider": "", "model": "", "effort": ""}
            if profiles:
                default["provider"] = profiles[0].id
                if profiles[0].models:
                    default["model"] = profiles[0].models[0]
        self._data["default"] = default
        # 整理的那份选择也要跟着清：否则它会继续指着一个已经不存在的接口，
        # 而界面上那个下拉框会显示成「跟随默认」（模型不在列表里）——
        # 存储与界面各说一套。清空之后语义与"没设过"一致：跟随 default。
        dream = self.dream_defaults()
        if dream["provider"] == provider_id:
            self._data["dream"] = {"provider": "", "model": "", "effort": ""}
        self.save()

    def set_defaults(
        self, provider: str | None = None, model: str | None = None, effort: str | None = None
    ) -> dict[str, str]:
        default = self.defaults()
        if provider is not None:
            default["provider"] = _clean_id(provider)
        if model is not None:
            default["model"] = model.strip()
        if effort is not None:
            default["effort"] = effort.strip()
        self._data["default"] = default
        self.save()
        return default

    def set_dream_defaults(
        self, provider: str | None = None, model: str | None = None, effort: str | None = None
    ) -> dict[str, str]:
        """改整理（dream）专用的接口 / 模型 / 档位。**传空串 = 跟随 default**。

        与 `set_defaults` 一样只负责存；"这家接口还在不在、这个模型列没列出来"
        由调用方（Web 接口）校验 —— store 层保持"只存不判"的一致性。
        """
        dream = self.dream_defaults()
        if provider is not None:
            dream["provider"] = _clean_id(provider)
        if model is not None:
            dream["model"] = model.strip()
        if effort is not None:
            dream["effort"] = effort.strip()
        self._data["dream"] = dream
        self.save()
        return dream


# --------------------------------------------------------------------------
# 把档案折算成一份 Settings
# --------------------------------------------------------------------------


def resolve_settings(
    base: Settings,
    profile: ProviderProfile,
    model: str = "",
    effort: str = "",
) -> Settings:
    """把「供应商档案 + 模型 + 思考档位」折算成一份 Settings。

    这样 provider 层完全不用改：它读的还是 `settings.api_key / base_url / model /
    reasoning_effort`，只是这些字段现在来自选中的档案，而不是 .env。
    `.env` 退化成"首次运行时用来种一个默认档案"的角色。
    """
    chosen_model = (model or "").strip() or (
        profile.models[0] if profile.models else base.model
    )
    chosen_effort = (effort or "").strip()
    if not profile.thinking_options:
        # 该供应商没声明可选档位 → 不下发，避免给它塞一个不认识的参数
        chosen_effort = ""
    return replace(
        base,
        api_key=profile.api_key,
        base_url=profile.base_url,
        model=chosen_model,
        reasoning_effort=chosen_effort or None,
        # 上下文窗口随模型变：compact 在哪触发就看这个值（见 Settings.history_budget）
        context_window=profile.context_window(chosen_model),
    )


def build_provider(
    settings: Settings, profile: ProviderProfile
) -> Any:
    """按档案的 type 造出对应的客户端。

    只有两种协议：OpenAI 兼容（覆盖绝大多数）与 Anthropic 兼容
    （Kimi Code 订阅端点、Claude 等）。
    """
    from .anthropic_compat import AnthropicCompatProvider
    from .openai_compat import OpenAICompatProvider

    if profile.type == "anthropic":
        return AnthropicCompatProvider(
            settings,
            label=profile.label,
            send_reasoning_content=profile.send_reasoning_content,
        )
    return OpenAICompatProvider(
        settings,
        label=profile.label,
        send_reasoning_content=profile.send_reasoning_content,
    )


# --------------------------------------------------------------------------
# 模型发现
# --------------------------------------------------------------------------


def fetch_models(base_url: str, api_key: str, *, timeout: float = 20.0) -> list[str]:
    """调该供应商的 `/models` 端点发现可用模型。

    这是 OpenAI 兼容接口的通用能力，比在代码里写死模型名可靠得多。

    **两种鉴权头一起发**：OpenAI 兼容端点认 `Authorization: Bearer`，
    Anthropic 兼容端点（Kimi Code、Claude）认 `x-api-key`。
    多发一个用不上的头是无害的，而少发一个就直接 401。
    """
    url = base_url.rstrip("/") + "/models"
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Accept": "application/json",
            },
            method="GET",
        )
    except ValueError as exc:
        # 地址本身不合法（缺 scheme / 含非法字符）：Request() 在 urlopen 之前就抛。
        # 不在这里兜住，它就不是 ProviderError，调用方（/api/providers/*）也不会
        # 翻译成 400 —— 用户只会看到 500 加一句 Python 原文。
        raise ProviderError(t("provider.store.base_url_scheme")) from exc
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise ProviderError(
            t("provider.store.models_http", status=exc.code, detail=detail or exc.reason)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(
            t("provider.store.models_unreachable", url=url, detail=exc)
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(t("provider.store.models_not_json", url=url)) from exc

    data: Any = body.get("data") if isinstance(body, dict) else body
    models: list[str] = []
    for entry in data or []:
        if isinstance(entry, dict) and entry.get("id"):
            models.append(str(entry["id"]))
        elif isinstance(entry, str) and entry.strip():
            models.append(entry.strip())
    if not models:
        raise ProviderError(t("provider.store.models_empty"))
    return sorted(dict.fromkeys(models))
