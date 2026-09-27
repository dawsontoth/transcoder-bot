"""Command-line interface."""

from __future__ import annotations

import argparse
import dataclasses
import errno
import functools
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from slack_sdk.errors import SlackApiError

from transcoder_bot import __version__
from transcoder_bot.config import Config, ConfigError, load_config, resolve_config_path
from transcoder_bot.formatting import human_duration, human_size
from transcoder_bot.loudnorm import LoudnormError
from transcoder_bot.macos import ensure_mounted, keep_awake
from transcoder_bot.media import ProbeError, probe
from transcoder_bot.recordings import find_recordings, find_untranscoded, is_transcoded
from transcoder_bot.runner import FfmpegError, ThrottledProgress, find_executable, format_progress
from transcoder_bot.slack_bot import describe_error
from transcoder_bot.state import AlreadyRunningError, StateStore, default_state_dir
from transcoder_bot.transcode import OutputExistsError, TranscodeError, Transcoder
from transcoder_bot.trash import purge_trash

log = logging.getLogger("transcoder_bot")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2
TRANSCODE_ERRORS = (TranscodeError, FfmpegError, ProbeError, LoudnormError, ConfigError, OSError)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    # launchd stops jobs with SIGTERM; turn it into an exception so cleanup code runs
    # (ffmpeg gets stopped, the Slack poll is marked as interrupted).
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return EXIT_CONFIG
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return EXIT_FAILED
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130
    except OSError as exc:
        log.error("%s", exc)
        if exc.errno == errno.EPERM:
            log.error(
                "macOS privacy settings blocked that. Give Full Disk Access to %s "
                "(see Troubleshooting in the README).",
                Path(sys.executable).resolve(),
            )
        log.debug("Details:", exc_info=True)
        return EXIT_FAILED
    except SlackApiError as exc:
        log.error("Slack refused the request: %s", describe_error(str(exc.response.get("error"))))
        return EXIT_FAILED
    except Exception:
        log.exception("Unexpected error")
        return EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcoder-bot",
        description="Rotate, loudness-normalize and downscale HyperDeck recordings with ffmpeg.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="config file (default: $TRANSCODER_BOT_CONFIG, else "
        "~/.config/transcoder-bot/config.toml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    init = commands.add_parser("init-config", help="write a starter config file")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    init.set_defaults(handler=cmd_init_config)

    doctor = commands.add_parser("doctor", help="check ffmpeg, folders and Slack")
    doctor.add_argument(
        "--post-test", action="store_true", help="also post a test message to the Slack channel"
    )
    doctor.set_defaults(handler=cmd_doctor)

    scan = commands.add_parser("scan", help="list recordings from the lookback window")
    scan.set_defaults(handler=cmd_scan)

    transcode = commands.add_parser("transcode", help="transcode recordings next to the source")
    transcode.add_argument("files", nargs="*", type=Path, help="recordings to transcode")
    transcode.add_argument(
        "--new", action="store_true", help="transcode every recent recording not done yet"
    )
    transcode.add_argument("--force", action="store_true", help="redo existing outputs")
    transcode.add_argument(
        "--dry-run", action="store_true", help="show the plan without transcoding"
    )
    transcode.set_defaults(handler=cmd_transcode)

    poll = commands.add_parser(
        "poll", help="ask Slack which recording to keep, trash the rest, transcode the pick"
    )
    poll.add_argument(
        "--dry-run",
        action="store_true",
        help="post a real poll, but don't move, delete or transcode anything",
    )
    poll.add_argument("--timeout-hours", type=float, help="override slack.poll_timeout_hours")
    poll.set_defaults(handler=cmd_poll)

    purge = commands.add_parser(
        "purge-trash", help="delete trashed recordings older than trash.retention_days"
    )
    purge.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    purge.set_defaults(handler=cmd_purge_trash)

    schedule = commands.add_parser("schedule", help="run a command daily via launchd (macOS)")
    schedule.add_argument(
        "action", choices=("install", "uninstall", "print"), help="what to do with the agent"
    )
    schedule.add_argument("--at", default="18:00", help="time of day, 24-hour HH:MM (18:00)")
    schedule.add_argument("--days", help="only on these days, e.g. sun,wed (default: daily)")
    schedule.add_argument(
        "--run",
        choices=("poll", "transcode"),
        help="poll = Slack poll (default when Slack is set up); transcode = transcode --new",
    )
    schedule.set_defaults(handler=cmd_schedule)
    return parser


def cmd_init_config(args: argparse.Namespace) -> int:
    from transcoder_bot.config import write_example_config

    path = write_example_config(resolve_config_path(args.config), force=args.force)
    print(f"Wrote {path}")
    print("Edit recordings_dir (and the [slack] section for the poll), then run:")
    print("  transcoder-bot doctor")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from transcoder_bot.doctor import run_checks

    config = _load(args)
    print(f"Config: {resolve_config_path(args.config)}")
    failed = False
    for check in run_checks(config, slack_test_message=args.post_test):
        print(f"  {check}")
        failed |= check.status == "fail"
    print("\nSome checks failed." if failed else "\nAll good.")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    config = _load(args)
    ensure_mounted(config.recordings_dir, config.mount_url)
    recordings = find_recordings(config)
    hours = f"{config.scan.lookback_hours:g}"
    if not recordings:
        print(f"No recordings in {config.recordings_dir} from the last {hours} hours.")
        return EXIT_OK
    print(f"Recordings in {config.recordings_dir} from the last {hours} hours:")
    for recording in recordings:
        try:
            info = probe(recording.path, config.ffprobe)
            details = (
                f"{human_duration(info.duration):>8}  {info.width}x{info.height} {info.video_codec}"
            )
        except ProbeError:
            details = f"{'unreadable':>8}"
        done = "  (transcoded)" if is_transcoded(recording, config) else ""
        when = recording.modified.astimezone().strftime("%a %Y-%m-%d %H:%M")
        size = human_size(recording.size)
        print(f"  {when}  {size:>10}  {recording.name}  {details}{done}")
    return EXIT_OK


def cmd_transcode(args: argparse.Namespace) -> int:
    config = _load(args)
    if bool(args.files) == bool(args.new):
        raise ConfigError("Pass either recording file(s) or --new")
    transcoder = Transcoder(config)
    state = StateStore(default_state_dir())

    if args.dry_run:
        failures = 0
        for source in _sources(args, config):
            try:
                print(transcoder.describe(source))
            except TRANSCODE_ERRORS as exc:
                log.error("%s: %s", source.name, exc)
                failures += 1
        return EXIT_FAILED if failures else EXIT_OK

    failures = 0
    with state.lock(), keep_awake():
        sources = _sources(args, config)
        if not sources:
            log.info("Nothing new to transcode")
        reporter = ThrottledProgress(lambda p: log.info("%s", format_progress(p)), interval=30)
        for source in sources:
            try:
                transcoder.transcode(source, force=args.force, on_progress=reporter)
            except OutputExistsError as exc:
                log.info("Skipping: %s", exc)
            except TRANSCODE_ERRORS as exc:
                log.error("Transcoding %s failed: %s", source.name, exc)
                failures += 1
    return EXIT_FAILED if failures else EXIT_OK


def cmd_poll(args: argparse.Namespace) -> int:
    from transcoder_bot.poll import PollRunner
    from transcoder_bot.slack_bot import SlackBot

    config = _load(args)
    if not config.slack.enabled:
        raise ConfigError(
            f"The poll needs Slack settings: {', '.join(config.slack.missing())}. "
            "See the README's Slack setup section."
        )
    if args.timeout_hours is not None and args.timeout_hours <= 0:
        raise ConfigError("--timeout-hours must be greater than 0")
    state = StateStore(default_state_dir())
    with state.lock(), keep_awake():
        bot = SlackBot(
            bot_token=config.slack.bot_token,
            app_token=config.slack.app_token,
            channel=config.slack.channel,
            allowed_user_ids=config.slack.allowed_user_ids,
        )
        with bot:
            runner = PollRunner(
                config,
                bot,
                Transcoder(config),
                probe_fn=functools.partial(probe, ffprobe=config.ffprobe),
                state=state,
                prepare=functools.partial(ensure_mounted, config.recordings_dir, config.mount_url),
                dry_run=args.dry_run,
            )
            outcome = runner.run(timeout_hours=args.timeout_hours)
    log.info("Poll finished: %s", outcome.status)
    return EXIT_FAILED if outcome.status == "failed" else EXIT_OK


def cmd_purge_trash(args: argparse.Namespace) -> int:
    config = _load(args)
    ensure_mounted(config.recordings_dir, config.mount_url)
    purged = purge_trash(
        config.recordings_dir, config.trash, now=datetime.now(UTC), dry_run=args.dry_run
    )
    verb = "Would delete" if args.dry_run else "Deleted"
    for folder in purged:
        print(f"{verb} {folder}")
    if not purged:
        print("Nothing to purge.")
    return EXIT_OK


def cmd_schedule(args: argparse.Namespace) -> int:
    from transcoder_bot import schedule

    if args.action == "uninstall":
        schedule.unload_agent()
        path = schedule.agent_path()
        path.unlink(missing_ok=True)
        print(f"Removed {path}")
        return EXIT_OK

    config_path = resolve_config_path(args.config).absolute()
    config = _load(args)
    run = args.run or ("poll" if config.slack.enabled else "transcode")
    command = ["poll"] if run == "poll" else ["transcode", "--new"]
    program = [sys.executable, "-m", "transcoder_bot", "--config", str(config_path), *command]
    hour, minute = schedule.parse_time(args.at)
    agent = schedule.build_agent(
        program=program,
        hour=hour,
        minute=minute,
        weekdays=schedule.parse_days(args.days),
        log_path=schedule.default_log_path(),
    )
    if args.action == "print":
        import plistlib

        sys.stdout.write(plistlib.dumps(agent).decode())
        return EXIT_OK

    path = schedule.agent_path()
    schedule.write_agent(agent, path)
    schedule.load_agent(path)
    days = args.days or "every day"
    print(f"Installed {path}: runs `transcoder-bot {' '.join(command)}` at {args.at} ({days}).")
    print(f"Logs: {schedule.default_log_path()}")
    print(f"Run it now with: launchctl kickstart gui/$(id -u)/{schedule.LABEL}")
    return EXIT_OK


def _load(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    return dataclasses.replace(
        config, ffmpeg=find_executable(config.ffmpeg), ffprobe=find_executable(config.ffprobe)
    )


def _sources(args: argparse.Namespace, config: Config) -> list[Path]:
    if args.files:
        return [Path(f) for f in args.files]
    ensure_mounted(config.recordings_dir, config.mount_url)
    return [r.path for r in find_untranscoded(config)]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    if not verbose:
        # slack_sdk's Socket Mode client is chatty at INFO level.
        logging.getLogger("slack_sdk").setLevel(logging.WARNING)


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt
