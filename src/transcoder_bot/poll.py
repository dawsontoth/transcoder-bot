"""The Slack poll: ask which recording to keep, trash the others, transcode the pick."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from transcoder_bot.config import Config
from transcoder_bot.descript import DescriptUpload
from transcoder_bot.formatting import human_duration, human_size
from transcoder_bot.media import MediaInfo, ProbeError
from transcoder_bot.recordings import find_untranscoded
from transcoder_bot.runner import ProgressCallback, ThrottledProgress, format_progress
from transcoder_bot.slack_messages import (
    MAX_OPTIONS,
    Block,
    Decision,
    Poll,
    PollOption,
    closed_blocks,
    escape,
    poll_blocks,
    poll_text,
)
from transcoder_bot.state import PendingPoll, StateStore
from transcoder_bot.transcode import TranscodeResult
from transcoder_bot.trash import TrashedFile, TrashError, describe_action, purge_trash, trash_files

log = logging.getLogger(__name__)


class PollTransport(Protocol):
    """What the poll needs from Slack (implemented by SlackBot; faked in tests)."""

    channel: str

    def open_poll(self, poll: Poll) -> None: ...

    def wait_for_decision(self, timeout: float) -> Decision | None: ...

    def post(
        self, text: str, *, blocks: list[Block] | None = None, thread_ts: str | None = None
    ) -> str: ...

    def update(self, ts: str, text: str, *, blocks: list[Block] | None = None) -> None: ...


class TranscoderLike(Protocol):
    def output_path(self, source: Path) -> Path: ...

    def transcode(
        self, source: Path, *, force: bool = False, on_progress: ProgressCallback | None = None
    ) -> TranscodeResult: ...


class UploaderLike(Protocol):
    def upload(
        self,
        video: Path,
        *,
        title: str,
        recorded: datetime | None = None,
        name: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> DescriptUpload: ...


@dataclass(frozen=True)
class PollOutcome:
    status: str  # "nothing-new", "expired", "skipped", "done" or "failed"
    chosen: Path | None = None
    trashed: tuple[TrashedFile, ...] = ()
    result: TranscodeResult | None = None
    error: str | None = None
    descript_url: str | None = None
    descript_error: str | None = None


def describe_transcode(config: Config) -> str:
    """E.g. "rotated 90° counter-clockwise, scaled to 1080p and loudness-normalized"."""
    rotations = {
        "ccw": "rotated 90° counter-clockwise",
        "cw": "rotated 90° clockwise",
        "180": "rotated 180°",
    }
    steps = [rotations[config.video.rotate]] if config.video.rotate in rotations else []
    steps.append(f"scaled to {config.video.short_side}p")
    if config.audio.normalize:
        steps.append("loudness-normalized")
    return steps[0] if len(steps) == 1 else ", ".join(steps[:-1]) + " and " + steps[-1]


class PollRunner:
    def __init__(
        self,
        config: Config,
        slack: PollTransport,
        transcoder: TranscoderLike,
        *,
        probe_fn: Callable[[Path], MediaInfo],
        state: StateStore | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        prepare: Callable[[], None] | None = None,
        uploader: UploaderLike | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.slack = slack
        self.transcoder = transcoder
        self.uploader = uploader
        self._probe = probe_fn
        self._state = state
        self._now = now
        self._prepare = prepare
        self.dry_run = dry_run

    def run(self, *, timeout_hours: float | None = None) -> PollOutcome:
        try:
            return self._run(timeout_hours or self.config.slack.poll_timeout_hours)
        except Exception as exc:
            self._post(f"❌ transcoder-bot ran into a problem: {escape(str(exc))}")
            raise

    def _run(self, timeout_hours: float) -> PollOutcome:
        self._expire_stale_poll()
        if self._prepare is not None:
            self._prepare()
        purge_trash(
            self.config.recordings_dir, self.config.trash, now=self._now(), dry_run=self.dry_run
        )

        options, unreadable, omitted = self._collect_options()
        if not options:
            message = (
                f"No new recordings from the last {self.config.scan.lookback_hours:g} hours "
                f"in `{escape(str(self.config.recordings_dir))}`."
            )
            log.info("No new recordings to poll about")
            if self.config.slack.notify_when_empty:
                self._post(message)
            return PollOutcome("nothing-new")

        poll = Poll(
            id=secrets.token_hex(6),
            options=tuple(options),
            deadline=self._now() + timedelta(hours=timeout_hours),
            folder=str(self.config.recordings_dir),
            lookback_hours=self.config.scan.lookback_hours,
            transcode_summary=describe_transcode(self.config),
            trash_action=describe_action(self.config.trash),
            unreadable=tuple(unreadable),
            omitted=omitted,
            dry_run=self.dry_run,
        )
        self.slack.open_poll(poll)
        ts = self.slack.post(poll_text(poll), blocks=poll_blocks(poll))
        log.info(
            "Posted a poll with %d recording(s); waiting up to %g h", len(options), timeout_hours
        )
        if self._state is not None:
            self._state.set_pending_poll(PendingPoll(self.slack.channel, ts, poll.id))
        try:
            decision = self.slack.wait_for_decision(timeout_hours * 3600)
        except BaseException:
            headline = "⚠️ This poll was interrupted before anyone picked. Nothing was deleted."
            self._close(poll, ts, headline=headline, status="transcoder-bot stopped.")
            raise
        finally:
            if self._state is not None:
                self._state.clear_pending_poll()

        if decision is None:
            log.info("Nobody picked within %g hours", timeout_hours)
            headline = (
                f"⌛ Nobody picked a recording within {timeout_hours:g} hours, so nothing was "
                "deleted or transcoded."
            )
            self._close(poll, ts, headline=headline, status="Poll closed.")
            return PollOutcome("expired")
        if decision.option is None:
            log.info("%s skipped the poll", decision.user_id)
            headline = (
                f"⏭️ <@{decision.user_id}> skipped this one. Nothing was deleted or transcoded."
            )
            self._close(poll, ts, headline=headline, status="Poll closed.")
            return PollOutcome("skipped")
        return self._keep(poll, ts, decision.user_id, decision.option)

    def _keep(self, poll: Poll, ts: str, user_id: str, chosen: PollOption) -> PollOutcome:
        source = chosen.recording.path
        others = [o for o in poll.options if o is not chosen]
        headline = f"🎬 <@{user_id}> picked *{escape(chosen.key)}*."
        log.info("%s picked %s", user_id, chosen.key)
        dry = "[Dry run] " if self.dry_run else ""

        trashed: list[TrashedFile] = []
        if others:
            self._close(poll, ts, headline=headline, status="🗑️ Trashing the others…", chosen=chosen)
            failures = []
            for other in others:  # one at a time, so one failure doesn't block the rest
                try:
                    trashed += trash_files(
                        [other.recording.path],
                        recordings_dir=self.config.recordings_dir,
                        trash=self.config.trash,
                        now=self._now(),
                        dry_run=self.dry_run,
                    )
                except (TrashError, OSError) as exc:
                    log.exception("Couldn't trash %s", other.key)
                    failures.append(f"{escape(other.key)} ({escape(str(exc))})")
            if trashed:
                names = ", ".join(escape(t.source.name) for t in trashed)
                self._post(f"🗑️ {dry}{len(trashed)} recording(s) {poll.trash_action}: {names}", ts)
            if failures:
                self._post(f"⚠️ Couldn't trash: {'; '.join(failures)}", ts)
        trashed_keys = {t.source.name for t in trashed}

        def status(text: str) -> None:
            self._close(
                poll, ts, headline=headline, status=text, chosen=chosen, trashed=trashed_keys
            )

        output_name = escape(self.transcoder.output_path(source).name)
        if self.dry_run:
            then = " and send it to Descript" if self.uploader is not None else ""
            self._post(f"{dry}Would transcode *{escape(chosen.key)}* → *{output_name}*{then}.", ts)
            status("Dry run finished: nothing was changed.")
            return PollOutcome("done", chosen=source, trashed=tuple(trashed))

        progress_ts = self._post(f"⚙️ Transcoding *{escape(chosen.key)}*…", ts)
        status("⚙️ Transcoding… (progress in the thread)")
        reporter = None
        if progress_ts and self.config.slack.progress_update_seconds:
            message_ts = progress_ts
            reporter = ThrottledProgress(
                lambda p: self.slack.update(message_ts, f"⚙️ {format_progress(p)}"),
                self.config.slack.progress_update_seconds,
            )
        try:
            result = self.transcoder.transcode(source, on_progress=reporter)
        except Exception as exc:
            log.exception("Transcoding %s failed", chosen.key)
            self._post_or_update(
                progress_ts, f"❌ Transcoding *{escape(chosen.key)}* failed: {escape(str(exc))}", ts
            )
            status("❌ Transcoding failed. Details in the thread.")
            return PollOutcome("failed", chosen=source, trashed=tuple(trashed), error=str(exc))

        done = (
            f"✅ Saved *{escape(result.output.name)}* ({human_size(result.output_bytes)}) next "
            f"to the original. Took {human_duration(result.elapsed)}."
        )
        if result.loudness_before is not None:
            done += f" Audio was {result.loudness_before.input_i:.1f} LUFS"
            after = result.loudness_after
            if after is not None and after.output_i is not None:
                done += f", now {after.output_i:.1f} LUFS"
            done += "."
        self._post_or_update(progress_ts, done, ts)
        saved = f"✅ Saved *{escape(result.output.name)}* next to the original."
        if self.uploader is None:
            status(saved)
            return PollOutcome("done", chosen=source, trashed=tuple(trashed), result=result)

        status(f"{saved} 📝 Sending it to Descript…")
        url, error = self._send_to_descript(result.output, chosen, ts)
        if url:
            status(f"{saved} 📝 <{url}|Open in Descript>")
        else:
            status(f"{saved} ⚠️ Sending it to Descript failed; details in the thread.")
        return PollOutcome(
            "done",
            chosen=source,
            trashed=tuple(trashed),
            result=result,
            descript_url=url,
            descript_error=error,
        )

    def _send_to_descript(
        self, video: Path, chosen: PollOption, ts: str
    ) -> tuple[str | None, str | None]:
        """Upload the finished video; returns (project URL, error message)."""
        assert self.uploader is not None
        message_ts = self._post(f"📝 Sending *{escape(video.name)}* to Descript…", ts)
        reporter = None
        if message_ts and self.config.slack.progress_update_seconds:
            progress_ts = message_ts
            reporter = ThrottledProgress(
                lambda p: self.slack.update(progress_ts, f"📝 {format_progress(p)}"),
                self.config.slack.progress_update_seconds,
            )
        try:
            upload = self.uploader.upload(
                video,
                title=chosen.recording.path.stem,
                recorded=chosen.recording.modified,
                on_progress=reporter,
            )
        except Exception as exc:
            log.exception("Sending %s to Descript failed", video.name)
            retry = f'transcoder-bot descript-upload "{video}"'
            self._post_or_update(
                message_ts,
                f"⚠️ Couldn't send it to Descript: {escape(str(exc))}\nTo retry: `{retry}`",
                ts,
            )
            return None, str(exc)
        notes = []
        if upload.shrunk:
            notes.append("It's a smaller copy, to fit Descript's upload limit.")
        if not upload.finished:
            notes.append("Descript is still processing it.")
        text = f"📝 Ready to edit in Descript: <{upload.project_url}|{escape(upload.project_name)}>"
        self._post_or_update(message_ts, " ".join([text, *notes]), ts)
        return upload.project_url, None

    def _collect_options(self) -> tuple[list[PollOption], list[str], int]:
        recordings = find_untranscoded(self.config, now=self._now())
        omitted = max(0, len(recordings) - MAX_OPTIONS)
        if omitted:
            log.warning(
                "%d recordings found; only the newest %d fit in a poll",
                len(recordings),
                MAX_OPTIONS,
            )
            recordings = recordings[omitted:]
        options, unreadable = [], []
        for recording in recordings:
            try:
                duration = self._probe(recording.path).duration
            except ProbeError as exc:
                log.warning("Leaving %s out of the poll: %s", recording.name, exc)
                unreadable.append(recording.name)
                continue
            options.append(PollOption(recording, duration))
        return options, unreadable, omitted

    def _expire_stale_poll(self) -> None:
        """If a previous run died mid-poll, disable that poll's buttons."""
        stale = self._state.pending_poll() if self._state is not None else None
        if stale is None:
            return
        if stale.channel == self.slack.channel:
            text = (
                "⚠️ This poll was interrupted (transcoder-bot stopped before anyone picked). "
                "Nothing was deleted."
            )
            try:
                self.slack.update(stale.ts, text, blocks=[_text_block(text)])
            except Exception:
                log.warning("Couldn't close the interrupted poll", exc_info=True)
        if self._state is not None:
            self._state.clear_pending_poll()

    def _close(
        self,
        poll: Poll,
        ts: str,
        *,
        headline: str,
        status: str,
        chosen: PollOption | None = None,
        trashed: set[str] | None = None,
    ) -> None:
        blocks = closed_blocks(
            poll,
            headline=headline,
            status=status,
            chosen_key=chosen.key if chosen else None,
            trashed_keys=trashed or set(),
        )
        try:
            self.slack.update(ts, poll_text(poll), blocks=blocks)
        except Exception:
            log.warning("Couldn't update the poll message", exc_info=True)

    def _post(self, text: str, thread_ts: str | None = None) -> str | None:
        """Best effort: a Slack hiccup must never stop the trash/transcode work."""
        try:
            return self.slack.post(text, thread_ts=thread_ts)
        except Exception:
            log.warning("Couldn't post to Slack: %s", text, exc_info=True)
            return None

    def _post_or_update(self, ts: str | None, text: str, thread_ts: str) -> None:
        if ts is None:
            self._post(text, thread_ts)
            return
        try:
            self.slack.update(ts, text)
        except Exception:
            log.warning("Couldn't update Slack: %s", text, exc_info=True)


def _text_block(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}
