# Fortnite Item Shop Monitor

Watches the Fortnite Item Shop and alerts you the moment a cosmetic on your
watchlist appears. Add items by name to `watchlist.json`, leave it running,
get a Discord (or Telegram) push notification when one shows up.

Runs entirely on free services. No API key, no paid tier, no credit card.

---

## The one thing worth understanding first

**The Fortnite Item Shop rotates once per day, at exactly 00:00 UTC, globally.**
It is not a live stream of changes.

That single fact drives the whole design. Polling every 5 seconds around the
clock would mean ~17,000 requests a day to learn something that changes once.
Instead this app **bursts around the rotation and idles the rest of the day**:

| Mode | When (UTC) | Interval | Why |
|---|---|---|---|
| **Burst** | 23:57 → 00:35 | every 10s | Catch the daily rotation within seconds |
| **Idle** | rest of the day | every 15 min | Cheap cover for Epic's occasional mid-day hotfixes |
| **Hot** | 10 min after any unexpected change | every 10s | Once something moves, more may follow |

That is **~320 requests/day (~24 MB gzipped)** instead of ~17,000 — while still
detecting the rotation within seconds.

### The real latency floor (measured, not assumed)

`fortnite-api.com/v2/shop` sits behind a **30-minute server-side cache**. I
measured this directly — the `Age` header climbs 1:1 with the clock and resets
at exactly 1800s:

```text
17:27:30  age=1781
17:27:51  age=1        <- TTL is exactly 1800s
```

Cache-busting query params don't defeat it, `HEAD` is refused (405), and there
is no `ETag`/`Last-Modified` for conditional requests. **So polling faster than
~10s cannot make detection meaningfully quicker** — the upstream cache is the
bottleneck, not your poll rate. Anything claiming sub-second Item Shop alerts
from a public API is either using a private Epic-authenticated feed or
guessing.

**What this app does about it:** each `language` variant of the endpoint has
its own *independently staggered* 30-minute cache. Racing two of them roughly
halves worst-case staleness:

```text
lang=en age=17     lang=de age=1158    lang=fr age=1771
```

Because item IDs and prices are language-independent, the app takes whichever
variant is freshest and then restores English names from a locally cached
catalog — so you get the speed benefit without German notifications.

---

## Quick start

```bash
pip install -r requirements.txt
```

Then create your `.env` from the template:

```bash
cp .env.example .env
```

Get a Discord webhook (about 30 seconds, no developer account needed):

> Discord → your server → **Server Settings** → **Integrations** →
> **Webhooks** → **New Webhook** → pick a channel → **Copy Webhook URL**

Paste it into `.env`:

```text
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Check it works:

```bash
python run.py --test-notify
```

Then start monitoring:

```bash
python run.py
```

---

## Managing your watchlist

Edit `watchlist.json`. Plain names are fine — they are resolved to stable item
IDs automatically at startup:

```json
{
  "items": [
    "Renegade Raider",
    "Travis Scott",
    "Take The L",
    "Star Wand",
    { "id": "CID_028_Athena_Commando_F", "name": "Renegade Raider (pinned)" }
  ]
}
```

**You never need to touch the code.** The file is also **hot-reloaded** — add an
item while the monitor is running and it is picked up on the next poll.

Pinning an `id` is more reliable than a name, because an ID survives Epic
renaming the display name. To find IDs:

```bash
python tools/resolve.py renegade
```

```text
29 match(es) for 'renegade':
  Renegade                    CID_013_Athena_Commando_F     type=Outfit  rarity=Uncommon
  The Renegade                EID_JustHome                  type=Emote   rarity=Icon Series
  Renegade Star               Pickaxe_HeadhunterStar        type=Pickaxe rarity=Uncommon
