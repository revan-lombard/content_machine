# Getting started checklist

Work top to bottom. Each phase ends with a **✅ Done when** check. Don't move on until it passes.

**Goal for launch week (GTA 6, 19 November):** finish Phases 1–3 before launch day.
Everything after that can be added while clips are already going out.

---

## Phase 0: Start the slow things today (15 min)

These take days or weeks to be approved, so start them now and carry on while you wait.

- [ ] **Find permission to clip at least one streamer.** Pick one or more:
  - [ ] Search [Content Rewards](https://contentrewards.com/c/discover) for "GTA" and join a
        campaign (check it daily from launch week).
  - [ ] Join the Discord of 2–3 mid-size GTA / GTA RP Twitch streamers and look for a clipping
        program.
  - [ ] DM a smaller GTA streamer and ask if you may clip them. Screenshot their "yes".
  - Never pay to join a campaign: that's a scam.
- [ ] **Instagram:** switch your Instagram account to **Creator** or **Business** (Instagram app →
      Settings → Account type) and link it to a **Facebook Page** you own. Create the Page if needed.
- [ ] **Create the accounts you'll post from:** YouTube channel, TikTok, Instagram, Facebook Page,
      and X if you want it. Use the same name on all of them.
- [ ] **YouTube audit** (removes "uploads stay private"): you'll apply in Phase 5 once you have a
      Google Cloud project. Note it as a to-do.
- [ ] **TikTok audit** (removes the "tap to post" step): you'll apply in Phase 5 from the TikTok
      developer portal. Note it as a to-do.

✅ **Done when:** you have permission for at least one streamer (a campaign link, a screenshot,
or a public "anyone can clip me" post).

---

## Phase 1: Install on your PC (20 min)

- [ ] Install **Python 3.10+** from [python.org/downloads](https://www.python.org/downloads/).
      On the first screen, **tick "Add python.exe to PATH"**.
- [ ] Install **Git** from [git-scm.com/download/win](https://git-scm.com/download/win) (default options).
- [ ] Open **Command Prompt** (Start → type `cmd`) and run, one at a time:
  ```
  cd %USERPROFILE%\Documents
  git clone https://github.com/revan-lombard/content_machine.git
  ```
  Sign in to GitHub in the browser window that opens.
- [ ] Open `Documents\content_machine` in File Explorer and double-click **`setup.bat`**.
  - If it says ffmpeg was just installed, close the window and run `setup.bat` again.
- [ ] Double-click **`shell.bat`**. This opens a command window that's ready to run
      `python -m clipfarm ...` commands. Use it whenever this checklist shows one.

✅ **Done when:** in `shell.bat`, `python -m clipfarm -h` prints a list of commands, and the
folder contains `secrets.yaml`.

---

## Phase 2: Your first clips (30 min)

- [ ] **Twitch API keys:**
  1. Go to [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) → **Register Your Application**.
  2. Fill in: any name, OAuth Redirect URL `http://localhost`, Category *Application
     Integration*, Client type *Confidential*.
  3. Copy the **Client ID**, click **New Secret**, and copy that too.
  4. Paste both into `secrets.yaml` under `twitch:` (open it with Notepad).
- [ ] **Add your streamer to `sources.yaml`:**
  1. Copy the `example` block.
  2. Set `name`, `login` (from `twitch.tv/<login>`), `permission.type` and `permission.proof`.
     For a Kick streamer, also add `platform: kick` and use the name from `kick.com/<login>`.
     For a YouTube streamer, add `platform: youtube` and use their `@handle` as `login`.
  3. Set `enabled: true`.
  4. Copy the campaign's rules: `credit`, `hashtags`, `max_length`, and `platforms` if the
     campaign limits where you can post.
- [ ] Check it in `shell.bat`:
  ```
  python -m clipfarm sources
  ```
  Your streamer should show **`[OK ]`**. If it shows `[OFF]`, fix whatever it lists.
- [ ] Double-click **`twitch.bat`**. It finds your streamer's latest VOD, picks the best moments,
      and makes clips. The first run downloads the caption model, so give it a few minutes.
- [ ] Double-click **`review.bat`**. Your browser opens on the **Review & post** page.

✅ **Done when:** you can play your first clips on the review page.
**No clips?** Read the `twitch.bat` window. "No moments found" means nobody made viewer clips
of that stream. Try a bigger stream with `twitch.bat NAME --vod <id>`.

---

## Phase 3: Make the clips look right (20 min, optional but recommended)

- [ ] **Face cam split layout** (face cam on top, gameplay below):
  ```
  python -m clipfarm facecam NAME
  ```
  Open the image it saves in the `out` folder. Each grid square is 10% of the frame. Read off the
  face cam's box and set `facecam: [x, y, w, h]` in `sources.yaml` (for example `[0.0, 0.7, 0.2, 0.3]`).
- [ ] **Sound effects:** put free `.wav` files in `assets\sfx\`, named after the beat they play on:
      `hook.wav`, `obstacle.wav`, `result.wav`, `rewind.wav`.
- [ ] **Better hook text with free local AI (works on your RX 580):**
  1. Install [LM Studio](https://lmstudio.ai).
  2. Download an 8B "Instruct" model and start its **local server**.
  3. In `config.yaml`, set `llm: provider: lmstudio`, `url: http://localhost:1234`, and `model`
     to the name LM Studio shows.
- [ ] **Captions on your GPU (faster):** download whisper.cpp's Vulkan build and a
      `ggml-small.en.bin` model. In `config.yaml`, set `captions: engine: whisper-cpp` and the two
      paths. Skip this if captions already feel fast enough.
- [ ] Run `twitch.bat` again and compare on the review page.

✅ **Done when:** you'd be happy to post the clips as they are.

---

## Phase 4: Turn on auto-posting (1–2 hours in total; do each platform when you're ready)

Do one platform, test it, then do the next. After each one, check what would be posted, without
posting anything:
```
python -m clipfarm publish --dry-run
```

### Instagram + Facebook (one connection covers both)
- [ ] Create an app at [developers.facebook.com/apps](https://developers.facebook.com/apps) (type
      **Business**). Put its **App ID** and **App Secret** in `secrets.yaml` under `meta:`.
- [ ] Open [Graph API Explorer](https://developers.facebook.com/tools/explorer/) and select your app.
      Add these permissions: `pages_show_list, pages_read_engagement, pages_manage_posts,
      instagram_basic, instagram_content_publish, instagram_manage_insights, business_management,
      read_insights`. Then click **Generate Access Token**.
- [ ] Connect, pasting your token:
  ```
  python -m clipfarm connect meta --token PASTE_TOKEN
  ```
  It should name your Page and your Instagram account.

### YouTube
- [ ] At [console.cloud.google.com](https://console.cloud.google.com), create a project and enable
      **YouTube Data API v3**.
- [ ] Go to **OAuth consent screen**, choose **External**, and add yourself as a **test user**.
- [ ] Go to **Credentials → Create credentials → OAuth client ID → Desktop app**. Put the client ID
      and secret in `secrets.yaml` under `youtube_oauth:`.
- [ ] Connect:
  ```
  python -m clipfarm connect youtube
  ```
- [ ] **Apply for the YouTube API audit** (the "YouTube API Services audit and quota extension" form
      in Google Cloud). Until it's approved, uploads stay **private**.

### TikTok
- [ ] At [developers.tiktok.com](https://developers.tiktok.com), create an app and add **Login Kit**
      (platform **Desktop**, redirect URI `http://localhost:8899/callback/`) and **Content Posting API**.
      Add the scopes `user.info.basic, video.list, video.upload`.
- [ ] While the app is in **Sandbox**, add your TikTok account as a **target user**.
- [ ] Put the **Client key** and **Client secret** in `secrets.yaml` under `tiktok:`.
- [ ] Connect:
  ```
  python -m clipfarm connect tiktok
  ```
- [ ] Clips will arrive in your TikTok app as **drafts**. Tap the notification, paste the caption
      (the **Copy** button on the review page), and post.
- [ ] Optional, later: apply for TikTok's **Content Posting audit**. Once approved, set
      `publish: tiktok: mode: direct` in `config.yaml` and run `connect tiktok` again.

### X (paid: about 1.5 cents per post)
- [ ] At [developer.x.com](https://developer.x.com), sign up, **add credits under Billing**, and create an app.
- [ ] Copy **Keys and tokens → Bearer Token** into `secrets.yaml` under `x: bearer_token`.
- [ ] Go to **User authentication settings → OAuth 2.0**: set permissions to **Read and write**, the
      callback to `http://127.0.0.1:8898/callback`, and copy the **Client ID** (and **Secret**) into
      `secrets.yaml`.
- [ ] Connect:
  ```
  python -m clipfarm connect x
  ```
- [ ] Not paying for X? Remove `x` from `publish: platforms:` in `config.yaml`.

### Go live
- [ ] On the review page, **Approve** one clip.
- [ ] Double-click **`publish.bat`** and leave it open. It posts approved clips, spaced
      90 minutes apart per platform, between 09:00 and 23:00.
- [ ] Check each platform for the post.

✅ **Done when:** one approved clip appears on every platform you set up. (On YouTube it shows as
private until the audit is approved. On TikTok it's a draft until you post it.)

---

## Phase 5: Turn on performance tracking (15 min)

Instagram, Facebook, TikTok and X stats already work if you connected them in Phase 4.

- [ ] **YouTube stats:** in the same Google Cloud project, go to **Credentials → Create credentials →
      API key** and put it in `secrets.yaml` under `youtube: api_key:`.
- [ ] Wait 48h after your first posts, then open `review.bat` → **Performance**.
- [ ] Anything under **"Enter numbers"** is a post the APIs couldn't read (usually a short link like
      `vm.tiktok.com` or `fb.watch`). Type those numbers in, or log the full link next time.

✅ **Done when:** the Performance tab shows views for your first posts, with nothing (or almost
nothing) to type in.

---

## Phase 6: Your daily routine (about 30 min a day)

| When | What | Time |
|---|---|---|
| Morning | Double-click **`twitch.bat`** (or let Task Scheduler run it, see below) | 0 min |
| Morning | **`review.bat`**: watch each clip, **Approve** or **Reject** | 1 min per clip |
| All day | **`publish.bat`** stays open and posts, spaced out | 0 min |
| When TikTok notifies you | Open the draft, paste the caption, post | 15 sec per clip |
| Weekly | **Performance** tab: see which formats and hooks win (it shifts the mix automatically) | 5 min |
| Launch week | `react find` / `react build` for multi-streamer reaction clips (see README) | 10 min each |

- [ ] **Make it automatic:** Task Scheduler → **Create Basic Task** → **Daily** → **Start a program**
      → pick `twitch.bat`. Clips will be waiting for you every morning.
- [ ] Put `publish.bat` in your Startup folder (Win+R → `shell:startup`) so it starts with Windows.

---

## Keeping it updated

When new features are added, run this in `shell.bat`:
```
git pull
```
Then double-click `setup.bat` once, in case new packages were added.

## Back up

- `data\tracker.db`: all your results and post history.
- `secrets.yaml`: your keys. **Never share or upload this file.** Git already ignores it.
- `sources.yaml`: your approved streamers and their permission proof.

## When something goes wrong

| Problem | Fix |
|---|---|
| "python is not recognized", or the Microsoft Store opens | Reinstall Python with **"Add to PATH"** ticked |
| A `.bat` window flashes and closes | Run the same command in `shell.bat` so you can read the error |
| "Twitch API keys missing" | Fill in `twitch:` in `secrets.yaml` |
| "'NAME' is not in sources.yaml" / `[OFF]` | Fix the entry; `python -m clipfarm sources` says what's missing |
| A platform shows "run: python -m clipfarm connect ..." | Run that command (a permission was added, or the sign-in expired) |
| "Auto-post failed, retrying: ..." on the review page | The error text says why. Send it over if it's unclear |
| Any other error | Copy the full message and send it. Nothing has run against the live platforms yet, so expect a few first-day fixes |
