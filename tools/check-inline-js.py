#!/usr/bin/env python3
"""Syntax-check every inline <script> block of the pages (and the external *.js), via `node --check`.

Run from anywhere with `python3 tools/check-inline-js.py` — needs `node` on PATH.
Kept in the repository because the pages hold most of their JavaScript inline; this
catches a broken edit before the browser does.
"""
import pathlib
import re
import subprocess
import sys
import tempfile

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
PAT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def main() -> int:
    fail = count = 0
    for p in sorted(STATIC.glob("*.html")):
        t = p.read_text(encoding="utf-8")
        for i, m in enumerate(PAT.finditer(t)):
            body = m.group(1)
            if len(body.strip()) < 5:
                continue
            count += 1
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(body)
                tmp = f.name
            r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
            if r.returncode != 0:
                fail += 1
                print(f"!! {p.name} block #{i}: {r.stderr.splitlines()[:6]}")
    for js in sorted(STATIC.glob("*.js")):
        r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        if r.returncode != 0:
            fail += 1
            print(f"!! {js.name}: {r.stderr[:200]}")
    print(f"{count} inline blocks checked, {fail} broken")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
