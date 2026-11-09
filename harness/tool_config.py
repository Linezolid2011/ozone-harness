"""工具配置文件（ADR-114）：一份默认配置 + 每会话一份配置。

**只存启用的工具名**（JSON 字符串数组）——"禁用了什么"不落盘：
手改文件时只看到被允许的东西，差量可读。

两条铁律：

1. **默认配置永不直接决定已出生会话的工具集**。它只在会话**出生那一刻**
   （第一次 API 调用之前）被逐字复制成会话配置；此后这个会话只听自己的
   配置文件，默认配置怎么改都与它无关。
2. **完整性在启动时与每次保存时检查**。合法形状 = JSON 字符串数组、
   元素都在 ``CHAT_TOOLS`` 名单内、无重复。保存时非法一律拒绝（400）；
   启动时坏档**修复**（默认配置重建为全部启用、会话配置复制默认）——
   启动是"自愈"场合，保存是"用户显式意图"场合，两种场合对同一份非法
   输入的处理刻意不同（ADR-114）。

``CHAT_TOOLS`` 是**静态名单**而不是从注册表反推：注册表受环境约束
（依赖缺失时联网工具根本不注册），配置的合法性不该随机器变化——
否则同一份配置在这台机器合法、换台机器就"坏"了，属于最难查的那类故障。
"""

from __future__ import annotations

import json
from pathlib import Path

from .atomic_io import write_text_atomic
from .i18n import t
from .session_ids import validate_session_id

DEFAULT_CONFIG_NAME = "tool_config.json"
SESSION_CONFIG_DIR = "tool_config"

#: 对话侧全部工具（校验配置文件的"已知 universe"，见模块 docstring）。
CHAT_TOOLS: tuple[str, ...] = (
    "dream_create",
    "dream_delete",
    "dream_edit",
    "dream_list",
    "dream_mkdir",
    "dream_read",
    "dream_search",
    "mem_create",
    "mem_del",
    "mem_edit",
    "time",
    "web_open_url",
    "web_search",
)


class ToolConfigError(Exception):
    """保存时的校验失败（用户可见，消息用当前语言取）。"""


def validate_config(data: object, known: tuple[str, ...] = CHAT_TOOLS) -> list[str]:
    """校验一份配置内容，合法则返回**规范化**（去重保持、排序）的名单。

    非法抛 ``ToolConfigError``，消息说明**哪里**不对（下一步怎么做）：
    保存路径上绝不能"宽容写盘、下次启动再悄悄重建"——那是静默降级。
    """
    if not isinstance(data, list):
        raise ToolConfigError(t("ui.toolconfig.error.not_list"))
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, str):
            raise ToolConfigError(t("ui.toolconfig.error.not_string"))
        if item not in known:
            raise ToolConfigError(t("ui.toolconfig.error.unknown", name=item))
        if item in seen:
            raise ToolConfigError(t("ui.toolconfig.error.duplicate", name=item))
        seen.add(item)
    # 排序落盘：文件字节稳定，测试与手工 diff 都不受勾选顺序影响。
    return sorted(seen)


