# How was this created? Learn from my prompts!

Claude Code built this whole repo from the seven prompts below. Each prompt is quoted word for word, followed by a short timeline of what Claude did with it.

[← Back to the README](../README.md)

## 1. The brief

> Question for ya to research and recommend. I’ve got a Blackmagic HyperDeck recording to a NAS. I’ve tweaked and we’ve looked at the codecs, which have landed us at a setting that gets our 45 minute recordings to be about 70 GiB.
>
> I’d like to set up a transcoding step that happens later in the day on my Mac Studio. It needs to do a few things to the video file:
> 1. Rotate it 90 degrees counter clockwise.
> 2. Normalize the audio.
> 3. Downscale the 4K footage to 1080p.
> 4. Save it beside the source file.
>
> I can have ffmpeg installed on the Mac, and it will have the NAS mounted.
>
> Bonus points: if it could send a Slack poll (I can create an app) with the recordings found in the last 48 hours. Wait until we pick one, then trash the ones we don’t pick, then transcode the one we pick.
>
> Work through as much of this as you can, set up the repo well (lint, unit tests, readme) and leave clear setup instructions for me. Thanks!

- **Research:** sent two research agents out in parallel. One covered HyperDeck file formats and data rates, and ffmpeg's rotation, loudness and hardware-decoding options. The other covered macOS scheduling and privacy permissions, and Slack's Socket Mode.
- **Prototyping:** tried the ffmpeg steps on generated test clips first, including two-pass loudness normalization and progress reporting.
- **The tool:** built a Python command-line tool:
  - config loading and validation;
  - the transcode, staged locally and checked before it's published next to the source;
  - the recordings scan and the trash folder;
  - the Slack poll with buttons;
  - launchd scheduling and a `doctor` command.
- **Quality:** wrote 215 tests. Some run the real ffmpeg and check pixels to prove the rotation goes counter-clockwise. Also set up Ruff, strict mypy, CI and the README. → `611e62d`

## 2. Descript

> One more part will be auto upload to Descript AI for us to do editing later, they have a CLI or API?

- **Found both.** Descript has an API and an official CLI.
- **Worked around blocked docs.** Descript's docs site was blocked from Claude's sandbox, so it read the CLI's published package to learn the upload flow.
- **Built the uploader:** create an import job, upload to a signed URL, then wait for the import. It's tested against a local fake Descript server.
- **Mistake:** Claude took a "1 GB upload limit" from search-result summaries as fact, and built a feature to shrink bigger files to fit. → `dd559e0`

## 3. Asking for the source

> Can you point me at where in their API docs it limits it to 1 GB? In our paid plan I think they limit us to 50 GB, at least through the browser.

- Admitted the limit came from search summaries, not Descript's docs, and couldn't be verified. Suggested checking the docs or testing with a real upload.

## 4. The docs don't say that

> I’ve read the import page and I don’t see mention of 1 GB.

- Made full-quality uploads the default, and removed the other unverified Descript claims from the docs. → `034b7f9`

## 5. A quality target

> Let’s aim for like 25 Mbps, if we are using a good quality codec. When we test against the real thing, we will either find that the limit doesn’t exist, or I will upload by hand. 1GB is too small.

- Added a `video.bitrate` setting that defaults to 25 Mbit/s H.264, and checked it with a real encode.
- Removed the file-shrinking feature. → `cbc8de9`

## 6. Going public

> Can you audit the repo for any secrets? And for great easy to follow setup docs. Customized stuff shouldn’t touch source controlled files (that is, easy setting changes, secrets, you know). I’ll flip it to public shortly. Bonus points for a mermaid flow chart up top describing what this repo does visually!
>
> What else would be cool is to add a “how was this created? Learn from my prompts!” link on the README, to a sub-docs page. Place the human prompts and a very succinct timeline of the actions you took based on them on that sub-page.
>
> Thanks!

- **Secrets:** scanned the files and the whole git history, with a pattern search and Yelp's detect-secrets. It found only test fakes and example placeholders.
- **Settings:** confirmed that all settings and tokens live outside the repo.
- **Docs:** rewrote them: a flowchart at the top of the README, a step-by-step setup guide with a check after each step, and this page.

## 7. License and main

> Let’s do ISC for the license, go ahead and merge to main.

- Added the ISC license, in `LICENSE` and the package metadata.
- Created `main` from the work branch.

## Takeaways

- **Ask for research and a recommendation, not just code.** The design choices then come with their reasons.
- **Ask "where does it say that?"** It caught a real mistake here, so ask for sources on anything that matters.
- **Steer with small, concrete prompts.** "Aim for like 25 Mbps" changed the design in one step.
