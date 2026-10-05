#!/usr/bin/env python3
"""Update the pinned SimulationCraft image tag to the latest dated build.

Usage:
    python3 tools/bump_simc.py            # apply changes
    python3 tools/bump_simc.py --dry-run  # preview only

Requires: Python 3.11+, internet access to Docker Hub.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

# --- path handling --------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent

FILES = [
    "app/main.py",
    "worker/simrun.py",
    "docker-compose.yml",
    "deploy/docker-compose.yml",
]

# --- helpers -------------------------------------------------------------

DOCKERHUB_URL = (
    "https://hub.docker.com/v2/repositories/"
    "simulationcraftorg/simc/tags?page_size=50&ordering=last_updated"
)

TAG_RE = re.compile(r"^\d+-\d{4}-\d{2}-\d{2}-[0-9a-f]+$")
SIMC_TAG_RE = re.compile(r"simulationcraftorg/simc:([A-Za-z0-9._-]+)")


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


def _extract_tag(path: Path) -> str | None:
    """Return the first SIMC_IMAGE tag found in *path*, or None."""
    text = path.read_text(encoding="utf-8")
    m = SIMC_TAG_RE.search(text)
    return m.group(1) if m else None


def _check_consistency() -> tuple[str, list[str]]:
    """Check that all FILES contain the same tag.

    Returns (common_tag, list_of_files_that_differ).
    Raises SystemExit(1) with diagnostics if tags differ.
    """
    tags: dict[str, list[str]] = {}
    for rel in FILES:
        fp = ROOT / rel
        tag = _extract_tag(fp)
        if tag is None:
            print(f"no SIMC_IMAGE found in {rel}", file=sys.stderr)
            sys.exit(1)
        tags.setdefault(tag, []).append(rel)

    if len(tags) != 1:
        parts = []
        for tag, files in tags.items():
            parts.append(f"  {tag!r} → {', '.join(files)}")
        print("SIMC_IMAGE tags differ across files:", file=sys.stderr)
        print("\n".join(parts), file=sys.stderr)
        sys.exit(1)

    return next(iter(tags)), []


def _replace_in_file(rel: str, old: str, new: str) -> None:
    fp = ROOT / rel
    text = fp.read_text(encoding="utf-8")
    text = text.replace(old, new)
    fp.write_text(text, encoding="utf-8")


def _apply(old: str, new: str) -> None:
    for rel in FILES:
        _replace_in_file(rel, old, new)


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
    old, _ = _check_consistency()

    if old == latest:
        print("already up to date")
        return

    if args.dry_run:
        print(f"{old} → {latest}")
        return

    _apply(old, latest)
    print(f"{old} → {latest}")


if __name__ == "__main__":
    main()
