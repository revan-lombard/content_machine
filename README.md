# GTA 6 Clip Farm

You play with OBS recording and press a hotkey after anything good happens.
Then one command turns the recording into:

- **1 challenge Short (60s)** in the 8-beat "Can I do X in GTA 6?" structure, and
- **up to 10 moment clips (15–20s)** that open on the payoff and then rewind,

all in 9:16 with on-screen text, captions and sound effects, plus a review page with
ready-to-paste captions for YouTube, TikTok, Instagram, Facebook and X.

Every tool it uses is free: ffmpeg, OBS, faster-whisper, and optionally Ollama.

## The structure

| Beat | Time | Hotkey that supplies the footage | Default on-screen line |
|---|---|---|---|
| 01 Hook | 0–3s | F5 *problem* | "Can I {challenge} in GTA 6?" |
| 02 Problem | 3–8s | F5 *problem* | "This should be impossible" |
| 03 Challenge | 8–12s | F5 *problem* | "No cheats. One attempt. Will it work?" |
| 04 Build | 12–30s | F6 *attempt* (press 1–3+ times) | "Here's the plan" |
| 05 Obstacle | 30–40s | F7 *obstacle* | "That's NOT supposed to happen..." |
| 06 Re-engage | 40–48s | F8 *retry* | "One more thing to try..." |
| 07 Result | 48–57s | F9 *result* | "IT ACTUALLY WORKED" |
| 08 End | 57–60s | F9 *result* | "And that's how you {challenge}." Hard cut. |

F10 tags any other clip-worthy moment. F11 undoes the last tag.
**Press the key just after the moment happens.** The clip keeps the seconds before
the press, plus 1.5s after it.

A beat you didn't tag is skipped and the Short comes out shorter. Only F9 (result)
is required. Beat lengths, hotkeys and everything else are in `config.yaml`.

## Setup (Windows, once)

1. Install [Python 3.10+](https://www.python.org/downloads/) (tick "Add to PATH") and [OBS](https://obsproject.com).
2. Double-click `setup.bat`. It installs ffmpeg if needed and the Python packages.
3. In OBS, go to **Tools → WebSocket Server Settings → Enable**. If you set a password,
   copy it into `config.yaml` under `obs: password:`.
4. In OBS, go to **Settings → Output → Recording**. Use format **MKV** (or "Hybrid MP4"),
   so a crash doesn't corrupt the file, at 1080p60 or 1440p.
5. Optional: drop sound effects into `assets/sfx/`, named after the beat they play on:
   `hook.wav`, `obstacle.wav`, `result.wav`, `reengage.wav`, `rewind.wav`
   (moment clips play this at the rewind), and so on. Use free sources like Pixabay.
6. Optional, for better writing: install [Ollama](https://ollama.com), run `ollama pull llama3.1`,
   and set `llm: provider: ollama` in `config.yaml`.

## Daily loop

```
log.bat "escape 5 stars with no car"     # 1. start this, then start recording in OBS and play
                                          #    F5..F10 while playing; stop recording when done
daily.bat                                 # 2. builds everything from your newest session
```

Then open `out\<session>\post\review.html`. Watch the clips, copy each caption, post,
and tick "posted".

**Recommended:** voice over the challenge Short. `out\<session>\challenge\vo_script.txt`
has your line for each beat with its time window. Record one take, save it as `vo.wav`
in that folder, then run:

```
python -m clipfarm finish out\<session>\challenge
python -m clipfarm kit out\<session>
```

Your own voice is the biggest thing that separates your clips from reposted gameplay,
which platforms pay less for and push to fewer viewers.

**Tweak any text:** edit `script.yaml` in the clip's folder and run `finish` on it again.
Nothing is re-cut, so it only takes seconds.

**No tags at all?** `daily.bat path\to\recording.mkv` finds the loudest moments on its
own and makes moment clips only.

## Commands

```
python -m clipfarm log --challenge "..."      hotkey logger
python -m clipfarm daily SESSION              challenge + moments + finish + kit
python -m clipfarm challenge SESSION          rough cut of the 60s Short only
python -m clipfarm moments SESSION [--no-auto]
python -m clipfarm finish CLIP_DIR... [--vo file] [--no-captions]
python -m clipfarm kit out\SESSION
```

`SESSION` is a `sessions\*.beats.json` file from the logger, or a video file.
Add `--video PATH` if the recording moved.

## What's automated and what isn't

| Step | Who | Notes |
|---|---|---|
| Recording | you | You play the game, OBS records |
| Marking moments | you, 1 key press each | Or none: loudness detection finds moments by itself (less accurate) |
| Choosing footage for each beat | program | |
| Cutting, vertical 9:16, zoom punch-ins | program | |
| On-screen beat text, titles, descriptions, hashtags | program | Templates, or a local AI with Ollama. Edit in `script.yaml` |
| Captions | program | faster-whisper, runs on your PC |
| Sound effects | program | Once you've added the files |
| Voiceover | you (recommended) | ~5 min per challenge Short, script provided |
| Quality check | you | review.html, about 1 min per clip |
| Posting | you, for now | Captions are ready to paste. API auto-posting is the next phase (see below) |

## Next phase: auto-posting

- **YouTube Shorts:** the Data API is free. Uploads from a new, unverified API project
  are forced to *private* until Google approves an audit, so apply for the audit early.
- **Instagram Reels / Facebook:** the Graph API is free with a Business or Creator account.
  The video has to be at a public URL while it uploads.
- **TikTok:** the Content Posting API requires an app review. Until then, posts can only
  go up as private (self-only).

Until those are approved, the review page's copy buttons keep manual posting to about
30 seconds per clip per platform.

## Footage rules

Use your own gameplay, or streamer clips only with permission (for example through their
clipping programs). Don't post leaks or re-uploaded trailers. Rockstar and Take-Two take
them down, and three strikes ends a YouTube channel.

## Tests

```
python -m pytest -q
```

The tests build a fake recording with ffmpeg and run the whole pipeline on it.
