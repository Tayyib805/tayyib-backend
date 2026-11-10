"""Integration tests: router code against a real local Postgres with every migration applied.

Skipped unless a local Postgres is reachable. Point TAYYIB_TEST_PG at it as
"host_or_socket_dir:port[:user]" (default "/var/tmp/pgtest:5499:postgres").
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent.parent

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "test")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
sys.path.append(str(HERE / "stubs"))  # last: real packages win when installed

from tests.integration.pgfake import Pg  # noqa: E402


def _conn() -> list[str] | None:
    if not shutil.which("psql"):
        return None
    host, port, *rest = os.environ.get("TAYYIB_TEST_PG", "/var/tmp/pgtest:5499:postgres").split(":")
    base = ["psql", "-h", host, "-p", port, "-U", rest[0] if rest else "postgres"]
    ok = subprocess.run(base + ["-d", "postgres", "-Atc", "select 1"], capture_output=True, text=True)
    return base if ok.returncode == 0 else None


def _use_real_app_package() -> None:
    """Some unit tests register a placeholder module named "app"; these tests need the real package."""

    mod = sys.modules.get("app")
    if mod is not None and not hasattr(mod, "__path__"):
        for key in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
            del sys.modules[key]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def pg():
    _use_real_app_package()
    base = _conn()
    if base is None:
        pytest.skip("no local Postgres for integration tests")
    name = f"tayyib_test_{os.getpid()}"
    subprocess.run(base + ["-d", "postgres", "-qc", f"create database {name}"], check=True)
    db = base + ["-d", name]
    subprocess.run(db + ["-q", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(HERE / "supabase_shim.sql")], check=True, capture_output=True)
    for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
        # Older migrations assume objects Lovable created outside this repo; those statements are allowed to fail.
        strict = path.name >= "022"
        out = subprocess.run(db + ["-q", "-X"] + (["-v", "ON_ERROR_STOP=1"] if strict else []) + ["-f", str(path)], capture_output=True, text=True)
        if strict and out.returncode != 0:
            raise RuntimeError(f"{path.name} failed: {out.stderr}")
    try:
        yield Pg(db)
    finally:
        subprocess.run(base + ["-d", "postgres", "-qc", f"drop database if exists {name} with (force)"], capture_output=True)


@pytest.fixture()
def people(pg):
    """Factory for accounts: people(days_old=30, scans=5, admin=False) -> user dict as the API sees it."""

    def make(days_old: int = 30, scans: int = 5, admin: bool = False) -> dict:
        uid = str(uuid.uuid4())
        created = pg.run(
            f"with u as (insert into auth.users (id, email, created_at) values ('{uid}', '{uid[:8]}@example.com', now() - interval '{int(days_old)} days') returning created_at) "
            "select to_json(created_at) from u"
        )
        pg.run(f"with p as (insert into public.user_profiles (id, is_admin) values ('{uid}', {str(admin).lower()}) on conflict (id) do update set is_admin = excluded.is_admin returning 1) select to_json(count(*)) from p")
        if scans:
            pg.run(f"with s as (insert into public.scan_logs (user_id, barcode) select '{uid}', 'x' from generate_series(1, {int(scans)}) returning 1) select to_json(count(*)) from s")
        return {"id": uid, "email": f"{uid[:8]}@example.com", "created_at": created}

    return make


def run(coro):
    return asyncio.run(coro)
