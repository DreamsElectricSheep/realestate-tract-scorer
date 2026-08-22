"""Database helpers for the realestate DB."""
from __future__ import annotations

import logging

import psycopg2
from psycopg2.extras import execute_values
from sqlalchemy import create_engine

from config import DB_URL, DB_URL_RAW

log = logging.getLogger("db")

_engine = None


def engine():
    """Lazily-created SQLAlchemy engine (used by geopandas/pandas)."""
    global _engine
    if _engine is None:
        _engine = create_engine(DB_URL, pool_pre_ping=True)
    return _engine


def raw_conn():
    """Raw psycopg2 connection for bulk upserts."""
    return psycopg2.connect(DB_URL_RAW)


def upsert(table: str, cols: list[str], rows: list[tuple], conflict_cols: list[str],
           update: bool = True, page_size: int = 5000) -> int:
    """
    Bulk INSERT ... ON CONFLICT. Returns rows submitted.

    Idempotent by construction: re-running an ingest overwrites rather than
    duplicating, so a partial failure can always be re-run safely.

    Deduplicates on conflict_cols before sending — Postgres rejects an
    ON CONFLICT DO UPDATE batch that would touch the same row twice in one
    statement (e.g. a source file carrying a stray duplicate county/year row).
    Last occurrence wins.
    """
    if not rows:
        return 0

    if conflict_cols:
        idxs = [cols.index(c) for c in conflict_cols]
        dedup: dict[tuple, tuple] = {}
        for r in rows:
            dedup[tuple(r[i] for i in idxs)] = r
        if len(dedup) < len(rows):
            log.warning("upsert %s: deduped %d -> %d rows on %s",
                        table, len(rows), len(dedup), conflict_cols)
        rows = list(dedup.values())
    collist = ", ".join(cols)
    conflict = ", ".join(conflict_cols)
    if update:
        setters = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in conflict_cols
        )
        action = f"DO UPDATE SET {setters}" if setters else "DO NOTHING"
    else:
        action = "DO NOTHING"

    sql = (
        f"INSERT INTO {table} ({collist}) VALUES %s "
        f"ON CONFLICT ({conflict}) {action}"
    )
    with raw_conn() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows, page_size=page_size)
        conn.commit()
    return len(rows)


def log_ingest(dataset: str, status: str, rows: int = 0, requests: int = 0,
               detail: str = "") -> None:
    try:
        with raw_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ingest_log (dataset, status, rows, requests, detail) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (dataset, status, rows, requests, detail[:2000]),
                )
            conn.commit()
    except Exception as e:
        log.warning("ingest_log write failed: %s", e)


def scalar(sql: str, params: tuple = ()) -> object:
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


def apply_schema(schema_path: str) -> None:
    with open(schema_path) as f:
        ddl = f.read()
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    log.info("schema applied from %s", schema_path)
