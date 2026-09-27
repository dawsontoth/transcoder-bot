"""Loading and validating the TOML configuration file.

Every setting except ``recordings_dir`` has a default; see ``config.example.toml`` for the
annotated reference. Unknown keys are rejected so typos don't silently fall back to defaults.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import stat
import tomllib
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn, TypeVar

log = logging.getLogger(__name__)

CONFIG_ENV_VAR = "TRANSCODER_BOT_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/transcoder-bot/config.toml")

ROTATIONS = ("ccw", "cw", "180", "none")
ENCODERS = ("libx264", "libx265", "h264_videotoolbox", "hevc_videotoolbox")
SOFTWARE_ENCODERS = ("libx264", "libx265")
X264_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)
OUTPUT_EXTENSIONS = (".mp4", ".mov", ".m4v")
TRASH_MODES = ("folder", "delete")
TEAM_ACCESS_LEVELS = ("", "edit", "comment", "none")


class ConfigError(ValueError):
    """The configuration is missing or invalid."""


_BITRATE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])")


def bitrate_kbps(text: str) -> int:
    """``"25M"`` → ``25000``; ``"8000k"`` → ``8000``."""
    match = _BITRATE.fullmatch(text.strip())
    if match is None:
        raise ConfigError(f"Not a bitrate: {text!r} (expected something like 25M or 8000k)")
    value, unit = float(match.group(1)), match.group(2).lower()
    return round(value * (1000 if unit == "m" else 1))


def _fail(message: str) -> NoReturn:
    raise ConfigError(message)


def _check(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


@dataclass(frozen=True)
class ScanConfig:
    """Which files in the recordings folder count as candidate recordings."""

    lookback_hours: float = 48.0
    min_age_minutes: float = 5.0
    extensions: tuple[str, ...] = (".mov", ".mp4", ".mxf")

    def __post_init__(self) -> None:
        _check(self.lookback_hours > 0, "scan.lookback_hours must be greater than 0")
        _check(self.min_age_minutes >= 0, "scan.min_age_minutes can't be negative")
        _check(len(self.extensions) > 0, "scan.extensions must list at least one extension")
        normalized = tuple("." + ext.strip().lower().lstrip(".") for ext in self.extensions)
        object.__setattr__(self, "extensions", normalized)


@dataclass(frozen=True)
class VideoConfig:
    """Rotation, scaling and video encoder settings."""

    rotate: str = "ccw"
    short_side: int = 1080
    encoder: str = "libx264"
    bitrate: str = "25M"
    crf: int = 20
    preset: str = "medium"
    vt_quality: int = 65
    hwaccel: str = ""
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check(self.rotate in ROTATIONS, f"video.rotate must be one of {ROTATIONS}")
        _check(
            self.short_side >= 16 and self.short_side % 2 == 0,
            "video.short_side must be an even number of pixels (e.g. 1080)",
        )
        _check(self.encoder in ENCODERS, f"video.encoder must be one of {ENCODERS}")
        _check(
            self.bitrate == "" or _BITRATE.fullmatch(self.bitrate) is not None,
            'video.bitrate must look like "25M" or "8000k" (or "" to use crf instead)',
        )
        _check(0 <= self.crf <= 51, "video.crf must be between 0 and 51")
        _check(self.preset in X264_PRESETS, f"video.preset must be one of {X264_PRESETS}")
        _check(1 <= self.vt_quality <= 100, "video.vt_quality must be between 1 and 100")


@dataclass(frozen=True)
class AudioConfig:
    """Channel selection and EBU R128 loudness normalization settings."""

    normalize: bool = True
    target_lufs: float = -16.0
    true_peak: float = -1.5
    lra: float = 11.0
    stream: int = 0
    channels: tuple[int, ...] = (1, 2)
    mono: bool = False
    bitrate: str = "192k"
    sample_rate: int = 48000

    def __post_init__(self) -> None:
        # Ranges are the ones ffmpeg's loudnorm filter accepts.
        _check(-70 <= self.target_lufs <= -5, "audio.target_lufs must be between -70 and -5")
        _check(-9 <= self.true_peak <= 0, "audio.true_peak must be between -9 and 0")
        _check(1 <= self.lra <= 50, "audio.lra must be between 1 and 50")
        _check(self.stream >= 0, "audio.stream can't be negative")
        _check(
            len(self.channels) in (1, 2) and all(c >= 1 for c in self.channels),
            "audio.channels must list one or two channel numbers, starting at 1 (e.g. [1, 2])",
        )
        _check(
            re.fullmatch(r"\d+k", self.bitrate) is not None,
            'audio.bitrate must look like "192k"',
        )
        _check(self.sample_rate in (44100, 48000), "audio.sample_rate must be 44100 or 48000")


@dataclass(frozen=True)
class OutputConfig:
    """How the transcoded file next to the source is named."""

    suffix: str = "_1080p"
    extension: str = ".mp4"

    def __post_init__(self) -> None:
        # An empty suffix could make the output path equal the source path.
        _check(self.suffix != "", "output.suffix can't be empty (the source would be overwritten)")
        _check(
            "/" not in self.suffix and "\\" not in self.suffix,
            "output.suffix can't contain slashes",
        )
        _check(
            self.extension.lower() in OUTPUT_EXTENSIONS,
            f"output.extension must be one of {OUTPUT_EXTENSIONS}",
        )


@dataclass(frozen=True)
class TrashConfig:
    """What happens to the recordings that weren't picked in the Slack poll."""

    mode: str = "folder"
    folder: str = "_Trash"
    retention_days: int = 7

    def __post_init__(self) -> None:
        _check(self.mode in TRASH_MODES, f"trash.mode must be one of {TRASH_MODES}")
        _check(self.folder.strip() != "", "trash.folder can't be empty")
        _check(self.retention_days >= 0, "trash.retention_days can't be negative")