def _load(path: Path) -> object | None:
    """读一份配置文件。**不修复、不创建**：坏/缺一律返回 None。

    用 ``utf-8-sig``：这些文件用户会手改，而 Windows 记事本存出来带 BOM
    （ADR-085 的教训：配置 store 读 utf-8、文件带 BOM = 整份配置被当成
    "没有"，改了配置却静默不生效）。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


class ToolConfigStore:
    """默认配置 + 会话配置的读写与修复。所有写都是原子写（同目录临时文件 + os.replace）。"""

    def __init__(self, data_dir: Path, known: tuple[str, ...] = CHAT_TOOLS) -> None:
        self.data_dir = Path(data_dir)
        self.known = known

    @property
    def default_path(self) -> Path:
        return self.data_dir / DEFAULT_CONFIG_NAME

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / SESSION_CONFIG_DIR

    def _session_path(self, session_id: str) -> Path:
        # 拼路径前先校验：坏 id 不能产生任何磁盘副作用（ADR-049）。
        validate_session_id(session_id)
        return self.sessions_dir / f"{session_id}.json"

    # -- 读 ----------------------------------------------------------------

    def read_default(self) -> list[str] | None:
        """纯读默认配置；坏/缺返回 None（不落盘）。"""
        raw = _load(self.default_path)
        if raw is None:
            return None
        try:
            return validate_config(raw, self.known)
        except ToolConfigError:
            return None

    def read_session(self, session_id: str) -> list[str] | None:
        """纯读会话配置；坏/缺返回 None（不落盘）。非法 id 抛 ValueError。"""
        raw = _load(self._session_path(session_id))
        if raw is None:
            return None
        try:
            return validate_config(raw, self.known)
        except ToolConfigError:
            return None

    # -- 出生 / 修复 --------------------------------------------------------

    def ensure_default(self) -> list[str]:
        """读默认配置；坏/缺则按**全部启用**重建并落盘（启动自愈路径）。"""
        current = self.read_default()
        if current is not None:
            return current
        return self._write(self.default_path, sorted(self.known))

    def ensure_session(self, session_id: str) -> list[str]:
        """读会话配置；坏/缺则**逐字复制默认配置**生成（会话出生，ADR-114）。

        复制的是"当下这份默认配置的内容"，而且只复制这一次 —— 默认配置
        从此与这个会话无关。
        """
        current = self.read_session(session_id)
        if current is not None:
            return current
        return self._write(self._session_path(session_id), self.ensure_default())

    # -- 保存（用户显式编辑）-------------------------------------------------

    def save_default(self, tools: object) -> list[str]:
        """校验并写默认配置。写后复读验证（保存时也查完整性，ADR-114）。"""
        return self._write(self.default_path, validate_config(tools, self.known))

    def save_session(self, session_id: str, tools: object) -> list[str]:
        """校验并写会话配置（写后复读验证）。"""
        return self._write(self._session_path(session_id), validate_config(tools, self.known))

    # -- 生命周期 -----------------------------------------------------------

    def delete_session(self, session_id: str) -> bool:
        """删会话连带删它的配置文件。坏 id 抛 ValueError；文件不在返回 False。"""
        path = self._session_path(session_id)
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def copy_session(self, source_id: str, target_id: str) -> list[str]:
        """分叉：把源会话的配置复制给新会话（目标按"出生"处理：坏/缺才写）。"""
        existing = self.read_session(target_id)
        if existing is not None:
            return existing
        source = self.read_session(source_id)
        if source is None:
            # 源也没有（出生前的老会话）：按出生规则复制默认。
            source = self.ensure_default()
        return self._write(self._session_path(target_id), source)

    # -- 启动检查 ------------------------------------------------------------

    def check_all(self, sessions_dir: Path | None = None) -> list[tuple[str, str]]:
        """启动时的完整性自检。返回修复记录 ``(kind, session_id)``

        （``kind`` 为 ``"default"`` 时 session_id 是空串；空列表 = 一切完好）。
        文案由调用方落：控制台输出按项目约定**保持中文**，不随界面语言变，
        所以这里只回结构化记录、不取 t()。

        默认配置坏/缺 → 全部启用重建；**每个已有会话**的配置坏/缺 →
        复制默认配置（"没找到就建一份"在启动这个场合是自愈，不是副作用：
        启动本来就会碰数据目录，且修复结果幂等）。
        """
        repairs: list[tuple[str, str]] = []
        before = self.read_default()
        self.ensure_default()
        if before is None:
            repairs.append(("default", ""))
        log_dir = Path(sessions_dir) if sessions_dir is not None else self.data_dir / "sessions"
        try:
            logs = sorted(log_dir.glob("*.jsonl"))
        except OSError:
            logs = []
        for log in logs:
            session_id = log.stem
            try:
                validate_session_id(session_id)
            except ValueError:
                continue  # 手扔进目录的杂文件不算会话
            if self.read_session(session_id) is None:
                self.ensure_session(session_id)
                repairs.append(("session", session_id))
        return repairs

    # -- 内部 ----------------------------------------------------------------

    def _write(self, path: Path, tools: list[str]) -> list[str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(
            path, json.dumps(tools, ensure_ascii=False, indent=2) + "\n"
        )
        # 保存时也查完整性：写盘后复读一次，确认落盘的内容与意图一致
        # （原子写保证不会半截，但"复读验证"防的是"规范化之前就算错"这类
        # 逻辑错误——本项目最讨厌的错误类型是"看起来成功了"）。
        raw = _load(path)
        try:
            return validate_config(raw, self.known)
        except ToolConfigError as exc:  # pragma: no cover - 原子写后不可能坏
            raise ToolConfigError(t("ui.toolconfig.error.write_failed")) from exc
