"""Unit tests for whose community votes count."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location("trust", Path(__file__).resolve().parents[1] / "app/services/trust.py")
trust = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trust)
MIN_ACCOUNT_AGE_DAYS, MIN_SCANS = trust.MIN_ACCOUNT_AGE_DAYS, trust.MIN_SCANS
account_age_days, is_established = trust.account_age_days, trust.is_established

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def test_age_parses_supabase_timestamps():
    created = (NOW - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    assert round(account_age_days(created, NOW)) == 10


def test_unknown_or_bad_dates_count_as_new():
    assert account_age_days(None, NOW) == 0
    assert account_age_days("not a date", NOW) == 0


def test_new_accounts_do_not_count():
    assert not is_established(0.5, 50)
    assert not is_established(MIN_ACCOUNT_AGE_DAYS - 0.1, MIN_SCANS)


def test_inactive_old_accounts_do_not_count():
    assert not is_established(400, MIN_SCANS - 1)


def test_established_and_admin_accounts_count():
    assert is_established(MIN_ACCOUNT_AGE_DAYS, MIN_SCANS)
    assert is_established(0, 0, admin=True)
