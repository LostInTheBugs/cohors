# Changelog

All notable changes to this project are documented in this file.

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
  `Domain=.gensbien.fr` and forwards straight to the client — no login detour
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

- Voice access is now **members-only**: ts.gensbien.fr sits behind the app
  login (anonymous visitors are redirected to the sign-in page). The web
  client is proxied by the app itself (HTTP + WebSocket) after session
  validation — no more open access.
- Session cookie is shared on `.gensbien.fr` (production) so the voice
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
  self-hosted WebSpeak instance (ts.gensbien.fr) that runs TeamSpeak voice
  directly in the browser.

### Added (infrastructure)

- WebSpeak (browser TeamSpeak client + gateway) deployed on papouille5
  (Docker, host network, ufw-restricted; Apache reverse proxy + Let's Encrypt
  on ts.gensbien.fr, WebSocket upgrade enabled). Target locked to the guild
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