@dataclass(frozen=True)
class SlackConfig:
    """Slack app credentials and poll behaviour."""

    bot_token: str = field(default="", repr=False)
    app_token: str = field(default="", repr=False)
    channel: str = ""
    poll_timeout_hours: float = 12.0
    allowed_user_ids: tuple[str, ...] = ()
    notify_when_empty: bool = False
    progress_update_seconds: int = 60

    def __post_init__(self) -> None:
        _check(
            self.bot_token == "" or self.bot_token.startswith("xoxb-"),
            "slack.bot_token should be the Bot User OAuth Token (starts with xoxb-)",
        )
        _check(
            self.app_token == "" or self.app_token.startswith("xapp-"),
            "slack.app_token should be an app-level token with connections:write "
            "(starts with xapp-)",
        )
        _check(self.poll_timeout_hours > 0, "slack.poll_timeout_hours must be greater than 0")
        _check(self.progress_update_seconds >= 0, "slack.progress_update_seconds can't be negative")

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.app_token and self.channel)

    def missing(self) -> list[str]:
        """Names of the settings that still need a value before the poll can run."""
        names = {"bot_token": self.bot_token, "app_token": self.app_token, "channel": self.channel}
        return [f"slack.{name}" for name, value in names.items() if not value]


@dataclass(frozen=True)
class DescriptConfig:
    """Uploading finished videos to Descript for editing."""

    api_key: str = field(default="", repr=False)
    project_name: str = "{date} {stem}"
    folder: str = ""
    team_access: str = ""
    language: str = ""
    wait_minutes: float = 60.0
    api_url: str = "https://descriptapi.com/v1/"

    def __post_init__(self) -> None:
        _check(
            self.team_access in TEAM_ACCESS_LEVELS,
            f"descript.team_access must be one of {TEAM_ACCESS_LEVELS}",
        )
        _check(
            self.language == "" or re.fullmatch(r"[a-z]{2}", self.language) is not None,
            'descript.language must be a two-letter code like "en" (or empty to auto-detect)',
        )
        _check(self.wait_minutes >= 0, "descript.wait_minutes can't be negative")
        _check(
            self.api_url.startswith(("https://", "http://")),
            "descript.api_url must be an http(s) URL",
        )
        try:
            rendered = self.project_name.format(stem="x", date="2026-01-01")
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(
                "descript.project_name can only use the placeholders {stem} and {date}"
            ) from exc
        _check(rendered.strip() != "", "descript.project_name can't be empty")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class Config:
    recordings_dir: Path
    mount_url: str = ""
    temp_dir: Path | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    scan: ScanConfig = field(default_factory=ScanConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    trash: TrashConfig = field(default_factory=TrashConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    descript: DescriptConfig = field(default_factory=DescriptConfig)


_SECTIONS = ("scan", "video", "audio", "output", "trash", "slack", "descript")
_TOP_LEVEL = ("recordings_dir", "mount_url", "temp_dir", "ffmpeg", "ffprobe")

T = TypeVar("T")


def resolve_config_path(path: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """``--config`` wins, then ``$TRANSCODER_BOT_CONFIG``, then the default location."""
    env = os.environ if env is None else env
    if path is None:
        path = Path(env[CONFIG_ENV_VAR]) if env.get(CONFIG_ENV_VAR) else DEFAULT_CONFIG_PATH
    return path.expanduser()


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = resolve_config_path(path, env)
    if not path.is_file():
        raise ConfigError(
            f"No config file at {path}. Run `transcoder-bot init-config` to create one."
        )
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} isn't valid TOML: {exc}") from exc
    config = parse_config(data, env=env, base_dir=path.parent)
    _warn_if_shared(path, data)
    return config


def parse_config(
    data: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> Config:
    """Build a validated :class:`Config` from parsed TOML."""
    env = {} if env is None else env
    unknown = sorted(set(data) - set(_SECTIONS) - set(_TOP_LEVEL))
    _check(not unknown, f"Unknown setting(s): {', '.join(unknown)}")

    recordings_dir = data.get("recordings_dir")
    if not isinstance(recordings_dir, str) or not recordings_dir.strip():
        _fail(
            "recordings_dir is required: the folder your HyperDeck records into, "
            'e.g. "/Volumes/HyperDeck"'
        )
    temp_dir = _top_level_str(data, "temp_dir")

    slack = _build_section(SlackConfig, data.get("slack"), "slack")
    if env.get("SLACK_BOT_TOKEN") or env.get("SLACK_APP_TOKEN"):
        slack = dataclasses.replace(
            slack,
            bot_token=env.get("SLACK_BOT_TOKEN") or slack.bot_token,
            app_token=env.get("SLACK_APP_TOKEN") or slack.app_token,
        )

    descript = _build_section(DescriptConfig, data.get("descript"), "descript")
    if env.get("DESCRIPT_API_KEY"):
        descript = dataclasses.replace(descript, api_key=env["DESCRIPT_API_KEY"])

    return Config(
        recordings_dir=_resolve_path(recordings_dir, base_dir),
        mount_url=_top_level_str(data, "mount_url"),
        temp_dir=_resolve_path(temp_dir, base_dir) if temp_dir else None,
        ffmpeg=_top_level_str(data, "ffmpeg") or "ffmpeg",
        ffprobe=_top_level_str(data, "ffprobe") or "ffprobe",
        scan=_build_section(ScanConfig, data.get("scan"), "scan"),
        video=_build_section(VideoConfig, data.get("video"), "video"),
        audio=_build_section(AudioConfig, data.get("audio"), "audio"),
        output=_build_section(OutputConfig, data.get("output"), "output"),
        trash=_build_section(TrashConfig, data.get("trash"), "trash"),
        slack=slack,
        descript=descript,
    )


def example_config_text() -> str:
    """The annotated example config that ships with the package."""
    return resources.files("transcoder_bot").joinpath("config.example.toml").read_text("utf-8")


def write_example_config(dest: Path, *, force: bool = False) -> Path:
    """Write the example config to ``dest`` (mode 600, since it will hold Slack tokens)."""
    dest = dest.expanduser()
    if dest.exists() and not force:
        raise ConfigError(f"{dest} already exists (use --force to overwrite it)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(example_config_text())
    dest.chmod(0o600)
    return dest


def _top_level_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        _fail(f"{key} must be a string")
    return value.strip()


def _resolve_path(value: str, base_dir: Path | None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path


def _build_section(cls: type[T], raw: object, name: str) -> T:
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        _fail(f"[{name}] must be a table")
    known = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(raw) - known)
    _check(not unknown, f"Unknown setting(s) in [{name}]: {', '.join(unknown)}")
    hints = typing.get_type_hints(cls)
    kwargs = {key: _coerce(value, hints[key], f"{name}.{key}") for key, value in raw.items()}
    return cls(**kwargs)


def _coerce(value: object, hint: Any, key: str) -> Any:
    """Check a TOML value against the dataclass field's type (TOML ints are accepted as floats)."""
    if hint is bool:
        if not isinstance(value, bool):
            _fail(f"{key} must be true or false")
        return value
    if hint is int:
        if not isinstance(value, int) or isinstance(value, bool):
            _fail(f"{key} must be a whole number")
        return value
    if hint is float:
        if not isinstance(value, int | float) or isinstance(value, bool):
            _fail(f"{key} must be a number")
        return float(value)
    if hint is str:
        if not isinstance(value, str):
            _fail(f"{key} must be a string")
        return value
    if typing.get_origin(hint) is tuple:
        if not isinstance(value, list):
            _fail(f"{key} must be a list")
        item_type = typing.get_args(hint)[0]
        return tuple(_coerce(item, item_type, f"{key}[{i}]") for i, item in enumerate(value))
    raise TypeError(f"Unsupported config field type for {key}: {hint!r}")


def _warn_if_shared(path: Path, data: Mapping[str, Any]) -> None:
    secrets = {"slack": ("bot_token", "app_token"), "descript": ("api_key",)}
    has_secrets = any(
        isinstance(data.get(section), dict) and any(data[section].get(key) for key in keys)
        for section, keys in secrets.items()
    )
    if has_secrets and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning(
            "%s holds API tokens but other users can read it. Fix with: chmod 600 %s",
            path,
            path,
        )
