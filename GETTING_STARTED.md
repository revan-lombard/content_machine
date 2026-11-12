# Getting started checklist

Most setup is now automatic. **`install.bat`** installs everything, **`wizard.bat`** walks you
through every key and sign-in (testing each one as you paste it), and **`doctor`** tells you
exactly what's left. Prefer ticking boxes? Open `setup-checklist.html`.

**Goal for launch week (GTA 6, 19 November):** finish Phases 1–3 before launch day.

---

## Phase 0: Start the slow things today (15 min)

These depend on other people or platforms, so start them now.

- [ ] **Get permission to clip at least one streamer.** Join a GTA campaign on
      [Content Rewards](https://contentrewards.com/c/discover), find a streamer's clipping program
      on Discord, or DM a smaller streamer and screenshot their yes. (Once installed,
      `discover.bat` finds candidates and drafts the messages for you.)
- [ ] **Instagram:** switch to a **Creator** or **Business** account and link it to a **Facebook Page**.
- [ ] **Create the accounts you'll post from:** YouTube channel, TikTok, Instagram, Facebook Page,
      and X if you want it.

✅ **Done when:** you have permission for at least one streamer.

---

## Phase 1: Install (10 min, one click)

- [ ] On GitHub, open `install.bat` in the repository, click **Download raw file**, then
      double-click it. It installs Python, Git and ffmpeg, downloads the project to
      `Documents\content_machine`, and sets it up. Click **Yes** when Windows asks, and sign in to
      GitHub when the browser opens.

✅ **Done when:** the `content_machine` folder opens by itself.

---

## Phase 2: Run the wizard (20–60 min, depending on how many platforms)

- [ ] Double-click **`wizard.bat`**. It goes step by step:
  1. **You:** your name and clip channel, used to sign messages to streamers.
  2. **Twitch keys** (required): it opens the Twitch console, you paste the two keys, and it tests them.
  3. **Streamers:** add the ones you have permission for, with the proof.
  4. **Optional, each skippable:**
     - Instagram + Facebook
     - YouTube
     - TikTok
     - X (paid)
     - email outreach
     - Twitch whispers

     For each one, it opens the right page and tells you what to click. Then you paste the
     keys, it tests them, and it signs you in.
  5. **Automation:** creates the scheduled tasks for daily clips, discovery 3 times a day, the
     publisher at logon, and nightly backups.

  At the end it runs the health check. Re-run `wizard.bat` any time to add a platform or a streamer.

✅ **Done when:** the health check at the end shows no `[ ]` items (`[-]` = optional, not set up).

---

## Phase 3: First clips (15 min)

- [ ] Double-click **`twitch.bat`**. It clips the latest stream of each approved streamer
      (Twitch, Kick or YouTube).
- [ ] Double-click **`review.bat`**: watch the clips and **Approve** a good one. `publish.bat`
      (started at logon by the scheduled task, or double-click it) posts it.
- [ ] Optional look and feel:
  - **Face cam layout:** run `python -m clipfarm facecam NAME` in `shell.bat`, then set `facecam`
    in `sources.yaml`.
  - **Sound effects:** put them in `assets\sfx\`.
  - **Better hooks from a local AI:** set up LM Studio (see the README).

✅ **Done when:** your first approved clip is live. (On YouTube it's private until the audit; on
TikTok it's a draft in your app until you post it.)

---

## Phase 4: Apply for the audits (10 min, then wait)

- [ ] **YouTube API audit:** removes "uploads stay private". Use the "YouTube API Services audit
      and quota extension" form in Google Cloud.
- [ ] **TikTok Content Posting audit (optional):** removes the "tap to post" step. Once
      approved, set `publish: tiktok: mode: direct` and re-run the wizard's TikTok step.

---

## Your daily routine (about 20 minutes)

| When | What |
|---|---|
| Morning | Open **`review.bat`**: Approve or Reject the clips the scheduled task made overnight |
| When TikTok notifies you | Open the draft, paste the caption from the review page, post |
| Weekly | **Performance** tab: formats, hooks and sources that win (the mix adjusts itself) |
| Weekly | **Discover** tab: queue permission requests to the top new streamers; mark replies |

## Keeping it running

| Command (in `shell.bat`) | What it does |
|---|---|
| `python -m clipfarm doctor` | Checks every setup item and says what's missing |
| `python -m clipfarm update` | Downloads the latest version and installs new packages |
| `python -m clipfarm backup` | Backs up results, keys, streamers and settings (runs nightly when scheduled; uses OneDrive if you have it) |
| `python -m clipfarm schedule` | Re-creates the scheduled tasks (`--remove` to delete them) |

## When something goes wrong

| Problem | Fix |
|---|---|
| `install.bat` says Python can't be seen yet | Restart your PC and run `install.bat` again |
| A `.bat` window flashes and closes | Run the same command in `shell.bat` so you can read the error |
| Anything marked `[ ]` in the health check | Run the fix it shows next to the item |
| A scheduled job seems to do nothing | Read its log in `data\logs\` |
| Any other error | Copy the full message and send it. Nothing has run against the live platforms yet, so expect a few first-day fixes |
