# Changelog

All notable changes to this project are documented in this file.

## [2026.09.004] — 2026-09-17

### Added

- Stat-weights mode ("optimiseur", Mr Robot-style): runs SimulationCraft with
  `calculate_scale_factors=1`, parses the resulting weights (values, error
  margins, normalized), stores them with the simulation and renders them as a
  bar table in the web UI.
- Guild crest (in-game emblem) as page logo and favicon, with a crimson/gold
  color theme across all pages.

### Changed

- Sim submissions now carry a `kind` (`dps` or `weights`); the result cache is
  keyed on it.

## [2026.09.003] — 2026-09-17

### Added

- Accounts: invitation-only registration (`/invite/<token>` links with expiry,
  optional e-mail binding), login with sessions (scrypt password hashing,
  HttpOnly session cookie), logout.
- Admin panel (`/admin`): create and revoke invitations, list accounts,
  activate/deactivate, generate password-reset links, delete accounts.
- Auth guards: the simulator and its API now require a session; simulation
  reports stay shareable via link; `/api/health` stays public.
- Simulations are attributed to the member who submitted them; per-user
  concurrent-sim quota added on top of the per-IP one.

### Changed

- The app creates its first admin account at startup from the `ADMIN_EMAIL` /
  `ADMIN_PASSWORD` environment variables when no admin exists yet.

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
