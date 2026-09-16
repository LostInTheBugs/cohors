# WoW Companion (working title)

Self-hosted companion for World of Warcraft: a personal simulation engine based on the
official SimulationCraft Docker image, Warcraft Logs and Battle.net API integrations,
and a guild-friendly web app with invite-only accounts and shared simulation reports.

> Early development. Not affiliated with Blizzard Entertainment, Inc. Game data is
> provided by Blizzard Entertainment and Warcraft Logs (see attribution requirements).

## Status

- [x] Simulation engine wrapper (`worker/simrun.py`) — runs a SimulationCraft sim via the
      official `simulationcraftorg/simc` Docker image, extracts DPS, produces HTML + JSON reports.
- [ ] Web app (accounts, invites, sim queue, shared reports)
- [ ] Warcraft Logs / Battle.net integrations

## Requirements

- Docker (for the SimulationCraft engine image)
- Python 3.11+

## Quick start

```bash
# Simulate a profile shipped inside the image (works out of the box after a docker pull)
python3 worker/simrun.py --container-profile profiles/MID2/MID2_Mage_Arcane.simc --iterations 500

# Simulate your own in-game `/simc` export (or any .simc profile file)
python3 worker/simrun.py --profile ./my-export.simc --iterations 10000 --outdir ./out
```

The command prints a JSON result (DPS, error, report paths) and writes `report.html`
plus `report.json` into the output directory.

## License

MIT — see [LICENSE](LICENSE).
