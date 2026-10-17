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

The pages hold most of their JavaScript inline. The Content-Security-Policy allows each page's
embedded `<script>` block by its hash (recomputed at startup, nothing to update by hand), but it
**refuses inline event handlers**: never write `onclick="…"` (or any `on…=` attribute), in the HTML
or in HTML built from JavaScript — use `addEventListener` / a delegated listener instead
(`tests/test_api.py` checks this). Two checks cover the scripts themselves (both run in CI):

```bash
python3 tools/check-inline-js.py                 # syntax (node --check)
npm install --no-save eslint@9 globals@15        # once
node tools/lint-js.mjs                           # undeclared variables, const/let used before definition
```

The suite covers the API (`tests/test_api.py`: FastAPI TestClient on a throwaway SQLite
database, including role guards), the stdlib-only security helpers (`app/security.py`: password
hashing, SimulationCraft profile guard), the simulation worker, and repository consistency
(VERSION/CHANGELOG/i18n/add-on).
CI runs it on every push and pull request.

## Dependencies

- `requirements.txt` lists the direct dependencies with version ranges; `requirements.lock`
  pins every package (direct and transitive) to an exact version with hashes. The app image
  and CI install **only** the lock, with `pip install --require-hashes`, so a rebuild gets
  exactly what was tested.
- After changing `requirements.txt`, or to pick up updates (security fixes included),
  regenerate the lock and run the tests:

  ```bash
  pip install uv
  uv pip compile requirements.txt --generate-hashes --universal --python-version 3.12 -o requirements.lock
  python -m pytest -q
  ```

  `tests/test_repo.py` fails if the lock is missing a direct dependency, pins it outside its
  range, or has an entry without hashes.
- GitHub Actions are pinned to a full commit SHA, with the release tag as a comment
  (`uses: actions/checkout@<sha> # v5.1.0`). Dependabot (`.github/dependabot.yml`) opens a
  weekly PR to bump them; never replace a SHA with a tag (a test checks this).

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
- Run `python -m pytest -q` and `node tools/lint-js.mjs` before submitting.
- Before a release, run `python3 tools/bump_simc.py` to follow game patches
  and update the pinned SimulationCraft engine tag.
- Note behavior changes in `CHANGELOG.md` (an "Unreleased" section is fine).
- Screenshots help a lot for UI changes.

## Security

Please report anything sensitive privately (open a minimal issue asking for a contact, or
reach the maintainer on GitHub) rather than filing a public issue with exploit details.
Reminder for self-hosters: the worker (not the app) mounts the Docker socket to launch SimulationCraft
containers — see the security note in the README.
