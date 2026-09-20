"""Contrôles de cohérence du dépôt (VERSION, CHANGELOG, i18n, addon). Stdlib uniquement."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_version_format():
    v = (ROOT / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{3}(-c\d+)?", v), v


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


def test_addon_version_consistent():
    """TOC et constante Lua doivent afficher la même version (dérive constatée au renommage v137)."""
    toc = (ROOT / "addon/Cohors/Cohors.toc").read_text(encoding="utf-8")
    lua = (ROOT / "addon/Cohors/Cohors.lua").read_text(encoding="utf-8")
    m_toc = re.search(r"## Version:\s*(\S+)", toc)
    m_lua = re.search(r'local ADDON_VER = "([^"]+)"', lua)
    assert m_toc and m_lua, "versions introuvables dans le TOC ou le Lua"
    assert m_toc.group(1) == m_lua.group(1), (m_toc.group(1), m_lua.group(1))


def test_compose_and_env_example_stay_in_sync():
    """Chaque variable documentée dans .env.example et utilisée par le code reste disjointe du compose."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "DATA_DIR" in compose and "PORT" in compose


def _compose_service(text: str, name: str) -> str:
    """Bloc YAML d'un service (indentation compose : 2 espaces = nom, 4+ = corps)."""
    out, inside = [], False
    for ln in text.splitlines():
        if re.match(r"^  \S", ln):
            inside = ln.strip().startswith(name + ":")
            continue
        if inside and (ln.startswith("    ") or not ln.strip()):
            out.append(ln)
    return "\n".join(out)


def test_app_container_has_no_docker_socket_and_runs_non_root():
    """Régression (revue externe) : sans ce test, le compose pourrait remettre root + socket."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app = _compose_service(compose, "app")
    worker = _compose_service(compose, "worker")
    assert app, "service app introuvable dans docker-compose.yml"
    assert worker, "service worker introuvable dans docker-compose.yml"
    assert "docker.sock" not in app, "l'app ne doit plus monter le socket Docker"
    assert "docker.sock" in worker, "seul le worker doit monter le socket Docker"
    m = re.search(r'^\s*user:\s*(.+)$', app, re.M)
    assert m, "l'app doit fixer un user: uid:gid (non-root)"
    val = m.group(1).strip().strip('"')
    assert "root" not in val
    m2 = re.match(r"^(?:\$\{[A-Z_]+:-(\d+)\}|(\d+)):(\d+)$", val)
    assert m2, f"format user inattendu : {val}"
    assert (m2.group(1) or m2.group(2)) != "0", "l'app ne doit pas tourner en root"
    assert "simsock" in app and "simsock" in worker, "socket Unix app ↔ worker manquant"


def test_app_dockerfile_drops_root_and_docker_cli():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER\s+\d+:", df, re.M), "USER non-root attendu dans le Dockerfile de l'app"
    assert "docker:cli" not in df, "le client docker ne doit plus être dans l'image de l'app"
    wdf = (ROOT / "worker" / "Dockerfile").read_text(encoding="utf-8")
    assert "docker:cli" in wdf, "le client docker doit être dans l'image du worker"


def test_officer_compose_uses_prebuilt_images_and_keeps_the_socket_isolated():
    """Parcours « officier de guilde » : images GHCR uniquement (jamais de build), même isolation."""
    c = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "ghcr.io/lostinthebugs/cohors:latest" in c
    assert "ghcr.io/lostinthebugs/cohors-simworker:latest" in c
    assert "build:" not in c, "le compose officier ne doit jamais builder"
    app = _compose_service(c, "app")
    worker = _compose_service(c, "worker")
    assert "docker.sock" not in app and "docker.sock" in worker, "isolation du socket Docker"
    assert "simsock" in app and "simsock" in worker, "socket Unix app ↔ worker manquant"
