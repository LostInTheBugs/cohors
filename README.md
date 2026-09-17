# WoW Companion (working title)

Self-hosted companion for World of Warcraft: guild-friendly simulation reports built on
the official SimulationCraft Docker image, plus a guild roster and character pages fed
by the Battle.net API and raid reports (parses) from Warcraft Logs.

> Early development. Not affiliated with Blizzard Entertainment, Inc. Game data is
> provided by Blizzard Entertainment and Warcraft Logs (see attribution requirements).

## Status

- [x] Simulation engine wrapper (`worker/simrun.py`) — runs a SimulationCraft sim via the
      official `simulationcraftorg/simc` Docker image, extracts DPS, produces HTML + JSON reports.
- [x] Web app (`app/`) — `/simc` export submission, FIFO sim queue (one sim at a time),
      shared result cache, per-IP + per-user quotas, French UI, report archive over HTTP.
- [x] Accounts — invite-only registration (`/invite/<token>` links), login sessions,
      admin panel (`/admin`) for invitations and account management.
- [x] Stat-weights mode ("optimiseur", Mr Robot-style) — scale factors from the same engine.
- [x] Guild roster & characters (« Personnages ») — Battle.net API: ranks, levels, item
      level, last seen, per-character equipped items (Wowhead links), 30-min server cache.
- [x] Raid reports (« Rapports ») — Warcraft Logs v2 API: guild report list, per-report
      boss pulls (kill/wipe, difficulty, item level) and per-fight parses (role tables,
      percentile scores), 15–30 min server cache.
- [x] Member comparison (« Comparateur ») — 2–6 characters side by side: equipped ilvl
      and last seen (Battle.net) + best Warcraft Logs parses for the current raid
      (per-boss percentile, best/median averages).
- [x] Sim profiles (« Profils ») — save a `/simc` export once, reload it in one click,
      optionally share it with the guild (max 20 per account).
- [x] Invitation e-mails — send (and re-send) invite links by e-mail from
      `noreply@ruban-adhesif.com` (SMTP), with a French HTML template.
- [x] « Top Stuff » — paste Wowhead item links or IDs (max 15): each item is
      simulated on your character (SimulationCraft profilesets) and ranked by DPS;
      rings and trinkets are tested on both slots (item data from the Blizzard API).
- [x] Discord bot (« 🤖 Bot Discord » admin tab) — invite it with a generated
      OAuth2 link, activate it, pick the announcement channel and let it post new
      raid reports (Warcraft Logs) and guild roster changes.
- [x] Account ↔ character links — link your account to your guild characters
      (⭐ main + alts) from the Personnages page; stars show up in the roster.
- [x] Roles: membre / officier / administrateur — officers manage invitations,
      admins handle accounts, roles and the Discord bot.
- [x] Bilingual UI (FR/EN) — header switcher, remembered choice; English is
      applied automatically for English-language browsers.
- [x] Account settings page — language stored on the account, display name,
      password change (per-account preference follows you everywhere).
- [x] One main per account (enforced) — first linked character becomes the main;
      switch it anytime from its chip.
- [x] Guild dashboard — weekly reset countdown, latest raid summary (top
      parses), recent roster moves.
- [x] Raid calendar — officers plan raids, members answer
      Présent/Peut-être/Absent; the Discord bot announces and reminds.
- [x] Group simulation — combine members' `/simc` profiles into one
      multi-actor run (raid buffs) and rank the guild by DPS.
- [x] Guild page — Discord invite, TeamSpeak details (one-click join),
      editable by administrators; plus a full Help page.

## Requirements

- Docker Engine with the Compose plugin (app container + SimulationCraft engine image)
- Python 3.11+ (only for running the engine wrapper standalone)

## Quick start (engine wrapper)

```bash
# Simulate a profile shipped inside the image (works out of the box after a docker pull)
python3 worker/simrun.py --container-profile profiles/MID2/MID2_Mage_Arcane.simc --iterations 500

# Simulate your own in-game `/simc` export (or any .simc profile file)
python3 worker/simrun.py --profile ./my-export.simc --iterations 10000 --outdir ./out
```

## Web app (Docker Compose)

```bash
cp .env.example .env    # set DATA_DIR and the ADMIN_* bootstrap variables (see below)
docker compose up -d --build
# the app listens on 127.0.0.1:${PORT} (default 8030) — put a reverse proxy in front for TLS
```

