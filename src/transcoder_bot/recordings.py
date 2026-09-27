"""Finding recent HyperDeck recordings in the recordings folder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from transcoder_bot.config import Config
from transcoder_bot.transcode import is_output_file, output_path_for


@dataclass(frozen=True)
class Recording:
    path: Path
    size: int
    modified: datetime  # last write, i.e. roughly when the recording stopped

    @property
    def name(self) -> str:
        return self.path.name


def find_recordings(config: Config, *, now: datetime | None = None) -> list[Recording]:
    """Recordings modified within the lookback window, oldest first.

    Skips hidden files (including macOS ``._`` AppleDouble files on SMB shares and our own
    in-progress ``.partial`` copies), files we produced, empty files, and files touched within
    the last few minutes (they may still be recording). Doesn't descend into subfolders, so the
    trash folder is never scanned.
    """
    folder = config.recordings_dir
    if not folder.is_dir():
        raise FileNotFoundError(f"Recordings folder {folder} not found. Is the NAS mounted?")
    now = now or datetime.now(UTC)
    earliest = now - timedelta(hours=config.scan.lookback_hours)
    latest = now - timedelta(minutes=config.scan.min_age_minutes)

    found = []
    for path in folder.iterdir():
        if path.name.startswith(".") or path.suffix.lower() not in config.scan.extensions:
            continue
        if is_output_file(path, config.output) or not path.is_file():
            continue
        info = path.stat()
        modified = datetime.fromtimestamp(info.st_mtime, tz=UTC)
        if info.st_size > 0 and earliest <= modified <= latest:
            found.append(Recording(path=path, size=info.st_size, modified=modified))
    return sorted(found, key=lambda r: (r.modified, r.name))


def is_transcoded(recording: Recording, config: Config) -> bool:
    return output_path_for(recording.path, config.output).exists()


def find_untranscoded(config: Config, *, now: datetime | None = None) -> list[Recording]:
    return [r for r in find_recordings(config, now=now) if not is_transcoded(r, config)]
