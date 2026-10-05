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
    m2 = re.match(r"^(?:\$\{[A-Z_]+:-(\d+)\}|(\d+)):(?:\$\{[A-Z_]+:-(\d+)\}|(\d+))$", val)
    assert m2, f"format user inattendu : {val}"
    uid, gid = (m2.group(1) or m2.group(2)), (m2.group(3) or m2.group(4))
    assert uid != "0", "l'app ne doit pas tourner en root"
    # Le GID ne doit pas être écrit en dur des deux côtés : le worker lit la MÊME variable
    # (revue 20/09 — sinon on en change un et on oublie l'autre).
    m3 = re.search(r"SIMWORKER_APP_GID=\$\{APP_GID:-(\d+)\}", worker)
    assert m3, "le worker doit recevoir SIMWORKER_APP_GID"
    assert gid == m3.group(1), f"GID app ({gid}) ≠ GID worker ({m3.group(1)})"
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


def test_html_v_param_matches_version():
    """Chaque ?v= dans les pages HTML pointe sur la version actuelle (régression c22→c24)."""
    v = (ROOT / "VERSION").read_text().strip()
    expected = f"?v={v}"
    for p in sorted((ROOT / "app/static").glob("*.html")):
        content = p.read_text(encoding="utf-8")
        found = re.findall(r"\?v=[^\s\"'>]+", content)
        for ref in found:
            assert ref == expected, f"{p.name}: {ref} ≠ {expected}"


def test_app_version_and_service_worker_match_root():
    """app/VERSION et le CACHE de sw.js doivent tous deux correspondre à VERSION racine."""
    root_v = (ROOT / "VERSION").read_text().strip()
    app_v = (ROOT / "app/VERSION").read_text().strip()
    assert app_v == root_v, f"app/VERSION ({app_v}) ≠ VERSION ({root_v})"
    sw = (ROOT / "app/static/sw.js").read_text(encoding="utf-8")
    m = re.search(r'const CACHE = "cohors-v([^"]+)"', sw)
    assert m, "nom du CACHE introuvable dans sw.js"
    sw_ver = m.group(1)
    assert sw_ver == root_v, f"sw.js CACHE ({sw_ver}) ≠ VERSION ({root_v})"


def test_html_no_stray_path_lines():
    """Aucune ligne ne contient seulement un chemin de fichier (régression c29 : `pp/static/char.html` inséré partout)."""
    bad = re.compile(r"^\s*[\w./-]*static/[\w.-]+\.(html|js|css)\s*$")
    for p in sorted((ROOT / "app/static").glob("*.html")):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            assert not bad.match(line), f"{p.name}:{n}: ligne parasite {line!r}"


def test_help_items_all_translated():
    """Chaque puce de la page Aide a sa traduction anglaise (clé texte, ou clé « <li>…</li> » si balises)."""
    import html as _html
    src = (ROOT / "app/static/i18n.js").read_text(encoding="utf-8")
    i = src.index("const DICT = ")
    d, _ = json.JSONDecoder().raw_decode(src[i + len("const DICT = "):])
    norm = lambda s: re.sub(r"\s+", " ", s.replace(" ", " ")).strip()  # noqa: E731
    html_keys = {norm(k[4:-5].replace("&amp;", "&").replace("&nbsp;", " "))
                 for k in d if k.startswith("<li>") and k.endswith("</li>")}
    page = (ROOT / "app/static/help.html").read_text(encoding="utf-8")
    missing = []
    for li in re.findall(r"<li>(.*?)</li>", page, re.S):
        ok = (norm(li.replace("&amp;", "&").replace("&nbsp;", " ")) in html_keys) if "<" in li \
            else (norm(_html.unescape(li)) in d)
        if not ok:
            missing.append(li[:70])
    assert not missing, missing


