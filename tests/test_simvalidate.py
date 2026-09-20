"""Validation partagée app ↔ worker (shared/simvalidate.py) — mêmes règles des deux côtés."""
from pathlib import Path

from shared.simvalidate import (BLOCKED_KEYS, check_profile, validate_container_profile,
                                validate_extra, validate_iterations, validate_profile_text)

ROOT = Path(__file__).resolve().parent.parent


def test_profile_directives_refused():
    for key in BLOCKED_KEYS:
        text = f'warrior="T"\n{key}=/etc/passwd\n'
        assert check_profile(text) == key, key
        ok, err = validate_profile_text(text, 4096)
        assert not ok and key in (err or ""), key


def test_profile_directive_behind_profileset_prefix_refused():
    # profileset_x+=output=… : le garde-fou v141 (^clé=) ne le voyait pas.
    assert check_profile('profileset_toto+=output=/tmp/evil\n') == "output"
    assert check_profile('x+=apikey=abc\n') == "apikey"


def test_profile_accepts_normal_export():
    text = ('warrior="T"\nhead=,id=123,ilevel=600\n# output= in a comment is fine\n'
            'profileset_a+=combo=1\n')
    ok, err = validate_profile_text(text, 4096)
    assert ok and err is None


def test_profile_empty_and_size_cap():
    assert not validate_profile_text("", 4096)[0]
    assert not validate_profile_text(None, 4096)[0]
    ok, err = validate_profile_text("x" * 5000, 1024)
    assert not ok and "volumineux" in (err or "")


def test_iterations_bounds():
    assert validate_iterations(10000, 200000) == (True, None)
    assert not validate_iterations(0, 200000)[0]
    assert not validate_iterations(200001, 200000)[0]
    assert not validate_iterations("abc", 200000)[0]
    assert not validate_iterations(None, 200000)[0]


def test_extra_options():
    assert validate_extra(["max_time=1", "calculate_scale_factors=0", "fight_style=Patchwerk"]) == (True, None)
    assert validate_extra(None) == (True, None)
    assert not validate_extra(["max_time=/etc/passwd"])[0]      # pas de chemins
    assert not validate_extra(["output=/tmp/x"])[0]             # clé interdite
    assert not validate_extra(["no_equals"])[0]
    assert not validate_extra(["a b=1"])[0]
    assert not validate_extra("pas une liste")[0]


def test_container_profile_paths():
    assert validate_container_profile("profiles/MID2/MID2_Mage_Arcane.simc") == (True, None)
    assert not validate_container_profile("profiles/../../etc/passwd")[0]
    assert not validate_container_profile("/etc/passwd")[0]
    assert not validate_container_profile("profiles/x.sh")[0]
    assert not validate_container_profile(None)[0]


def test_worker_revalidates_what_app_checked():
    """Le worker utiliserait le MÊME module : app/security.check_profile est un re-export."""
    from app.security import check_profile as reexported
    assert reexported is check_profile
