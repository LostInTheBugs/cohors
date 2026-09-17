#!/usr/bin/env python3
"""simrun — minimal wrapper around the official SimulationCraft Docker image.

Runs a SimulationCraft simulation inside the official `simulationcraftorg/simc`
image (nightly builds, updated daily by the SimulationCraft team) and returns
the parsed result (DPS, error, report paths).

Two input modes:
  * --profile FILE           host file mounted read-only as /sim/input.simc
                             (a `/simc` addon export or any .simc profile file)
  * --container-profile PATH path of a profile shipped INSIDE the image, e.g.
                             profiles/MID2/MID2_Mage_Arcane.simc

Outputs: report.html and report.json are written into --outdir (default: a
fresh temp dir) and reported in the JSON printed on stdout.

Examples:
  python3 simrun.py --container-profile profiles/MID2/MID2_Mage_Arcane.simc \
      --iterations 500 --outdir /tmp/sim1
  python3 simrun.py --profile ~/export.simc --iterations 10000

Environment:
  SIMC_IMAGE   override the image tag (default: simulationcraftorg/simc:latest)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGE = os.environ.get("SIMC_IMAGE", "simulationcraftorg/simc:latest")
DPS_RE = re.compile(r"DPS=([0-9.]+)\s+DPS-Error=([0-9.]+)/([0-9.]+)%")
SF_WEIGHTS_RE = re.compile(r"Weights\s*:\s*(.+)")
SF_ITEM_RE = re.compile(r"(\w+)=([0-9.]+)\(([0-9.]+)\)")


def parse_scale_factors(log: str, json_path: Path | None = None) -> list[dict] | None:
    """Extract scale factors (stat weights) from a simc run — JSON first, text fallback."""
    factors: list[dict] | None = None
    if json_path is not None and Path(json_path).exists():
        try:
            data = json.loads(Path(json_path).read_text())
            sf = (data.get("sim", {}).get("players") or [{}])[0].get("scale_factors") or {}
            if sf:
                factors = [{"stat": k, "value": float(v)} for k, v in sf.items()]
        except Exception:  # noqa: BLE001
            factors = None
    m = SF_WEIGHTS_RE.search(log)
    errs: dict[str, float] = {}
    if m:
        errs = {k: float(e) for k, _v, e in SF_ITEM_RE.findall(m.group(1))}
    if factors is None and m:
        factors = [{"stat": k, "value": float(v), "error": float(e)}
                   for k, v, e in SF_ITEM_RE.findall(m.group(1))]
    if not factors:
        return None
    for f in factors:
        if f["stat"] in errs and "error" not in f:
            f["error"] = errs[f["stat"]]
    top = max((f["value"] for f in factors), default=0.0) or 1.0
    for f in factors:
        f["normalized"] = round(f["value"] / top, 4)
    return factors


def parse_gear_results(json_path: Path | None) -> list[dict] | None:
    """Comparaison de pièces (profilesets) : nom, DPS, marge absolue."""
    if json_path is None or not Path(json_path).exists():
        return None
    try:
        data = json.loads(Path(json_path).read_text())
        results = ((data.get("sim") or {}).get("profilesets") or {}).get("results") or []
    except Exception:  # noqa: BLE001
        return None
    out: list[dict] = []
    for r in results:
        mean = r.get("mean")
        if mean is None:
            continue
        out.append({
            "name": r.get("name") or "?",
            "dps": float(mean),
            "err": float(r["mean_error"]) if r.get("mean_error") else None,
        })
    return out or None


_CLASS_NORM = {
    "deathknight": "DeathKnight", "demonhunter": "DemonHunter", "druid": "Druid", "evoker": "Evoker",
    "hunter": "Hunter", "mage": "Mage", "monk": "Monk", "paladin": "Paladin", "priest": "Priest",
    "rogue": "Rogue", "shaman": "Shaman", "warlock": "Warlock", "warrior": "Warrior",
}


def _norm_class(raw: str) -> str:
    k = re.sub(r"[^a-z]", "", (raw or "").lower())
    return _CLASS_NORM.get(k, raw or "")


_CLASS_NAMES = ("Death Knight", "Demon Hunter", "Druid", "Evoker", "Hunter", "Mage", "Monk",
                "Paladin", "Priest", "Rogue", "Shaman", "Warlock", "Warrior")


def parse_group_results(json_path: Path | None) -> list[dict] | None:
    """Sim de groupe : DPS de chaque acteur (nom, classe, spé, ilvl)."""
    if json_path is None or not Path(json_path).exists():
        return None
    try:
        data = json.loads(Path(json_path).read_text())
        players = (data.get("sim") or {}).get("players") or []
    except Exception:  # noqa: BLE001
        return None
    out: list[dict] = []
    for p in players:
        cd = p.get("collected_data") or {}
        dps = cd.get("dps") or {}
        mean = dps.get("mean")
        if mean is None:
            continue
        # « specialization » de simc = « Arcane Mage » → on sépare spé et classe.
        spec_raw = str(p.get("specialization") or "")
        cls, spec = "", spec_raw
        for cn in _CLASS_NAMES:
            if spec_raw.endswith(cn):
                cls, spec = cn, spec_raw[: len(spec_raw) - len(cn)].strip()
                break
        ilvl = None
        gear = p.get("gear") or {}
        if isinstance(gear, dict):
            vals = [float(it.get("ilevel")) for it in gear.values()
                    if isinstance(it, dict) and it.get("ilevel")]
            if vals:
                ilvl = round(sum(vals) / len(vals), 1)
        out.append({
            "name": p.get("name") or "?",
            "class": _norm_class(cls) if cls else "",
            "spec": spec,
            "ilvl": ilvl,
            "dps": float(mean),
            "err": float(dps["mean_error"]) if dps.get("mean_error") else None,
        })
    out.sort(key=lambda r: r["dps"], reverse=True)
    return out or None


def run_sim(
    profile_path: Path | None = None,
    container_profile: str | None = None,
    iterations: int = 10000,
    outdir: Path | None = None,
    extra: list[str] | None = None,
    timeout: int = 900,
) -> dict:
    """Run one simulation. Returns a dict with ok/rc/dps/report paths/log tail."""
    if bool(profile_path) == bool(container_profile):
        raise ValueError("exactly one of profile_path / container_profile is required")

    outdir = Path(outdir) if outdir else Path(tempfile.mkdtemp(prefix="simc-"))
    outdir.mkdir(parents=True, exist_ok=True)

    cmd = ["docker", "run", "--rm", "-v", f"{outdir.resolve()}:/sim/out"]
    if profile_path is not None:
        cmd += ["-v", f"{Path(profile_path).resolve()}:/sim/input.simc:ro"]
        target = "/sim/input.simc"
    else:
        target = container_profile

    cmd += [
        IMAGE,
        target,
        f"iterations={iterations}",
        "report_details=0",
        "html=/sim/out/report.html",
        "json2=/sim/out/report.json",
    ]
    if extra:
        cmd += list(extra)

    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    wall = time.time() - t0

    log = (proc.stdout or "") + (proc.stderr or "")
    m = DPS_RE.search(log)
    html = outdir / "report.html"
    js = outdir / "report.json"

    return {
        "ok": proc.returncode == 0 and m is not None,
        "rc": proc.returncode,
        "dps": float(m.group(1)) if m else None,
        "dps_error": float(m.group(2)) if m else None,
        "dps_error_pct": float(m.group(3)) if m else None,
        "iterations": iterations,
        "wall_s": round(wall, 3),
        "html": str(html) if html.exists() else None,
        "json": str(js) if js.exists() else None,
        "log_tail": "\n".join(log.splitlines()[-30:]),
        "scale_factors": parse_scale_factors(log, js),
        "gear": parse_gear_results(js),
        "group": parse_group_results(js),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run SimulationCraft in the official Docker image")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--profile", type=Path, help="host .simc file (addon export / profile)")
    g.add_argument("--container-profile", help="profile path inside the image, e.g. profiles/MID2/...")
    ap.add_argument("--iterations", type=int, default=10000)
    ap.add_argument("--outdir", type=Path, default=None)
    ap.add_argument("--extra", nargs="*", default=None, help="extra simc options, e.g. threads=4 max_time=60")
    args = ap.parse_args()

    result = run_sim(
        profile_path=args.profile,
        container_profile=args.container_profile,
        iterations=args.iterations,
        outdir=args.outdir,
        extra=args.extra,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