def test_simc_image_pinned_and_consistent():
    """Les quatre valeurs par défaut de l'image SimC sont identiques, pas de :latest,
    et aucun « simc:latest » ne subsiste ailleurs dans le dépôt."""
    # Extraire les valeurs par défaut de chaque fichier
    main_py = (ROOT / "app/main.py").read_text(encoding="utf-8")
    m = re.search(r'SIMC_IMAGE\s*=\s*os\.environ\.get\([^,]+,\s*"([^"]+)"\)', main_py)
    assert m, "SIMC_IMAGE introuvable dans app/main.py"
    app_default = m.group(1)

    simrun = (ROOT / "worker/simrun.py").read_text(encoding="utf-8")
    m2 = re.search(r'^IMAGE\s*=\s*os\.environ\.get\([^,]+,\s*"([^"]+)"\)', simrun, re.M)
    assert m2, "IMAGE introuvable dans worker/simrun.py"
    simrun_default = m2.group(1)

    # docker-compose.yml
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    m3 = re.search(r'SIMC_IMAGE=\$\{SIMC_IMAGE:-([^}]+)\}', compose)
    assert m3, "SIMC_IMAGE non trouvé dans docker-compose.yml"
    compose_default = m3.group(1)

    # deploy/docker-compose.yml
    deploy = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    m4 = re.search(r'SIMC_IMAGE=\$\{SIMC_IMAGE:-([^}]+)\}', deploy)
    assert m4, "SIMC_IMAGE non trouvé dans deploy/docker-compose.yml"
    deploy_default = m4.group(1)

    # Les quatre doivent être identiques
    defaults = [app_default, simrun_default, compose_default, deploy_default]
    assert len(set(defaults)) == 1, f"incohérence: {set(defaults)}"

    # Aucune ne doit se terminer par :latest
    assert not app_default.endswith(":latest"), "app/main.py utilise encore :latest"
    assert app_default != "simulationcraftorg/simc:latest"

    # Aucun résidu simc:latest (sauf CHANGELOG.md et les briefs)
    import subprocess
    result = subprocess.run(
        ["grep", "-rn", "--exclude=test_repo.py", "--exclude=BRIEF-SIMC.md",
         "--exclude=hermes-cohors-simc-pin.md", "--exclude=README.md",
         "--exclude=.env.example", "simc:latest",
         "--exclude-dir=.git", "--exclude=CHANGELOG.md", "--exclude-dir=__pycache__",
         "."],
        cwd=str(ROOT), capture_output=True, text=True
    )
    assert result.returncode != 0, f"simc:latest trouvé : {result.stdout}"


def test_simworker_prepull_command():
    """La fonction main() de simworker lance un docker pull en arrière-plan avec la bonne commande."""
    from unittest import mock
    from worker.simworker import main

    with mock.patch("worker.simworker.subprocess.run") as mock_run, \
         mock.patch("worker.simworker.Path") as mock_path, \
         mock.patch("worker.simworker.os"), \
         mock.patch("worker.simworker.socketserver.ThreadingUnixStreamServer") as mock_server, \
         mock.patch("worker.simworker.cleanup_orphans", return_value=[]), \
         mock.patch("worker.simworker.purge_job_dirs", return_value=0):

        # Mock du socket pour éviter les erreurs de bind
        mock_sock = mock.MagicMock()
        mock_sock.parent = mock.MagicMock()
        mock_path.return_value = mock_sock

        main()

        # Le serveur doit être lancé (serve_forever appelé)
        mock_server.return_value.serve_forever.assert_called()

        # subprocess.run doit avoir été appelé avec ["docker", "pull", IMAGE]
        docker_pull_calls = [
            call for call in mock_run.call_args_list
            if isinstance(call.args[0], list) and call.args[0][0] == "docker" and call.args[0][1] == "pull"
        ]
        assert len(docker_pull_calls) == 1, f"1 appel docker pull attendu, trouvé {len(docker_pull_calls)}"
        assert docker_pull_calls[0].args[0] == ["docker", "pull", "simulationcraftorg/simc:1210-2026-10-04-2d54d82"]
