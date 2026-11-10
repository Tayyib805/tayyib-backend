"""A stand-in for the Supabase client that runs against a local Postgres.

It translates the handful of query-builder calls the API uses into SQL and
runs them through psql, so router code is exercised against the real schema
(all migrations applied). Test-only: not safe for untrusted input.
"""

from __future__ import annotations

import json
import re
import subprocess


class Result:
    def __init__(self, data=None, count=None):
        self.data, self.count = data, count


def lit(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, dict)):
        value = json.dumps(value)
    return "'" + str(value).replace("'", "''") + "'"


class Pg:
    def __init__(self, psql: list[str]):
        self.psql = psql
        self.auth = None

    def run(self, sql: str):
        out = subprocess.run(self.psql + ["-At", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql], capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"SQL failed: {out.stderr.strip()}\n{sql}")
        text = out.stdout.strip()
        return json.loads(text) if text else None

    def table(self, name: str) -> "Query":
        return Query(self, name)

    def rpc(self, fn: str, params: dict | None = None) -> "Rpc":
        return Rpc(self, fn, params or {})


class Rpc:
    def __init__(self, pg: Pg, fn: str, params: dict):
        self.pg, self.fn, self.params = pg, fn, params

    def execute(self) -> Result:
        args = ", ".join(f"{k} => {lit(v)}" for k, v in self.params.items())
        is_set = self.pg.run(f"select to_json(proretset) from pg_proc where proname = {lit(self.fn)} limit 1")
        if is_set:
            data = self.pg.run(f"select coalesce(json_agg(t), '[]'::json) from public.{self.fn}({args}) t")
        else:
            data = self.pg.run(f"select to_json(public.{self.fn}({args}))")
        return Result(data)


