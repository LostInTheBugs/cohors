# Changelog

All notable changes to this project are documented in this file.

## 2026.09.146-c1 - 2026-09-20

### Fixed
- **First-time setup page (`/start`)** — the Help tab fell back to the browser-default blue link (the
  page was missing the shared tab styles); the step markers are now a uniform rail (green disc / empty
  ring), and the checklist rows align with the card content.
- Simulator page — the « Itérations : » label was never translated to English.
- Setup checklist — singular « 1 membre » (was « 1 membres »).

## 2026.09.146 - 2026-09-20

### Added
- **First-time setup checklist** (`/start`, administrator) — the eight steps to get a guild running
  (administrator account, guild identity, Battle.net and Warcraft Logs API keys, site identity, SMTP
  and Discord bot as optional steps, first members) with their live status and a direct link into
  each matching Settings section. Admins also get a dashboard card while steps remain.
- **`deploy/` — guild-officer quick start**: a standalone `docker-compose.yml` running the prebuilt
  GHCR images (no git clone, no build) plus a minimal `.env.example` (data dir, admin account, public
  URL — everything else is configured from the app). README section added.

## 2026.09.145-c8 - 2026-09-20

### Fixed
- Recipe import (Prep page): an add-on export with one « profession » entry per expansion tier
  (older exports, up to ~22 entries with every recipe duplicated) was truncated to its first
  10 entries — whole professions (e.g. Cooking: 53 recipes) were silently dropped while the import
  still reported success. Entries are now merged per profession with per-recipe deduplication
  (keeping the most complete material list), the cap is gone, and the page shows the real number
  of imported recipes.
- « Mes recettes » page: a stray `loadStats()` call (a function from « Mes statistiques ») crashed
  right after a successful import and replaced the success message with « loadStats is not
  defined ».
- This release also ships the whole in-game add-on **1.9.x** line from today's iterations
  (c2–c7): recipes export on the current client APIs, guided collection with the « ▶ Ouvrir »
  button, archaeology skip, incremental saves (see the sections below).


## 2026.09.145-c7 - 2026-09-20

### Fixed
- In-game add-on **1.9.2** — recipes are now saved **incrementally**: after every profession read (or
  skipped) and again on `/reload` or logout (`PLAYER_LOGOUT`). Previously the export file was only
  written when the whole run finished, so stopping mid-way (a stuck profession, a reload) lost
  everything already read — the web import then reported no usable recipe even after a large
  session. An empty run can also no longer overwrite an existing export.
- One read pass per profession: `GetAllRecipeIDs` returns the **whole profession** (every expansion
  tier at once — confirmed in a live trace: identical id counts per tier), so switching tiers read
  nothing extra and **duplicated every recipe** (Cuisine: 53 recipes counted 3× = 159). Each recipe
  is now listed once.
- Offline harness: new « logout » scenario (reload mid-export keeps what was read) — 11 scenarios
  in CI.

## 2026.09.145-c6 - 2026-09-20