```

Check that everything on your list resolved:

```bash
python tools/resolve.py --check
```

See what is in the shop right now (useful for grabbing exact spellings):

```bash
python tools/shop_now.py           # everything
python tools/shop_now.py outfit    # just outfits
python tools/shop_now.py --search rene
```

**All cosmetic types are supported** — outfits, emotes, pickaxes, back blings,
gliders, wraps, sprays, contrails, plus Festival jam tracks (matched by title),
instruments, Rocket Racing cars and LEGO kits.

---

## How duplicate detection works

Two separate questions are tracked, which is what makes the behaviour
predictable:

1. **"Did it just appear?"** — the `absent → present` transition, using the set
   of watched items seen at the last *successful* poll. This is what makes a
   return months later alert again.
2. **"Have I already told you today?"** — the shop day each item was last
   alerted for.

With `repeat_daily_while_in_shop` on (the default), you get **one alert per
shop day** for as long as an item stays in the shop:

```text
Day 1 00:01  Harley Quinn appears  -> ALERT   (new appearance)
Day 1 00:15  still there           -> silent  (already alerted for day 1)
Day 1 12:00  still there           -> silent
Day 2        still there           -> ALERT   (new shop day)
Day 3        still there           -> ALERT   (new shop day)
Day 4        leaves the shop       -> silent
Months later returns               -> ALERT   (absent -> present again)
```

Reminders are worded differently from appearances — *"is still available in
the Item Shop today"* rather than *"is now in the Item Shop"* — and use an
amber embed, so a reminder never reads as a fresh drop.

"Day" means the **shop's own date field**, not a rolling 24 hours, so
reminders land with the 00:00 UTC rotation instead of drifting.

Set `repeat_daily_while_in_shop` to `false` to go back to alerting only once,
when an item first appears.

**`reminder_at_utc`** controls when the daily reminder may go out. It defaults
to `"00:00"`, i.e. right at the rotation — which is the middle of the night in
many timezones. Set it to e.g. `"09:00"` to get the nudge at a civilised hour.
A brand-new appearance always alerts immediately and ignores this setting.

State lives in `state/state.json` and is written **atomically** (temp file +
`os.replace`), so a crash or power cut mid-write cannot corrupt it. A corrupt
file is moved aside to `state.corrupt-<timestamp>` rather than silently
deleted, and the app keeps running.

Two extra safety nets:

- **Failed polls never advance state.** If the API returns garbage, is
  unreachable, or hands back an empty `entries` list, the app refuses to
  believe it. Otherwise a blip would look like "every tracked item left" and
  fire a false alert storm the moment it recovered.
- **An anti-flap cooldown** (`renotify_cooldown_hours`, default 1). Racing
  language variants can briefly disagree about the shop, which would otherwise
  bounce an item `absent → present → absent` within seconds. One hour absorbs
  that and is far too short to ever block a daily reminder or a real return.

**First run** establishes a baseline rather than alerting on everything already
in the shop. If watched items are present, you get one clearly-labelled
*startup summary* — not a flood of "new item" alerts.

---

## What a notification looks like

Discord gets a rich embed per item, colour-coded by rarity, with the artwork
as a thumbnail:

```text
FORTNITE ITEM SHOP ALERT

Renegade Raider is now in the Item Shop!

Type: Outfit          Price: 1,200 V-Bucks
Rarity: Rare          Leaves: 2026-08-23
Shop section: Featured

