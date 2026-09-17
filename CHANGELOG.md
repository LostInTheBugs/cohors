# Changelog

All notable changes to this project are documented in this file.

## [2026.09.002] — 2026-09-17

### Added

- Web app (FastAPI + SQLite): submit a `/simc` addon export, FIFO simulation
  queue (one simulation at a time — a sim saturates every core), shared result
  cache keyed on SHA-256(input + iterations), per-IP anti-abuse quotas.
- French dark UI: submission form, live queue/results list, links to the
  SimulationCraft HTML/JSON reports.
- REST API: `POST /api/sim`, `GET /api/sims`, `GET /api/sims/{id}`,
  `GET /reports/{id}/report.html|json`, `GET /api/health`.
- Docker deployment: app container (Docker CLI + mounted socket) launching the
  officially maintained `simulationcraftorg/simc` image; compose + `.env`.
- Self-hosted deployment documented: reverse proxy + TLS termination on the
  host, DNS record for the app hostname.
- In-app help panel (collapsible): installing the SimulationCraft addon and
  getting the `/simc` export.

## [2026.09.001] — 2026-09-16

### Added

- SimulationCraft engine wrapper (`worker/simrun.py`): runs a simulation in the
  official `simulationcraftorg/simc` Docker image (embedded profile or a
  `/simc` export file), parses DPS/DPS-Error, produces HTML + JSON reports.
