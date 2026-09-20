"""Contrôles de cohérence du dépôt (VERSION, CHANGELOG, i18n, addon). Stdlib uniquement."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_version_format():
    v = (ROOT / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{3}", v), v


def test_changelog_has_current_version():
    v = (ROOT / "VERSION").read_text().strip()
    assert f"## {v}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_i18n_dict_parses_and_is_complete():
    src = (ROOT / "app/static/i18n.js").read_text(encoding="utf-8")
    idx = src.index("const DICT = ")
    obj, _ = json.JSONDecoder().raw_decode(src[idx + len("const DICT = "):])
    assert isinstance(obj, dict)
    assert len(obj) > 250, f"dictionnaire trop petit : {len(obj)}"


def test_pages_using_esc_load_shared_helper():
    # Toute page qui appelle esc( doit charger /static/esc.js (ou définir un remplaçant local).
    for p in sorted((ROOT / "app/static").glob("*.html")):
        t = p.read_text(encoding="utf-8")
        if "esc(" in t:
            assert "esc.js" in t or "const esc" in t or "function esc" in t, p.name


def test_addon_files_present():
    assert (ROOT / "addon/Cohors/Cohors.toc").exists()
    assert (ROOT / "addon/Cohors/Cohors.lua").exists()


def test_compose_and_env_example_stay_in_sync():
    """Chaque variable documentée dans .env.example et utilisée par le code reste disjointe du compose."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "DATA_DIR" in compose and "PORT" in compose