On first start, an admin account is created from `ADMIN_EMAIL` / `ADMIN_PASSWORD` if no
admin exists yet. The admin signs in, creates invitation links in `/admin`, and sends them
to guild members; members register (`/invite/<token>`), then sign in to use the simulator.

The app container mounts the host Docker socket to launch SimulationCraft containers, so
`DATA_DIR` must be the same absolute path on the host and inside the container — it is
passed as-is to the SimulationCraft containers for input/output files.

Note: mounting the Docker socket is root-equivalent on the host. Keep the app behind a
reverse proxy and small until the simulation worker is split out.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8030` | HTTP port (bound to localhost) |
| `DATA_DIR` | `./data` | SQLite database + generated reports |
| `QUEUE_MAX` | `20` | Max queued + running simulations |
| `PER_IP_ACTIVE` | `3` | Max queued/running simulations per IP |
| `PER_USER_ACTIVE` | `3` | Max queued/running simulations per account |
| `PER_IP_COOLDOWN_S` | `15` | Minimum delay between submissions per IP |
| `SIM_TIMEOUT` | `900` | Hard timeout per simulation (seconds) |
| `SIMC_IMAGE` | `simulationcraftorg/simc:latest` | SimulationCraft engine image |
| `ADMIN_EMAIL` | — | First admin login (created at startup if no admin exists) |
| `ADMIN_PASSWORD` | — | First admin password (same condition) |
| `PUBLIC_BASE_URL` | — | Public base URL used to build invitation links |
| `SESSION_DAYS` | `30` | Session cookie lifetime (days) |
| `INVITE_TTL_DAYS` | `7` | Invitation link validity (days) |
| `COOKIE_SECURE` | `1` | Set to `0` for plain-HTTP local development only |
| `BNET_CLIENT_ID` | — | Battle.net API client ID (https://develop.battle.net) — roster page |
| `BNET_CLIENT_SECRET` | — | Battle.net API client secret |
| `BNET_REGION` | `eu` | Battle.net region |
| `BNET_GUILD_REALM` | `hyjal` | Guild realm slug |
| `BNET_GUILD_SLUG` | `lords-of-the-pit` | Guild slug |
| `WCL_CLIENT_ID` | — | Warcraft Logs API client ID (https://www.warcraftlogs.com/api/clients) |
| `WCL_CLIENT_SECRET` | — | Warcraft Logs API client secret |
| `WCL_GUILD_NAME` | `Lords Of The Pit` | Guild name on Warcraft Logs |
| `WCL_GUILD_REALM` | `hyjal` | Guild realm slug |
| `WCL_GUILD_REGION` | `EU` | Region |
| `WCL_RAID_ZONE_ID` | `53` | Warcraft Logs zone id used for character rankings |
| `SMTP_HOST` | — | SMTP server for outgoing e-mails (e.g. `mail.ruban-adhesif.com`) |
| `SMTP_PORT` | `587` | SMTP submission port (STARTTLS) |
| `SMTP_USER` / `SMTP_PASSWORD` | — | SMTP credentials (mailbox used to send) |
| `SMTP_FROM` | — | From header (e.g. `LOTP Simulateur <noreply@ruban-adhesif.com>`) |
| `BOT_POLL_S` | `300` | Discord announcement polling interval (seconds) |

## API

All endpoints require a signed-in session, except `/api/health`,
`/api/invite/{token}` (public invitation info) and `/reports/*` (shareable reports).
`/api/admin/*` requires the admin account.

| Endpoint | Method | Description |
|---|---|---|
| `/api/login` / `/api/logout` | POST | Sign in / sign out (session cookie) |
| `/api/me` | GET | Current account |
| `/api/register` | POST | Register (or reset a password) from an invitation `{token, name, email?, password}` |
| `/api/invite/{token}` | GET | Invitation info (public) |
| `/api/me/settings` | POST | Save your account settings (language / display name) |
| `/api/me/password` | POST | Change your password (other sessions signed out) |
| `/api/admin/users/{id}/role` | POST | Change an account role (`member`/`officer`/`admin`) |
| `/api/guild/info` | GET / POST | Guild info (write: admin only) |
| `/api/group/sim` | POST | Group sim (profiles combined, multi-actor) |
| `/api/raids` | GET / POST | Raid calendar (create: officer+) |
| `/api/raids/{id}` | DELETE | Delete a raid (officer+) |
| `/api/raids/{id}/signup` | POST | Answer yes / no / maybe |
| `/api/dashboard` | GET | Guild activity (roster moves, roster size) |
| `/api/me/chars` | GET / POST | Linked characters — POST links one (`{name, main?}`) |
| `/api/me/chars/{id}` | DELETE | Unlink one of your characters |
| `/api/me/chars/{id}/main` | POST | Set a linked character as your main |
| `/api/admin/bot` | GET / POST | Discord bot status / configuration |
| `/api/admin/bot/guilds` | GET | Servers the bot is in (live) |
| `/api/admin/bot/guilds/{id}/channels` | GET | Text channels of a server (live) |
| `/api/admin/bot/test` | POST | Send a test message to the configured channel |
| `/api/sim` | POST | Submit `{input, iterations, label?, kind?, items?}` (`kind`: `dps`, `weights` or `gear` — `items` = item refs for gear) — returns `{id, status, position?, cached, warnings}` |
| `/api/sims` | GET | Last 50 simulations (summary) |
| `/api/sims/{id}` | GET | One simulation (full record) |
| `/reports/{id}/report.html` | GET | SimulationCraft HTML report (public) |
| `/reports/{id}/report.json` | GET | SimulationCraft JSON report (public) |
| `/api/roster` | GET | Guild roster — `?refresh=1` forces a refetch (1×/min max) |
| `/api/char/{realm}/{name}/summary` | GET | Character summary (Battle.net) |
| `/api/char/{realm}/{name}/equipment` | GET | Equipped items (Battle.net) |
| `/api/wcl/reports` | GET | Recent guild reports (Warcraft Logs) — `?refresh=1` forces |
| `/api/wcl/report/{code}` | GET | One report: boss pulls + parses (Warcraft Logs) |
| `/api/compare` | GET | Side-by-side characters `?chars=realm:name,…` (Battle.net + WCL) |
| `/api/profiles` | GET / POST | List sim profiles / create one (`{name, input, shared}`) |
| `/api/profiles/{id}` | GET / PATCH / DELETE | Read / update / delete a sim profile |
| `/api/admin/invites` | GET / POST | List / create invitations (`send_email` to mail the link) |
| `/api/admin/invites/{token}/send` | POST | Re-send a pending invitation by e-mail |
| `/api/admin/invites/{token}` | DELETE | Revoke an invitation |
| `/api/admin/users` | GET | List accounts |
| `/api/admin/users/{id}/active` | POST | Activate / deactivate an account |
| `/api/admin/users/{id}/reset-link` | POST | Generate a password-reset link |
| `/api/admin/users/{id}` | DELETE | Delete an account |
| `/api/health` | GET | Health + version + queue state (public) |

Allowed iteration counts: `1000, 5000, 10000, 25000, 50000`.

## Project structure

```
app/main.py             FastAPI app (auth, admin, sim queue, reports)
app/static/index.html   Simulator UI (French)
app/static/login.html   Login page
app/static/register.html Invitation registration page
app/static/admin.html   Admin panel (invitations + accounts)
app/static/characters.html Guild roster & character pages (French)
app/bnet.py             Battle.net API client (roster, characters, 30-min cache)
app/wcl.py              Warcraft Logs v2 client (raid reports, parses)
app/static/raids.html   Raid reports page (French)
app/static/compare.html Member comparison page (French)
app/mailer.py           Outgoing e-mail (invitations) via SMTP
app/static/gear.html    « Top Stuff » gear comparison page (French)
app/static/i18n.js      FR/EN translation engine (dictionary + DOM translation)
app/discord_bot.py      Discord REST client (bot announcements, no dependencies)
worker/simrun.py        SimulationCraft engine wrapper (official Docker image)
Dockerfile              App image (Python + Docker CLI)
docker-compose.yml      App deployment (Docker socket + data dir, both required)
CHANGELOG.md            Release history
VERSION                 Current version
```

## Version

Current version: `2026.09.024` (see `CHANGELOG.md`).

## License

MIT — see [LICENSE](LICENSE).
