# GTA 6 Clip Farm

There are two ways to feed it:

- **Twitch streamers you have permission to clip.** This is the main mode. It finds the
  best moments of their latest stream from viewer clips and chat, downloads only those
  parts, and makes split-screen clips that credit the streamer. See
  [Clipping Twitch streamers](#clipping-twitch-streamers).
- **Your own gameplay.** This is the original mode, described below.

## Clipping Twitch streamers

### 1. Approved sources only

`sources.yaml` lists the streamers you're allowed to clip, with proof: a campaign link,
a screenshot of a DM, or a link to where they publicly allow clipping. **Anything not
listed, or listed without proof, is refused.** Reposting streams without permission
gets accounts struck and banned, so this check protects your accounts. Copy the
campaign's rules (credit, hashtags, clip length) into the entry.

```
python -m clipfarm sources        # shows which entries are approved, and what's missing
```

### 2. One-time setup

1. Twitch API keys (free). Create an app at https://dev.twitch.tv/console/apps,
   copy `secrets.example.yaml` to `secrets.yaml`, and paste in the Client ID and Secret.
2. Optional: chat replay. Chat spikes catch moments viewers didn't clip. Download
   [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader/releases) and put
   `TwitchDownloaderCLI.exe` in a `tools\` folder here. Without it, only viewer clips are used.
3. Optional: face cam split layout. Run `python -m clipfarm facecam <name>`. It saves a
   frame from their stream with a 10% grid on it. Read off the face cam's box and set
   `facecam: [x, y, w, h]` in `sources.yaml`. Without this, clips use the blurred-background layout.

### 3. Daily

```
twitch.bat                     # latest VOD of every approved streamer
twitch.bat name --vod 123456   # a specific VOD
```

For each VOD it:

1. Gets the viewer clips through Twitch's API. Many people clipping the same moment is
   the strongest signal there is.
2. Reads the chat replay, if you have it, for message bursts and laugh/hype emotes.
3. Ranks the moments and downloads only those parts of the VOD (seconds, not hours).
4. Makes payoff-first clips with the streamer credited on screen and in every caption,
   plus the campaign's hashtags. A viewer's clip title becomes the hook text when it's good.
5. Writes everything to `out\<streamer>-<vod>\post\review.html`.

Each clip's `script.yaml` records where it came from: VOD link, timestamp and permission type.

### Running it on an AMD GPU (RX 580)

- **Video encoding:** `video: encoder: h264_amf` in `config.yaml` uses the card's encoder.
  If it fails, it switches to the CPU automatically.
- **Captions on the GPU:** download a whisper.cpp release with Vulkan support for Windows,
  plus a model such as `ggml-small.en.bin`. Then set `captions: engine: whisper-cpp` and
  the two paths `whisper_cpp_exe` and `whisper_cpp_model`.
  Until then, captions run on the CPU, which is fine for short clips.
- **Local AI writing:** [LM Studio](https://lmstudio.ai) runs on AMD cards through Vulkan.
  Load an 8B instruct model, start its local server, and set
  `llm: provider: lmstudio`, `url: http://localhost:1234`.

## Format testing and performance tracking

Every clip is made in one of several **formats** with one of several **hook styles**. The
tracker learns which ones get the most views, and makes more of the winners.

| Format | What it is |
|---|---|
| `payoff_first` | 1.5s flash of the payoff, then rewinds to the build-up |
| `straight` | Plain build-up into the payoff. This is the control the others are measured against |
| `beat_story` | Your structure, compressed: question hook, build, "Wait for it...", then the payoff with a zoom punch, then a hard cut |
| `loop` | About 9s, ends right on the payoff so it loops |
| `countdown` | Top 3 moments of a stream, #3 to #1 (one extra clip per stream) |

| Hook style | Opening line |
|---|---|
| `viewer_title` | The title a Twitch viewer gave their clip of the moment |
| `question` | "Did that really just happen?" and similar |
| `curiosity` | "Wait for it...", "I did NOT expect that" and similar |
| `ai` | Written by your local AI (only when `llm` is set up) |

### How a winner is decided

- **A post wins** when its views 48 hours after posting beat the median of your last 30
  posts on the same platform. That keeps the test fair as your accounts grow, and lets
  YouTube and TikTok numbers be compared even though their reach differs.
- **Warm-up:** until every option has 15 scored clips, they rotate evenly.
- **After warm-up:** formats that win more get more of the next clips. 20% of picks stay
  random, so a lucky early streak can't lock in a winner and tastes that change get noticed.
- **Settings:** all in `experiments:` in `config.yaml`. Turn an option off by removing it
  from the list.

### The daily loop

```
twitch.bat      # make today's clips (formats are assigned automatically)
review.bat      # updates YouTube stats, then opens http://localhost:8765
```

- **Review & post tab:** watch each clip, copy its caption, post it, then paste the link
  to the post and press **Posted**. That link is how its views get tracked. Clips you've
  posted everywhere hide themselves.
- **Performance tab:** shows win rate, median views and engagement per format and per hook
  style, each one's share of upcoming clips, per-platform medians, and your best posts.
  Posts that are 48h+ old and still missing numbers are listed at the top: open each one
  and type in its views.

### Where the numbers come from

- **YouTube: automatic.** Create a free YouTube Data API key (steps in
  `secrets.example.yaml`).
- **Instagram: automatic** once Meta is connected (`python -m clipfarm connect meta`, see
  Auto-posting). This covers both kinds of Instagram post:
  - Auto-posted Reels.
  - Reels you post by hand and log with their link. It matches the link to your account's
    posts by its code (`instagram.com/reel/CODE`).
  It reads views, likes, comments and shares. If Meta retires a metric (as it did when
  `plays` became `views` in 2025), it falls back to what's still available instead of failing.
- **When numbers update:**
  - Every time you run `review.bat` or `python -m clipfarm stats`.
  - Hourly while `publish.bat` runs.
  Young posts (under 72h) refresh hourly, which keeps their 48h score accurate. Older posts
  refresh once a day, which keeps you well under Meta's rate limits.
- **Facebook: automatic** for Reels on the connected Page, both auto-posted ones and ones you
  log with a `facebook.com/reel/...` or `.../videos/...` link.
  - Views are total plays **including replays**, which is the number Facebook's own
    insights page shows. Likes are all reactions combined. Comments and shares are counted too.
  - `fb.watch/...` short links don't contain the video id, so log the full link instead.
  - Needs the `read_insights` permission. If you connected Meta before this was added, run
    `python -m clipfarm connect meta --token ...` once more with it ticked.
- **TikTok: automatic** for posts you log with a full `tiktok.com/@you/video/...` link.
  Posting to TikTok stays manual, but its numbers don't need typing.
  - One-time setup: follow the `tiktok` steps in `secrets.example.yaml` (a free developer
    app with Login Kit and the `video.list` scope), then run `python -m clipfarm connect tiktok`.
  - TikTok only returns stats for the account that signed in, so connect the account you post with.
  - `vm.tiktok.com/...` short links don't contain the video id, so log the full link instead.
  - While the app is in TikTok's **Sandbox**, add your posting account as a target user. If
    TikTok asks for an app review before `video.list` works for you, submit it from the same page.
- **X: automatic** for posts you log with an `x.com/you/status/...` link, **but it costs money**:
  since 2026 X charges per post read (about $0.005). To keep that to roughly a cent per post:
  - **Two reads per post:** once at 48h (the score) and once at 7 days, not hourly.
  - **Monthly read limit:** `x.monthly_read_limit` (default 1,000, about $5) in `config.yaml`
    stops it from reading more than that in a month.
  - **Setup:** sign up at developer.x.com, add credits under Billing, create an app, and paste
    its **Bearer Token** into `secrets.yaml`. Leave it empty to keep typing X numbers in for free.
- **Typed in by hand:** links from accounts other than the connected ones, on any platform.
- **Data storage:** everything is kept in `data/tracker.db` on your PC. Back it up now and
  then, because it holds all your results.

### Optional: run it every day by itself

In Windows **Task Scheduler**, go to **Create Basic Task → Daily**, choose
**Start a program**, and point it at `twitch.bat` in this folder. Clips will be waiting
for you to review.

## Multi-streamer reactions

This format shows several streamers hitting **the same moment** (a mission, a cutscene, a
famous jump) in one clip. It's at its strongest in launch week.
**Every streamer in it must be approved in `sources.yaml`**, and the clip is refused otherwise.

### 1. Find the moment

```
python -m clipfarm react find dam jump --sources alpha bravo charlie
```

This searches each streamer's recent viewer clips for those words and writes
`plans\dam-jump.yaml`, with each streamer's best-matching VOD link and timestamp plus
runner-up matches. Streamers with no match are left as a commented line for you to fill in.

### 2. Check the plan

Open each link and make sure the timestamp is the instant the moment happens. Then
**order the streamers so the strongest reaction is last**, because the clip builds up to it.

```yaml
title: "3 streamers react to the dam jump"
moment: "the dam jump"
layout: sequence        # or grid
align: true
entries:
  - source: charlie
    vod: https://www.twitch.tv/videos/333?t=1h02m40s
  - source: bravo
    vod: 222
    at: 3:12:05          # a separate time works too
    label: "BRAVO"       # optional on-screen name (default @login)
```

### 3. Build

```
python -m clipfarm react build plans\dam-jump.yaml
```

It downloads about 25s around each timestamp and syncs the streams, then builds,
finishes, and adds the clip to the review page. Approve it there to auto-post it.

- **Sync:** when the moment has shared game audio (cutscenes, music, explosions), the
  streams are lined up to a fraction of a second. If the audio doesn't match well enough,
  your timestamps are used as they are, and the console prints the match score for each
  streamer.
- **`sequence` layout:**
  1. Hook: a 2s flash of the last, best reaction
  2. The moment, shown in the first streamer's full gameplay
  3. Each reaction in turn (~5s each, name on screen, zoom punch), then a hard cut

  With 5 streamers that's about 33s. If a campaign sets a `max_length`, the slots shrink to fit.
- **`grid` layout:** gameplay on top and up to 4 face cams below, all reacting at the same
  instant. Every streamer needs a `facecam` box; otherwise it falls back to `sequence`.
- **Credit and posting:** every streamer is credited in every caption, and their campaign
  hashtags are merged. Auto-posting only uses platforms that **every** streamer's campaign allows.

## Auto-posting

**Approve** a clip on the review page and it gets posted to **YouTube Shorts, Instagram
Reels and Facebook Reels** automatically, with the captions, credit and hashtags already
written. **TikTok and X stay manual** (copy the caption, post, paste the link back), for
these reasons:

| Platform | Status |
|---|---|
| YouTube | Works. **Uploads stay private until Google approves an audit of your API project.** Apply early: Google Cloud Console → YouTube Data API → the "YouTube API Services audit and quota extension" form |
| Instagram | Works. Needs an Instagram **Business or Creator** account linked to a **Facebook Page** |
| Facebook | Works. Posts Reels to that Page. Meta caps it at 30 API Reels per Page per day |
| TikTok | Manual. Until TikTok approves an app, API posts can only be private and only to private accounts |
| X | Manual. Posting through its API is a paid tier |

### One-time setup

1. **YouTube:**
   - Follow the `youtube_oauth` steps in `secrets.example.yaml` and fill them into `secrets.yaml`.
   - Run `python -m clipfarm connect youtube` and sign in in the browser that opens.
2. **Instagram and Facebook:**
   - Create a Meta app and put its ID and secret in `secrets.yaml` (steps in `secrets.example.yaml`).
   - In [Graph API Explorer](https://developers.facebook.com/tools/explorer/), select your app and add
     these permissions: `pages_show_list, pages_read_engagement, pages_manage_posts,
     instagram_basic, instagram_content_publish, instagram_manage_insights, business_management,
     read_insights`.
   - Click **Generate Access Token**, then run:
```
python -m clipfarm connect meta --token PASTE_TOKEN_HERE
```
   It swaps that token for a permanent Page token and finds your linked Instagram account.
3. **Campaign rules:** if a campaign only allows certain platforms, set
   `platforms: [...]` under the streamer's `rules` in `sources.yaml`. The publisher
   respects it.

### Every day

```
publish.bat          # keeps running: posts approved clips, spaced out, until you close it
```

Or run `python -m clipfarm publish` hourly from Task Scheduler. Use
`python -m clipfarm publish --dry-run` to see what would go out without posting.

### Rules it follows

- **Approval:** only clips you approved get posted, oldest first.
- **Permission:** the streamer must still be approved in `sources.yaml` at posting time.
  If you remove a streamer, their queued clips stop.
- **Pacing:** each platform gets at most one post per `min_gap_minutes` (90), only between
  `hours` (09:00–23:00), and up to its `daily_max` per 24h.
- **Failures:** a post that fails is retried on later runs, up to 3 times. Errors show on
  the review page and the Performance tab.
- **Tracking:** every post is logged with its link automatically, so its views are tracked
  with no typing. View counts for YouTube, Instagram and Facebook (and TikTok, from the links
  you log) are fetched automatically, hourly while `publish.bat` runs.

## Clipping your own gameplay

You play with OBS recording and press a hotkey after anything good happens.
Then one command turns the recording into:

- **1 challenge Short (60s)** in the 8-beat "Can I do X in GTA 6?" structure, and
- **up to 10 moment clips (15–20s)** that open on the payoff and then rewind,

all in 9:16 with on-screen text, captions and sound effects, plus a review page with
ready-to-paste captions for YouTube, TikTok, Instagram, Facebook and X.

Every tool it uses is free: ffmpeg, OBS, faster-whisper, and optionally Ollama.

### The structure

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

### Setup (Windows, once)

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

### Daily loop

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
python -m clipfarm sources                    which streamers are approved
python -m clipfarm twitch NAME [--vod ID] [--chat FILE] [--max N]
python -m clipfarm twitch --all               latest VOD of every approved streamer
python -m clipfarm facecam NAME               frame + grid to find the face cam box
python -m clipfarm review                     review/post page + performance dashboard
python -m clipfarm stats                      fetch YouTube stats, print the format leaderboard
python -m clipfarm post CLIP_ID PLATFORM URL  log a post without the web page
python -m clipfarm connect youtube|meta|tiktok   one-time sign-in (posting / stats)
python -m clipfarm approve CLIP_ID... [--reject]
python -m clipfarm publish [--dry-run] [--watch] [--platform youtube]
python -m clipfarm react find WORDS... --sources NAME...   draft a multi-streamer plan
python -m clipfarm react build plans\NAME.yaml            build the reaction clip
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