Detected: 19:00:03 UTC on 22 Aug 2026
```

If several tracked items appear at once (the common case at rotation) they are
**combined into one message** by default. Set `combine_multiple` to `false` in
`config.json` for one message per item.

---

## Project structure

```text
fortnite_shop/
├── run.py                    entry point / CLI
├── watchlist.json            <- the file you edit
├── config.json               timings and behaviour
├── .env                      secrets (gitignored, never committed)
├── .env.example              template
├── requirements.txt
│
├── src/
│   ├── api.py                HTTP client, retries, validation, mock source
│   ├── matcher.py            flattens shop entries; watchlist matching
│   ├── watchlist.py          loading + name→ID resolution + localization
│   ├── state.py              persistence, transition logic, dead-letter queue
│   ├── scheduler.py          adaptive burst/idle polling
│   ├── monitor.py            the main loop
│   ├── config.py             config + env loading
│   ├── console.py            UTF-8 console fix for Windows
│   └── notifiers/
│       ├── discord.py        webhook + rich embeds
│       ├── telegram.py       bot API
│       ├── console.py        always-on stdout channel
│       └── __init__.py       fan-out dispatcher
│
├── tools/
│   ├── resolve.py            find item IDs / verify watchlist
│   ├── shop_now.py           list the current shop
│   └── telegram_chat_id.py   Telegram setup helper
│
├── tests/                    33 behavioural tests, no network needed
└── deploy/                   systemd, Docker, Windows task, GitHub Actions
```

**Language: Python 3.9+.** Chosen because it is already on your machine
(3.14.2), the logic is I/O-bound so async buys nothing here, and the result
stays readable. **Only two dependencies**: `requests` and `python-dotenv` —
deliberately minimal, because every dependency is a thing that can break at
3 AM.

---

## Commands

```bash
python run.py                  # start monitoring (the normal one)
python run.py --once           # single check, then exit
python run.py --status         # show config, watchlist resolution, state
python run.py --test-notify    # send a test alert to your channels
python run.py --reset-state    # forget what is currently in the shop
```

---

## Configuration

`config.json` — no code changes needed:

```jsonc
{
  "polling": {
    "burst_window_start_utc": "23:57",
    "burst_window_end_utc":   "00:35",
    "burst_interval_seconds": 10,
    "idle_interval_seconds":  900,
    "race_languages": ["en", "de"]    // ["en"] to be maximally light
  },
  "notifications": {
    "combine_multiple": true,
    "max_items_per_message": 10,
    "renotify_cooldown_hours": 12
  }
}
```

Secrets go in `.env` **only**, never in code or config. `.env` is in
`.gitignore` along with `state/`, `logs/` and `cache/`.

---

## Reliability

Every failure mode in the brief is handled:

| Situation | Behaviour |
|---|---|
| API offline / DNS failure | Retry with exponential backoff + jitter, then back off and continue |
| No internet | Same path; recovers automatically when the link returns |
| Rate limited (429) | Honours `Retry-After`, waits, retries |
| Invalid JSON / HTML error page | Rejected as untrusted, state untouched |
| **Empty `entries` list** | **Explicitly rejected** — never read as "everything left the shop" |
| 5xx server errors | Retried; treated as transient |
| Notification service down | Queued to a dead-letter file and retried next poll |
| Notifier throws an exception | Caught per-channel; other channels still deliver |
| App restarts | State reloaded from disk; no duplicate alerts |
| Corrupt state file | Quarantined, app continues |
| Duplicate offers for one item | De-duplicated, cheapest offer kept |
| Multiple items at once | Combined into one message (configurable) |
| Item renamed by Epic | Still matched via pinned/resolved item ID |
| Item ID changed | Falls back to normalised name matching |
| Broken `watchlist.json` while editing | Keeps the previous watchlist, logs the error |
| Non-ASCII item names on Windows | UTF-8 console, lossy at worst, never fatal |

The main loop has a catch-all backstop: **it cannot die from an unhandled
exception.** Logs rotate at 2 MB × 3 files, so an unattended run cannot fill
the disk.

---

## Testing

```bash
cd tests && python -m unittest test_monitor
```

33 tests, entirely offline via a mock API driven by JSON fixtures. They cover
exactly what you asked for:

- a newly appeared tracked item triggers a notification
- an item that stays in the shop does **not** trigger another
- an item that leaves and later returns triggers a **new** notification
- an item still in the shop alerts once per shop day (and only once per day)
- turning daily reminders off reverts to alert-once-on-appearance
- `reminder_at_utc` delays a reminder but never a new appearance
- upgrading from v1 state does not spam reminders for items already present
- multiple items appearing together are handled (batched and separate modes)
- staggered arrivals only alert for the newly added item
- API errors don't crash the app and don't corrupt state
- notification failures are dead-lettered and retried, and do **not** mark the
  item as notified
- state survives a restart; corrupt state is quarantined
- burst/idle scheduling, including the midnight wrap-around

To simulate an item entering the shop yourself, set `FORTNITE_SHOP_SOURCE=mock`
in `.env` and edit `tests/fixtures/scenario.json` — each array element is one
shop state, stepped through on successive polls.

### Verified against the live API

Beyond the mocks, the full pipeline was tested end-to-end against the real
shop: two items actually in the shop (`Aura`, `Bushranger`) were added to a
watchlist mid-run, detected on the next poll with correct English names, real
prices, real rarities and working image URLs (both HTTP 200 `image/png`), and
a second poll correctly stayed silent.

---

## Deployment — keeping it running 24/7

Yes, it needs to run continuously. Four free options, best first:

### 1. Your own PC (simplest, genuinely free)

Only catches rotations while the machine is on — fine if it usually is.

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
```

