# Troubleshooting

[← Back to the README](../README.md)

Start with the log, `~/Library/Logs/transcoder-bot.log`, and `uv run transcoder-bot doctor`. Add `-v` to any command for more detail.

## The NAS and macOS

- **`Operation not permitted`.** macOS privacy settings are blocking access to the NAS. See [setup step 9](setup.md#9-let-macos-allow-it).
- **`Recordings folder … not found. Is the NAS mounted?`** Mount the share in Finder, or set `mount_url` so the job can mount it.
- **The share shows up as `/Volumes/HyperDeck-1`.** macOS left an empty `/Volumes/HyperDeck` folder behind after a disconnect. Eject the share and reconnect it; restart if the folder is still there.
- **`Mounting … timed out`.** macOS is waiting at a login dialog because no password is saved. Connect once in Finder with **Remember this password** ticked.
- **The job didn't run.**
  - The Mac must be logged in; a locked screen is fine.
  - Check the job is loaded with `launchctl print gui/$(id -u)/local.transcoder-bot`.
  - Run `uv run transcoder-bot schedule install` again if it isn't.

## The video

- **Rotated the wrong way.** Set `rotate = "cw"` under `[video]`, then redo the file with `transcode --force FILE`.
- **Silent or one-sided audio.** The program audio isn't on channels 1–2.
  - Run `ffprobe your-file.mov` to see how the channels are laid out, then set `audio.channels`.
  - Add `mono = true` if you want the selected channels mixed together.
- **Encoding is slow.** See [Speed](how-it-works.md#speed).
- **`encoder libx264 … not available`.** Homebrew has said it may drop x264 from its `ffmpeg` formula. Either:
  - set `encoder = "h264_videotoolbox"`, or
  - `brew install ffmpeg-full`, then set `ffmpeg` and `ffprobe` in your config to the binaries in `/opt/homebrew/opt/ffmpeg-full/bin/`.

## Slack

- **`not_in_channel`.** Type `/invite @transcoder-bot` in the channel.
- **`channel_not_found`.** `channel` must be the channel ID (like `C0123456789`), not its name.
- **`invalid_auth`.** Check `bot_token` (`xoxb-…`) and `app_token` (`xapp-…`) in your config.
- **Slack says "This app is not responding" when you click.** The job isn't running. The poll may have timed out, or the Mac slept or restarted. The next scheduled run posts a fresh poll.

## Descript

- **"rejected the API key".** Create a new token under **Settings → API tokens** and update `descript.api_key`. Each token belongs to one Drive.
- **HTTP 402 ("payment required").** The Drive may have run out of credits for your plan. Check its usage in Descript.
- **Descript rejects a big upload.** Upload the `_1080p.mp4` by hand in the Descript app.
- **An upload failed.** Retry it with `uv run transcoder-bot descript-upload FILE`.

## Known limitations

- **One file is one recording.** If a HyperDeck ever splits a very long take into several files, each file is listed, and transcoded, separately.
- **One run at a time.** A manual `poll` or `transcode --new` refuses to start while another run is in progress.
