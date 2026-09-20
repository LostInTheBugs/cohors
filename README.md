# Cohors

**Guild companion for World of Warcraft — self-hosted.** SimulationCraft simulations,
gear advice, raid preparation, Warcraft Logs reports, roster tracking, crafting, wishlist,
guild calendar and Discord announcements — one web app, one instance per guild.

**Live demo:** https://cohors.cloudfr.net ·
**Releases:** https://github.com/LostInTheBugs/cohors/releases

> Not affiliated with Blizzard Entertainment, Inc. Game data is provided by Blizzard
> Entertainment and by Warcraft Logs. Simulations run on the official SimulationCraft
> engine (Docker image).

## Features

- **Simulator** — paste your in-game `/simc` export: DPS simulations through the official
  SimulationCraft image, stat-weights mode, sim profiles (save once, reload in one click),
  group simulations (combine members' profiles with raid buffs).
- **Gear advice** — « Top Stuff » (simulate Wowhead items on your character, ranked by DPS),
  « Stuff conseillé » (what to wear among what you already own — three modes: current pieces,
  every piece at its maximum upgrade rank, or the season's BiS list — for all 40 specs),
  member comparison.
- **Roster & characters** — Battle.net roster and character pages (gear, professions,
  progress), account ↔ character links with one main per account, daily snapshots and
  progression tracking.
- **Raids & parses** — Warcraft Logs reports, per-boss parses, guild rankings, and a raid
  calendar with signups (Present / Maybe / Absent), unavailabilities, reminders and
  in-game calendar import through the Cohors add-on.
- **Crafting & wishlist** — guild-known recipes, materials and crafters; wishlist with
  priorities (BiS marked), drop alerts on the bosses you plan to raid.
- **Mythic+** — rating tracking and score alerts for the guild.
- **Discord bot** — announces new raid reports, roster movements, character milestones and
  a weekly recap; posts raid reminders before start.
- **Optional voice portal** — browser TeamSpeak client served behind the app login.
- **Bilingual UI (FR/EN)** — language stored per account; English auto-detected for
  English browsers.
- **Admin in the app** — invitations, accounts and roles, API keys (Battle.net /
  Warcraft Logs), sync cadences, SMTP e-mails, Discord bot, guild identity (name, short
  name, logo, background) and guild server identity (realm, region, Warcraft Logs guild).
  No file editing required after installation.
- **PWA** — installable on desktop and mobile, offline fallback page.

## Requirements

- Docker Engine with the Compose plugin (app container + SimulationCraft engine image)
- A host with enough CPU for SimulationCraft runs — the app launches sibling SimulationCraft
  containers through the mounted Docker socket
- Python 3.11+ (only to run the engine wrapper standalone)

## Quick start

```bash
git clone https://github.com/LostInTheBugs/cohors.git
cd cohors
cp .env.example .env     # set DATA_DIR (absolute path, same on host and container) + ADMIN_*
docker compose up -d --build
# the app listens on 127.0.0.1:${PORT} (default 8030) — put a reverse proxy in front for TLS
```

On first start, an admin account is created from `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Then set
everything else from inside the app:

1. **Settings → Administration → 🏰 Guild (realm & WCL)** — realm (slug), Battle.net region,
   guild slug and Warcraft Logs guild name. The **🔎 Check** button verifies the guild is
   found on both services.
2. **🔑 API keys** — your Battle.net and Warcraft Logs client credentials (tested on save).
3. **✉️ E-mail (SMTP)** — optional, for invitation e-mails.
4. **🤖 Discord bot** — optional, create a Discord application and paste its token.
5. **🎨 Identity** — guild name, short name, logo and background.

Invite members from **Invitations**; they register through their invite link.

Note: mounting the Docker socket is root-equivalent on the host. Keep the app behind a
reverse proxy, and run it on a machine you trust.

## WoW add-on (Cohors)

`addon/Cohors` is a small in-game add-on that collects the guild calendar (raids and
answers) and exports it for the site. Members download it as a zip from the **Calendar**
page, then in game:

- `/cohors` — open the panel and collect the guild calendar;
- `/cohors export` — export the data to paste on the Calendar page (or import the
  `Cohors.lua` SavedVariables file);
- `/cohors recettes` — export the crafting recipes you know (professions import);
- `/cohors diag`, `/cohors reset` — diagnostics and reset.

## Configuration

Most settings can also be edited in the app by an admin — `.env` values are the fallback.

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
| `COOKIE_DOMAIN` | — | Parent cookie domain (voice subdomain, e.g. `.example.org`) |
| `BNET_CLIENT_ID` / `BNET_CLIENT_SECRET` | — | Battle.net API client (https://develop.battle.net) |
| `BNET_REGION` | `eu` | Battle.net region |
| `BNET_GUILD_REALM` | — | Guild realm slug (admin UI preferred) |
| `BNET_GUILD_SLUG` | — | Guild slug (admin UI preferred) |
| `WCL_CLIENT_ID` / `WCL_CLIENT_SECRET` | — | Warcraft Logs API client |
| `WCL_GUILD_NAME` | — | Guild name on Warcraft Logs (admin UI preferred) |
| `WCL_GUILD_REALM` / `WCL_GUILD_REGION` | — / `EU` | Warcraft Logs realm & region |
| `WCL_RAID_ZONE_ID` | `53` | Zone id used for character rankings |
| `SMTP_HOST` … `SMTP_FROM` | — | Outgoing e-mail (invitations) |
| `VOICE_BACKEND` | `http://127.0.0.1:3040` | Voice gateway (optional voice portal) |
| `VOICE_PUBLIC_HOST` | — | Voice subdomain (served to the UI by the API) |
| `VOICE_HEADER` | `X-Cohors-Voice` | Gate header set by the voice vhost |
| `BOT_POLL_S` | `300` | Discord announcement polling interval (seconds) |

See `.env.example` for the full list (quotas, snapshot cadences, retention).

## API

All endpoints require a signed-in session, except `/api/health`, `/api/invite/{token}`,
`/api/voice/config` and `/reports/*` (shareable reports). `/api/admin/*` requires an admin
account (invitation endpoints accept officers).

| Endpoint | Method | Description |
|---|---|---|
| `/api/login` / `/api/logout` | POST | Sign in / sign out (session cookie) |
| `/api/me` | GET | Current account |
| `/api/register` | POST | Register (or reset a password) from an invitation |
| `/api/invite/{token}` | GET | Invitation info (public) |
| `/api/me/settings` / `/api/me/password` | POST | Account settings / password change |
| `/api/sim` | POST | Submit a simulation (`dps`, `weights`, `gear`, `stuff` kinds) |
| `/api/sims` / `/api/sims/{id}` | GET | Simulation list / one simulation |
| `/reports/{id}/report.html` | GET | SimulationCraft HTML report (public) |
| `/api/roster` | GET | Guild roster (Battle.net, cached) |
| `/api/char/{realm}/{name}/summary` | GET | Character summary (Battle.net) |
| `/api/wcl/reports` / `/api/wcl/report/{code}` | GET | Raid reports & parses (Warcraft Logs) |
| `/api/stuff` | POST | « Stuff conseillé » (current / max-rank / BiS modes) |
| `/api/profiles` (+ `/{id}`) | GET / POST / PATCH / DELETE | Sim profiles |
| `/api/raids` (+ signup) | GET / POST / DELETE | Raid calendar & signups |
| `/api/me/chars` (+ `/{id}`) | GET / POST / DELETE | Linked characters, main |
| `/api/wishlist` | GET / POST / DELETE | Wishlist (priorities, BiS) |
| `/api/addon` | GET | The Cohors add-on as a zip |
| `/api/admin/invites` (+ send/revoke) | GET / POST / DELETE | Invitations (officer+) |
| `/api/admin/users` (+ role/active/…) | GET / POST / DELETE | Accounts (admin) |
| `/api/admin/api-keys` / `jobs` / `mail` / `bot` / `guild` | GET / POST | In-app administration |
| `/api/health` | GET | Health + version + queue state (public) |

## Project structure

```
app/main.py               FastAPI app (auth, admin, sim queue, pages, APIs)
app/bnet.py               Battle.net API client (roster, characters, items)
app/wcl.py                Warcraft Logs v2 client (reports, parses)
app/mailer.py             Outgoing e-mail (invitations) via SMTP
app/discord_bot.py        Discord REST client (announcements)
app/static/               29 pages (FR) + i18n.js FR/EN engine, branding.js, PWA
app/data/bis.json         Embedded BiS lists (Wowhead guide snapshots, 40 specs)
addon/Cohors/             In-game add-on (guild calendar + professions export)
worker/simrun.py          SimulationCraft engine wrapper (official Docker image)
Dockerfile                App image (Python + Docker CLI)
docker-compose.yml        App deployment (Docker socket + data dir, both required)
CHANGELOG.md              Release history
VERSION                   Current version
```

## Development (engine wrapper)

```bash
# Simulate a profile shipped inside the image (works out of the box after a docker pull)
python3 worker/simrun.py --container-profile profiles/MID2/MID2_Mage_Arcane.simc --iterations 500

# Simulate your own in-game `/simc` export (or any .simc profile file)
python3 worker/simrun.py --profile ./my-export.simc --iterations 10000 --outdir ./out
```

## Version

Current version: `2026.09.137` (see [releases](https://github.com/LostInTheBugs/cohors/releases)).

## License

MIT — see [LICENSE](LICENSE).

## Credits

Simulation engine: [SimulationCraft](https://www.simulationcraft.org/). Raid data:
[Warcraft Logs](https://www.warcraftlogs.com/). Game data: Blizzard Entertainment.
Gear guides referenced in the app: Wowhead, Icy Veins and Archon (snapshots stored in
`app/data/bis.json`).