### Fixed
- In-game add-on **1.9.1** — professions that have no standard trade-skill window (archaeology:
  the dig-site UI, not the crafting UI) no longer block the export: when an open was requested and
  nothing readable appears within 8 seconds, the add-on marks the profession as skipped with a
  clear message (« « Archéologie » ne s'ouvre pas comme un métier standard — ignoré »), moves on,
  and records it in the report. Clicking the « ▶ Ouvrir » button a second time skips a stuck
  profession immediately. If that profession ever opens as a real trade-skill window, it will
  still be read.
- Offline harness: new « unreadable » scenario (client accepts the open but no window appears) —
  10 scenarios in CI.

## 2026.09.145-c5 - 2026-09-20

### Changed
- In-game add-on **1.9.0** — the guided recipes export gains a clickable **« ▶ Ouvrir »** button on
  the progress window: **one click opens the next profession**. This works because `OpenTradeSkill`
  is only accepted by the client from a hardware event — a click *is* one (the add-on cannot open
  windows from its own timers, which is why the bar could sit at « 1/5 » waiting). The progress
  window now states the next action explicitly (« recettes — 1/5 lu(s) · clique « ▶ » (« Minage ») ·
  … »), reminds you every 45 s while waiting, and says when the client refused an open so you can
  open the profession by hand. Opening professions yourself still works exactly as before.

### Fixed
- Waiting state no longer looks frozen at N/5: the label names the next profession, lists the
  remaining ones, and the button updates to the profession it will open.
- Offline harness: new « openbtn » scenario where the client refuses every non-click open — the
  export only completes through the button. 9 scenarios in CI.

## 2026.09.145-c4 - 2026-09-20

### Fixed
- In-game add-on **1.8.1 — critical load fix**: v1.8.0 registered `TRADE_SKILL_UPDATE`, an event
  that no longer exists in the current client. Registering an unknown event raises an error that
  aborts the whole file load, leaving the add-on half-loaded (spamming
  « attempt to call a nil value » every frame). The bogus registration is removed; all event
  registrations now go through a safe wrapper that records refused events (shown in
  `/cohors diag`), and login now warns if the file did not load completely.
- Offline harness is now as strict as the client about event names (unknown name = hard error at
  load) — this class of abort can no longer pass CI. All 8 scenarios rerun.

## 2026.09.145-c3 - 2026-09-20

### Fixed
- In-game add-on **1.8.0** — the recipes export now understands the live client:
  `C_TradeSkillUI.OpenTradeSkill` is **restricted to hardware events** (the client silently refuses
  add-on calls made from timers — which is why every profession came back empty). The add-on no
  longer tries to open profession windows itself: it reads the windows the player opens
  (`TRADE_SKILL_SHOW` plus a periodic check), walks **every expansion tier** of each profession,
  and reports along the way (« ✔ Cuisine : 42 recette(s) », « métier(s) restant(s) : … »).
  Closing a window early ends that profession's remaining tiers cleanly. A best-effort auto-open
  is still attempted from the click/slash command (the client accepts it from hardware input).
- Gathering and dummy recipes are excluded from the export (they are not crafts), and the add-on
  never closes the player's profession window.
- Offline harness: « closemid » scenario (window closed mid-read) and reworked « apifail »
  (client refusing to open) — 8 scenarios now run in CI.

## 2026.09.145-c2 - 2026-09-20

### Fixed
- In-game add-on 1.7.2 — the recipes export reads recipes through the **current client API**:
  `C_TradeSkillUI.GetAllRecipeIDs()` (the client replaced `GetFilteredRecipeIDs` with it; the
  removed function failed silently, hence « 0 recette(s) exportée(s) » even on characters with
  professions). Non-learned recipes are filtered out through `recipeInfo.learned`.
- Every failed recipes API call is now recorded and reported: a chat warning plus a
  `Cohors_DB.rec_diag` report with per-profession/tier counts, data source, unlearned/disabled
  skips and material counts — a silent `pcall` can no longer produce an unexplained empty export.
  Longer waits for the profession window, and a clear in-chat reason when a profession yields
  nothing; the start message now lists the detected professions by name.
- Offline harness: two new scenarios (legacy-client fallback, no recipes API at all) — 7 scenarios
  now run in CI.

## 2026.09.145-c1 - 2026-09-20

### Fixed
- In-game add-on 1.7.1: `/cohors` with no argument no longer starts a calendar collection by
  itself — it only opens the panel. Typing `/cohors` to reach the recipes button could silently
  launch a raid-data collection first, which looked like the recipes export was collecting
  calendar data. Collections are now always explicit (« Collecter » / « 📚 Recettes » buttons or
  `/cohors collect`).
- The two engines are now mutually exclusive: starting the recipes export while a calendar
  collection runs (or the reverse) answers with a clear message instead of running both at once.
- Offline harness: two new regression scenarios (mutual exclusion both ways, `/cohors` starts
  nothing) — 5 scenarios run in CI.

## 2026.09.145 - 2026-09-20

### Security
- **The app no longer sees the Docker socket.** A new tiny `worker` service (stdlib only, its own
  image) is the only component mounting `/var/run/docker.sock`; the app submits restricted jobs
  over a shared Unix socket (mode 0660, group reserved for the app, caller uid verified with
  `SO_PEERCRED`) and now runs **non-root** (`user: ${APP_UID}:10001`), with no Docker CLI shipped
  in its image. This closes the "Docker socket / root" finding of the first external review.
- The worker re-validates **every** job itself (`shared/simvalidate.py`, copied into both images):
  profile size cap, iterations bounds, allowlisted `key=value` options and forbidden SimulationCraft
  directives (`input=`, `output=`, `html=`, `json`/`json2=`, `apikey=`) — now also detected behind
  `profileset_+=` prefixes, which the 2026.09.141 guard could miss.
- Job paths come from worker-generated job ids only: no path from the app ever reaches the
  filesystem. SimC containers run with `--rm` and `sim-<job>` names, are killed explicitly
  (`docker kill`) on timeout, and leftovers from a crashed worker are removed at startup.
- New regression tests: the compose app service must not mount the Docker socket, must run
  non-root, and the app image must not contain the Docker CLI.

### Changed
- README security section rewritten around the worker architecture; `.env.example` gains
  `APP_UID` (uid the app runs as — must own `DATA_DIR`) and the worker tuning variables
  (`SIM_MAX_PROFILE_KB`, `SIM_MAX_ITERATIONS`).

## 2026.09.144 - 2026-09-20

### Added
- In-game add-on 1.7.0 — the recipes export now shows its progress live: a small draggable
  window with a bar and percentage, the current profession and expansion tier, the recipe count
  and elapsed time. It appears even when the panel is closed (e.g. `/cohors recettes`) and fades
  out a few seconds after finishing; the calendar collection drives the same window (months
  scanned, then raids opened) instead of the old cryptic "… recettes phase" status line.
- `tools/addon-harness.lua`: the offline add-on harness now ships with the repository (lua5.1 +
  Blizzard API stubs, `os`/`io` removed like the game client). Scenarios: full recipes export,
  calendar collection, API failures, no professions; it checks the exported JSON, the progress
  bar (monotonic, reaches 100%) and the window closing. Runs in CI.

### Fixed
- Add-on version was drifting: the TOC said 1.6.0 while the Lua constant still said 1.5.1
  (left over from the rename) — both now read 1.7.0, and a repository test keeps them in sync.
- Recipes progress could briefly fall back to 0% when the last expansion tier of a profession
  was read; profession switches now reset the per-tier state cleanly.

## 2026.09.143-c1 - 2026-09-20

### Fixed
- The add-on download link on the Guild page now uses the same button style as the page's other
  actions — it referenced CSS classes that only existed on the Calendar page, so it fell back to
  the browser's default button look.

## 2026.09.143 - 2026-09-20

### Added
- Cohors gets its own identity mark: a new default logo (crimson roundel, gold laurel and « C »,
  drawn in the app's Cinzel typeface) replaces the guild crest as the shipped default — used as
  favicon, PWA icons and header logo when a guild has not uploaded its own logo. Source of truth:
  `app/static/logo.svg`; social-preview art lives in `docs/cohors-social.png`.
- The add-on download now has its own card on the **Guild** page, next to the Discord/TeamSpeak
  links — the in-game add-on does more than the calendar (raid signups *and* professions export),
  so the Guild page is its natural home. The Calendar page keeps the import box and points there.

## 2026.09.142 - 2026-09-20

### Security
- Login rate-limiting can no longer be bypassed with a spoofed `X-Forwarded-For` header: the client
  address is now read from the **last** entry — the one appended by the reverse proxy — instead of
  the first (client-supplied, forgeable) one. Reproduced live before the fix (16 attempts, 16 forged
  values, never throttled) and re-tested after (throttled on the 16th attempt).
- Every response now carries baseline security headers: Content-Security-Policy (external scripts and
  objects blocked, framing limited to the app and the configured voice host, `base-uri` locked),
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`. `script-src` still allows inline
  code because the UI is built from per-page scripts — nonce-based tightening is planned together
  with the front-end refactor.
- The HTML escape helper (`esc`) is now one shared module (`app/static/esc.js`) instead of a
  copy-pasted `const` in 23 pages — one place to audit, and a test now asserts that every page
  calling it loads the shared file.

### Added
- API tests (`tests/test_api.py`): health, security headers, failed login, rate-limit enforcement,
  and a regression test proving a spoofed `X-Forwarded-For` cannot dodge the throttle. CI now
  installs the app requirements to run them.

## 2026.09.141 - 2026-09-20

### Security
- SimulationCraft containers are now sandboxed: no network, read-only root filesystem (reports
  come out through the single mounted volume), memory/process caps (`SIM_MEM`, `SIM_PIDS`,
  optional `SIM_CPUS`) and `no-new-privileges`. A malformed profile can no longer reach the
  network or push the host past its caps.
- SimulationCraft honours a few options written inside a profile file (`input=` reads files,
  `output=` writes files — verified against the official image): profiles containing `input=`,
  `output=`, `html=`, `json=`/`json2=` or `apikey=` lines are now rejected up front with a clear
  message. A `/simc` export never contains them.
- Report share-links use longer random ids (80 bits instead of 48) — existing links keep working.

### Added
- Test suite (pytest) and CI (`tests/`, `.github/workflows/tests.yml`): password hashing
  (scrypt — now in `app/security.py`), the profile guard, and repository consistency
  (VERSION/CHANGELOG/i18n/add-on).
- `CONTRIBUTING.md` and GitHub issue templates.
- `tools/refresh-bis.md` + `tools/bis-merge.py`: the (previously internal) procedure and
  script used to refresh the embedded BiS snapshot — `app/data/bis.json` now points there.
- Container image published to GHCR on release tags (`.github/workflows/image.yml`).
- Healthcheck on the app container in `docker-compose.yml` (uses `/api/health`).

### Changed
- README: simulation sandbox and security notes, backups & updates, a « Why not just
  Raidbots + Warcraft Logs + Raid-Helper? » section, CalVer explained, published image.
- `.env.example`: documents the simulation sandbox knobs, a pinned `SIMC_IMAGE` example and the
  `ADMIN_PASSWORD` hygiene (change it in the app, then remove the variable).

## 2026.09.140 - 2026-09-20

### Fixed
- English mode: the "Refresh" buttons (roster, reports) and the role filter "All" now
  translate like the rest of the UI.

### Notes
- First public release: the app is open-sourced under the name **Cohors** — see the
  2026.09.137 entry for the rename details (generic branding, `Cohors` add-on, guild
  identity configured in the app) and the entries below for the pre-release history.

## 2026.09.139 - 2026-09-20

### Fixed
- English mode: several page names in the header stayed French — Characters, Raid reports,
  Compare, Fun achievements, Mythic+ and Voice now translate.

## 2026.09.138 - 2026-09-20

### Changed
- The app presents itself as simply **Cohors**: the sign-in and registration screens no
  longer append "Simulateur" — the app does much more than simulations (roster, raids,
  calendar, crafting, wishlist, suivi...).

## 2026.09.137 - 2026-09-20

### Added
- The app has a name: **Cohors** — guild companion for World of Warcraft. Branding is now
  generic (page titles, PWA manifest, e-mails, Discord embeds, User-Agents); the guild
  identity (name, short name, logo, background) stays configurable in Settings -> Identity.
- Public demo instance at https://cohors.cloudfr.net.
- `GET /api/voice/config` (public): the voice portal host for the Voice page — no longer
  hardcoded in the browser.

### Changed
- WoW add-on renamed to **Cohors**: folder `addon/Cohors`, title "Cohors - Guild calendar",
  SavedVariables `Cohors_DB`, slash command `/cohors` (professions export: `/cohors recettes`),
  download `Cohors-addon.zip`. Reinstall the add-on — the old folder and its saved variables
  are ignored.
- The guild identity (realm, guild slug, Warcraft Logs guild name) has no hardcoded default
  anymore: values come from Settings -> Administration -> Guild (or the server file), and the
  roster/reports pages show a clear message until the guild is configured.
- Discord announcements and invitation e-mails display the guild's configured name instead of
  fixed text.
- Voice portal: gate header configurable via `VOICE_HEADER` (default `X-Cohors-Voice`), voice
  host via `VOICE_PUBLIC_HOST` (served to the UI by the API), portable client zip renamed.
- Session cookie renamed to `cohors_session` and browser storage keys to `cohors_*` — existing
  sessions sign in once again; local UI drafts (simulator input, comparison selection) reset.

## 2026.09.136 - 2026-09-20

### Added
- Administration: guild identity is now configurable (Settings -> Administration -> Guild (realm & WCL)), covering both service sides: Battle.net region, realm (slug), guild slug and default data language, plus Warcraft Logs region and guild name. A "Check" button verifies the guild is found on Battle.net and on Warcraft Logs before/after saving. Values saved here override the server file (.env), so the site can be pointed at another guild without editing files.

### Changed
- Guild identity (realm, region, Warcraft Logs guild name) is now read from the administration when set, falling back to the server file otherwise; changing it immediately refreshes the cached data.

### Fixed
- English mode: help-page list items declared as HTML blocks in the i18n dictionary (API keys, sync jobs, e-mail/SMTP bullets) are now actually translated — whole-item blocks are compared against the rendered HTML instead of single text nodes.

## 2026.09.135 - 2026-09-19

### Added
- Administration: SMTP is now configurable (Settings -> Administration -> E-mail (SMTP)): server, port, security (STARTTLS/SSL/none), username, password, sender, HELO. Connection checked on save, test e-mail button.

### Changed
- Invitation e-mails now use the guild identity (name, short name, site URL) instead of hardcoded text.

## 2026.09.134 - 2026-09-19

### Fixed
- Sync jobs: job status lines are fully translated in English mode.

## 2026.09.133 - 2026-09-19

### Fixed
- Sync jobs: the "task enabled" checkbox reads and saves its state correctly.

## 2026.09.132 - 2026-09-19

### Added
- Administration: sync jobs are now configurable (Settings -> Administration -> Sync jobs): snapshot cadence and re-snapshot delays, characters per run, history retention, professions refresh, Discord bot interval. Shows the last run time, errors, and a "Run now" button for the snapshot pass.

## 2026.09.131 - 2026-09-19

### Fixed
- API keys: the Warcraft Logs quota test message is fully translated in English mode.

## 2026.09.130 - 2026-09-19

### Fixed
- API keys (administration): English wording for the status badge and the connection test messages.

## 2026.09.129 - 2026-09-19

### Added
- Administration: API keys (Battle.net, Warcraft Logs) can now be set from Settings → Administration → API keys, tested on save, and override the server environment values (.env) without a redeploy.

## 2026.09.128 - 2026-09-19

### Added
- Gear advice (BiS mode): BiS lists now cover all 40 specializations (every class, hero specs included), each snapshot dated and sourced from the Wowhead guides. Every item id verified against the Blizzard API (469 entries, slot/type check, 0 anomalies).

## 2026.09.127 - 2026-09-19

### Fixed
- Gear advice: item names and slot labels now follow the active language (browser or saved preference) instead of the saved preference only.

## 2026.09.126 - 2026-09-19

### Fixed
- English mode: slot labels (Head, Neck, Waist, Ring 1…) are now translated everywhere, including gear advice tables.

## 2026.09.125 - 2026-09-19

### Added
- Gear advice (BiS mode): bundled BiS lists for 9 more specializations (all seven healer specs, Elemental and Enhancement Shaman, Arcane Mage) — every item id verified against the Blizzard API (slot/type check, 144 entries).

## 2026.09.124 - 2026-09-19

### Fixed
- Gear advice (BiS mode): remaining French fragments translated in English mode.

## 2026.09.123 - 2026-09-19

### Added
- Gear advice: BiS mode — the guide's best-in-slot list per spec (bundled dated snapshot in app/data/bis.json, Wowhead), slot by slot with source, max item level and ownership status (equipped / in bags / missing), compared against what you wear.

## 2026.09.122 - 2026-09-19

### Added
- Gear advice: new mode "all items at max upgrade rank" (each item modelled at the highest version published by Wowhead, cached in item_max_ilvl), a "Mode" selector, a note that the selected build's talents are applied, and reference links (Wowhead, Icy Veins, Archon).

## 2026.09.121 - 2026-09-19

### Added
- Gear advice: healing stat priorities are now per content (Raid / Mythic+ / Delves) and cover all healing specs (Shaman, Druid, Paladin, Holy & Discipline Priest, Mistweaver, Preservation Evoker), each with its Wowhead source and a short note; Delves use the Mythic+ order (no delve-specific priority is published) — the page states this. Sims show their settings per content (Delves: short 90 s solo fight, no raid buffs).

## 2026.09.120 - 2026-09-19

### Fixed
- English wording on the Gear advice page for counters split by markup (e.g. "15 item(s) equipped").

## 2026.09.119 - 2026-09-19

### Added
- New "Gear advice" page (Simulation menu): pick a /simc export, a saved build and a content (Mythic+, Raid, Delves) and see which items you already own you should wear. Healing specs are ranked by item level then stat priority (sourced guide — SimulationCraft cannot sim healers); DPS specs get real per-item simulations. Items your character cannot wear are filtered out automatically.

## 2026.09.118 - 2026-09-19

### Fixed
- Upcoming raids: player names in the "no answer yet" line now render with their class colour instead of showing raw span markup.

## 2026.09.117 - 2026-09-19

### Changed
- Characters page: the "Available to play" section is now titled "Seen recently" (FR: « Vu dernièrement »), help text updated.

## 2026.09.116 - 2026-09-19

### Fixed
- Infinite refresh loop for officers/admins: pages tried to reveal a nav entry (#admin-link) that no longer exists and bounced to /login on any init error. The stale calls are removed and pages now redirect to /login only on a real auth error (401).

## 2026.09.115 - 2026-09-19

### Changed
- After logging in (or registering), the app now lands on the Dashboard instead of the Simulator page; opening /login while already signed in also redirects to the Dashboard. Custom ?next= destinations keep working.

## 2026.09.114 - 2026-09-19

### Fixed
- Settings: in-page hash links (e.g. /settings#identite) now open the matching section too (hashchange listener).

## 2026.09.113 - 2026-09-19

### Changed
- Settings now use a left-hand menu (Profil, Vocal, Mot de passe, then Administration for staff: Invitations, Accounts, Discord bot, Identity) instead of stacked cards and top tabs; deep links like /settings#identite open the matching section. Help unchanged.

## 2026.09.112 - 2026-09-19

### Changed
- The administration area (invitations, accounts, Discord bot, guild identity) now lives inside ⚙️ Settings for officers and admins; the separate "Administration" entry and page are gone (the /admin URL redirects to /settings). Help updated.

## 2026.09.111 - 2026-09-19

### Added
- Guild identity (white-labeling): admins can change the logo, the guild name (full + short) and the background (color and/or image) from Administration → 🎨 Identity. The logo (favicon, header, mobile drawer, PWA manifest) and the names apply everywhere, including the login page. Help updated.

## 2026.09.110 - 2026-09-19

### Changed
- Dashboard is now reached by clicking the logo (top left, and the crest in the mobile drawer) instead of a menu entry; the "🏠 Tableau de bord" entry was removed from the menu. Help updated.

## 2026.09.109 - 2026-09-19

### Added
- Wishlist: priority pieces (⭐) — mark an item as priority from the wishlist or straight from a Top Stuff result (⭐ button next to 🎯); items that come out as the best upgrade per slot in a Top Stuff sim are flagged "⭐ BIS for <character>" automatically, priority pieces sort first, and the raid calendar flags them ⭐ in the wanted-loot lines. Help updated.

## 2026.09.108 - 2026-09-19

### Added
- Calendar: each raid date lists the guild wishlist pieces that drop from its targeted bosses/raids (🎁, with who wants them, class colours). Help updated.

## 2026.09.107 - 2026-09-19

### Added
- Wishlist: personal craft check — if you have the profession, the page tells you whether you can craft the piece (declared recipe) or that you are missing the recipe; game recipes are now also matched by item name (Blizzard omits crafted-item ids for many recipes). Help updated.

## 2026.09.106 - 2026-09-19

### Added
- Wishlist: crafted items now also list who in the guild has the profession (from the characters' professions, top by skill) and who declared the recipe (or that nobody did yet); declared materials are used when the game catalogue has no recipe. Help updated.

## 2026.09.105 - 2026-09-19

### Added
- Calendar: player names now use the in-game class colours (tank/heal/dps lists, unavailability lines and the officer player panel), from the character snapshots (localized FR/EN). Help updated.

## 2026.09.104 - 2026-09-19

### Added
- Wishlist: each wanted piece now shows how to get it (raid instance + bosses, or dungeon/MM+ from the in-game journal loot tables, synced to `item_loot`) and, when it is crafted, its materials plus who in the guild can craft it (game recipes + declared crafters). Help updated.

## 2026.09.103 - 2026-09-19

### Added
- Calendar "Upcoming raids", officer editor ✏️: set each attendee's role (🛡️ tank / 💚 heal / ⚔️ dps — shown with the names and counted in the badge) and force answers (✅ force attending / ❌ force not attending / ↺ back to the in-game answer), for the players who never reply in-game. Forced members are skipped by the Discord reminder. Stored per event (same gcal_meta row).

## 2026.09.102 - 2026-09-19

### Changed
- Raid preparation: the "🎯 Objective" card is gone (title/date/raid-boss chips removed; the ♻️ Reset button moved next to the recipe tooling).
- Calendar "Upcoming raids": officers can now set, for each in-game date, the targeted raid(s), the bosses, and the starting raid 🚩 (✏️ editor per event, stored per calendar event id; visible to all members).

## 2026.09.101 - 2026-09-19

### Changed
- Calendar page: now keeps only the in-game calendar card, renamed "🗓️ Upcoming raids" (raid creation/invites happen in-game only — the in-app create/signup cards are gone). Player unavailabilities moved here: each upcoming raid flags invitees who declared an unavailability, plus a 14-day list of declared periods (⚠️ accepted but unavailable).
- Raid preparation: the 🚫 unavailability card is gone (now on the calendar page).

## 2026.09.100 - 2026-09-19

### Changed
- Menu: new "⚔️ Raid/MM+" section right after Guild, gathering raid reports, calendar, raid preparation and Mythic+ (moved out of the Guild section).

## 2026.09.099 - 2026-09-19

### Removed
- Raid preparation: the officer "⚙️ House recipes & catalogue" section is gone — the recipe search field (jump to any of the ~1000 in-game recipes, guild-filter and materials preview) covers browsing, and guild imports live in 🙋 Moi → 📖 Mes recettes. House recipes already saved keep working (resolution priority unchanged); the ⚠️ hint now points to the addon export path.

## 2026.09.098 - 2026-09-19

### Added
- Raid preparation: a "Only recipes known by guild players" checkbox next to the recipe search field, filtering the list down to recipes some guild member can craft (declared in 🙋 Moi → 📖 Mes recettes). The recipe preview and ⚠️ marker now use the effective materials (first non-empty source, same rule as the server) and list all guild crafters.

## 2026.09.097 - 2026-09-19

### Changed
- Raid preparation: recipe sync is now fully automatic (at startup, then at most once a day — was every 6 days) and the "🔄 Sync" button is gone; the in-game recipe count and last sync time stay visible ("1001 recettes du jeu · maj il y a 3 h"). The artisan export import button was removed from this page too: exports are imported in 🙋 Moi → 📖 Mes recettes.

## 2026.09.095 - 2026-09-19

### Changed
- Raid preparation: the recipe picker is now a search field — type to filter among house recipes, the in-game catalogue and crafter exports (accent-insensitive, prefix matches first, keyboard navigation, ⚠️ on recipes without known materials), instead of scrolling a long dropdown list.

## 2026.09.094 - 2026-09-19

### Fixed
- Raid preparation materials: recipes whose materials are missing from Blizzard's static API (164 in-game recipes, mostly recent Midnight content: leatherworking, engineering, inscription, alchemy…) no longer silently produce an empty materials list. When several sources describe the same item, the first non-empty one wins (house recipe > in-game catalogue > crafter exports) — previously an empty in-game entry could override a crafter export that had the materials.
- Such recipes are now flagged ⚠️ in the recipe dropdown and listed as "sans compos connues" under the plan, with a hint to add a house recipe (its materials then take precedence).

## 2026.09.093 - 2026-09-19

### Changed
- Raid preparation simplified: the three cards (to craft / materials / recipes) are now a single flow. Officers pick recipes from a dropdown (grouped: house recipes, in-game catalogue, guild crafters — with materials preview), click "＋ Add", set the quantity (also editable inline afterwards). Materials now resolve from the in-game catalogue (FR/EN) and guild imports, not only from manually registered recipes. The old recipes card is folded into a collapsed "house recipes & catalogue" section for officers.
- New guild bank stock per material (officers): enter what is already in the bank 🏦; the materials list shows needed / bank / contributions / remaining ("reste"), with the progress bar counting bank + contributions.

## 2026.09.092 - 2026-09-19

### Changed
- Raid preparation: the objective date is now set directly with a date & time picker — the "calendar raid" dropdown was redundant now that raids and bosses are picked from the journal (v091). The unavailability block still finds the matching in-game calendar event: exact timestamp first, otherwise by day (Paris).

## 2026.09.091 - 2026-09-19

### Added
- Raid preparation: the objective can now specify the raid(s) and boss(es) targeted for the evening — pickers built from the in-game journal (Blizzard API, localized, current season: 7 raids and their bosses). Stored on the plan (`prep_plan.raids` / `prep_plan.bosses`), shown under the objective for everyone, cleared by the plan reset. Handy below the objective: the unavailability block uses the same evening date.

## 2026.09.090 - 2026-09-19

### Added
- 🙋 Me → new "🚫 My time off" page (`/mesindispos`): declare unavailability periods (dates + optional note). API: `GET/POST /api/me/unavail`, `DELETE /api/me/unavail/{id}`.
- Raid preparation (`/prep`): new "🚫 Unavailability" block under the objective — lists members unavailable on that raid's date, cross-checked with their in-game calendar answers (✅ accepted / ❓ maybe / ❌ declined / ⏳ no answer); contradictions (⚠️ "unavailable but accepted the raid") are highlighted. Members are matched through their linked characters.

## 2026.09.089 - 2026-09-19

### Changed
- Characters page: the "🪪 Your characters" info card is removed — the roster card keeps a discreet status line for the inline link button; character management lives in the 🙋 Me section (`/mespersos`).

## 2026.09.088 - 2026-09-19

### Changed
- The "🙋 Me" section is split into dedicated pages (one topic per page) instead of a single hub: `/mespersos` (characters), `/mesrecettes` (recipes), `/messtats` (statistics), `/alertes` (Mythic+ alerts + notifications); `/moi` now redirects to `/mespersos`. The Crafting and Characters pages point to the matching page, and the unread-notification badge also marks the 🔔 Alerts menu entry.

## 2026.09.087 - 2026-09-19

### Added
- New "🙋 Me" menu section and page (`/moi`): everything personal in one place — your linked characters (linking, ⭐ main), your known recipes, your statistics (item level, raid attendance, declared recipes, last login per character) and your Mythic+ alerts. 🎯 My wishlist moved into this section.
- Mythic+ key alerts: pick a dungeon (or any) and a minimum level; when someone announces a matching key on the ⚔️ Mythic+ page, you get an in-app notification (page 🙋 Me, with an unread badge in the menu). Dungeon names match across FR/EN (canonical English key). Alerts API: `GET/POST /api/me/alerts`, `DELETE /api/me/alerts/{id}`; notifications: `GET /api/me/notifs`, `POST /api/me/notifs/read`; hub data: `GET /api/me/overview`.

### Changed
- The "Known recipes" editor moved from the Crafting page to the 🙋 Me page (Crafting keeps a pointer); the character-linking field moved from the Characters page to the 🙋 Me page (roster keeps its inline quick-link button).
- Help page: new "🙋 Me" section; Characters/Crafting bullets updated to the new locations.

### Removed
- Dashboard: the "Weekly reset" countdown card (little value) — the roster/report counters it contained moved to the "Guild movements" card.

## 2026.09.086 - 2026-09-19

### Added
- Crafting: players can now declare the recipes their characters know, themselves — from the Crafting page, new "My known recipes" card. Two ways: tick recipes from the in-game catalog fetched from the Blizzard API (per character and trade, with materials shown), or import their own addon export (`/lotp recettes`). Declarations feed "Who can craft what" and the raid prep materials. Members can only declare for their own linked characters (officers keep the ability to import for anyone); API: `GET/POST /api/my/recipes`, self-service addon import on `POST /api/prep/import-recipes`.

### Changed
- Characters page: the linking field now suggests guild roster names as you type (substring match — type "sala" to find Arssalag); click a suggestion to fill it.
- Help page: Crafting and Raid-prep sections document the new self-service declarations (addon import and catalog ticking); the First-steps bullet now states the chosen language applies to game data too (fetched as-is from Blizzard).

## 2026.09.085 - 2026-09-19

### Added
- Localized game data: Blizzard API data is now fetched in the account's language — "en" accounts get the English data (en_US) straight from the API, everyone else French (fr_FR). Live pages follow the account language (character sheet, equipment, roster summaries, comparator, items / Top Stuff, wishlist, professions, M+ dungeons, game recipe catalog), and the daily snapshots now store class/spec/item/slot names in FR **and** EN so the history views (evolution, gear comparison, attendance, progression, availability) are readable in both languages. Warcraft Logs data was already English (no locale on their side); user-entered content (raids, prep plan, MM+ posts, guild info) stays as typed.
- Class colors on character views now use the API class key (`class_key`), working with English and French data alike.

### Changed
- Professions and the game recipe catalog now carry both FR and EN names (`name_en`, `item_en`, `mats_en`, `tier_en`); stored FR fields are pinned to `fr_FR` explicitly.
- One-shot migration (`meta.loc_en_profs_v1` / `loc_en_recipes_v1`) forces a re-fetch of already-stored professions and recipes so English names appear without waiting for the weekly refresh.
- Prep catalog expansion chips handle both "Cuisine de Midnight" (FR) and "Midnight Cooking" (EN).

### Fixed
- Gear comparison (snapdiff) slot labels now use the English slot name when the stored snapshot has one.

## 2026.09.084 - 2026-09-19

### Added
- Help page brought up to date with everything shipped since v052: new sections « ⭐ Mains & alts », « 🔨 Artisanat », « 🧪 Préparation de raid », « ⚔️ MM+ » and « 🧩 The in-game addon » (guild calendar + recipe exports), plus new notes in the existing sections (character-sheet selector, Evolution curve / gear comparison / weak slots, « Dispo pour jouer », attendance, progression, dashboard « Prochain raid » tile, in-game calendar import + « Relancer », character alerts and weekly recap, mobile install tip).

### Changed
- i18n: 32 new EN entries (all the new help content; `MM+` now reads « Mythic+ » in English), and the dead dictionary key containing inline `<b>` tags was replaced by the real DOM fragments so the wishlist bullet now translates.

### Fixed
- Help page: « Succès fun » bullets and the wishlist note were not translated to English.

## 2026.09.083 - 2026-09-19

### Added
- New "MM+" page (guild menu): members post their Mythic+ availability over the next 2 weeks — roles (tank/heal/dps), day slots with hour ranges, and the keys they hold (character + dungeon + level, several characters allowed). The board shows who is available on each day, and a key list summarizes every announced key. The season's dungeon list is fetched automatically (Raider.IO + Blizzard journal API, FR names). API: `GET/POST /api/mplus`, table `mplus_posts`.

## 2026.09.082 - 2026-09-19

### Added
- Raid prep / recipe catalog sources: a built-in game recipe base (Blizzard Game Data API — `/data/wow/profession/*` + `/data/wow/recipe/*`) covering the last 2 expansion tiers (Midnight + Khaz Algar) of Cooking, Alchemy, Inscription, Blacksmithing, Leatherworking and Engineering (~1000 recipes, background sync at startup, weekly refresh, manual « Sync » button for officers). The prep catalog now has a source filter: « Game · last 2 expansions » (default, works without any player export — each recipe shows its materials and, when known, which guild crafters can craft it) or « Guild crafters » (imported exports only).

## 2026.09.081 - 2026-09-19

### Added
- Known recipes: expansion filter. The addon (v1.5.1, `/lotp recettes`) now walks every expansion tier of each profession (`GetChildProfessionInfos`, newest first, `SetProfessionChildSkillLineID`) and tags every recipe with its expansion and tier rank. The site stores the expansion per recipe and the prep catalog gains a filter (default: last 2 expansions, option: all) with expansion badges, plus a clearer split between the known-recipe catalog and your prep recipes.

## 2026.09.080 - 2026-09-18

### Added
- Raid prep / known recipes: crafters export their professions from the game with the LOTP addon (v1.5.0, `/lotp recettes` — reads the profession UI like the game does, writes every known recipe with its materials into SavedVariables); officers import the file on the prep page (`POST /api/prep/import-recipes`, raw JSON or SavedVariables) — the site keeps a catalog of the guild's known recipes (item, materials, who can craft it) and the recipe picker searches it and pre-fills the materials.

## 2026.09.079 - 2026-09-18

### Added
- New "Raid prep" page (`/prep`, guild menu): officers pick what the guild crafts for the upcoming raid (items chosen from the recipes catalog), the page aggregates the required materials (items x quantities x recipes), and every member can claim how much of each material they bring (progress per material, list of contributors). Officers manage the recipes (crafted item -> materials) and can reset the plan. API: `GET /api/prep`, `POST /api/prep/plan|recipes|claim|reset`, `DELETE /api/prep/recipes/{id}`.

## 2026.09.078 - 2026-09-18

### Added
- Dashboard: new "Next raid" tile — shows the next upcoming guild raid from the in-game calendar import (date, attendance counters, import freshness); `/api/dashboard` now returns a `next_raid` object (raid-type events preferred over world events).

## 2026.09.077 - 2026-09-18

### Fixed
- Calendar import: the site now accepts the WoW client's hexadecimal event ids (64-bit world events, e.g. `0x1F45...`) — they are quoted before JSON parsing and converted to integers on import; the import is also tolerant of an export string pasted together with other text, error messages now show what was received, and the Discord reminder endpoint handles all id forms.
- Addon 1.4.6: exports always emit valid JSON (non-decimal ids are emitted as strings).

## 2026.09.076 - 2026-09-18

### Fixed
- Addon 1.4.5: root cause found — the WoW client's Lua sandbox has no `os` library; the addon used `os.date`/`os.time`, so every slash command and collection crashed at the first date call (invisible: BugGrabber was swallowing the errors). All date handling now uses the client's native `date()` plus a pure-Lua civil-to-epoch conversion. The offline test harness now removes `os` to simulate the sandbox, so this class of bug can no longer slip through.

## 2026.09.075 - 2026-09-18

### Fixed
- Addon 1.4.4: bisect fix — the only code executed during loading that was new versus the working 1.4.0 (a backup timer and per-frame counters) has been removed; nothing runs at load anymore beyond what 1.4.0 already did (lazy panel, click-driven steps and version display are all kept, none executed at load); a startup handshake message now proves whether the file executes to its end on the client.

## 2026.09.074 - 2026-09-18

### Fixed
- Addon 1.4.3: the panel is now built on demand (never during loading) — a failing UI can no longer break the addon: `/lotp collect`, `export`, `diag` and `reset` keep working without a window, a frame-name collision (leftover duplicate) automatically falls back to an unnamed frame instead of crashing the load, the load line and `/lotp` show the addon version + folder + TOC version, and any panel error is saved in the report.

## 2026.09.073 - 2026-09-18

### Fixed
- Addon 1.4.2: the version is now visible everywhere (window title, status line, `/lotp` message) and each load records `loaded_ver` / `loaded_at` / `loaded_dossier` into SavedVariables — so a simple reload + file check proves exactly which version the game actually executes; every click of "Collecter" now advances the collection one step synchronously (works even if the game blocks timers and frame updates), and `/reload` after any collection leaves a report with heartbeat counters.

## 2026.09.072 - 2026-09-18

### Fixed
- Addon 1.4.1: every click and slash command is now failure-proof and gives visible feedback (errors shown in chat, click logged in the report); second collection driver as a fallback (frame update + backup timer, whichever runs drives the collection); clicking "Collecter" during a collection advances it manually one step; heartbeat counters saved in the report (proof of which mechanisms actually run on the client).

## 2026.09.071 - 2026-09-18

### Fixed
- Addon 1.4.0: collection engine rebuilt to run frame-by-frame from OnUpdate instead of timer chains (a lost timer froze the collection forever, leaving "collecte déjà en cours…" even after a reload). Hard 90-second cap, per-step chat progress ("mois +0 : N événement(s)", "ouverture 1/2…"), live status line in the panel, new « Réinitialiser » button and `/lotp reset` command; duplicate LOTP addon folders are detected and reported (chat warning + report); the startup line now shows the folder the addon was loaded from.

## 2026.09.070 - 2026-09-18

### Fixed
- Addon 1.3.2: hardened calendar collection — every step is error-guarded (errors are surfaced in chat, saved to the report, and end the collection cleanly instead of leaving it stuck), a 75-second watchdog aborts a stuck collection, a blocked collection older than 60 seconds can be relaunched, and a progressive trace is saved (`LOTP_DB.trace`) showing exactly which step stopped. Fixes the permanent "collecte déjà en cours…" lock.

## 2026.09.069 - 2026-09-18

### Fixed
- Addon 1.3.1: `/lotp` always starts a fresh collection (it previously skipped when the last export was under an hour old — that is why no new collection ran after the update); the diagnostic report is now written automatically into SavedVariables on every collection (start marker + full report + result), so any LOTP.lua file sent back contains the debug info.

## 2026.09.068 - 2026-09-18

### Fixed
- Addon 1.3.0: calendar collection now uses Blizzard's own UI method — set the displayed month (`SetAbsMonth`/`SetMonth`), then read day events (`GetNumDayEvents`/`GetDayEvent`) and open each event with the correct `OpenEvent(0, day, index)`; the addon opens the calendar frame while collecting and restores the previous view afterwards.
- Addon 1.3.0: `/lotp diag` now writes the report into SavedVariables (send the LOTP.lua file instead of screenshots).

## 2026.09.067 - 2026-09-18

### Fixed
- Addon 1.2.0: guild calendar reading reworked — primary source is now `C_Calendar.GetClubCalendarEvents` (guild club events over the next 21 days), with the guild invitation list as fallback; events are now opened with the correct `OpenEvent(offsetMonths, monthDay, index)` signature (the previous call passed the event ID, which fails silently, so answers could never be read); richer `/lotp diag` (guild club id, both sources, displayed month).

## 2026.09.066 - 2026-09-18

### Fixed
- Addon 1.1.0: the calendar collection no longer gives up after 1.5 s when the server's answer is slow — it retries for up to ~30 s (this caused "0 raids" reports). Events are kept even when the date can't be parsed, invites get a retry, and a new `/lotp diag` command dumps what the client actually sees (raw fields) for troubleshooting.

## 2026.09.065 - 2026-09-18

### Fixed
- Addon: TOC interface corrected from `120105` (PTR) to `120100` (live 12.1.0) — the addon appeared as "incompatible" in the in-game addon list and `/lotp` was unavailable.
- Addon: prints the client's interface version in the login message (debug aid); safer `GetBuildInfo` handling.

## 2026.09.064 - 2026-09-18

### Added
- In-game calendar bridge: World of Warcraft addon "LOTP" (download from the Calendar page) that collects guild calendar events (raids, invites, answers) in game and exports them as a string or SavedVariables file.
- Calendar page: "In-game calendar" card — import from officers (file or pasted string), per-event answer summary (available / tentative / declined / **no answer yet**), and a "Remind" button posting the non-responders to Discord.
- APIs: `GET /api/addon` (zip), `GET /api/gcal`, `POST /api/gcal/import` (officers), `POST /api/gcal/relance/{event_id}` (officers).

## 2026.09.063 - 2026-09-18

### Added
- Character page: interactive iLvl curve — hovering a point on the Évolution chart shows a tooltip (date, item level, change vs previous snapshot) and highlights the point.
- Character page: weak gear slots — slots at least 5 iLvl below the character's average are highlighted (amber marker + "upgrade first" line under the equipment).

## 2026.09.062 - 2026-09-18

### Added
- Discord bot: milestone alerts for linked characters (iLvl milestone every 5 levels, new mounts / pets) — toggle "Paliers des personnages liés" in the admin bot card.
- Discord bot: weekly recap (Mondays from 9:00, Paris time) — top iLvl progressions of the week, raid activity, guild movements; admin button "Tester le récap" (POST /api/admin/bot/recap).
- Admin bot config: `notify_chars` / `notify_weekly` toggles (columns migrated automatically).

## 2026.09.061 - 2026-09-18

### Added
- Characters page: "Dispo pour jouer" card — max-level members seen within 24 h / 48 h / 7 days, grouped by role (tanks / healers / DPS) with spec, item level and last seen (links to profiles). API: `GET /api/avail`.
- Snapshots now record `last_login` (used by the finder); past rows are enriched on next capture.

## 2026.09.060 - 2026-09-18

### Added
- "Artisanat" page (/craft): who can craft what in the guild — professions of every roster character grouped by trade (skill of the latest tier), search by profession or character, links to profiles. Professions are refreshed weekly alongside the daily snapshots (table `char_professions`). API: `GET /api/craft`.
- Nav: new "🔨 Artisanat" entry in the Guilde menu.

## 2026.09.059 - 2026-09-18

### Added
- Raids page: "Assiduité" card — real attendance per player over the last 30 days from Warcraft Logs (nights attended, percentage, last raid), plus evening chips that open the matching report. API: `GET /api/attendance`.

## 2026.09.058 - 2026-09-18

### Added
- Classements page: new "Progression" card — gains over 7 / 30 days per character (iLvl, achievements, mounts, pets) from the daily snapshots, plus a guild average-iLvl curve (constant population). API: `GET /api/progression?days=7|30`.

## 2026.09.057 - 2026-09-18

### Changed
- Daily snapshots and the Warcraft Logs backfill now cover the entire guild roster (all members), not only linked characters. Linked characters are refreshed more often (6 h vs 20 h); roster sweep is capped per pass.
- Roster page: every character row now links to its profile page (/char).

### Added
- `GET /api/char/{realm}/{name}/history|snapdiff` now serves any guild member.

## 2026.09.056 - 2026-09-18

### Added
- Character snapshots backfilled from Warcraft Logs raid nights (last 30 days): gear per combat comes from CombatantInfo events; only days without a Blizzard snapshot are filled. Runs once automatically at startup; re-run via `POST /api/admin/snap-backfill`. Backfilled days are marked with ° in the Evolution table.

## 2026.09.055 - 2026-09-18

### Changed
- Character page "Evolution": the date comparison now shows the full gear side by side (slot, item at date A, item at date B, iLvl delta), with changed rows highlighted — replaces the changed-items-only list.

## 2026.09.054 - 2026-09-18

### Added
- Daily character snapshots (linked characters): gear, level, ilvl, achievements, mounts/pets and M+ rating.
- Character page: "📈 Evolution" section — day-by-day table with deltas, iLvl sparkline and any-to-any date comparison including gear changes. History kept for a rolling 30 days (Blizzard API ToU 30-day retention rule).

### Changed
- Restored the original dark texture background (bg-texture.png); the stone-wall wallpaper is no longer used.

## 2026.09.053 - 2026-09-18

### Added
- Character profile page (`/char/<realm>/<name>`): full sheet (level, ilvl, achievements, mounts/pets, M+ rating, equipped gear) with a switcher to jump between the linked characters of the same account.
- "Mains & alts": clicking an account frame opens its main's profile; clicking a character chip opens that character's profile.

### Changed
- i18n dictionary extended (new strings; "Niveau max (90) uniquement" translated).

## 2026.09.052 - 2026-09-17
- New page "⭐ Mains & alts" (Guild menu): all linked characters grouped per account under their main, with search.

## 2026.09.051 - 2026-09-17
- Cache: static assets (js/css) now served with Cache-Control no-cache + versioned URLs in pages (?v=) so phones/PWA always pick up new versions; service worker cache bumped.

## 2026.09.050 - 2026-09-17
- Mobile drawer polish: brighter section labels, darker scrim, safe-area bottom padding.

## 2026.09.049 - 2026-09-17
- Mobile menu redesign: hamburger button opens a full side drawer (all sections visible, grouped, app-like) instead of the horizontally scrolling pill row; desktop unchanged.

## 2026.09.048 - 2026-09-17
- Mobile: right-edge fade on the scrollable nav (scroll affordance).

## 2026.09.047 - 2026-09-17
- Mobile polish: more compact nav pills, full-width form controls in cards, consistent list action alignment.

## 2026.09.046 - 2026-09-17
- Mobile: global responsive layer (nav as one scrollable row, full-width form fields, scrollable compact tables, touch-friendly buttons, darker background veil, no fixed backgrounds on iOS).
- PWA hygiene: service worker cache bumped + background asset pre-cached, viewport-fit=cover on all pages.

## 2026.09.045 - 2026-09-17
- Invitations: a used code is now deleted (no more "Used" rows cluttering the list); consumed codes are purged.

## 2026.09.044 - 2026-09-17
- Theme: new WoW-style background (dark fortress stone, AI-generated locally) applied to logged-in pages only; login/register stay plain.

## 2026.09.043 - 2026-09-17
- Music player: play now verifies the track actually starts (deleted file => clear message instead of silent nothing).
- Music player: status badge shows Paused / Stopped states (feedback on pause/stop clicks).

## 2026.09.042 - 2026-09-17
- Music: deleting a track now also removes the local mp3 file (no leftover on disk).

## 2026.09.041 - 2026-09-17
- Music: the Stop button no longer doubles as "end of track" for the auto-advance engine (SinusBot freezes the position on stop, which made the v037 engine restart the track - especially with loop mode on).
- Music: watcher diagnostics - /api/music/watch (ticks, last event, last error) + log line on each detected track end.

## 2026.09.040 - 2026-09-17
- Sim engine: safety net for SimC segfaults - items known to crash the engine (Réceptacle rituel de l'Entortillâme, id 270162) are auto-stripped and the sim is retried with a visible warning.
- Clearer failure hint when the engine segfaults.

## 2026.09.039 - 2026-09-17
- Dashboard: TeamSpeak users grouped by channel (one line per channel, live).

## 2026.09.038 - 2026-09-17
- Nav: Music page moved into the Voice menu (voice panel + Music).
- Dashboard: live TeamSpeak user count (music bot excluded).

## 2026.09.037 - 2026-09-17
- Music: shuffle mode (random playback of the library) and loop mode (repeat the current track) - toggles on the Music page.
- Auto-chaining engine runs app-side (background watcher) while modes are on.

## 2026.09.036 - 2026-09-17
- Music: rename the bot and change its channel from the page (officers/admins) - the bot restarts itself (~10 s).
- Bot renamed "DJ Fosse Septique" and moved to "La taverne".

## [2026.09.035] — 2026-09-17

### Added

- 🎵 **Music bot**: SinusBot (Docker) connected to the guild TeamSpeak server
  (client « 🎵 Musique LOTP », server password — no TS restart needed).
- **Music page** (`/music`, officers & admins only): player controls
  (play / pause / stop / volume), music library, add-by-URL and mp3 upload
  (files stored in `data/music/`, served to the bot over the internal network).
  Backend proxies the SinusBot HTTP API server-side.

## [2026.09.034] — 2026-09-17

### Added

- Voice auto-join: opening the 🎧 voice panel now connects to the guild server
  automatically (nickname pre-filled + the client's join button clicked by the
  injected helper script — `#autojoin=1` fragment set by the shell only).
  No more manual « Enter voice space » click; the browser may still ask for
  microphone permission the first time.

## [2026.09.033] — 2026-09-17

### Fixed

- Voice access now self-repairs old/host-only session cookies: the gate sends
  visitors to `GET /api/voice/handoff`, which re-issues the session cookie with
  the site's parent cookie domain, and forwards straight to the client — no login detour
  (and no stray landing on the simulator page).
- Service worker: static assets are now network-first (cache only as offline
  fallback) and the cache name is versioned, so updated JS/CSS can no longer be
  served stale after a deploy.
- HTML pages are served with `Cache-Control: no-cache, must-revalidate`.
- Login page accepts relative `?next=` targets (same-origin paths only).

### Fixed (post-deploy follow-up)

- Login page script repaired: a stray escape sequence from the v033 edit had
  broken the inline script, leaving the « Se connecter » button inert.

## [2026.09.032] — 2026-09-17

### Added

- 🎧 **Persistent voice panel**: the Voice page is now a shell — the TeamSpeak
  web client sits in a left panel while the app keeps working on the right, and
  navigating never reloads the voice client. The 🎧 « Vocal » menu item
  toggles the panel; a floating 🎧 button reopens it when hidden.
- Settings: « 🎧 Vocal (TeamSpeak) » — voice nickname, defaulting to the
  linked main character's name. The app pre-fills it in the web client
  (`#nickname=` fragment + a small injected script → `webspeak:nickname`).

### Removed

- Voice popup / « open in a window » buttons (superseded by the panel).

## [2026.09.031] — 2026-09-17

### Changed

- Voice access is now **members-only**: the voice subdomain sits behind the app
  login (anonymous visitors are redirected to the sign-in page). The web
  client is proxied by the app itself (HTTP + WebSocket) after session
  validation — no more open access.
- Session cookie is shared on the site's parent domain (production) so the voice
  subdomain is covered by the same login.

### Added

- 🎧 Voice page: « 📦 Portable client (Windows) » download — a preconfigured
  TeamSpeak client (address + password + bookmark, one-click launcher) served
  to signed-in members only (`GET /api/voice/client`).

### Fixed

- Voice proxy now forwards the original `Host`/`Origin` headers to WebSpeak
  (its `/api/join-ticket` same-origin check compares both — 403 ORIGIN_REJECTED
  otherwise), and the app container reaches the host gateway via
  `host.docker.internal` (`extra_hosts: host-gateway` + ufw rule for the
  Docker subnet, since 127.0.0.1 is not the host inside the container).

## [2026.09.030] — 2026-09-17

### Added

- « 🎧 Vocal » page: embedded TeamSpeak web client (WebSpeak) with a
  persistent-window button (and a 🎧 quick button in the menu) so voice keeps
  running while you browse the app.

### Changed

- World-of-Warcraft-flavoured theme applied site-wide via `/static/theme.css`:
  self-hosted Cinzel font for headings/brand, textured dark background with
  subtle gold accents, gold card underlines and table headers.

## [2026.09.029] — 2026-09-17

### Added

- PWA: web app manifest + icons (192/512/apple-touch), service worker
  (static asset cache, offline fallback page), installable from mobile
  browsers; theme colour set on every page.

### Fixed

- i18n engine: never rewrite a text node with an identical value (an
  identity dictionary entry plus surrounding whitespace could loop the
  MutationObserver endlessly and freeze the page in English). Removed
  12 no-op dictionary entries.

## [2026.09.028] — 2026-09-17

### Added

- Fun achievements page (« 🎉 Succès fun », Guild menu): graveyard (most
  deaths), bloodiest fight, first-blood leader, n°1 cause of death,
  untouchables (0 deaths), mad scientists (most sims), raid pillars
  (attendance), collectors (mounts) and golden hearts (shared profiles).
  New API `GET /api/fun` (30-min cache) + WCL deaths table integration.

## [2026.09.027] — 2026-09-17

### Added

- Wishlist (« 🎯 Ma wishlist », in the Simulation menu): save items you are
  after (from Top Stuff 🎯 or by pasting a Wowhead ref), see which of your
  linked characters already own them, and sim the DPS gain of the whole
  wishlist against a saved /simc profile. New API: GET/POST /api/wishlist,
  DELETE /api/wishlist/{item_id}.

## [2026.09.026] — 2026-09-17

### Added

- Guild page: new « Web client » card (editable link + note) pointing to the
  self-hosted WebSpeak instance that runs TeamSpeak voice
  directly in the browser.

### Added (infrastructure)

- WebSpeak (browser TeamSpeak client + gateway) deployed as a self-hosted
  Docker service (host network, firewall-restricted; Apache reverse proxy +
  Let's Encrypt, WebSocket upgrade enabled). Target locked to the guild
  TeamSpeak server (127.0.0.1:9987).

## [2026.09.025] — 2026-09-17

### Added

- Rankings page (`/rankings`, inside the Guild menu): best recent parses
  (role filter: all / DPS / tanks / healers), top-3 by boss and current Mythic+
  key ratings of linked mains. New API `GET /api/leaderboard` (30-min cache).

### Changed

- Sticky header + menu (`#topwrap`) that stays visible while scrolling; every
  page now uses the same column width (1060 px) so the menu no longer shifts
  between pages.

## [2026.09.024] — 2026-09-17

### Changed

- Structured navigation: every page now shares one rendered menu (`nav.js`)
  with direct links (Dashboard, Help) and drop-down sub-menus (Simulation →
  Simulator / Top Stuff / Comparison; Guild → Characters / Reports / Calendar /
  Info & links). Active section and page are highlighted; the admin link is
  rendered too, so the menu stays in sync everywhere.

## [2026.09.023] — 2026-09-17

### Added

- Enriched character profile: the roster « Détails » panel now shows mounts and
  pets collected and the current Mythic+ rating (Battle.net profile API,
  30-minute cache), alongside the existing equipment list.
- New API: `GET /api/char/{realm}/{name}/extras`.

## [2026.09.022] — 2026-09-17

### Added

- Guild page (`/guild`): presentation, Discord invite link and TeamSpeak
  details (address, password, copy buttons, `ts3server://` one-click join).
  Editable in place by administrators.
- Help page (`/help`): full documentation of every app feature with a table
  of contents (bilingual FR/EN).
- New API: `GET/POST /api/guild/info` (write: administrator only).

## [2026.09.021] — 2026-09-17

### Added

- Group simulation (simulator page, new card): pick saved `/simc` profiles and
  the app combines them into **one multi-actor SimulationCraft run** (raid buffs
  included) and ranks every character by DPS with class colors. Duplicate
  characters are skipped with a warning; results link to the full report.
- New API: `POST /api/group/sim` (launches a `group` kind sim).

## [2026.09.020] — 2026-09-17

### Added

- Raid calendar (`/calendar` tab): officers create raids (date/time, duration,
  note); every member answers **Présent / Peut-être / Absent** in one click and
  sees who answered what. Past raids are listed below.
- Discord bot (when active): new raids are announced automatically and a reminder
  is posted one hour before the start (with signup counts).
- New API: `GET/POST /api/raids`, `DELETE /api/raids/{id}`,
  `POST /api/raids/{id}/signup`.

## [2026.09.019] — 2026-09-17

### Added

- Guild dashboard (`/dashboard`, first tab): weekly reset countdown (EU —
  Wednesday 05:00 Paris time), latest raid summary from Warcraft Logs
  (kills/pulls + top parses with class colors) and recent guild roster moves
  (joins/leaves) plus roster size.
- Roster moves are now tracked continuously (new `guild_events` table, fed by
  the background tick) whether or not the Discord bot is enabled.
- New API: `GET /api/dashboard`.

## [2026.09.018] — 2026-09-17

### Fixed

- **One main per account, enforced**: a partial unique index now makes multiple
  mains impossible (legacy duplicates are auto-normalised at startup, keeping the
  most recent). The first character linked to an account automatically becomes its
  main. The character chip action is now clearly labelled « ⭐ définir main » /
  « ⭐ set main » (it used to read « ⭐ main », which looked like a status).

### Changed

- The language switcher was removed from page headers: the language is now
  chosen in exactly two places — **before signing in** (login and sign-up pages)
  or **in the account settings**.

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
  be sent automatically by e-mail (from a no-reply address, styled
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
