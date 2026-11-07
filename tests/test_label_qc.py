"""Which ingredient list is current, and which is waiting for confirmation."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name, rel in (("app.services.formulation_compare", "app/services/formulation_compare.py"),):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
_spec = importlib.util.spec_from_file_location("label_qc", ROOT / "app/services/label_qc.py")
qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qc)


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, key, value):
        return _Query([r for r in self.rows if r.get(key) == value])

    def order(self, key, desc=False):
        return _Query(sorted(self.rows, key=lambda r: str(r.get(key) or ""), reverse=desc))

    def limit(self, n):
        return _Query(self.rows[:n])

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def f(id, when, confirmed=None, closed=None, text="sugar, salt"):
    return {"id": id, "product_id": "p", "country_code": "US", "effective_from": when,
            "confirmed_at": confirmed, "effective_to": closed, "ingredients_text": text}


def test_photo_only_product_is_its_own_pending_list():
    db = _DB({"product_formulations": [f("a", "2026-11-07T10:00")]})
    current, pending = qc.formulations(db, "p", "US")
    assert current["id"] == "a" and pending["id"] == "a"


def test_reported_change_does_not_replace_confirmed_list():
    db = _DB({"product_formulations": [
        f("old", "2026-01-01", confirmed="2026-01-01"),
        f("new", "2026-11-07"),
    ]})
    current, pending = qc.formulations(db, "p", "US")
    assert current["id"] == "old" and pending["id"] == "new"


def test_stale_unconfirmed_reading_is_not_pending():
    db = _DB({"product_formulations": [
        f("stale", "2026-01-01"),
        f("trusted", "2026-06-01", confirmed="2026-06-01"),
    ]})
    current, pending = qc.formulations(db, "p", "US")
    assert current["id"] == "trusted" and pending is None


def test_confirmations_count_accounts_not_photos():
    db = _DB({"label_scans": [
        {"formulation_id": "x", "user_id": "u1", "verified": True},
        {"formulation_id": "x", "user_id": "u1", "verified": True},
        {"formulation_id": "x", "user_id": "u2", "verified": True},
        {"formulation_id": "x", "user_id": "u3", "verified": False},
    ]})
    assert qc.confirmations(db, "x") == 2
