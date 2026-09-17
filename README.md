# WoW Companion (working title)

Self-hosted companion for World of Warcraft: guild-friendly simulation reports built on
the official SimulationCraft Docker image, with Warcraft Logs and Battle.net API
integrations planned.

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
- [ ] Warcraft Logs / Battle.net integrations

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
| `/api/sim` | POST | Submit `{input, iterations, label?, kind?}` (`kind`: `dps` or `weights`) — returns `{id, status, position?, cached}` |
| `/api/sims` | GET | Last 50 simulations (summary) |
| `/api/sims/{id}` | GET | One simulation (full record) |
| `/reports/{id}/report.html` | GET | SimulationCraft HTML report (public) |
| `/reports/{id}/report.json` | GET | SimulationCraft JSON report (public) |
| `/api/admin/invites` | GET / POST | List / create invitations |
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
worker/simrun.py        SimulationCraft engine wrapper (official Docker image)
Dockerfile              App image (Python + Docker CLI)
docker-compose.yml      App deployment (Docker socket + data dir, both required)
CHANGELOG.md            Release history
VERSION                 Current version
```

## Version

Current version: `2026.09.003` (see `CHANGELOG.md`).

## License

MIT — see [LICENSE](LICENSE).
