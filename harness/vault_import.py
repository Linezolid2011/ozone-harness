"""安全导入原生 Kimi Dream vault（`.zip`）—— A 阶段的「能导入」。

这个模块只负责**把原生导出搬进 data/vault/**，不做任何内容改写：
`README.md` / `index.md` / `log.md` / `observations.md` / `sections.yaml` /
`sections/**` / `entities/**` / `raw/source_<ref>/**` 原样保留。导入之后
`VaultStore.check()` 会如实报告"生成物是否与 sections 一致"等问题。

安全要点（与路径校验同源）：
  * 只接受**已知的原生路径**：根级白名单 + sections/entities 的 md +
    `sections/archived_month/<YYYY-MM>/this_month.md` + `raw/source_<n>/<图片>`;
  * 拒绝绝对路径、`..`、NUL、符号链接条目、大小写撞名；
  * 单文件 / 总量 / 条目数都有上限；
  * 图片按**文件内容**嗅探格式（`attachments.sniff_ext`），不信扩展名；
  * 先解到一次性暂存目录，全部校验通过后才搬进 vault —— 失败时一个字节都不动。
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import attachments
from .i18n import t
from .vault import (
    LOG_NAME,
    MEMORY_INSTRUCTION_NAME,
    OBSERVATIONS_NAME,
    RAW_META_SUFFIX,
    SECTIONS_CONFIG,
    VaultPathError,
    VaultStore,
    normalize_raw_path,
    normalize_rel_path,
)

MAX_ENTRIES = 2000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
# 原生导出里会出现的根级文件；别的一律拒绝（`memory_instruction.md` 是旁路导出）。
_ALLOWED_ROOT = {
    "README.md",
    "index.md",
    "log.md",
    "observations.md",
    "memory_instruction.md",
    SECTIONS_CONFIG,
}
_IGNORED_PREFIXES = ("__MACOSX/",)
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class VaultImportError(ValueError):
    """导入被拒绝（路径不合法 / 超限 / 目标不是空 vault）。"""


def _entry_kind(name: str) -> str:
    if name.startswith("raw/"):
        return "raw"
    return "note"


def _checked_rel(raw_name: str, store: VaultStore) -> tuple[str, str]:
    """返回 (canonical, kind)。不合法抛 VaultImportError。"""
    text = raw_name.replace("\\", "/")
    if text.startswith("/") or "\x00" in text or any(
        part == ".." for part in text.split("/")
    ):
        raise VaultImportError(t("ui.vault.import_bad_path", path=repr(raw_name)))
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts:
        raise VaultImportError(t("ui.vault.import_bad_path", path=repr(raw_name)))
    name = pure.as_posix()
    if len(pure.parts) == 1 and name not in _ALLOWED_ROOT:
        raise VaultImportError(t("ui.vault.import_bad_path", path=repr(raw_name)))
    try:
        if name.startswith("raw/"):
            return (
                normalize_raw_path(name, name_max_chars=store.name_max_chars * 3),
                "raw",
            )
        return normalize_rel_path(name, name_max_chars=store.name_max_chars), "note"
    except VaultPathError as exc:
        raise VaultImportError(
            t("ui.vault.import_bad_path", path=f"{raw_name}：{exc}")
        ) from exc


def _commit_staging(staging: Path, root: Path) -> None:
    """在同级临时目录里合并新旧 vault，再换名提交；复制失败绝不碰目标。"""
    root.parent.mkdir(parents=True, exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix=".vault-import-", dir=root.parent))
    candidate = transaction / "new"
    backup = transaction / "old"
    committed = False
    try:
        # 旧 copytree(..., dirs_exist_ok=True) 是覆盖同名文件、保留其余文件；
        # 先复制旧库到新候选目录，既保留这个语义，也让所有写入都远离真目标。
        if root.exists():
            shutil.copytree(root, candidate)
        else:
            candidate.mkdir()
        shutil.copytree(staging, candidate, dirs_exist_ok=True)
        try:
            if root.exists():
                root.replace(backup)
            # candidate 与 root 同在 root.parent 下，换名不会退化成跨盘复制。
            candidate.replace(root)
            committed = True
        except BaseException:
            if backup.exists():
                backup.replace(root)
            raise
    finally:
        # 若回滚自己也失败，备份就是唯一完整的旧库，必须留在磁盘上供恢复。
        if committed or not backup.exists():
            shutil.rmtree(transaction, ignore_errors=True)


def import_vault_zip(
    zip_path: Path,
    store: VaultStore,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """把 `zip_path` 导入 `store.root`。返回统计；失败时 vault 不变。"""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise VaultImportError(t("ui.vault.import_missing_zip", path=str(zip_path)))
    if store.root.exists() and any(store.root.iterdir()) and not overwrite:
        raise VaultImportError(t("ui.vault.import_not_empty", path=str(store.root)))

    warnings: list[str] = []
    entries: list[tuple[zipfile.ZipInfo, str, str]] = []
    seen: dict[str, str] = {}
    total = 0
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise VaultImportError(
            t("ui.vault.import_bad_zip", path=str(zip_path), error=exc)
        ) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise VaultImportError(
                t("ui.vault.import_too_many", count=len(infos), limit=MAX_ENTRIES)
            )
        for info in infos:
            raw_name = info.filename
            if raw_name.endswith("/"):
                continue
            if raw_name.startswith(_IGNORED_PREFIXES) or Path(raw_name).name in _IGNORED_NAMES:
                warnings.append(t("ui.vault.import_skipped", path=raw_name))
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise VaultImportError(
                    t("ui.vault.import_bad_path", path=f"{raw_name} (symlink)")
                )
            canonical, kind = _checked_rel(raw_name, store)
            key = canonical.casefold()
            if key in seen:
                if seen[key] != canonical:
                    raise VaultImportError(
                        t("ui.vault.import_case_clash", a=seen[key], b=canonical)
                    )
                raise VaultImportError(
                    t("ui.vault.import_bad_path", path=f"{raw_name} (duplicate)")
                )
            seen[key] = canonical
            if info.file_size > MAX_FILE_BYTES:
                raise VaultImportError(
                    t(
                        "ui.vault.import_file_too_large",
                        path=canonical,
                        size=info.file_size,
                        limit=MAX_FILE_BYTES,
                    )
                )
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise VaultImportError(
                    t("ui.vault.import_too_large", limit=MAX_TOTAL_BYTES)
                )
            entries.append((info, canonical, kind))

        if not entries:
            raise VaultImportError(t("ui.vault.import_empty", path=str(zip_path)))

        staging = Path(tempfile.mkdtemp(prefix="vault-import-"))
        try:
            counts = {"notes": 0, "raw": 0}
            read_total = 0
            for info, canonical, kind in entries:
                data = archive.read(info)
                # 不能只信 `ZipInfo.file_size`：伪造的中央目录可以少报大小。
                if len(data) > MAX_FILE_BYTES:
                    raise VaultImportError(
                        t(
                            "ui.vault.import_file_too_large",
                            path=canonical,
                            size=len(data),
                            limit=MAX_FILE_BYTES,
                        )
                    )
                read_total += len(data)
                if read_total > MAX_TOTAL_BYTES:
                    raise VaultImportError(
                        t("ui.vault.import_too_large", limit=MAX_TOTAL_BYTES)
                    )
                if kind == "raw" and not canonical.endswith(RAW_META_SUFFIX):
                    sniffed = attachments.sniff_ext(data)
                    if sniffed is None:
                        raise VaultImportError(
                            t("ui.vault.import_bad_image", path=canonical)
                        )
                    ext = canonical.rsplit(".", 1)[-1].lower()
                    if ext == "jpeg":
                        ext = "jpg"
                    if sniffed != ext:
                        raise VaultImportError(
                            t(
                                "ui.vault.import_image_mismatch",
                                path=canonical,
                                sniffed=sniffed,
                            )
                        )
                target = staging / canonical
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                counts["raw" if kind == "raw" else "notes"] += 1
            _commit_staging(staging, store.root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    manifest_warnings: list[str] = []
    if (store.root / SECTIONS_CONFIG).is_file():
        _manifest, manifest_warnings = store.manifest_report()
    return {
        "ok": True,
        "path": str(store.root),
        "files": len(entries),
        "notes": counts["notes"],
        "raw": counts["raw"],
        "native": store.is_native(),
        "warnings": [*warnings, *manifest_warnings],
    }
