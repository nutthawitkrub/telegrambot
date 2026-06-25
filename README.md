# Telegram Instagram Monitor Bot

Bridges [`instagram_monitor`](https://github.com/misiektoja/instagram_monitor)
(v3.1) to Telegram. Every log line the monitor produces for a tracked user,
plus every image/video it downloads, gets forwarded into your Telegram chat
automatically.

## How it works

- `/track` starts watching the username in `INSTAGRAM_USERNAME` (.env).
- `/trackother` asks you for a username via a reply, then starts watching that one too.
- `/image <username> <profile|post|story>` sends the most recently downloaded
  image or video of that type for that username, **on demand** — useful for
  pulling something again, or checking what's there right now without
  waiting for a new event.
- `/data <username>` sends the full log file plus the CSV activity export
  (every post/profile/follower change `instagram_monitor` recorded) for
  that username — useful before `/stop`, since stopping deletes both.
- Each tracked username gets:
  - its own `instagram_monitor` subprocess, running inside `./monitors/<username>/`
    (so logs/JSON/CSV files from different targets never collide), launched
    with CSV logging enabled (`-b instagram_monitor_<username>.csv`)
  - a background thread that reads `monitors/<username>/instagram_monitor_<username>.log`
    **from the very first byte** (including the startup banner) and forwards
    every line to Telegram, prefixed `[username]`
  - a second background thread that polls the same folder for newly
    downloaded profile pics, posts/reels, and stories, and **pushes each one
    to Telegram automatically**, the moment it's saved to disk — no command
    needed. Files that already existed before tracking started are not
    re-sent.
  - a third background thread that watches the subprocess itself. If
    `instagram_monitor` crashes or exits unexpectedly, you get an immediate
    Telegram message with the exit code and captured error output — instead
    of silently going dark until someone happens to check `/status`.
- `/status` lists what's running. `/stop <username>` kills that target's
  process, all three threads, **and deletes its entire data folder** — see
  the warning below.
- Shared settings (check interval, jitter, be-human mode, etc.) live in one
  `telegram_monitor.conf` at the project root and apply to every target.

## Anonymous mode only — Instagram login support has been removed

This bot **deliberately never logs into Instagram**. It runs
`instagram_monitor` in **Mode 1** (anonymous) only. This means:

- ✅ Stable, no setup needed, zero risk of account flags or checkpoints
- ✅ Still tracks new/deleted posts, bio changes, follower/following *counts*
- ❌ No reels or stories detail (Instagram API limitation in this mode)
- ❌ No detail on exactly *who* followed/unfollowed, only count changes

This isn't just a default left unset — the session-login code path
(`-u <username>`) has been **removed from `build_monitor_command()` in
`bot.py` entirely**. There's no environment variable that re-enables it.

### Why

Two separate things led here:

1. While testing Mode 2 (session login), a watcher account got hit with
   Instagram's own checkpoint/verification lock, requiring manual
   identity verification to recover. That's a real, direct cost of
   running session login against a real account.
2. Independently, Instagram/Instaloader's session-login path was hitting
   an unresolved, widely-reported bug (`401 Unauthorized... Please wait a
   few minutes before you try again`) even with a freshly-imported, valid
   session — affecting Firefox cookie import, plain login, and saved
   sessions alike, not specific to this setup.

Given both, anonymous-only is the safer default going forward.

### If you want Mode 2 back in the future

This is a deliberate decision to make a careless re-enable harder, not a
permanent technical wall. To bring it back, you'd need to:

1. Re-add a `-u <username>` (and ideally a `SESSION_USERNAME` env var
   feeding it) to `build_monitor_command()` in `bot.py`.
2. Set up a session the same way as before: log into a **dedicated**
   watcher account (not a personal one) in Firefox, then run
   `instagram_monitor --import-firefox-session`.
3. Check the [Instaloader issue tracker](https://github.com/instaloader/instaloader/issues)
   first for the current state of the 401 bug mentioned above — no point
   risking another checkpoint if the underlying bug is still unresolved.
4. Review `instagram_monitor`'s own [account safety guidance](https://github.com/misiektoja/instagram_monitor#how-to-prevent-getting-challenged-and-account-suspension)
   before running it against a real account again.

## Setup (local machine or plain server)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: API_KEY, INSTAGRAM_USERNAME
```

That's it — no Firefox, no login step, no session setup. This bot only
runs anonymous Mode 1; see "Anonymous mode only" above for why, and what
re-enabling Mode 2 would involve if you ever want to revisit that.

Generate the shared config once:

```bash
instagram_monitor --generate-config telegram_monitor.conf
```

Open `telegram_monitor.conf` and adjust at minimum:

```ini
INSTA_CHECK_INTERVAL = 10800   # 3 hours, in seconds
ENABLE_JITTER = True
BE_HUMAN = True
```

Run the bot:

```bash
python3 bot.py
```

## Deploying on Railway

Railway's container filesystem is **ephemeral** - it resets on every deploy
and restart. Two things in this project need to survive that:

1. `telegram_monitor.conf` (settings; may contain `WEBHOOK_URL`/`SMTP_PASSWORD`)
2. The Instaloader session file + `monitors/` (per-target logs, downloaded
   media, follower/following JSON) - losing this means re-logging-in and
   losing tracking history on every redeploy.

Both live under one directory controlled by the `DATA_DIR` environment
variable, which defaults to the project folder locally but should point at
a **mounted Railway Volume** in production. `bot.py` and the bundled
`Dockerfile` already support this - nothing else to change in code.

### 1. Push this repo to GitHub, then create the Railway service

In the Railway dashboard: **New Project → Deploy from GitHub repo**, pick
this repo. Railway will detect the `Dockerfile` and build from it
automatically.

### 2. Attach a volume

In the service's **Volumes** tab, add a volume mounted at `/data`. This is
the one volume Railway allows per service, so everything that needs to
persist (config + session + monitors/) shares this single path.

### 3. Set environment variables

In the service's **Variables** tab:

| Variable | Value |
|---|---|
| `API_KEY` | your Telegram bot token |
| `INSTAGRAM_USERNAME` | the default username for `/track` |
| `DATA_DIR` | `/data` (must match the volume's mount path) |
| `INSTA_CHECK_INTERVAL` | e.g. `10800` (optional - has a default) |
| `ENABLE_JITTER` | `True` (optional - has a default) |
| `BE_HUMAN` | `True` (optional - has a default) |
| `WEBHOOK_URL` | only if you want Discord-style webhooks (optional) |
| `SMTP_PASSWORD` | only if you want email notifications (optional) |

On first boot, `bot.py` writes `telegram_monitor.conf` onto `/data` itself,
generated from these variables - you never need to hand-create or upload
that file. It only does this once; if the file already exists on the
volume from a previous boot, it's left untouched, so manual edits made via
`railway ssh` survive future restarts too.

### 4. Deploy and verify — no session setup needed

There's no step 4 anymore — this bot is anonymous-only (Mode 1), so there's
no Firefox login, no session file, and no `railway ssh` step required to
get tracking working. Deploy, then go straight to verifying below.

If you want to bring Mode 2 back in the future, see "If you want Mode 2
back in the future" earlier in this README — that section covers what
re-adding the `-u` flag to `bot.py` would involve, including the
Railway-specific complication that a headless container has no Firefox to
log in with (you'd need to create the session on your own machine and
copy it up via `railway ssh`).

### 5. Verify

Message your bot on Telegram with `/start`, then `/track`. Check `/status`.
If you redeploy (push a new commit) and run `/status` again afterward, the
previously tracked usernames should resume automatically only if you also
add startup logic to re-launch them - **by default, this bot does not
persist which usernames were being tracked across a restart**, only the
underlying data (session, logs, media) for whichever usernames you
`/track` again. After a redeploy, re-run `/track` / `/trackother` for any
usernames you want resumed.

## Multi-user / shared bot usage

This bot is designed to be used by your whole team, not just you — no access
control is enforced; anyone who can message the bot can use it.

- **Private 1:1 chats**: Each person can `/track` or `/trackother` independently.
  Logs for a target go back to whichever chat started it.
- **Group chats**: Add the bot to a group and anyone in it can use the same
  commands. **Important**: Telegram bots default to "privacy mode," which
  means in a group the bot only sees `/commands` and messages that **reply
  directly to one of the bot's own messages** — not arbitrary plain text.
  When you run `/trackother` in a group, the bot's prompt will show a
  "reply" interface (tap the prompt, hit Reply, then type the username).
  If you just type the username without replying to the prompt, the bot
  will never see it.
- **Ownership**: If someone tries to track a username that's already being
  tracked elsewhere, the bot tells them who started it and where, and asks
  them to `/stop <username>` it first if they want to take it over. Tracking
  the *same* username twice from the *same* chat is just a no-op message.
- `/status` shows who started each currently-tracked username.
- Anyone can `/stop` anyone else's target — there's no ownership lock, just
  visibility into who started what.

## Telegram commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Show usage |
| `/track` | Track `INSTAGRAM_USERNAME` from `.env` |
| `/trackother` | Bot asks for a username; reply with it as plain text |
| `/image <username> <profile\|post\|story>` | Send the latest downloaded media of that type |
| `/data <username>` | Send the full log file plus the CSV activity export |
| `/cancel` | Cancel a pending `/trackother` username prompt |
| `/status` | List currently tracked usernames and process state |
| `/stop <username>` | Stop tracking **and permanently delete** all saved data for that username |

### ⚠️ `/stop` deletes data — get a copy first if you need one

`/stop <username>` doesn't just stop the process — it deletes the entire
`monitors/<username>/` folder: logs, the CSV activity export, follower/
following JSON snapshots, and every downloaded image/video for that
username. This is intentional (so disk usage doesn't grow forever once
you're done with a target), but it's irreversible.

If you want to keep a record before stopping, run `/data <username>` (sends
the log and CSV) and `/image <username> <type>` for any specific media you
want, **before** `/stop`. The CSV accumulates history across multiple
`/track` → `/stop` cycles of the *same* username for as long as you keep
tracking it without stopping — it only disappears once you actually `/stop`.

## Known limitations

- CSV logging is always on (`-b instagram_monitor_<username>.csv` is added
  to every launch). If your `telegram_monitor.conf` also sets `CSV_FILE`,
  the CLI flag should take precedence per standard argparse conventions,
  but this hasn't been verified against `instagram_monitor`'s actual
  source — if you see unexpected CSV behavior, check for a conflicting
  `CSV_FILE` setting in your config first.
- New media (profile pic, post/reel, story) is detected by polling the
  target folder every ~5 seconds, not via filesystem events — so there can
  be up to a ~5s delay between `instagram_monitor` saving a file and the
  bot pushing it. In practice this is negligible since checks themselves
  only happen every few hours.
- `/image` only finds media that `instagram_monitor` has already downloaded
  to disk — nothing is fetched on demand. If the monitor hasn't completed a
  check cycle yet for that username, `/image` will say so. Profile pictures
  appear after the first check; posts/reels/stories only appear once new
  ones are actually posted while the monitor is running. The automatic push
  has the same constraint — it can only forward what's actually been saved.
- Telegram bots can't upload files over 50 MB. If a downloaded reel/story
  video exceeds that, both `/image` and the automatic push will tell you
  where it's saved on disk instead of trying to send it.
- This forwards **raw log lines**, not parsed/formatted events — you'll see
  the monitor's own text output (including its startup banner), not a
  cleaned-up "📸 New post from X" style message. `instagram_monitor` does
  support **webhook notifications** natively (Discord-compatible JSON
  payloads) which could drive nicer formatted alerts later if wanted.
- If `instagram_monitor` crashes, the bot now notices immediately (not just
  when you happen to run `/status`) and sends a message with the exit code
  and the captured error output, so you can see *why* it died — missing
  dependency, auth failure, bad config option, etc. — instead of tracking
  silently going dark. Restarting itself still isn't automatic: run
  `/track` or `/trackother` again for that username once you've fixed
  whatever caused the crash.
- Username validation only checks Instagram's character rules
  (letters/digits/periods/underscores, ≤30 chars) — it doesn't verify the
  account actually exists before spawning the subprocess.
- Re-tracking a username (`/stop` then `/track` again) clears that
  username's previous log and error-capture files before starting, so old
  runs' text isn't replayed into Telegram. Downloaded media files from
  before are **not** deleted, but won't be re-sent either since the media
  watcher only pushes files it hasn't seen yet on this run.