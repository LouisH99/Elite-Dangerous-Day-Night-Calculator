from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_db_path() -> Path:
    env_path = os.environ.get("ELITE_DAYNIGHT_DB", "").strip()
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().with_name("elite_daynight.db")


def file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def db_file_sizes(db_path: Path) -> Dict[str, int]:
    return {
        "db_bytes": file_size(db_path),
        "wal_bytes": file_size(Path(str(db_path) + "-wal")),
        "shm_bytes": file_size(Path(str(db_path) + "-shm")),
    }


def mb(value: int) -> str:
    return f"{value / 1048576:.2f} MB"


def prediction_cache_stats(con: sqlite3.Connection) -> Dict[str, Any]:
    row = con.execute(
        """
        SELECT COUNT(*) AS rows,
               COALESCE(SUM(LENGTH(prediction_json)), 0) AS json_bytes,
               MIN(created_at_utc) AS oldest_created_at_utc,
               MAX(created_at_utc) AS newest_created_at_utc
          FROM prediction_cache
        """
    ).fetchone()
    return {
        "rows": int(row["rows"] or 0),
        "json_bytes": int(row["json_bytes"] or 0),
        "oldest_created_at_utc": row["oldest_created_at_utc"],
        "newest_created_at_utc": row["newest_created_at_utc"],
    }


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=2.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 2000")
    return con


def ensure_prediction_cache_exists(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'prediction_cache'"
    ).fetchone()
    if row is None:
        raise RuntimeError("This database has no prediction_cache table.")


def print_stats(label: str, stats: Dict[str, Any], sizes: Dict[str, int]) -> None:
    print(label)
    print(f"  prediction_cache rows: {stats['rows']}")
    print(f"  prediction_cache JSON: {mb(stats['json_bytes'])}")
    print(f"  oldest row: {stats['oldest_created_at_utc'] or '-'}")
    print(f"  newest row: {stats['newest_created_at_utc'] or '-'}")
    print(f"  db file: {mb(sizes['db_bytes'])}")
    print(f"  wal file: {mb(sizes['wal_bytes'])}")
    print(f"  shm file: {mb(sizes['shm_bytes'])}")


def delete_prediction_cache_rows(
    con: sqlite3.Connection,
    *,
    clear_all: bool,
    keep_hours: Optional[float],
    max_rows: Optional[int],
) -> Dict[str, int]:
    deleted_all = 0
    deleted_old = 0
    deleted_over_limit = 0

    if clear_all:
        cur = con.execute("DELETE FROM prediction_cache")
        deleted_all = int(cur.rowcount if cur.rowcount is not None else 0)
        return {
            "deleted": deleted_all,
            "deleted_all": deleted_all,
            "deleted_old": 0,
            "deleted_over_limit": 0,
        }

    if keep_hours is not None:
        cutoff = format_utc(utc_now() - timedelta(hours=float(keep_hours)))
        cur = con.execute("DELETE FROM prediction_cache WHERE created_at_utc < ?", (cutoff,))
        deleted_old = int(cur.rowcount if cur.rowcount is not None else 0)

    if max_rows is not None and int(max_rows) >= 0:
        row = con.execute("SELECT COUNT(*) AS c FROM prediction_cache").fetchone()
        extra = int(row["c"] or 0) - int(max_rows)
        if extra > 0:
            cur = con.execute(
                """
                DELETE FROM prediction_cache
                 WHERE id IN (
                       SELECT id
                         FROM prediction_cache
                        ORDER BY created_at_utc ASC, id ASC
                        LIMIT ?
                 )
                """,
                (extra,),
            )
            deleted_over_limit = int(cur.rowcount if cur.rowcount is not None else 0)

    return {
        "deleted": deleted_old + deleted_over_limit,
        "deleted_all": deleted_all,
        "deleted_old": deleted_old,
        "deleted_over_limit": deleted_over_limit,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline maintenance for the legacy prediction_cache table. Stop the API/website before using --apply."
    )
    parser.add_argument("--db", default=str(default_db_path()), help="SQLite DB path. Defaults to ELITE_DAYNIGHT_DB or ./elite_daynight.db.")
    parser.add_argument("--apply", action="store_true", help="Actually delete rows. Without this, only stats are printed.")
    parser.add_argument("--vacuum", action="store_true", help="Run VACUUM after deleting rows to shrink the DB file. Requires --apply.")
    parser.add_argument("--all", dest="clear_all", action="store_true", default=True, help="Delete all prediction_cache rows. This is the default.")
    parser.add_argument("--keep-hours", type=float, default=None, help="Keep rows newer than this many hours instead of clearing all rows.")
    parser.add_argument("--max-rows", type=int, default=None, help="Keep only this many newest rows instead of clearing all rows.")
    parser.add_argument("--no-checkpoint", action="store_true", help="Skip WAL checkpoint/truncate.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 2
    if args.vacuum and not args.apply:
        print("--vacuum requires --apply")
        return 2

    clear_all = args.clear_all and args.keep_hours is None and args.max_rows is None
    con = connect(db_path)
    try:
        ensure_prediction_cache_exists(con)
        before = prediction_cache_stats(con)
        print_stats("Before cleanup", before, db_file_sizes(db_path))

        if not args.apply:
            print()
            print("Dry run only. Stop the API/website, then rerun with --apply --vacuum to clean and shrink the DB.")
            return 0

        print()
        print("Applying cleanup. Make sure the API and website are stopped.")
        con.execute("BEGIN IMMEDIATE")
        deleted = delete_prediction_cache_rows(
            con,
            clear_all=clear_all,
            keep_hours=args.keep_hours,
            max_rows=args.max_rows,
        )
        con.commit()
        print(f"Deleted rows: {deleted['deleted']}")

        if not args.no_checkpoint:
            checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            print(f"WAL checkpoint/truncate result: {[tuple(row) for row in checkpoint]}")

        if args.vacuum:
            print("Running VACUUM. This can take a while on a large database.")
            con.execute("VACUUM")
            if not args.no_checkpoint:
                checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                print(f"Post-VACUUM WAL checkpoint/truncate result: {[tuple(row) for row in checkpoint]}")

        after = prediction_cache_stats(con)
        print()
        print_stats("After cleanup", after, db_file_sizes(db_path))
        return 0
    except sqlite3.OperationalError as exc:
        print(f"SQLite error: {exc}")
        print("If the database is locked, stop the API/website and try again.")
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
