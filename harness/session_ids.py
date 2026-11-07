"""会话 id 与事件日志路径的统一校验（ADR-049）。

这个模块不依赖 Web 或 Runtime，调用方可以在任何磁盘写入之前使用它。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 白名单比过滤 `..` / 分隔符可靠：Windows 还有盘符、ADS、设备名等特殊路径。
SESSION_ID_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$")

_WINDOWS_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def _invalid_session_id(session_id: Any) -> ValueError:
    shown = repr(str(session_id)[:64])
    return ValueError(
        f"非法会话 id：{shown}。请使用 1–64 位字母、数字、下划线或连字符，"
        "并以字母或数字开头。"
    )


def validate_session_id(session_id: Any) -> str:
    """返回合法的原始 id；非法时抛 ValueError，不创建目录或文件。"""
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise _invalid_session_id(session_id)
    # 即使加了扩展名，Windows 仍会把 CON.jsonl 之类名字当成设备而不是普通文件。
    if session_id.upper() in _WINDOWS_DEVICE_NAMES:
        raise _invalid_session_id(session_id)
    return session_id


def session_log_path(sessions_dir: str | Path, session_id: Any) -> Path:
    """校验 id 并返回事件日志路径；用路径包含关系再兜一层，但不落盘。"""
    sid = validate_session_id(session_id)
    root = Path(sessions_dir)
    candidate = root / f"{sid}.jsonl"
    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
        contained = resolved_candidate.is_relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid_session_id(session_id) from exc
    if not contained:
        raise _invalid_session_id(session_id)
    return candidate
