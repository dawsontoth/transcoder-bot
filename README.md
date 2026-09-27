# transcoder-bot

Turns Blackmagic HyperDeck recordings on a NAS into vertical 1080p video on a Mac. It can also ask your team in Slack which take to keep, and upload the result to Descript for editing.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/flowchart/flowchart-dark.svg">
    <img alt="Flowchart. The HyperDeck records 4K takes to a NAS. Each day, the Mac finds takes from the last 48 hours and asks in a Slack poll which take to keep. Skip, or no answer: nothing changes. Keep this one: the other takes move to _Trash, and the pick is downscaled to 1080p, rotated 90° counter-clockwise, loudness-normalized and encoded as H.264 at 25 Mbit/s. Without Slack, every new take is transcoded. The result is saved next to the original as Name_1080p.mp4 and can also be uploaded to Descript, with the link posted in Slack." src="docs/flowchart/flowchart-light.svg">
  </picture>
</p>

## What it does

For each recording, transcoder-bot:

1. **Rotates** the picture 90° counter-clockwise.
2. **Normalizes the audio** to −16 LUFS, with two-pass EBU R128 loudness normalization.
3. **Downscales** 4K to 1080p, so the rotated video is 1080×1920.
4. **Saves** it next to the original as `<name>_1080p.mp4`: H.264 at about 25 Mbit/s, with AAC audio.

It runs every day on a Mac that has the NAS mounted. Two extras are optional:

- **Slack poll:** before transcoding, ask your team which take to keep, and move the rest to a trash folder.
- **Descript:** upload each finished video to a new Descript project, ready for editing.

## Get started

The **[setup guide](docs/setup.md)** walks through everything step by step, with a check after each step. The short version:

```sh
brew install ffmpeg uv
git clone https://github.com/dawsontoth/transcoder-bot.git ~/transcoder-bot
cd ~/transcoder-bot
uv sync --managed-python
uv run transcoder-bot init-config   # then set recordings_dir in the file it creates
uv run transcoder-bot doctor
uv run transcoder-bot schedule install --at 18:00
```

## Your settings live outside the repo

Everything you customize lives in your home folder, not in this repository. `git pull` never conflicts with your setup, and your tokens can't end up in a commit.

| What | Where |
|---|---|
| Settings, Slack tokens and the Descript key | `~/.config/transcoder-bot/config.toml`, readable only by you |
| The daily schedule | `~/Library/LaunchAgents/local.transcoder-bot.plist` |
| Logs | `~/Library/Logs/transcoder-bot.log` |
| Run state (lock, open poll) | `~/Library/Application Support/transcoder-bot/` |

The repo only holds the annotated [example config](src/transcoder_bot/config.example.toml). `init-config` copies it to your home folder; edit that copy, never the example. You can also keep tokens out of files entirely with the `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `DESCRIPT_API_KEY` environment variables.

## Documentation

- **[Setup guide](docs/setup.md):** install, configure, test and schedule it, plus changing settings, updating and uninstalling.
- **[How it works](docs/how-it-works.md):** the ffmpeg pipeline and why, the Slack poll, the trash folder and Descript.
- **[Troubleshooting](docs/troubleshooting.md)**
- **[All settings](src/transcoder_bot/config.example.toml)**, each explained.
- **[How was this created? Learn from my prompts!](docs/how-this-was-made.md)**

## Commands

| Command | What it does |
|---|---|
| `transcoder-bot init-config` | Writes a starter config to `~/.config/transcoder-bot/config.toml`. |
| `transcoder-bot doctor [--post-test]` | Checks ffmpeg, the folders, free space, Slack and Descript. |
| `transcoder-bot scan` | Lists recent recordings with their length and resolution. |
| `transcoder-bot transcode FILE… [--force] [--dry-run]` | Transcodes the given files. |
| `transcoder-bot transcode --new` | Transcodes every recent recording that isn't done yet. |
| `transcoder-bot poll [--dry-run] [--timeout-hours H]` | Runs the Slack poll, then trashes the other takes and transcodes the pick. |
| `transcoder-bot descript-upload FILE… [--name NAME]` | Uploads videos to new Descript projects, e.g. to retry a failed upload. |
| `transcoder-bot purge-trash [--dry-run]` | Deletes trash folders older than `retention_days`. `poll` does this too. |
| `transcoder-bot schedule install\|uninstall\|print` | Manages the daily launchd job. |

Run them from the repo folder as `uv run transcoder-bot …`. Every command accepts `--config PATH`, and `-v` for debug logging. `transcode` and `poll` also take `--no-descript`.

## Development

```sh
make install    # uv sync
make check      # lint, type-check and test (what CI runs)
make format     # auto-format and apply safe lint fixes
make test-unit  # skip the ffmpeg integration tests
make flowchart  # re-render the README flowchart (needs Node.js)
```

- **Python and packaging:** Python 3.11+ (3.13 pinned in `.python-version`), managed with uv.
- **Linting and formatting:** Ruff.
- **Type checking:** mypy in strict mode.
- **Tests:** pytest.
  - `tests/test_integration.py` runs the real ffmpeg. It transcodes a generated ProRes clip, checks pixels to prove the rotation goes counter-clockwise, and measures the output's loudness.
  - Those tests are skipped when ffmpeg isn't installed.
  - Slack and Descript are tested against local fakes, so no tokens are needed.
- **CI:** GitHub Actions runs the same checks on Ubuntu, with ffmpeg installed.
- **The flowchart:** the README shows pre-rendered SVGs, because GitHub's mobile app shows Mermaid charts as code. Edit `docs/flowchart/flowchart.mmd`, then run `make flowchart`.

```
src/transcoder_bot/
  cli.py              command-line entry point
  config.py           config loading and validation (see config.example.toml)
  recordings.py       finding recent recordings
  media.py            ffprobe wrapper
  commands.py         ffmpeg filter and argument builders (pure functions)
  loudnorm.py         two-pass loudness normalization helpers
  runner.py           running ffmpeg with progress reporting
  transcode.py        probe → measure → encode → verify → publish
  trash.py            the trash folder and purging it
  slack_messages.py   Slack Block Kit messages and click parsing (pure functions)
  slack_bot.py        Slack Web API and Socket Mode
  poll.py             the poll → trash → transcode → Descript flow
  descript.py         Descript API client and uploader
  state.py            the run lock and open-poll bookkeeping
  schedule.py         the launchd job
  macos.py            caffeinate and mounting the share
  doctor.py           setup checks
```

## License

[ISC](LICENSE)
