"""Unit tests for notification payloads and companion unlocks."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("push", Path(__file__).resolve().parents[1] / "app/services/push.py")
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


def test_unlocks_only_thresholds_crossed():
    assert push.unlocked_between(95, 105) == [("cat", "Muezza")]
    assert push.unlocked_between(100, 110) == []
    assert push.unlocked_between(190, 360) == [("ant", "Naml"), ("hoopoe", "Hudhud")]
    assert push.unlocked_between(0, 0) == []


def test_payload_speaks_as_the_users_companion():
    p = push.build_payload("Psst!", "{companion} has news.", "/?product=1", "t", "hoopoe")
    assert p["body"] == "Hudhud has news." and p["icon"] == "/companions/hoopoe.png"


def test_payload_defaults_and_limits():
    p = push.build_payload("x" * 200, "y" * 500, "https://evil.example", None, "dragon")
    assert p["icon"] == "/companions/bee.png"
    assert len(p["title"]) == 80 and len(p["body"]) == 240
    assert p["url"] == "/"
