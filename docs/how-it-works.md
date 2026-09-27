# How it works

[← Back to the README](../README.md)

## The daily run

At the scheduled time, a launchd job on the Mac:

1. Makes sure the NAS share is mounted, remounting it with `mount_url` if needed.
2. Deletes trash folders older than `retention_days`.
3. Finds recordings from the last 48 hours that haven't been transcoded yet.
4. With Slack set up, runs [the poll](#the-slack-poll), trashes the takes nobody picked, and transcodes the pick. Without Slack, it transcodes every new recording.
5. Uploads each finished video [to Descript](#sending-videos-to-descript), if that's set up.

## The ffmpeg pipeline

Each recording gets two ffmpeg passes. The first reads just the audio track and measures its loudness. The second does everything else, using those measurements:

```sh
# Pass 1: measure the loudness (reads only the audio, so it's quick)
ffmpeg -i Service.mov -map 0:a:0 \
  -af 'pan=stereo|c0=c0|c1=c1,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json' -f null -

# Pass 2: rotate, scale, normalize and encode, with the numbers from pass 1
ffmpeg -i Service.mov -map 0:v:0 -map 0:a:0 \
  -vf 'scale=1920:1080:flags=lanczos,setsar=1,transpose=dir=cclock,format=yuv420p' \
  -c:v libx264 -preset medium -b:v 25000k -maxrate 37500k -bufsize 50000k -profile:v high \
  -af 'pan=stereo|c0=c0|c1=c1,loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=-27.61:measured_TP=-4.47:measured_LRA=8.06:measured_thresh=-38.20:offset=0.58:linear=true,aresample=48000' \
  -c:a aac -b:a 192k -map_metadata 0 -movflags +faststart Service_1080p.mp4
```

`transcoder-bot transcode --dry-run FILE` shows the plan for a particular recording.

| Step | Choice | Why |
|---|---|---|
| Rotate | `transpose=dir=cclock` | Rotates the pixels themselves. A rotation *flag* would be quicker to write, but some players and upload pipelines ignore it. |
| Scale | Lanczos, before rotating | 3840×2160 → 1920×1080 is a clean 2:1. Scaling first means rotating a 1080p frame rather than a 4K one, which is 4× less work. It never upscales, so a 1080p source is only rotated. |
| Loudness | Two-pass `loudnorm`: −16 LUFS, −1.5 dBTP peak, 11 LU range | −16 LUFS is the usual target for phones, podcasts and social video. YouTube plays back at about −14, so use `target_lufs = -14` if that's your main outlet. Two passes let ffmpeg apply one clean gain change instead of riding the level. |
| Audio channels | Channels 1–2 as left/right | HyperDecks record 2–16 embedded channels, and the program mix is normally on 1 and 2. Change `audio.channels` if yours differs, e.g. `[1]` for a single mic on channel 1. |
| Video | H.264 High at about 25 Mbit/s, preset `medium` | A high-quality master for editing that still plays everywhere. At this bitrate H.264 is visually lossless for 1080p, so HEVC wouldn't look any better. 45 minutes comes to about 8.5 GB. |
| Container | MP4 with `faststart`, AAC 192 kb/s | Ready to upload or stream: playback can start before the whole file has downloaded. |

loudnorm only applies a single gain change when it can do so cleanly. If a recording's loudness range is wider than 11 LU, or a straight gain would push peaks past the limit, it quietly switches to gentle dynamic normalization. Raise `audio.lra` (maximum 50) if you'd rather keep more of the original dynamics.

## Keeping your files safe

- **The encode is staged locally.** It's written to the Mac's own disk and checked for the right size, length and audio. Only then is it copied next to the original, under a hidden `.partial` name, and renamed. A half-written file never appears on the NAS.
- **The originals are never modified.** The only thing that ever touches a recording is the trash step, for takes nobody picked.
- **In-progress recordings are skipped.** HyperDeck files on shared storage can grow while they're still recording, so files modified in the last 5 minutes are ignored.
- **One run at a time.** A lock stops a manual run from colliding with the scheduled one.
- **Clean stops.** The Mac stays awake while the job works (via `caffeinate`), and ffmpeg is stopped cleanly if the job is.

## The Slack poll

The poll looks like this:

> **🎬 Which recording should we keep?**
> Found **3 recordings** from the last 48 hours in `/Volumes/HyperDeck`. Pick the one to keep: it'll be rotated 90° counter-clockwise, scaled to 1080p and loudness-normalized, and saved next to the original. The others will be moved to `_Trash` on the NAS (and deleted for good after 7 days).
>
> **Service_0930.mov** — finished Yesterday at 10:18 AM · 45m 12s · 70.1 GiB  `Keep this one`
> **Service_1100.mov** — finished Yesterday at 11:49 AM · 44m 01s · 68.2 GiB  `Keep this one`
> **Rehearsal.mov** — finished Yesterday at 8:02 AM · 12m 40s · 19.6 GiB  `Keep this one`
> `Skip, change nothing`
> *Closes Tomorrow at 6:00 AM. If nobody picks by then, nothing is deleted.*

- **Picking:** **Keep this one** asks for confirmation, and the first confirmed pick wins. Anyone who clicks after that is told privately that the poll has closed. `allowed_user_ids` limits who can pick.
- **After a pick:** the other takes move to the trash, then the pick is transcoded. Progress appears in a thread under the poll, and the poll message ends with ✅ or ❌.
- **Skipping or no answer:** **Skip, change nothing** leaves every file alone, and so does getting no answer within `poll_timeout_hours` (12 by default). Unpicked recordings come up again in the next poll while they're under 48 hours old.
- **What's listed:** up to 15 recordings from the last 48 hours that aren't already transcoded.
- **When clicks count:** only while the job is running. If Slack says the app didn't respond, the poll had already closed or the job was stopped. The next run marks any poll a crash left open as interrupted.

## The trash

Unpicked takes move to `_Trash/<date>/` inside the recordings folder.

- **It's instant.** The trash is on the same share, so moving a take is a rename, even for a 70 GiB file.
- **You can undo it.** Move a file back into the recordings folder.
- **It empties itself.** After `retention_days` (7 by default), the daily run deletes the take for good.
- **Or skip it.** Set `trash.mode = "delete"` to delete unpicked takes immediately.

## Sending videos to Descript

Descript's API is in beta. [Descript's API docs](https://docs.descriptapi.com/) cover which plans include it and how usage counts.

After each transcode, transcoder-bot:

1. **Creates a Descript project.** It's named like `2026-09-26 Service_0930`, set by `project_name`: `{date}` is the recording date and `{stem}` its file name. The project has the 1080p video on its timeline.
2. **Uploads the full-quality video** straight to Descript's storage. The Mac needs no public URL.
3. **Waits for Descript to finish importing it**, for up to `wait_minutes` (60 by default).
4. **Shares the link.** With Slack set up, it goes in the poll's thread and "Open in Descript" is added to the poll message. Otherwise it's written to the log.

**File size.** The upload is the full `_1080p.mp4`: about 8.5 GB for 45 minutes at 25 Mbit/s. Descript's import docs don't list a size limit for API uploads. If Descript does reject a file that big, upload it by hand in the Descript app.

**If an upload fails**, the transcode is kept. The error and a retry command go to the Slack thread and the log:

```sh
uv run transcoder-bot descript-upload "/Volumes/HyperDeck/Service_0930_1080p.mp4"
```

- **Any video works** with `descript-upload`. Add `--name` to choose the project name.
- **Skip the upload for one run** by adding `--no-descript` to `transcode` or `poll`.

Descript also has an official CLI for one-off imports: `npm i -g @descript/platform-cli` (needs Node 24+), then `descript-api import --name "My Project" --media ./video.mp4`. transcoder-bot calls the same API directly, so it doesn't need Node.

## About HyperDeck recordings

HyperDecks record ProRes or DNxHR in `.mov` or `.mxf`, or H.264/H.265 in `.mp4`, with 2–16 channels of audio. ffmpeg reads all of them.

4K files are big. The recordings this was built for are about 70 GiB per 45 minutes, which is roughly 220 Mbit/s. That's a little above 2160p29.97 ProRes 422 Proxy or DNxHR LB with 16 audio channels (about 200 Mbit/s). `transcoder-bot scan` prints each file's resolution. If yours turn out to be 1080p, the downscale step is simply skipped.

## Speed

Each transcode reads the whole recording from the NAS. For a 70 GiB file, that alone takes about 11 minutes over gigabit Ethernet, or 1–2 minutes over 10 GbE if the NAS keeps up. The encode usually takes longer than the read. If it's too slow for you:

- `encoder = "h264_videotoolbox"` encodes on Apple's media engine. It's several times faster, and at 25 Mbit/s it looks nearly as good as libx264.
- `hwaccel = "videotoolbox"` also decodes ProRes on the media engine, which leaves the CPU free for the encoder.
