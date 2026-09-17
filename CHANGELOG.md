# Changelog

All notable changes to this project are documented in this file.

## [2026.09.017] — 2026-09-17

### Added

- Account settings page (`/settings`): interface language stored **on the
  account** (follows you across devices; also applied right after login and
  at sign-up), display name, and self-service password change (other sessions
  are signed out). Header link « ⚙️ Paramètres / Settings » on every page.
- New API: `POST /api/me/settings`, `POST /api/me/password`; login and
  `/api/me` now return the account language.

## [2026.09.016] — 2026-09-17

### Added

- Bilingual UI (FR/EN): a FR/EN switcher in the header of every page,
  remembered in the browser (automatic English for English-language browsers,
  French otherwise). The whole site is translated client-side — pages stay
  French in the source and `app/static/i18n.js` translates the rendered DOM
  (exact dictionary + rules for dynamic strings, MutationObserver for
  JS-rendered content). Dates and numbers switch to English formatting.

## [2026.09.015] — 2026-09-17

### Added

- Account roles: **membre** (default), **officier**, **administrateur**.
  Officers can manage invitations (create / send / revoke); administrators keep
  accounts, roles and the Discord bot. Roles are changed from the Comptes tab
  (dropdown per account — never your own). API: `POST /api/admin/users/{id}/role`.

## [2026.09.014] — 2026-09-17

### Fixed

- `POST /api/admin/bot` now performs **partial** updates: fields omitted from
  the request are left untouched (installing a token no longer clears the
  previously saved Application ID, and vice-versa).

## [2026.09.013] — 2026-09-17

### Changed

- Discord bot admin tab: the invite link is generated and shown
  automatically as soon as the Application ID is set (copy / open buttons),
  with a note that the « Manage Server » permission is required to add the bot.

## [2026.09.012] — 2026-09-17

### Added

- Discord bot (admin tab « 🤖 Bot Discord »): ready-made OAuth2 invite
  link, token activation, live channel picker, and automatic announcements —
  new Warcraft Logs raid reports and guild roster changes (joins/leaves).
  First pass sets a baseline (no retroactive posts); test-message button;
  the token is validated against Discord before being saved.
- Account ↔ character links: link your account to your guild characters
  from the Personnages page (⭐ main + alts), star markers and row highlight
  in the roster, one-click link/unlink from a character's detail, and the
  admin account list now shows each account's main character.
- New API: `/api/me/chars` (GET/POST), `/api/me/chars/{id}` (DELETE),
  `/api/me/chars/{id}/main` (POST), `/api/admin/bot*` (status, config,
  guilds, channels, test).
- `BOT_POLL_S` env var — Discord announcement polling interval (default 300 s).

## [2026.09.011] — 2026-09-17

### Added

- « Top Stuff » — gear comparison: paste Wowhead item links or IDs (max 15);
  each item is simulated on the character (SimulationCraft profilesets) and
  ranked by DPS. Rings and trinkets are tested on both slots. Item names,
  quality and icons come from the Blizzard item API.
- New page `/gear` (🧰 Top Stuff); `POST /api/sim` accepts `kind=gear` with an
  `items` field (item references) and returns `warnings` when pieces are
  skipped.

## [2026.09.010] — 2026-09-17

### Added

- Invitation e-mails: when creating an invitation with an address, the link can
  be sent automatically by e-mail (from `noreply@ruban-adhesif.com`, styled
  French template). Pending invitations with an address get a « ✉️ Renvoyer »
  action, and the admin panel shows the SMTP status.
- `POST /api/admin/invites` accepts `send_email`; new
  `POST /api/admin/invites/{token}/send` endpoint.
- Configuration: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `SMTP_FROM`.

## [2026.09.009] — 2026-09-17

### Added

- Sim profiles (« Profils »): save a `/simc` export as a named profile, reload it in
  one click on the simulator page, update or delete it, and optionally share it with
  the whole guild (shared profiles appear under « Partagés par la guilde »).
- REST API: `GET/POST /api/profiles`, `GET/PATCH/DELETE /api/profiles/{id}` —
  max 20 profiles per account; only the owner (or an admin) can edit or delete.

## [2026.09.008] — 2026-09-17

### Added

- « Comparateur » page: put guild characters side by side (2–6) — equipped item
  level and last seen (Battle.net), plus best Warcraft Logs parses for the
  current raid: per-boss best percentile and the best/median performance
  averages (30-min server cache). Selection is kept in the browser.
- `GET /api/compare?chars=realm:name,…` endpoint.
- Configuration: `WCL_RAID_ZONE_ID` (default `53`).

## [2026.09.007] — 2026-09-17

### Changed

- New navigation: pill tabs (Simulateur / Personnages / Rapports) with a highlighted
  active page, shared across the app.
- The admin area is now clearly separated from the app: gold banner, its own header,
  sub-tabs (Invitations / Comptes) and an explicit « Retour à l'application » link.
  App pages only keep a discreet gold admin entry (visible to admins).

## [2026.09.006] — 2026-09-17

### Added

- « Rapports de raid » page: recent guild reports from the Warcraft Logs v2 API,
  per-report boss pulls (kill/wipe progress, difficulty, average item level,
  duration, raid size) and per-fight parses (tanks/healers/DPS tables with
  class colors and percentile scores).
- Warcraft Logs integration (GraphQL, client credentials) with server cache
  (report list 15 min, reports + parses 30 min).
- Configuration: `WCL_CLIENT_ID` / `WCL_CLIENT_SECRET` (plus optional
  `WCL_GUILD_NAME`, `WCL_GUILD_REALM`, `WCL_GUILD_REGION`).

## [2026.09.005] — 2026-09-17

### Added

- « Personnages » page: full guild roster from the official Battle.net API
  (ranks, levels, item level, last seen), search + level filter, and a detail
  view with the character's equipped items (per-item Wowhead links).
- Battle.net integration (OAuth client credentials) with a 30-minute server
  cache; manual refresh available (max once per minute).
- Configuration: `BNET_CLIENT_ID` / `BNET_CLIENT_SECRET` (plus optional
  `BNET_REGION`, `BNET_GUILD_REALM`, `BNET_GUILD_SLUG`, `BNET_LOCALE`).

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
