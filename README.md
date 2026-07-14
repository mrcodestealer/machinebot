# machinebot — Lark Machine / Encoder bot

Standalone Lark bot carrying **all machine + encoder features** mirrored from `osedutybot`.
Runs on its own Lark app in **persistent connection** mode (Subscription mode →
*Receive events through a persistent connection*) — no public webhook URL needed.

## Commands

| Command | What it does |
| --- | --- |
| `set maintenance NWR2008` / `unset test TBP8609 NCH1900` | PROD batch set/unset via the EGM backends (confirm card → Playwright job → per-row screenshots) |
| `ALL NWR MACHINES <Venue> set maintenance` | venue-wide set/unset expanded from `webmachine_data.json` |
| `/sm` | set-machine wizard: env picker card → action → machines |
| `/stresstest <paste announcement>` | one-time reminder 10 min before the announced set-maintenance time |
| maintenance schedule paste (@bot) | parses action + future time + machine list → auto reminder 10 min before |
| `machine status NWR2008` | read-only status from the live scrape (`webmachine_data.json`) |
| `/findmachine` or `/fm` | interactive card: environment + game type + online/offline → machine names |
| `/nch /nwr /wf /tbr /tbp /cp /dhs /mdr <id(s)>` | asset / encoder sheet lookup, rendered as a TRTC-parsed card |
| `/encoder nwr2205 & nwr2206` | MAIN/POOL/CCTV encoder IPs from `latestencoder.json` (`/encoder refresh` re-scrapes) |
| `/osmwatch [url]` | OSM-Watch dashboard screenshot (warm browser) |
| `/loginosmwatch` | force a fresh OSM-Watch login QR (posted to the lab group) |
| `/deploy` (or "git pull origin main and restart service") | git pull + restart the `machine` systemd unit |

The `/wm` machine dashboard (webmachine blueprint) is served on the bot's Flask port
(`PORT`, default 5010) with a background scrape loop that rewrites `webmachine_data.json`.

## Module inventory (copied verbatim from osedutybot unless noted)

- `main.py` — Lark plumbing + dispatch (authored for this bot, skeleton mirrored from logcreditbot)
- `smmachine.py`, `prod_machine_batch.py` — prod-batch set/unset engine (Playwright)
- `maintenancemachineagent.py` — LLM/regex intent parsing for maintenance messages
- `checkcredit.py`, `np_third_http_page.py`, `third_http_warm_pool.py` — backend URL/credential routing
- `webmachine.py` — machine dashboard + scrape loop; `webapp.py` here is a thin **alias** to it
- `findmachine.py`, `machine_card.py` — find-machine form card + TRTC card rendering
- `osmwatch.py` — OSM-Watch warm browser, QR login, encoder scraper (`latestencoder.json`)
- `reminder.py` — one-time maintenance reminders (Bitable sheet + APScheduler)
- `nch/nwr/winford/tbr/tbp/cp/dhs/mdr.py` — per-site asset sheet lookups (`mdr.py` patched to read
  `MDR_APP_ID`/`MDR_APP_SECRET` from `.env` instead of hardcoded credentials)
- `machine.py` — CLI shim (`python machine.py …` runs smmachine)

## Setup (local or server)

```bash
git clone https://github.com/mrcodestealer/machinebot.git
cd machinebot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
# On a fresh Linux server also: .venv/bin/playwright install-deps chromium
cp .env.example .env   # then fill in APP_ID/APP_SECRET/VERIFICATION_TOKEN + tokens/passwords
.venv/bin/python main.py
```

Lark developer console for this app:
- **Events & callbacks → Subscription mode**: *Receive events through a persistent connection*.
- Subscribe to `im.message.receive_v1`; card interactions arrive as `card.action.trigger`.
- Permissions: send/read messages, upload images, Sheets + Bitable read/write
  (asset lookup sheets and the reminder table must be accessible to **this** app).

### Data files to seed (gitignored — copy from the osedutybot server)

| File | Why |
| --- | --- |
| `webmachine_data.json` | machine list used by set/unset targeting + `/findmachine` before the first scrape finishes |
| `osmwatch.json` | OSM-Watch Playwright session — without it the bot needs a fresh `/loginosmwatch` QR |
| `latestencoder.json` | `/encoder` reads only this file; empty until the first authenticated scrape |

```bash
scp root@<oldserver>:/root/osedutybot/{webmachine_data.json,osmwatch.json,latestencoder.json} /root/machinebot/
```

## systemd service (server)

```bash
# 1. put the repo at /root/machinebot (see Setup above), then:
cp /root/machinebot/deploy/machine.service /etc/systemd/system/machine.service
systemctl daemon-reload
systemctl enable --now machine        # start now + auto-start on boot

# everyday operations
systemctl status machine              # is it running? last log lines
journalctl -u machine -f              # follow live logs
systemctl restart machine             # restart after a git pull
systemctl stop machine                # stop
```

The unit runs `.venv/bin/python main.py` with `Restart=always`, so a crash or a
`/deploy`-triggered restart comes back automatically within 5 s.

## Git workflow (local ↔ GitHub ↔ server)

Local (Windows) — after editing:

```bash
git add -A
git commit -m "describe the change"
git push origin main
```

Server — first time:

```bash
cd /root && git clone https://github.com/mrcodestealer/machinebot.git
```

Server — update to latest and restart:

```bash
cd /root/machinebot
git pull origin main
systemctl restart machine
```

Or just send **`/deploy`** to the bot in Lark — it runs `git pull origin main` in its own
folder and restarts the `machine` unit by itself (`MACHINEBOT_SERVICE` in `.env` names the unit).

If you edit directly on the server and want to push back:

```bash
cd /root/machinebot
git add -A && git commit -m "server-side fix"
git pull --rebase origin main   # replay on top of anything pushed from local
git push origin main
```

`.env` and the runtime JSON state files are gitignored, so pulls never clobber
credentials or scraped data.

## Notes / gotchas

- **App sharing**: this app ID is also used by logcreditbot. Lark delivers each persistent-connection
  event to only **one** of the connected clients — do not run both bots on the same app
  simultaneously or commands will randomly go unanswered (see the deploy notes in the PR/commit).
- Warm pools (`PROD_WARM_POOL`, `WEBMACHINE_WARM_POOL`, `THIRD_HTTP_WARM_POOL`) default **off**
  for CPU-only hosts; flip to `1` on beefier machines for faster set/unset.
- The maintenance-agent LLM (`BOT_CHAT_*`) is optional — without a reachable backend it falls
  back to regex parsing.
- Sheet lookups authenticate as this bot's app: each asset sheet must be shared with it
  (MDR keeps its own `MDR_APP_ID` because that sheet was shared to osedutybot's app).
