"""Tests des briques de sécurité (stdlib uniquement — aucun service requis)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security import check_profile, hash_password, real_client_ip, verify_password  # noqa: E402


def test_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong password", h)


def test_password_unique_salt():
    assert hash_password("same") != hash_password("same")


def test_password_rejects_garbage():
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$deadbeef")


def test_profile_guard_rejects_file_directives():
    for key in ("input", "output", "html", "json", "json2", "apikey"):
        assert check_profile(f'warrior="Test"\n{key}=/tmp/x\n') == key
        assert check_profile(f"  {key.upper()} = /tmp/x") == key


def test_real_client_ip_uses_last_hop():
    # Une valeur forgée par le client arrive en tête ; notre proxy ajoute la vraie IP en dernier.
    assert real_client_ip("6.6.6.6, 203.0.113.9", "127.0.0.1") == "203.0.113.9"
    assert real_client_ip("203.0.113.9", "127.0.0.1") == "203.0.113.9"
    assert real_client_ip("1.1.1.1, 2.2.2.2, 203.0.113.9", None) == "203.0.113.9"
    assert real_client_ip(None, "10.0.0.5") == "10.0.0.5"
    assert real_client_ip("", None) == "?"
    assert real_client_ip(" , ", "10.0.0.5") == "10.0.0.5"


def test_real_client_ip_hops_for_extra_proxies():
    # Deux proxys de confiance : la vraie IP est deux crans avant la fin.
    assert real_client_ip("1.1.1.1, 203.0.113.9, 172.16.0.2", None, hops=2) == "203.0.113.9"
    # Chaîne plus courte que prévu : on retombe sur la plus ancienne disponible, jamais hors bornes.
    assert real_client_ip("203.0.113.9", None, hops=3) == "203.0.113.9"
    assert real_client_ip("forge, 203.0.113.9", None, hops=0) == "203.0.113.9"  # borné à 1


def test_profile_guard_accepts_real_export_sample():
    sample = (
        "# SimC Addon 12.1.0-01\n"
        'warrior="Squall"\n'
        "level=90\n"
        "spec=fury\n"
        "talents=CgEAAAAAAAAAF\n"
        "head=,id=241149,bonus_id=1234\n"
        "main_hand=,id=222436\n"
        "iterations=10000\n"
        "max_time=300\n"
    )
    assert check_profile(sample) is None
