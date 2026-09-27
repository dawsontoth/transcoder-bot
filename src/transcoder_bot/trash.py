"""Moving unpicked recordings to a trash folder on the NAS (or deleting them), and purging it.

Trashed files go into a dated subfolder, e.g. ``_Trash/2026-09-27/Service.mov``, so they can be
restored by moving them back, and purged by age later. Moving within the same NAS share is an
instant rename, however large the file.
"""

from __future__ import annotations

import errno
import logging
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from transcoder_bot.config import TrashConfig

log = logging.getLogger(__name__)

_DAY_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}")


class TrashError(RuntimeError):
    """A file couldn't be trashed (or refused to be, for safety)."""


@dataclass(frozen=True)
class TrashedFile:
    source: Path
    destination: Path | None  # None when the file was deleted permanently


def trash_root(recordings_dir: Path, trash: TrashConfig) -> Path:
    folder = Path(trash.folder).expanduser()
    return folder if folder.is_absolute() else recordings_dir / folder


def trash_files(
    paths: Iterable[Path],
    *,
    recordings_dir: Path,
    trash: TrashConfig,
    now: datetime,
    dry_run: bool = False,
) -> list[TrashedFile]:
    """Trash ``paths``. Every path is validated before anything is touched.

    For safety, only regular files directly inside ``recordings_dir`` are accepted. Files that
    have already disappeared (e.g. deleted by hand) are skipped.
    """
    base = recordings_dir.resolve()
    files = []
    for path in paths:
        resolved = path.resolve()
        if resolved.parent != base:
            raise TrashError(f"Refusing to trash {path}: it isn't directly inside {recordings_dir}")
        if not resolved.exists():
            log.warning("%s is already gone", path.name)
            continue
        if not resolved.is_file():
            raise TrashError(f"Refusing to trash {path}: it isn't a file")
        files.append(resolved)

    results = []
    if trash.mode == "delete":
        for path in files:
            log.info("%sDeleting %s", "[dry run] " if dry_run else "", path.name)
            if not dry_run:
                path.unlink()
            results.append(TrashedFile(path, None))
        return results

    day_folder = trash_root(base, trash) / now.astimezone().date().isoformat()
    if not dry_run:
        day_folder.mkdir(parents=True, exist_ok=True)
    for path in files:
        dest = _unique_path(day_folder / path.name)
        log.info("%sMoving %s to %s", "[dry run] " if dry_run else "", path.name, dest.parent)
        if not dry_run:
            _move(path, dest)
        results.append(TrashedFile(path, dest))
    return results


def purge_trash(
    recordings_dir: Path,
    trash: TrashConfig,
    *,
    now: datetime,
    dry_run: bool = False,
) -> list[Path]:
    """Permanently delete dated trash folders older than ``retention_days``.

    Only ``YYYY-MM-DD`` folders inside the trash folder are touched; anything else you keep
    there is left alone.
    """
    root = trash_root(recordings_dir, trash)
    if trash.mode != "folder" or trash.retention_days <= 0 or not root.is_dir():
        return []
    cutoff = now.astimezone().date() - timedelta(days=trash.retention_days)
    purged = []
    for folder in sorted(root.iterdir()):
        day = _folder_date(folder)
        if day is None or day > cutoff:
            continue
        log.info("%sPurging %s", "[dry run] " if dry_run else "", folder)
        if not dry_run:
            shutil.rmtree(folder)
        purged.append(folder)
    return purged


def describe_action(trash: TrashConfig) -> str:
    """How unpicked recordings are handled, phrased to follow "will be" in Slack messages."""
    if trash.mode == "delete":
        return "*permanently deleted*"
    text = f"moved to `{trash.folder}` on the NAS"
    if trash.retention_days:
        days = trash.retention_days
        text += f" (and deleted for good after {days} day{'s' if days != 1 else ''})"
    return text


def _folder_date(folder: Path) -> date | None:
    if not folder.is_dir() or not _DAY_FOLDER.fullmatch(folder.name):
        return None
    try:
        return date.fromisoformat(folder.name)
    except ValueError:
        return None


def _unique_path(path: Path) -> Path:
    """``Service.mov`` → ``Service (2).mov`` if it's already taken."""
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def _move(source: Path, dest: Path) -> None:
    try:
        source.rename(dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        log.warning(
            "%s is on a different volume than the trash folder; copying %s instead of renaming "
            "it (slow for big recordings)",
            dest.parent,
            source.name,
        )
        shutil.move(source, dest)
