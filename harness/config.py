"""配置层：从 .env / 环境变量读取全部可调参数。

原则：所有魔数集中在这里，其他模块不允许硬编码阈值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .i18n import DEFAULT_LANGUAGE as LANGUAGE_DEFAULT
from .i18n import normalize as normalize_language
from .memory_limit import DEFAULT_LIMIT, MAX_LIMIT, MIN_LIMIT

ROOT = Path(__file__).resolve().parent.parent


def resolve_data_dir(env: dict[str, str]) -> Path:
    """决定数据目录。**相对路径按项目根解析**，而不是按当前工作目录。

    为什么较真这件事：`HARNESS_DATA_DIR=data-实验` 这种写法，
    如果跟着 CWD 走，那"在项目里跑"和"在别处跑"会指向**两个不同的目录** ——
    用户会以为自己在看同一份数据。项目根是可确定的（`__file__`），
    CWD 不是，所以以项目根为准。
    """
    raw = _opt_text(env, "HARNESS_DATA_DIR")
    if not raw:
        return ROOT / "data"
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (ROOT / candidate)


def display_path(path: Path | str) -> str:
    """给人看的路径：在项目目录内就显示成相对形式。

    用在**界面**上（设置页里那几处"数据目录：…"）。理由是
    "换台机器 / 截个图"：显示 `data-m2` 比显示完整盘符路径更耐用，
    也不会把某台机器的盘符烙进文档截图里。
    命令行仍然打绝对路径 —— 那是拿来在资源管理器里找文件的，绝对路径更好用。
    项目之外的路径原样返回（那时相对形式反而看不懂）。
    """
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(ROOT))
    except ValueError:
        return str(candidate)

_TRUTHY = {"1", "true", "yes", "on", "y"}


def parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与成对引号。不依赖第三方库。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _text(env: dict[str, str], key: str, default: str = "") -> str:
    value = env.get(key)
    return default if value is None or value == "" else value


def _opt_text(env: dict[str, str], key: str) -> str | None:
    value = env.get(key)
    return None if value is None or value.strip() == "" else value.strip()


def _int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(_text(env, key, str(default)))
    except ValueError:
        return default


def _int_range(
    env: dict[str, str], key: str, default: int, low: int, high: int
) -> int:
    """取一个整数并夹到 [low, high]。

    夹而不是报错：环境变量是"种子"，写坏了就退回默认值，
    不该让程序起不来（**设置界面**那条路才需要严格报错）。
    """
    return max(low, min(high, _int(env, key, default)))


def _float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(_text(env, key, str(default)))
    except ValueError:
        return default


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    value = _opt_text(env, key)
    if value is None:
        return default
    return value.lower() in _TRUTHY


def _tuple(env: dict[str, str], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = _opt_text(env, key)
    if value is None:
        return default
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    return parts or default


@dataclass(frozen=True)
class Settings:
    # 本次请求要用的后端参数。
    # **注意**：这三个字段不是"配置"，而是 `resolve_provider()` 从选中的那份
    # 接口档案里填进来的**结果**（provider 层只认它们，好让上层与厂商无关）。
    # 默认全空 —— 没有任何接口是"预置的"或"主要的"。
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str | None
    request_timeout: float
    max_output_tokens: int
    # 熔断
    max_steps: int
    max_tokens_per_turn: int
    max_wall_seconds: float
    max_tool_failures: int
    # 上下文
    context_token_budget: int
    show_reasoning: bool
    # 上下文窗口与自动 compact（按本轮选中的模型决定，见 resolve_settings）
    context_window: int
    compact_enabled: bool
    compact_threshold: float
    compact_target: float
    compact_max_output_tokens: int
    compact_max_input_chars: int
    # 记忆
    # 条数上限的**种子**：真正的值存在 `data/memory.json`，可在设置里改（ADR-057）。
    # 这里只负责"全新机器上第一次用什么值"。
    memory_limit: int
    max_memory_chars: int
    max_tool_result_chars: int
    # 记忆注入上下文的 token 预算。**记忆是全量注入的**（不再有按相关性挑选那套），
    # 所以这个预算就是唯一的兜底：条目总量超过它时会有条目看不到（并且会如实报出来）。
    memory_token_budget: int
    # dream 整理流水线（M7）：把会话记录整理成背景档案
    #
    # 这几个数比 M4 时代大得多，原因是**作用对象变了**：那时 dream 读的是几十条记忆
    # （一次看得完），现在它要读一天的对话记录、再写好几份文件 ——
    # 12 步 / 40k token 根本走不完一轮。
    dream_max_sessions: int
    dream_max_steps: int
    dream_token_budget: int
    dream_max_output_tokens: int
    dream_wall_seconds: float
    dream_record_max_events: int
    dream_record_max_chars: int
    # dream vault：README + sections + entities（M7）
    # README 每轮都进上下文，所以它的上限**必须**在 harness 里强制，
    # 不能只写在提示词里求模型自觉（一个啰嗦的模型能让每一轮都变贵）。
    vault_readme_token_budget: int
    vault_read_max_chars: int
    vault_write_max_chars: int
    vault_max_files: int
    vault_name_max_chars: int
    # 用户改提示词时的单条上限（契约 `docs/14-editable-prompts.md` §5）。
    # 提示词每轮都进上下文，一条几万字的 SYSTEM_PROMPT 会把预算吃光；
    # 内置最长的一条约 4200 字符，所以 20000 是"够你重写、又不至于失控"的量级。
    prompts_max_chars: int
    # 存储
    data_dir: Path
    # 图片附件（契约 §6）。这五项全都是"上传那一刻"的闸，不是运行时行为开关：
    #   * MAX_BYTES / MAX_COUNT 挡住"一次塞进来太多太大"的请求；
    #   * MAX_PIXELS / MAX_SIDE 是官方对模型的有效上限（≈1300×1300 等效像素、
    #     单边 8192），超过的图落盘前就缩掉 —— 存原图只增加上传与 base64 开销；
    #   * STORED_MAX_BYTES 是"缩完编码后"的兜底字节上限。
    # 给默认值：Settings 被大量测试直接构造（helpers 那份 _BASE 没有这些字段），
    # 而 dataclass 要求带默认值的字段排在最后，所以它们只能放在 data_dir 之后。
    attachment_max_bytes: int = 16777216
    attachment_max_count: int = 8
    attachment_max_pixels: int = 1690000
    attachment_max_side: int = 8192
    attachment_stored_max_bytes: int = 8388608
    # 界面语言（ADR-085）：这里只是**种子**，真正的值在 `data/settings.json`
    # （设置里可改、立刻生效，同 ADR-057 的记忆上限那套）。取值容错在 i18n.normalize。
    language: str = LANGUAGE_DEFAULT

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def memory_db_path(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def dreams_dir(self) -> Path:
        return self.data_dir / "dreams"

    @property
    def vault_dir(self) -> Path:
        """dream vault 的根目录。

        它和 `sessions/`、`memory.db` 并列，所以"把 data/ 改名走"这一招
        （恢复出厂设置）天然把 vault 一起带走了 —— 不需要在 reset 里单独照顾它，
        只需要在**预览**里如实报出会删掉多少档案。
        """
        return self.data_dir / "vault"

    @property
    def api_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        env: dict[str, str] = {}
        env.update(parse_env_file(env_file or (ROOT / ".env")))
        env.update(os.environ)  # 真实环境变量优先
        return cls(
            # 没有"主 key"这回事：接口（含 key）只存在 data/providers.json 里，
            # 由用户在界面上添加。这三个字段留空，等着 resolve_provider() 来填。
            api_key="",
            base_url="",
            model="",
            reasoning_effort=None,
            request_timeout=_float(env, "HARNESS_REQUEST_TIMEOUT", 120.0),
            max_output_tokens=_int(env, "HARNESS_MAX_OUTPUT_TOKENS", 2048),
            max_steps=_int(env, "HARNESS_MAX_STEPS", 8),
            max_tokens_per_turn=_int(env, "HARNESS_MAX_TOKENS_PER_TURN", 60000),
            max_wall_seconds=_float(env, "HARNESS_MAX_WALL_SECONDS", 180.0),
            # 连续失败多少次就收手。联网工具失败是常态，模型却容易开始"猜地址"式乱试。
            max_tool_failures=_int(env, "HARNESS_MAX_TOOL_FAILURES", 4),
            context_token_budget=_int(env, "HARNESS_CONTEXT_TOKEN_BUDGET", 24000),
            show_reasoning=_bool(env, "HARNESS_SHOW_REASONING", True),
            # context_window 是"本轮这个模型能用多大"的上限。
            # **只有按模型覆盖这一层**（供应商层已取消：同家模型差别太大），
            # 真正的值在 resolve_settings() 里按选中的模型取；
            # 这里只是"没有供应商档案时"的兜底，以及给 HARNESS_CONTEXT_WINDOW 留个口子。
            context_window=_int(env, "HARNESS_CONTEXT_WINDOW", 200000),
            compact_enabled=_bool(env, "HARNESS_COMPACT_ENABLED", True),
            compact_threshold=_float(env, "HARNESS_COMPACT_THRESHOLD", 0.75),
            compact_target=_float(env, "HARNESS_COMPACT_TARGET", 0.35),
            # 摘要调用也会思考，所以输出上限不能贴着小摘要的实际长度设。
            # 实测：400 字摘要用掉 977 token（其中 817 是思考），1024 会擦边截断。
            compact_max_output_tokens=_int(
                env, "HARNESS_COMPACT_MAX_OUTPUT_TOKENS", 2048
            ),
            compact_max_input_chars=_int(env, "HARNESS_COMPACT_MAX_INPUT_CHARS", 40000),
            # 记忆条数上限：**种子**。真正的值在 data/memory.json（设置里可改，ADR-057）。
            # 旧的 HARNESS_MAX_MEMORIES 不再读 —— 它默认 200，与新上限 0-100 冲突，
            # 留着只会让"配置文件里写着一个不可能生效的值"。
            memory_limit=_int_range(
                env, "HARNESS_MEMORY_LIMIT", DEFAULT_LIMIT, MIN_LIMIT, MAX_LIMIT
            ),
            max_memory_chars=_int(env, "HARNESS_MAX_MEMORY_CHARS", 400),
            max_tool_result_chars=_int(env, "HARNESS_MAX_TOOL_RESULT_CHARS", 2000),
            memory_token_budget=_int(env, "HARNESS_MEMORY_TOKEN_BUDGET", 6000),
            # 一次最多整理几个会话。默认 3：一天的对话可能有好几个会话，
            # 而"一轮里全整理完"既慢又贵 —— 剩下的下次再整理（界面上会说明还有几个）。
            dream_max_sessions=_int(env, "HARNESS_DREAM_MAX_SESSIONS", 3),
            dream_max_steps=_int(env, "HARNESS_DREAM_MAX_STEPS", 30),
            dream_token_budget=_int(env, "HARNESS_DREAM_TOKEN_BUDGET", 150000),
            # 一次输出要装得下"一份 section + 一份 README"，4096 会擦边截断
            dream_max_output_tokens=_int(
                env, "HARNESS_DREAM_MAX_OUTPUT_TOKENS", 8192
            ),
            dream_wall_seconds=_float(env, "HARNESS_DREAM_WALL_SECONDS", 600.0),
            # 一次读几条记录。这两项决定"读完一个会话要几步"：
            # 40 条 × 6000 字符 ≈ 一次读完半天到一天的对话。
            dream_record_max_events=_int(env, "HARNESS_DREAM_RECORD_MAX_EVENTS", 40),
            dream_record_max_chars=_int(env, "HARNESS_DREAM_RECORD_MAX_CHARS", 6000),
            # README 是每轮注入的那一份（≈ 一份用户画像）。1200 token 是"够当索引、
            # 又不至于把历史挤掉"的量级：全量记忆注入的预算是 6000。
            vault_readme_token_budget=_int(
                env, "HARNESS_VAULT_README_TOKEN_BUDGET", 1200
            ),
            vault_read_max_chars=_int(env, "HARNESS_VAULT_READ_MAX_CHARS", 4000),
            vault_write_max_chars=_int(env, "HARNESS_VAULT_WRITE_MAX_CHARS", 12000),
            vault_max_files=_int(env, "HARNESS_VAULT_MAX_FILES", 400),
            # 40 字符是 Windows 的务实上限：路径总长 260 的老限制还在，
            # 而 vault 里会出现中文实体名。名字短一点，就不会有哪台机器打不开。
            vault_name_max_chars=_int(env, "HARNESS_VAULT_NAME_MAX_CHARS", 40),
            prompts_max_chars=_int(env, "HARNESS_PROMPTS_MAX_CHARS", 20000),
            data_dir=resolve_data_dir(env),
            # 图片附件（契约 §6）。上限给的是"官方约束再收紧一点"的值：
            # 上传 16 MiB、落盘后 ≤ 8 MiB —— 官方单张 base64 内联上限是 32 MiB，
            # 但那是"能塞进去"，不是"该塞进去"。
            attachment_max_bytes=_int(env, "HARNESS_ATTACHMENT_MAX_BYTES", 16777216),
            attachment_max_count=_int(env, "HARNESS_ATTACHMENT_MAX_COUNT", 8),
            attachment_max_pixels=_int(env, "HARNESS_ATTACHMENT_MAX_PIXELS", 1690000),
            attachment_max_side=_int(env, "HARNESS_ATTACHMENT_MAX_SIDE", 8192),
            attachment_stored_max_bytes=_int(
                env, "HARNESS_ATTACHMENT_STORED_MAX_BYTES", 8388608
            ),
            # 界面语言的**种子**：运行期可改（data/settings.json），这里只是默认值。
            # 写坏了退回 zh，不让程序起不来（同上面那些 _int_range 的口径）。
            language=normalize_language(_opt_text(env, "HARNESS_LANGUAGE") or ""),
        )

    # ------------------------------------------------------------------
    # 上下文预算
    # ------------------------------------------------------------------

    def history_budget(self) -> int:
        """历史最多占多少 token。

        取两者的**较小值**：
          * `context_token_budget` —— 我们自己的成本上限（默认 24000）；
          * `context_window` 留出系统提示与输出的余量 —— 模型能力上限。

        这个区分很重要：**模型的上下文窗口讲的是能力，不是花钱许可**。
        一个 1M 窗口的模型如果把历史塞到 75 万 token，每轮输入就要 0.75 元，
        所以成本上限是独立的一根闸。
        """
        reserve = max(2048, int(self.max_output_tokens) + 4096)
        by_window = max(4096, int(self.context_window) - reserve)
        return min(int(self.context_token_budget), by_window)

    def describe(self) -> str:
        effort = self.reasoning_effort or "(未设置)"
        return (
            f"model={self.model}  thinking={effort}  "
            f"max_steps={self.max_steps}  turn_token_cap={self.max_tokens_per_turn}  "
            f"记忆=全量注入（上限见 data/memory.json）  lang={self.language}  "
            f"data_dir={self.data_dir}"
        )
