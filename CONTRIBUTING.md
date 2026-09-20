# Contributing to Cohors

Thanks for taking a look! Issues and pull requests are welcome. This is a small,
self-hosted project maintained on a best-effort basis.

## Development setup

```bash
git clone https://github.com/LostInTheBugs/cohors.git && cd cohors
cp .env.example .env          # set DATA_DIR (absolute path) and ADMIN_EMAIL/ADMIN_PASSWORD
docker compose up -d --build  # app on http://127.0.0.1:8030
```

For quick UI work, the static pages can also be served straight from `app/static/`
(with `python3 -m http.server`) — API calls will fail, but layout/CSS changes are testable.

## Tests

```bash
pip install pytest
python -m pytest -q
```

The suite covers the stdlib-only security helpers (`app/security.py`: password hashing,
SimulationCraft profile guard) and repository consistency (VERSION/CHANGELOG/i18n/add-on).
CI runs it on every push and pull request.

## Repository conventions

- **Everything on GitHub is in English** (README, CHANGELOG, commit messages, issues, PRs) —
  even though the app UI is French-first.
- **No server names, IPs, personal domains or access accounts** anywhere in the code,
  comments or docs — configuration goes through `.env` / the admin UI.
- **The UI is bilingual FR/EN.** Translations live in `app/static/i18n.js`:
  - `DICT` maps French → English for static text (keep the JSON strictly valid — a trailing
    comma breaks tooling even though JS tolerates it);
  - `RULES` handles dynamic strings (dates, counts…).
  Add both FR and EN for any new user-visible string.
- **Versioning** is CalVer `YEAR.MONTH.BUILD` (`2026.09.141` = September 2026, build 141),
  stored in `VERSION`, with an entry in `CHANGELOG.md` for every release. Release tags use
  the same number, corrections add `-cN` (e.g. `2026.09.141-c1`).
- Bump `?v=` cache-busters in the HTML pages + the service-worker cache name when changing
  `app/static/` assets.

## Pull requests

- Keep each PR focused; describe the user-visible effect.
- Run `python -m pytest -q` before submitting.
- Note behavior changes in `CHANGELOG.md` (an "Unreleased" section is fine).
- Screenshots help a lot for UI changes.

## Security

Please report anything sensitive privately (open a minimal issue asking for a contact, or
reach the maintainer on GitHub) rather than filing a public issue with exploit details.
Reminder for self-hosters: the app mounts the Docker socket to launch SimulationCraft
containers — see the security note in the README.
