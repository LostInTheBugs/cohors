#!/usr/bin/env python3
"""Update the pinned SimulationCraft image tag to the latest dated build.

Usage:
    python3 tools/bump-simc.py            # apply changes
    python3 tools/bump-simc.py --dry-run  # preview only

Requires: Python 3.11+, internet access to Docker Hub.
"""

import argparse
import json
import re
import sys
import urllib.request

# --- constants ----------------------------------------------------------

DOCKERHUB_URL = (
    "https://hub.docker.com/v2/repositories/"
    "simulationcraftorg/simc/tags?page_size=50&ordering=last_updated"
)

# Regex that matches the tag format: YYYY-MM-DD-sha
TAG_RE = re.compile(r"^\d+-\d{4}-\d{2}-\d{2}-[0-9a-f]+$")

FILES = [
    "app/main.py",
    "worker/simrun.py",
    "docker-compose.yml",
    "deploy/docker-compose.yml",
]

# --- helpers -------------------------------------------------------------

def fetch_latest_tag() -> str:
    r"""Return the latest tag matching ^\d+-\d{4}-\d{2}-\d{2}-[0-9a-f]+$."""
    req = urllib.request.Request(DOCKERHUB_URL)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    for tag in data.get("results", []):
        name = tag.get("name", "")
        if TAG_RE.match(name):
            return name

    print("No dated tag found on Docker Hub", file=sys.stderr)
    sys.exit(1)


def replace_tag(old: str, new: str) -> None:
    for path in FILES:
        full = f"tools/{path}" if not path.startswith("/") else path
        with open(full, "r") as f:
            content = f.read()
        content = content.replace(old, new)
        with open(full, "w") as f:
            f.write(content)


# --- main ----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump the pinned SimulationCraft image tag"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change"
    )
    args = parser.parse_args()

    latest = fetch_latest_tag()

    # Find the current pinned tag (first match in any file)
    old = None
    for path in FILES:
        with open(path, "r") as f:
            for line in f:
                m = re.search(r"simulationcraftorg/simc:(\S+)", line)
                if m:
                    old = m.group(1)
                    break
        if old:
            break

    if old is None:
        print("Could not find current SIMC_IMAGE tag", file=sys.stderr)
        sys.exit(1)

    if old == latest:
        print("already up to date")
        return

    if args.dry_run:
        print(f"would replace {old} → {latest}")
        return

    replace_tag(old, latest)
    print(f"{old} → {latest}")


if __name__ == "__main__":
    main()
