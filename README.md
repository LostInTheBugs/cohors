# WoW Companion (working title)

Self-hosted companion for World of Warcraft: guild-friendly simulation reports built on
the official SimulationCraft Docker image, with Warcraft Logs and Battle.net API
integrations planned.

> Early development. Not affiliated with Blizzard Entertainment, Inc. Game data is
> provided by Blizzard Entertainment and Warcraft Logs (see attribution requirements).

## Status

- [x] Simulation engine wrapper (`worker/simrun.py`) — runs a SimulationCraft sim via the
      official `simulationcraftorg/simc` Docker image, extracts DPS, produces HTML + JSON reports.
- [x] Web app v1 (`app/`) — `/simc` export submission, FIFO sim queue (one sim at a time),
      shared result cache, per-IP quotas, French UI, report archive served over HTTP.
- [ ] Accounts + invite links
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
cp .env.example .env    # set DATA_DIR (see note below)
docker compose up -d --build
# the app listens on 127.0.0.1:${PORT} (default 8030) — put a reverse proxy in front for TLS
```

The app container mounts the host Docker socket to launch SimulationCraft containers, so
`DATA_DIR` must be the same absolute path on the host and inside the container — it is
passed as-is to the SimulationCraft containers for input/output files.

Note: mounting the Docker socket is root-equivalent on the host. Keep the app behind a
reverse proxy, rate-limited, and small until the simulation worker is split out.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8030` | HTTP port (bound to localhost) |
| `DATA_DIR` | `./data` | SQLite database + generated reports |
| `QUEUE_MAX` | `20` | Max queued + running simulations |
| `PER_IP_ACTIVE` | `3` | Max queued/running simulations per IP |
| `PER_IP_COOLDOWN_S` | `15` | Minimum delay between submissions per IP |
| `SIM_TIMEOUT` | `900` | Hard timeout per simulation (seconds) |
| `SIMC_IMAGE` | `simulationcraftorg/simc:latest` | SimulationCraft engine image |

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/sim` | POST | Submit `{input, iterations, label?}` — returns `{id, status, position?, cached}` |
| `/api/sims` | GET | Last 50 simulations (summary) |
| `/api/sims/{id}` | GET | One simulation (full record) |
| `/reports/{id}/report.html` | GET | SimulationCraft HTML report |
| `/reports/{id}/report.json` | GET | SimulationCraft JSON report |
| `/api/health` | GET | Health + version + queue state |

Allowed iteration counts: `1000, 5000, 10000, 25000, 50000`.

## Project structure

```
app/                    FastAPI web app + French UI (static/index.html)
worker/simrun.py        SimulationCraft engine wrapper (official Docker image)
Dockerfile              App image (Python + Docker CLI)
docker-compose.yml      App deployment (Docker socket + data dir, both required)
CHANGELOG.md            Release history
VERSION                 Current version
```

## Version

Current version: `2026.09.002` (see `CHANGELOG.md`).

## License

MIT — see [LICENSE](LICENSE).