class Query:
    def __init__(self, pg: Pg, table: str):
        self.pg, self.t = pg, table
        self.cols, self.count_mode = "*", None
        self.where: list[str] = []
        self.orders: list[str] = []
        self.lim = None
        self.op, self.payload, self.conflict = "select", None, None
        self._negate = False

    # ── verbs ──
    def select(self, cols="*", count=None):
        self.cols, self.count_mode = cols, count
        return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows
        return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.payload, self.conflict = "upsert", rows, on_conflict
        return self

    def update(self, values):
        self.op, self.payload = "update", values
        return self

    def delete(self):
        self.op = "delete"
        return self

    # ── filters ──
    @property
    def not_(self):
        self._negate = True
        return self

    def _add(self, clause):
        self.where.append(f"not ({clause})" if self._negate else clause)
        self._negate = False
        return self

    def eq(self, c, v):
        return self._add(f"t.{c} = {lit(v)}")

    def neq(self, c, v):
        return self._add(f"t.{c} <> {lit(v)}")

    def gt(self, c, v):
        return self._add(f"t.{c} > {lit(v)}")

    def gte(self, c, v):
        return self._add(f"t.{c} >= {lit(v)}")

    def lt(self, c, v):
        return self._add(f"t.{c} < {lit(v)}")

    def lte(self, c, v):
        return self._add(f"t.{c} <= {lit(v)}")

    def is_(self, c, v):
        return self._add(f"t.{c} is {v}")

    def in_(self, c, values):
        values = list(values)
        return self._add(f"t.{c} in ({', '.join(lit(v) for v in values)})" if values else "false")

    def ilike(self, c, v):
        return self._add(f"t.{c} ilike {lit(v)}")

    def contains(self, c, v):
        return self._add(f"t.{c} @> {lit(v)}")

    def order(self, c, desc=False):
        self.orders.append(f"t.{c} {'desc' if desc else 'asc'}")
        return self

    def limit(self, n):
        self.lim = n
        return self

    def range(self, a, b):
        self.lim = b - a + 1
        return self

    # ── run ──
    def _select_list(self) -> str:
        return self._columns("t", self.t, self.cols, 0)

    def _has_column(self, table: str, column: str) -> bool:
        cache = self.pg.__dict__.setdefault("_columns_seen", {})
        if (table, column) not in cache:
            cache[(table, column)] = bool(self.pg.run(
                "select to_json(count(*)) from information_schema.columns "
                f"where table_schema = 'public' and table_name = {lit(table)} and column_name = {lit(column)}"
            ))
        return cache[(table, column)]

    def _columns(self, alias: str, table: str, cols: str, depth: int) -> str:
        """A select list, with PostgREST-style embeds: parent(cols) via <parent>_id, or children(cols) via <this>_id."""

        def singular(name: str) -> str:
            return name[:-1] if name.endswith("s") else name

        pieces, level, start = [], 0, 0
        for i, ch in enumerate(cols):
            level += ch == "("
            level -= ch == ")"
            if ch == "," and level == 0:
                pieces.append(cols[start:i])
                start = i + 1
        pieces.append(cols[start:])
        parts = []
        for col in (c.strip() for c in pieces):
            m = re.match(r"^(\w+)\((.*)\)$", col, re.S)
            if m:
                rel, inner = m.group(1), m.group(2)
                a = f"e{depth}"
                body = self._columns(a, rel, inner, depth + 1)
                if self._has_column(table, singular(rel) + "_id"):  # many-to-one: places(name, city)
                    parts.append(f"(select row_to_json(x) from (select {body} from public.{rel} {a} where {a}.id = {alias}.{singular(rel)}_id) x) as {rel}")
                else:  # one-to-many: product_ingredients(*)
                    parts.append(f"(select coalesce(json_agg(x), '[]'::json) from (select {body} from public.{rel} {a} where {a}.{singular(table)}_id = {alias}.id) x) as {rel}")
            elif col == "*":
                parts.append(f"{alias}.*")
            else:
                parts.append(f"{alias}.{col}")
        return ", ".join(parts)

    def _where(self) -> str:
        return (" where " + " and ".join(self.where)) if self.where else ""

    def execute(self) -> Result:
        t = f"public.{self.t}"
        if self.op == "select":
            sql = f"select {self._select_list()} from {t} t{self._where()}"
            if self.orders:
                sql += " order by " + ", ".join(self.orders)
            if self.lim is not None:
                sql += f" limit {int(self.lim)}"
            data = self.pg.run(f"select coalesce(json_agg(q), '[]'::json) from ({sql}) q")
            count = None
            if self.count_mode:
                count = self.pg.run(f"select to_json(count(*)) from {t} t{self._where()}")
            return Result(data, count)
        if self.op in ("insert", "upsert"):
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            if not rows:
                return Result([])
            cols = list(rows[0].keys())
            col_list = ", ".join(cols)
            sql = f"insert into {t} ({col_list}) select {col_list} from jsonb_populate_recordset(null::{t}, {lit(json.dumps(rows))}::jsonb)"
            if self.op == "upsert":
                keys = [c.strip() for c in (self.conflict or "id").split(",")]
                sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys)
                sql += f" on conflict ({', '.join(keys)}) " + (f"do update set {sets}" if sets else "do nothing")
            data = self.pg.run(f"with w as ({sql} returning *) select coalesce(json_agg(w), '[]'::json) from w")
            return Result(data)
        if self.op == "update":
            cols = list(self.payload.keys())
            sets = ", ".join(f"{c} = r.{c}" for c in cols)
            sql = f"update {t} t set {sets} from jsonb_populate_record(null::{t}, {lit(json.dumps(self.payload))}::jsonb) r{self._where()}"
            data = self.pg.run(f"with w as ({sql} returning t.*) select coalesce(json_agg(w), '[]'::json) from w")
            return Result(data)
        if self.op == "delete":
            data = self.pg.run(f"with w as (delete from {t} t{self._where()} returning t.*) select coalesce(json_agg(w), '[]'::json) from w")
            return Result(data)
        raise NotImplementedError(self.op)