Registers a Scheduled Task that starts at logon **and** at startup, runs
windowless via `pythonw`, and auto-restarts on failure. Remove with
`Unregister-ScheduledTask -TaskName FortniteShopMonitor -Confirm:$false`.

### 2. Oracle Cloud Always Free (best true 24/7, still free)

The only major cloud with a genuinely permanent free tier — an ARM VM with up
to 4 vCPU / 24 GB RAM, no time limit. This app needs a tiny fraction of that.

```bash
sudo cp deploy/systemd/fortnite-shop-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fortnite-shop-monitor
journalctl -u fortnite-shop-monitor -f
```

The unit restarts on crash, waits for the network, and is rate-limited so a
crash loop can't hammer the API. *Note: Oracle reclaims genuinely idle
instances — this app's daily activity is enough to avoid that.*

### 3. GitHub Actions (free, no hardware at all)

Copy `deploy/github-actions/monitor.yml` to `.github/workflows/`, add
`DISCORD_WEBHOOK_URL` as a repository secret. It wakes at 23:50 UTC and polls
across the rotation for ~45 min — about 1,350 min/month, inside the 2,000 free
minutes for private repos and unlimited on public ones. State is committed
back to the repo between runs — more durable than `actions/cache`, which can
be evicted, and the daily commit also stops GitHub auto-disabling a scheduled
workflow after 60 days of inactivity. Only the 1.3 MB cosmetics catalog uses
`actions/cache`, since it is regenerable and would bloat the repo.

That state commit is **named after whatever showed up**, so `git log` doubles
as a searchable drop history:

```text
alert: Son Goku in the shop [skip ci]        <- appeared this run
chore: Son Goku still in the shop [skip ci]  <- was already there
chore: update shop state [skip ci]           <- nothing watched in the shop
```

Only `alert:` means something new arrived. Grep your history with
`git log --grep '^alert:'`.

The message is written **after** the polling loop has exited, so it can never
delay or block a notification — the Discord alert went out up to 45 minutes
earlier. The commit is a record, not the alert.

**Caveat:** GitHub's cron is best-effort and can start several minutes late
under load. The wide polling window absorbs that, but an always-on host is
strictly more reliable.

### 4. Docker (anywhere)

```bash
docker compose -f deploy/docker-compose.yml up -d
```

### What is *not* free any more

Fly.io ended its free tier (trial only). Railway and Render moved to
usage-based/trial models — Render's free web services also sleep on inactivity,
which defeats a background poller.

---

## Data source

[fortnite-api.com](https://fortnite-api.com) — free, **no API key required**,
no documented limit on the shop endpoint.

Alternatives I checked and rejected:

- **Epic's official API** — `.../storefront/v2/catalog` returns **401**. It
  needs an OAuth token from a real Epic account, which would mean storing your
  Epic credentials and risking the account. Not worth it.
- **fnbr.co** — returns 401, requires an API key.
- **fortniteapi.io** — did not resolve/connect at all during testing.

The app always requests gzip (578 KB → 75 KB, a 7.7× saving) and sends an
identifying `User-Agent`, because being a polite client is what keeps a free
public API free.

---

## Security

- No credentials in code, config, or logs — everything via `.env`.
- `.env`, `state/`, `logs/` and `cache/` are all gitignored.
- `.env.example` is committed as a template; `.env` never is.
- If you push this to GitHub, use repository **secrets** for the webhook URL.
- Treat your Discord webhook URL as a password: anyone holding it can post to
  that channel. Regenerate it in Discord if it ever leaks.
