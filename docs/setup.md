# Setup guide

This walks through setting up transcoder-bot on the Mac that will run it, such as a Mac Studio. Each step ends with a **Check** so you know it worked before you move on. Plan on 20–30 minutes, plus the time for one test transcode.

[← Back to the README](../README.md)

## What you need

- **A Mac with Apple Silicon.** Use the account that will run the job. You'll need admin rights to install tools.
- **The NAS share your HyperDeck records to**, and its username and password.
- **For the Slack poll (optional):** permission to add apps to your Slack workspace.
- **For Descript uploads (optional):** a Descript plan that includes API access.

You never edit files in this repo. Your settings and tokens live in your home folder; see [where your settings live](../README.md#your-settings-live-outside-the-repo).

## 1. Install the tools

Install [Homebrew](https://brew.sh) if you don't have it, then:

```sh
brew install ffmpeg uv
```

`uv` installs Python and this project's dependencies for you.

**Check:** `ffmpeg -version` and `uv --version` each print a version.

## 2. Get the code

```sh
git clone https://github.com/dawsontoth/transcoder-bot.git ~/transcoder-bot
cd ~/transcoder-bot
uv sync --managed-python
```

`--managed-python` gives the project its own Python, separate from Homebrew's. That keeps the macOS permission step ([step 9](#9-let-macos-allow-it)) simple.

From now on, run commands from `~/transcoder-bot` as `uv run transcoder-bot …`.

**Check:** `uv run transcoder-bot --version` prints `transcoder-bot` and a version number.

## 3. Connect the NAS

1. In Finder, choose **Go → Connect to Server** (⌘K) and enter `smb://<your-nas>/<share>`.
2. Sign in, and tick **Remember this password in my keychain**. The job needs the saved password to remount the share if it drops.
3. Find the folder the HyperDeck records into, such as `/Volumes/HyperDeck`.

**Check:** `ls /Volumes/HyperDeck` lists your recordings.

## 4. Create your config

```sh
uv run transcoder-bot init-config
open -e ~/.config/transcoder-bot/config.toml
```

`init-config` creates your own copy of the settings file, readable only by you. Near the top, set where the recordings are and how to reach the share, then save:

```toml
recordings_dir = "/Volumes/HyperDeck"      # the folder from step 3
mount_url = "smb://your-nas/HyperDeck"     # lets the job remount the share
```

Everything else has a sensible default, and every setting is explained in the file.

**Check:** `uv run transcoder-bot doctor` shows ✓ for ffmpeg, the recordings folder and the temp folder. Slack and Descript show `!` until you set them up. Then `uv run transcoder-bot scan` lists your recent recordings with their length and resolution.

## 5. Try one recording by hand

```sh
uv run transcoder-bot transcode --dry-run "/Volumes/HyperDeck/Service.mov"
uv run transcoder-bot transcode "/Volumes/HyperDeck/Service.mov"
```

The dry run shows the plan without changing anything. The real run logs its progress. A 45-minute recording takes a while.

**Check:** `Service_1080p.mp4` appears next to the original. Open it: the picture should be upright, and the sound at a comfortable level. If it's turned the wrong way, set `rotate = "cw"` under `[video]`, then run the same command again with `--force` added.

## 6. Set up the Slack poll (optional)

1. Go to [api.slack.com/apps](https://api.slack.com/apps). Choose **Create New App → From a manifest**, pick your workspace, and paste in [`slack-app-manifest.yml`](../slack-app-manifest.yml). You can rename the app in Slack's editor before you create it.
2. Under **Basic Information → App-Level Tokens**, choose **Generate Token and Scopes**. Name it `socket`, add the `connections:write` scope, and generate it. Copy the `xapp-…` token.
3. Under **Install App**, choose **Install to Workspace**. Copy the **Bot User OAuth Token** (`xoxb-…`).
4. In Slack, open the channel for the poll and type `/invite @transcoder-bot`. Then click the channel name: the **channel ID** (like `C0123456789`) is at the bottom of the **About** tab.
5. Put all three in the `[slack]` section of your config:

   ```toml
   [slack]
   bot_token = "xoxb-…"
   app_token = "xapp-…"
   channel = "C0123456789"
   ```

6. Optionally, let only certain people pick: put their member IDs in `allowed_user_ids`. To find someone's ID, open their Slack profile and choose **⋯ → Copy member ID**.

The Mac connects out to Slack, so it doesn't need a public address or an open port.

**Check:** `uv run transcoder-bot doctor --post-test` shows ✓ for Slack and posts a test message in the channel. Then try a practice poll:

```sh
uv run transcoder-bot poll --dry-run --timeout-hours 0.25
```

This posts a real poll for your recent takes, and closes it after 15 minutes. Picking one only reports what would happen. Nothing is moved, deleted or transcoded.

## 7. Send videos to Descript (optional)

1. In Descript, create an API token under **Settings → API tokens** ([Descript's guide](https://help.descript.com/hc/en-us/articles/43370311322509-Descript-API)). Choose the Drive the projects should go in.
2. Put it in the `[descript]` section of your config:

   ```toml
   [descript]
   api_key = "dx_bearer_…:dx_secret_…"
   ```

**Check:** `uv run transcoder-bot doctor` shows ✓ for Descript.

## 8. Schedule it

```sh
uv run transcoder-bot schedule install --at 18:00
```

The job now runs every day at 6 PM. It runs the Slack poll if Slack is set up; otherwise it transcodes every new recording. To change the timing, run `schedule install` again with different options:

| Option | Effect |
|---|---|
| `--at 17:30` | Run at a different time. |
| `--days sun,wed` | Run only on those days. |
| `--run transcode` | Skip Slack and transcode every new recording. |

`schedule uninstall` removes the job.

**Check:** start a run now, and watch the log:

```sh
launchctl kickstart gui/$(id -u)/local.transcoder-bot
tail -f ~/Library/Logs/transcoder-bot.log
```

## 9. Let macOS allow it

macOS asks before a background job can use a network volume.

- If a prompt appears during the run above, asking whether Python may access files on a network volume, click **Allow**.
- If nobody will be at the Mac to click it, or the log shows `Operation not permitted`, grant access ahead of time. First print the path of the job's Python:

  ```sh
  uv run python -c 'import sys, pathlib; print(pathlib.Path(sys.executable).resolve())'
  ```

  Then open **System Settings → Privacy & Security → Full Disk Access**, click **+**, press ⌘⇧G, and paste that path.

The permission belongs to that exact Python file, so repeat this step if uv ever installs a newer Python for the project.

**Check:** run `launchctl kickstart` again (step 8). The log shouldn't show `Operation not permitted`.

## 10. Keep the Mac ready

- **Stay logged in.** The job runs in your login session. A locked screen is fine.
- **Sleep is fine.** If the Mac is asleep at the scheduled time, the job runs as soon as it wakes. To wake it on time instead, run `sudo pmset repeat wakeorpoweron MTWRFSU 17:55:00`.
- **No need to keep it awake.** While the job works, it keeps the Mac awake by itself.

## Changing settings

Edit `~/.config/transcoder-bot/config.toml`, then run `uv run transcoder-bot doctor` to check it. The next run uses the new settings; there's nothing to reinstall. Common changes:

| To… | Set |
|---|---|
| Rotate the other way | `[video] rotate = "cw"` |
| Encode faster | `[video] encoder = "h264_videotoolbox"` and `hwaccel = "videotoolbox"` |
| Make smaller files | `[video] bitrate = "12M"`, or `bitrate = ""` for constant quality (`crf`) |
| Match YouTube's loudness | `[audio] target_lufs = -14` |
| Use a single mic on channel 1 | `[audio] channels = [1]` |
| Use program audio on channels 3–4 | `[audio] channels = [3, 4]` |
| Look further back | `[scan] lookback_hours = 72` |
| Wait longer for someone to pick | `[slack] poll_timeout_hours = 20` |
| Keep trashed takes longer | `[trash] retention_days = 30` |
| Delete unpicked takes right away | `[trash] mode = "delete"` |
| Put the Descript projects in a folder | `[descript] folder = "HyperDeck"` |
| Let your team edit the Descript projects | `[descript] team_access = "edit"` |

Every setting is explained in the [example config](../src/transcoder_bot/config.example.toml).

To keep tokens out of the file, set the `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `DESCRIPT_API_KEY` environment variables instead. The scheduled job doesn't see your shell's environment, though, so for scheduled runs keep the tokens in the config file.

## Updating

```sh
cd ~/transcoder-bot
git pull
uv sync --managed-python
```

Your settings aren't in the repo, so there's nothing to merge. If an update moves the project to a newer Python version, repeat [step 9](#9-let-macos-allow-it).

## Uninstalling

```sh
cd ~/transcoder-bot
uv run transcoder-bot schedule uninstall
rm -rf ~/.config/transcoder-bot "$HOME/Library/Application Support/transcoder-bot" \
  ~/Library/Logs/transcoder-bot.log
cd ~ && rm -rf ~/transcoder-bot
```

If you set them up, also:

- remove the Python entry from **Full Disk Access**,
- delete the Slack app, and
- revoke the Descript API token.

Stuck? See [Troubleshooting](troubleshooting.md).
