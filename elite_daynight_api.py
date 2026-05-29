#!/usr/bin/env python3
"""
Elite Dangerous Day/Night Calculator - local HTTP API

Run locally:
  uvicorn elite_daynight_api:app --reload --host 127.0.0.1 --port 8000

Environment:
  ELITE_DAYNIGHT_DB=/path/to/elite_daynight.db

This API is intentionally small and local-first. It uses the compact SQLite
database and the separated v15 model module.
"""
from __future__ import annotations

import os
import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

import elite_daynight_db as dbmod
import elite_daynight_model_v15 as model

DB_PATH = os.environ.get(
    "ELITE_DAYNIGHT_DB",
    os.path.join(os.path.dirname(__file__), "elite_daynight.db"),
)
SQLITE_TIMEOUT_SECONDS = float(os.environ.get("ELITE_DAYNIGHT_SQLITE_TIMEOUT", "10"))
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_TIMEOUT_SECONDS * 1000)


def env_bool(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


PUBLIC_POI_SUBMISSIONS_ENABLED = env_bool("ELITE_DAYNIGHT_PUBLIC_POI_SUBMISSIONS_ENABLED", True)
DB_WRITE_RETRIES = int(os.environ.get("ELITE_DAYNIGHT_DB_WRITE_RETRIES", "5"))
DB_WRITE_RETRY_BASE_SECONDS = float(os.environ.get("ELITE_DAYNIGHT_DB_WRITE_RETRY_BASE_SECONDS", "0.08"))

WRITE_LOCK = threading.RLock()
ALLOWED_REVIEW = {"new", "approved", "rejected", "needs_check", "corrected"}
ALLOWED_OBSERVATIONS = {"sunrise", "sunset", "horizon", "rise", "set", "elevation", "altitude", "sun_altitude", "alt", "day", "night"}
ALLOWED_QUALITY = {"high", "medium", "low"}

app = FastAPI(
    title="Elite Dangerous Day/Night Calculator API",
    version="0.191",
    description="Local-first API for systems, bodies, observations, fitting and prediction.",
)

# Useful while developing a local website. Lock this down before public hosting.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_database_hardening() -> None:
    initialize_database_runtime()


# ------------------------------ request models ------------------------------

class SpanshImportRequest(BaseModel):
    system_name: str = Field(..., min_length=1)
    body_name: str = Field(..., min_length=1)
    system_address: Optional[int] = None
    fetch_body_details: bool = True
    fit: bool = False
    use_heading: bool = False
    time_weighting: bool = False
    time_half_life_hours: float = 24.0
    include_unreviewed: bool = False


class ObservationCreate(BaseModel):
    observer_name: str = ""
    timestamp_utc: str
    lat: float
    lon: float
    observation: str = "elevation"
    elevation: Optional[float] = None
    heading: Optional[float] = None
    quality: str = "medium"
    note: str = ""
    review_status: str = "new"
    source: str = "api"

    @validator("lat")
    def check_lat(cls, value: float) -> float:
        if value < -90 or value > 90:
            raise ValueError("lat must be between -90 and 90")
        return value

    @validator("lon")
    def check_lon(cls, value: float) -> float:
        if value < -180 or value > 180:
            raise ValueError("lon must be between -180 and 180")
        return value

    @validator("heading")
    def check_heading(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return value
        if value < 0 or value >= 360:
            raise ValueError("heading must be >= 0 and < 360")
        return value

    @validator("observation")
    def check_observation(cls, value: str) -> str:
        v = value.strip().lower()
        if v not in ALLOWED_OBSERVATIONS:
            raise ValueError("unsupported observation type")
        return v

    @validator("quality")
    def check_quality(cls, value: str) -> str:
        v = value.strip().lower()
        if v not in ALLOWED_QUALITY:
            raise ValueError("quality must be high, medium, or low")
        return v

    @validator("review_status")
    def check_review(cls, value: str) -> str:
        v = value.strip().lower()
        if v not in ALLOWED_REVIEW:
            raise ValueError("invalid review_status")
        return v


class ObservationPatch(BaseModel):
    observer_name: Optional[str] = None
    timestamp_utc: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    observation: Optional[str] = None
    elevation: Optional[float] = None
    heading: Optional[float] = None
    quality: Optional[str] = None
    note: Optional[str] = None
    review_status: Optional[str] = None


class ReviewStatusUpdate(BaseModel):
    review_status: str
    actor: str = "api-admin"

    @validator("review_status")
    def check_review(cls, value: str) -> str:
        v = value.strip().lower()
        if v not in ALLOWED_REVIEW:
            raise ValueError("invalid review_status")
        return v


class RefitRequest(BaseModel):
    use_heading: bool = False
    time_weighting: bool = False
    time_half_life_hours: float = 24.0
    include_unreviewed: bool = False
    force_refit: bool = False


class PoiCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    lat: float
    lon: float
    description: str = ""
    is_public: bool = True
    review_status: str = "approved"
    submitter_name: str = ""
    actor: str = "website-admin"

    @validator("lat")
    def check_poi_lat(cls, value: float) -> float:
        if value < -90 or value > 90:
            raise ValueError("lat must be between -90 and 90")
        return value

    @validator("lon")
    def check_poi_lon(cls, value: float) -> float:
        if value < -180 or value > 180:
            raise ValueError("lon must be between -180 and 180")
        return value

    @validator("review_status")
    def check_poi_review_status(cls, value: str) -> str:
        v = value.strip().lower()
        if v not in ALLOWED_REVIEW and v != "hidden":
            raise ValueError("invalid POI review_status")
        return v


class PoiPatch(BaseModel):
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None
    review_status: Optional[str] = None
    submitter_name: Optional[str] = None
    actor: str = "website-admin"


# ------------------------------ DB helpers ------------------------------

def harden_connection(con: sqlite3.Connection) -> None:
    """Apply per-connection SQLite settings used by the website/API.

    WAL is enabled once at startup, but busy_timeout and foreign_keys are
    connection-local, so every connection gets them.
    """
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


def initialize_database_runtime() -> None:
    """Create schema and enable WAL mode for safer concurrent reads/writes."""
    dbmod.init_db(DB_PATH)
    con = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)
    try:
        harden_connection(con)
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA wal_autocheckpoint = 1000")
        ensure_runtime_migrations(con)
        con.commit()
        print(f"SQLite ready: journal_mode={mode}, busy_timeout={SQLITE_BUSY_TIMEOUT_MS}ms, db={DB_PATH}")
    finally:
        con.close()



def ensure_runtime_migrations(con: sqlite3.Connection) -> None:
    """Small additive migrations used by the website layer."""
    body_cols = {r[1] for r in con.execute("PRAGMA table_info(bodies)").fetchall()}
    if "tracked_for_prediction" not in body_cols:
        con.execute("ALTER TABLE bodies ADD COLUMN tracked_for_prediction INTEGER NOT NULL DEFAULT 0")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bodies_tracked ON bodies(tracked_for_prediction)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS body_pois (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body_id INTEGER NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            is_public INTEGER NOT NULL DEFAULT 1,
            review_status TEXT NOT NULL DEFAULT 'approved',
            submitter_name TEXT NOT NULL DEFAULT '',
            reviewed_at_utc TEXT,
            reviewed_by TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            created_by TEXT,
            updated_by TEXT
        )
        """
    )
    poi_cols = {r[1] for r in con.execute("PRAGMA table_info(body_pois)").fetchall()}
    if "review_status" not in poi_cols:
        con.execute("ALTER TABLE body_pois ADD COLUMN review_status TEXT NOT NULL DEFAULT 'approved'")
    if "submitter_name" not in poi_cols:
        con.execute("ALTER TABLE body_pois ADD COLUMN submitter_name TEXT NOT NULL DEFAULT ''")
    if "reviewed_at_utc" not in poi_cols:
        con.execute("ALTER TABLE body_pois ADD COLUMN reviewed_at_utc TEXT")
    if "reviewed_by" not in poi_cols:
        con.execute("ALTER TABLE body_pois ADD COLUMN reviewed_by TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_body_pois_body_public ON body_pois(body_id, is_public, review_status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_body_pois_name ON body_pois(name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_body_pois_review ON body_pois(review_status)")
    if hasattr(dbmod, "ensure_fit_mode_columns"):
        dbmod.ensure_fit_mode_columns(con)
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at_utc DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action)")


def connect() -> sqlite3.Connection:
    dbmod.init_db(DB_PATH)
    con = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)
    con.row_factory = sqlite3.Row
    harden_connection(con)
    return con




def begin_write_transaction(con: sqlite3.Connection) -> None:
    """Start an explicit write transaction with short retry/backoff.

    BEGIN IMMEDIATE reserves the single SQLite writer early. Combined with the
    process-local WRITE_LOCK this makes write behavior predictable and avoids
    partially interleaved write flows. The retry covers the case where an admin
    tool, backup, or another process briefly holds the database writer slot.
    """
    for attempt in range(DB_WRITE_RETRIES + 1):
        try:
            con.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" not in msg and "database is busy" not in msg:
                raise
            if attempt >= DB_WRITE_RETRIES:
                raise
            time.sleep(DB_WRITE_RETRY_BASE_SECONDS * (2 ** attempt))


def connect_write() -> sqlite3.Connection:
    con = connect()
    begin_write_transaction(con)
    return con

def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def fit_review_metadata(con: sqlite3.Connection, fit_id: int) -> Dict[str, Any]:
    rows = con.execute(
        """
        SELECT o.review_status, COUNT(*) AS c
          FROM fit_observations fo JOIN observations o ON o.id = fo.observation_id
         WHERE fo.fit_id = ? AND fo.used_in_fit = 1
         GROUP BY o.review_status
        """,
        (fit_id,),
    ).fetchall()
    counts = {str(r["review_status"]): int(r["c"]) for r in rows}
    unverified_count = sum(v for k, v in counts.items() if k != "approved")
    return {
        "fit_observation_status_counts": counts,
        "fit_unverified_observation_count": int(unverified_count),
        "fit_uses_unverified_data": bool(unverified_count > 0),
        "fit_data_note": "This model includes unreviewed observations. Treat it as provisional." if unverified_count > 0 else "This model uses approved observations only.",
    }


def is_sqlite_busy(exc: Exception) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("database is locked" in msg or "database is busy" in msg)


def http_error_from_exception(exc: Exception) -> HTTPException:
    # Keep error messages useful while local. Public deployment should hide internals.
    if is_sqlite_busy(exc):
        return HTTPException(
            status_code=503,
            detail="The database is busy right now. Please wait a few seconds and try again.",
        )
    return HTTPException(status_code=400, detail=str(exc))


def get_body_row_or_404(con: sqlite3.Connection, body_pk: int) -> sqlite3.Row:
    row = con.execute(
        """
        SELECT b.*, s.name AS system_name, s.system_address
          FROM bodies b JOIN systems s ON s.id = b.system_id
         WHERE b.id = ?
        """,
        (body_pk,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Body id not found: {body_pk}")
    return row


def get_system_row_or_404(con: sqlite3.Connection, system_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM systems WHERE id = ?", (system_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"System id not found: {system_id}")
    return row


def body_public_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = row_to_dict(row)
    # Keep the public object compact and readable.
    return {
        "id": d["id"],
        "system_id": d["system_id"],
        "system_name": d.get("system_name"),
        "system_address": d.get("system_address"),
        "body_id": d["body_id"],
        "body_id64": d["body_id64"],
        "name": d["name"],
        "body_type": d["body_type"],
        "subtype": d["subtype"],
        "parent_type": d["parent_type"],
        "parent_body_id": d["parent_body_id"],
        "parent_name": d["parent_name"],
        "is_landable": None if d["is_landable"] is None else bool(d["is_landable"]),
        "is_tidally_locked": None if d["is_tidally_locked"] is None else bool(d["is_tidally_locked"]),
        "radius_m": d["radius_m"],
        "gravity_ms2": d["gravity_ms2"],
        "distance_from_arrival_ls": d["distance_from_arrival_ls"],
        "rotation_period_s": d["rotation_period_s"],
        "orbital_period_s": d["orbital_period_s"],
        "semi_major_axis_m": d["semi_major_axis_m"],
        "eccentricity": d["eccentricity"],
        "orbital_inclination_deg": d["orbital_inclination_deg"],
        "periapsis_deg": d["periapsis_deg"],
        "mean_anomaly_deg": d["mean_anomaly_deg"],
        "ascending_node_deg": d["ascending_node_deg"],
        "axial_tilt_deg": d["axial_tilt_deg"],
        "tracked_for_prediction": bool(d.get("tracked_for_prediction", 0)),
        "observations": d.get("observations", 0),
        "approved": d.get("approved", 0),
        "unreviewed": d.get("unreviewed", 0),
        "active_fit_id": d.get("active_fit_id"),
        "active_fit_score": d.get("active_fit_score"),
        "provisional_fit_id": d.get("provisional_fit_id"),
        "provisional_fit_score": d.get("provisional_fit_score"),
    }


def observation_public_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = row_to_dict(row)
    out = {
        "id": d["id"],
        "system_id": d["system_id"],
        "system_name": d.get("system_name"),
        "body_id": d["body_id"],
        "body_name": d.get("body_name"),
        "observer_name": d["observer_name"],
        "timestamp_utc": d["timestamp_utc"],
        "lat": d["lat"],
        "lon": d["lon"],
        "observation": d["observation"],
        "elevation": d["elevation"],
        "heading": d["heading"],
        "quality": d["quality"],
        "note": d["note"],
        "source": d["source"],
        "review_status": d["review_status"],
        "created_at_utc": d["created_at_utc"],
        "updated_at_utc": d["updated_at_utc"],
    }
    # Optional fields returned by richer admin queries. Keeping them optional
    # makes older/simple SELECTs remain compatible.
    for key in (
        "approved_fit_id",
        "approved_altitude_error_deg",
        "approved_heading_error_deg",
        "approved_effective_weight",
        "provisional_fit_id",
        "provisional_altitude_error_deg",
        "provisional_heading_error_deg",
        "provisional_effective_weight",
    ):
        if key in d:
            out[key] = d.get(key)
    return out


def poi_public_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = row_to_dict(row)
    return {
        "id": d["id"],
        "body_id": d["body_id"],
        "body_name": d.get("body_name"),
        "system_id": d.get("system_id"),
        "system_name": d.get("system_name"),
        "name": d["name"],
        "lat": d["lat"],
        "lon": d["lon"],
        "description": d.get("description") or "",
        "is_public": bool(d.get("is_public", 1)),
        "review_status": d.get("review_status", "approved"),
        "submitter_name": d.get("submitter_name", ""),
        "reviewed_at_utc": d.get("reviewed_at_utc"),
        "reviewed_by": d.get("reviewed_by"),
        "created_at_utc": d.get("created_at_utc"),
        "updated_at_utc": d.get("updated_at_utc"),
        "created_by": d.get("created_by"),
        "updated_by": d.get("updated_by"),
    }


def get_poi_row_or_404(con: sqlite3.Connection, poi_id: int) -> sqlite3.Row:
    row = con.execute(
        """
        SELECT p.*, b.name AS body_name, b.system_id, s.name AS system_name
          FROM body_pois p
          JOIN bodies b ON b.id = p.body_id
          JOIN systems s ON s.id = b.system_id
         WHERE p.id = ?
        """,
        (poi_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"POI id not found: {poi_id}")
    return row


def insert_audit(con: sqlite3.Connection, entity_type: str, entity_id: int, action: str, old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]], actor: str = "api") -> None:
    con.execute(
        "INSERT INTO audit_log(entity_type, entity_id, action, old_json, new_json, created_at_utc, actor) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, action, None if old is None else dbmod.json_dumps(old), None if new is None else dbmod.json_dumps(new), dbmod.utc_now(), actor),
    )


# ------------------------------ endpoints ------------------------------

@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "name": "Elite Dangerous Day/Night Calculator API",
        "version": "0.191",
        "db_path": DB_PATH,
        "docs": "/docs",
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    con = connect()
    try:
        systems = con.execute("SELECT COUNT(*) AS c FROM systems").fetchone()["c"]
        bodies = con.execute("SELECT COUNT(*) AS c FROM bodies").fetchone()["c"]
        observations = con.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
        fits = con.execute("SELECT COUNT(*) AS c FROM fits").fetchone()["c"]
    finally:
        con.close()
    return {
        "ok": True,
        "db_path": DB_PATH,
        "sqlite_timeout_seconds": SQLITE_TIMEOUT_SECONDS,
        "systems": systems,
        "bodies": bodies,
        "observations": observations,
        "fits": fits,
    }


@app.get("/api/summary")
def summary() -> Dict[str, Any]:
    con = connect()
    try:
        counts = {table: con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] for table in ("systems", "bodies", "observations", "fits", "fit_observations")}
        observed_count = con.execute("SELECT COUNT(DISTINCT body_id) AS c FROM observations").fetchone()["c"]
        tracked_count = con.execute("SELECT COUNT(*) AS c FROM bodies WHERE tracked_for_prediction = 1").fetchone()["c"]
        rows = con.execute(
            """
            WITH obs AS (
                SELECT body_id, COUNT(*) AS observations,
                       SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved,
                       SUM(CASE WHEN review_status IN ('new','needs_check') THEN 1 ELSE 0 END) AS unreviewed
                  FROM observations GROUP BY body_id
            ), active_approved AS (
                SELECT * FROM fits WHERE is_active = 1 AND fit_mode = 'approved'
            ), active_provisional AS (
                SELECT * FROM fits WHERE is_active = 1 AND fit_mode = 'provisional'
            )
            SELECT s.name AS system_name, b.id AS body_pk, b.name AS body_name,
                   COALESCE(obs.observations, 0) AS observations,
                   COALESCE(obs.approved, 0) AS approved,
                   COALESCE(obs.unreviewed, 0) AS unreviewed,
                   b.tracked_for_prediction,
                   active_approved.id AS active_fit_id,
                   active_approved.fit_score AS active_fit_score,
                   active_provisional.id AS provisional_fit_id,
                   active_provisional.fit_score AS provisional_fit_score
              FROM bodies b JOIN systems s ON s.id = b.system_id
              LEFT JOIN obs ON obs.body_id = b.id
              LEFT JOIN active_approved ON active_approved.body_id = b.id
              LEFT JOIN active_provisional ON active_provisional.body_id = b.id
             WHERE COALESCE(obs.observations, 0) > 0 OR b.tracked_for_prediction = 1
             ORDER BY s.name, b.name
            """
        ).fetchall()
    finally:
        con.close()
    return {"counts": counts, "observed_body_count": observed_count, "tracked_body_count": tracked_count, "tracked_bodies": [row_to_dict(r) for r in rows], "bodies_with_observations": [row_to_dict(r) for r in rows if int(r["observations"] or 0) > 0]}


@app.get("/api/systems/search")
def search_systems(q: str = Query("", description="Local database search"), limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    con = connect()
    try:
        if q.strip():
            rows = con.execute(
                "SELECT * FROM systems WHERE lower(name) LIKE lower(?) OR CAST(system_address AS TEXT) = ? ORDER BY name LIMIT ?",
                (f"%{q.strip()}%", q.strip(), limit),
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM systems ORDER BY name LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return {"results": [row_to_dict(r) for r in rows]}


@app.get("/api/spansh/search")
def spansh_search(system_name: str, limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    try:
        results = dbmod.spansh_search_systems(system_name, limit)
        return {"results": results}
    except Exception as exc:
        raise http_error_from_exception(exc)


@app.get("/api/spansh/resolve")
def spansh_resolve(system_name: str) -> Dict[str, Any]:
    try:
        address, resolved_name, candidates = dbmod.resolve_spansh_system_address(system_name)
        return {"system_name": resolved_name, "system_address": address, "spansh_candidates": candidates}
    except Exception as exc:
        raise http_error_from_exception(exc)


@app.post("/api/systems/import")
def import_spansh(req: SpanshImportRequest) -> Dict[str, Any]:
    try:
        with WRITE_LOCK:
            system_id, body_pk, matched_body_name = dbmod.import_spansh_system(
                DB_PATH,
                req.system_name,
                req.body_name,
                req.system_address,
                fetch_body_details=req.fetch_body_details,
            )
            con_track = connect_write()
            try:
                con_track.execute("UPDATE bodies SET tracked_for_prediction = 1, updated_at_utc = ? WHERE id = ?", (dbmod.utc_now(), body_pk))
                con_track.commit()
            finally:
                con_track.close()
            fit_id = None
            if req.fit:
                fit_id = dbmod.fit_body(DB_PATH, req.system_name, matched_body_name, req.use_heading, req.time_weighting, req.time_half_life_hours, req.include_unreviewed)
    except Exception as exc:
        raise http_error_from_exception(exc)

    con = connect()
    try:
        system = row_to_dict(get_system_row_or_404(con, system_id))
        body = body_public_dict(get_body_row_or_404(con, body_pk))
    finally:
        con.close()
    return {"system": system, "selected_body": body, "matched_body_name": matched_body_name, "fit_id": fit_id}


@app.get("/api/systems/{system_id}")
def get_system(system_id: int) -> Dict[str, Any]:
    con = connect()
    try:
        system = row_to_dict(get_system_row_or_404(con, system_id))
        body_count = con.execute("SELECT COUNT(*) AS c FROM bodies WHERE system_id = ?", (system_id,)).fetchone()["c"]
    finally:
        con.close()
    system["body_count"] = body_count
    return system


@app.get("/api/systems/by-address/{system_address}")
def get_system_by_address(system_address: int) -> Dict[str, Any]:
    con = connect()
    try:
        row = con.execute("SELECT * FROM systems WHERE system_address = ?", (system_address,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"System address not found: {system_address}")
        system = row_to_dict(row)
    finally:
        con.close()
    return system


@app.get("/api/systems/{system_id}/bodies")
def get_bodies(system_id: int, only_landable: bool = False) -> Dict[str, Any]:
    con = connect()
    try:
        get_system_row_or_404(con, system_id)
        sql = """
            WITH obs AS (
                SELECT body_id, COUNT(*) AS observations,
                       SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved,
                       SUM(CASE WHEN review_status IN ('new','needs_check') THEN 1 ELSE 0 END) AS unreviewed
                  FROM observations GROUP BY body_id
            ), active_approved AS (
                SELECT * FROM fits WHERE is_active = 1 AND fit_mode = 'approved'
            ), active_provisional AS (
                SELECT * FROM fits WHERE is_active = 1 AND fit_mode = 'provisional'
            )
            SELECT b.*, s.name AS system_name, s.system_address,
                   COALESCE(obs.observations, 0) AS observations,
                   COALESCE(obs.approved, 0) AS approved,
                   COALESCE(obs.unreviewed, 0) AS unreviewed,
                   active_approved.id AS active_fit_id,
                   active_approved.fit_score AS active_fit_score,
                   active_provisional.id AS provisional_fit_id,
                   active_provisional.fit_score AS provisional_fit_score
              FROM bodies b JOIN systems s ON s.id = b.system_id
              LEFT JOIN obs ON obs.body_id = b.id
              LEFT JOIN active_approved ON active_approved.body_id = b.id
              LEFT JOIN active_provisional ON active_provisional.body_id = b.id
             WHERE b.system_id = ?
        """
        params: List[Any] = [system_id]
        if only_landable:
            sql += " AND b.is_landable = 1"
        sql += " ORDER BY COALESCE(b.body_id, 999999), b.name"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return {"results": [body_public_dict(r) for r in rows]}


@app.get("/api/bodies/{body_id}")
def get_body(body_id: int) -> Dict[str, Any]:
    con = connect()
    try:
        body = body_public_dict(get_body_row_or_404(con, body_id))
        obs_counts = con.execute(
            "SELECT review_status, COUNT(*) AS c FROM observations WHERE body_id = ? GROUP BY review_status",
            (body_id,),
        ).fetchall()
        approved_fit = con.execute("SELECT * FROM fits WHERE body_id = ? AND fit_mode = 'approved' AND is_active = 1 ORDER BY id DESC LIMIT 1", (body_id,)).fetchone()
        provisional_fit = con.execute("SELECT * FROM fits WHERE body_id = ? AND fit_mode = 'provisional' AND is_active = 1 ORDER BY id DESC LIMIT 1", (body_id,)).fetchone()
    finally:
        con.close()
    body["observation_counts"] = {r["review_status"]: r["c"] for r in obs_counts}
    body["active_fit"] = None if approved_fit is None else row_to_dict(approved_fit)
    body["approved_fit"] = None if approved_fit is None else row_to_dict(approved_fit)
    body["provisional_fit"] = None if provisional_fit is None else row_to_dict(provisional_fit)
    return body


@app.get("/api/bodies/{body_id}/fit")
def get_fit(body_id: int, include_residuals: bool = True, model_mode: str = "approved") -> Dict[str, Any]:
    con = connect()
    try:
        get_body_row_or_404(con, body_id)
        mode = (model_mode or "approved").strip().lower()
        if mode not in {"approved", "provisional"}:
            raise HTTPException(status_code=400, detail="model_mode must be approved or provisional")
        fit = con.execute("SELECT * FROM fits WHERE body_id = ? AND fit_mode = ? AND is_active = 1 AND fit_status = 'ok' ORDER BY id DESC LIMIT 1", (body_id, mode)).fetchone()
        if not fit:
            raise HTTPException(status_code=404, detail=f"No active {mode} fit for this body")
        data = row_to_dict(fit)
        # Parse params_json safely without mutating compatibility fields.
        import json as _json
        data["params"] = _json.loads(fit["params_json"]) if fit["params_json"] else None
        data.update(fit_review_metadata(con, int(fit["id"])))
        if include_residuals:
            rows = con.execute(
                """
                SELECT fo.*, o.timestamp_utc, o.lat, o.lon, o.observation, o.elevation, o.heading, o.quality, o.note, o.review_status
                  FROM fit_observations fo JOIN observations o ON o.id = fo.observation_id
                 WHERE fo.fit_id = ? ORDER BY o.timestamp_utc
                """,
                (fit["id"],),
            ).fetchall()
            data["residuals"] = [row_to_dict(r) for r in rows]
    finally:
        con.close()
    return data


@app.get("/api/bodies/{body_id}/prediction")
def predict(body_id: int, lat: float, lon: float, time: str, prediction_hours: float = Query(72.0, ge=1.0, le=168.0), model_mode: str = "approved") -> Dict[str, Any]:
    con = connect()
    try:
        get_body_row_or_404(con, body_id)
        mode = (model_mode or "approved").strip().lower()
        if mode not in {"approved", "provisional"}:
            raise HTTPException(status_code=400, detail="model_mode must be approved or provisional")
        system, fitted, fit_id = dbmod.model_from_active_fit(con, body_id, fit_mode=mode)
        prediction = model.calculate_prediction(fitted, system, model.parse_utc(time), lat, lon, prediction_hours=prediction_hours)
        prediction["model_mode"] = mode
        prediction.update(fit_review_metadata(con, int(fit_id)))
        with WRITE_LOCK:
            begin_write_transaction(con)
            con.execute(
                "INSERT INTO prediction_cache(body_id, fit_id, lat, lon, target_time_utc, prediction_json, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (body_id, fit_id, lat, lon, prediction["target_time_utc"], dbmod.json_dumps(prediction), dbmod.utc_now()),
            )
            con.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error_from_exception(exc)
    finally:
        con.close()
    prediction["fit_id"] = fit_id
    return prediction


@app.post("/api/bodies/{body_id}/observations")
def add_observation(body_id: int, req: ObservationCreate) -> Dict[str, Any]:
    if req.observation in {"elevation", "altitude", "sun_altitude", "alt"} and req.elevation is None:
        raise HTTPException(status_code=400, detail="Elevation observations need an elevation value")
    try:
        ts = model.format_utc(model.parse_utc(req.timestamp_utc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Bad timestamp_utc: {exc}")

    with WRITE_LOCK:
        con = connect_write()
        try:
            body = get_body_row_or_404(con, body_id)
            system_id = int(body["system_id"])
            norm = {"timestamp_utc": ts, "lat": req.lat, "lon": req.lon, "observation": req.observation, "elevation": req.elevation, "heading": req.heading}
            ohash = dbmod.observation_hash(system_id, body_id, norm)
            now = dbmod.utc_now()
            con.execute(
                """
                INSERT INTO observations(
                    obs_hash, system_id, body_id, observer_name, timestamp_utc, lat, lon,
                    observation, elevation, heading, quality, note, source,
                    target_type, review_status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sun', ?, ?, ?)
                ON CONFLICT(obs_hash) DO UPDATE SET
                    observer_name = excluded.observer_name,
                    quality = excluded.quality,
                    note = excluded.note,
                    source = excluded.source,
                    review_status = excluded.review_status,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (ohash, system_id, body_id, req.observer_name, ts, req.lat, req.lon, req.observation,
                 req.elevation, req.heading, req.quality, req.note, req.source, req.review_status, now, now),
            )
            row = con.execute(
                """
                SELECT o.*, s.name AS system_name, b.name AS body_name
                  FROM observations o JOIN systems s ON s.id = o.system_id JOIN bodies b ON b.id = o.body_id
                 WHERE o.obs_hash = ?
                """,
                (ohash,),
            ).fetchone()
            insert_audit(con, "observation", int(row["id"]), "api_upsert_observation", None, observation_public_dict(row), req.observer_name or "api")
            con.commit()
            return observation_public_dict(row)
        except sqlite3.OperationalError as exc:
            raise http_error_from_exception(exc)
        finally:
            con.close()


@app.get("/api/bodies/{body_id}/pois")
def list_body_pois(body_id: int, public_only: bool = True) -> Dict[str, Any]:
    con = connect()
    try:
        get_body_row_or_404(con, body_id)
        clauses = ["p.body_id = ?"]
        params: List[Any] = [body_id]
        if public_only:
            clauses.append("p.is_public = 1")
            clauses.append("p.review_status = 'approved'")
        rows = con.execute(
            f"""
            SELECT p.*, b.name AS body_name, b.system_id, s.name AS system_name
              FROM body_pois p
              JOIN bodies b ON b.id = p.body_id
              JOIN systems s ON s.id = b.system_id
             WHERE {' AND '.join(clauses)}
             ORDER BY lower(p.name), p.id
            """,
            params,
        ).fetchall()
    finally:
        con.close()
    return {"results": [poi_public_dict(r) for r in rows]}


@app.get("/api/pois/{poi_id}")
def get_poi(poi_id: int, public_only: bool = True) -> Dict[str, Any]:
    con = connect()
    try:
        row = get_poi_row_or_404(con, poi_id)
        if public_only and (not bool(row["is_public"]) or str(row["review_status"]) != "approved"):
            raise HTTPException(status_code=404, detail="POI not found")
        return poi_public_dict(row)
    finally:
        con.close()


@app.get("/api/admin/pois")
def list_admin_pois(
    body_name: Optional[str] = None,
    system_id: Optional[int] = None,
    review_status: Optional[str] = None,
    public_only: Optional[bool] = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    con = connect()
    try:
        clauses: List[str] = []
        params: List[Any] = []
        if body_name:
            clauses.append("lower(b.name) LIKE lower(?)")
            params.append(f"%{body_name.strip()}%")
        if system_id is not None:
            clauses.append("b.system_id = ?")
            params.append(system_id)
        if review_status:
            clauses.append("p.review_status = ?")
            params.append(review_status.strip().lower())
        if public_only is not None:
            clauses.append("p.is_public = ?")
            params.append(1 if public_only else 0)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = con.execute(
            f"""
            SELECT p.*, b.name AS body_name, b.system_id, s.name AS system_name
              FROM body_pois p
              JOIN bodies b ON b.id = p.body_id
              JOIN systems s ON s.id = b.system_id
              {where}
             ORDER BY s.name, b.name, lower(p.name)
             LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
    finally:
        con.close()
    return {"results": [poi_public_dict(r) for r in rows], "limit": limit, "offset": offset}


@app.post("/api/bodies/{body_id}/pois/submit")
def submit_public_poi(body_id: int, req: PoiCreate) -> Dict[str, Any]:
    """Public POI submission. Saved for review and hidden until approved."""
    if not PUBLIC_POI_SUBMISSIONS_ENABLED:
        raise HTTPException(status_code=403, detail="Public POI submissions are currently disabled")
    with WRITE_LOCK:
        con = connect_write()
        try:
            get_body_row_or_404(con, body_id)
            submitter = req.submitter_name.strip() or req.actor.strip() or "public"
            if len(submitter) < 2:
                raise HTTPException(status_code=400, detail="submitter_name is required")
            now = dbmod.utc_now()
            cur = con.execute(
                """
                INSERT INTO body_pois(body_id, name, lat, lon, description, is_public, review_status, submitter_name, created_at_utc, updated_at_utc, created_by, updated_by)
                VALUES (?, ?, ?, ?, ?, 0, 'new', ?, ?, ?, ?, ?)
                """,
                (body_id, req.name.strip(), req.lat, req.lon, req.description.strip(), submitter, now, now, submitter, submitter),
            )
            poi_id = int(cur.lastrowid)
            row = get_poi_row_or_404(con, poi_id)
            insert_audit(con, "poi", poi_id, "api_submit_public_poi", None, poi_public_dict(row), submitter)
            con.commit()
            return poi_public_dict(row)
        except sqlite3.OperationalError as exc:
            raise http_error_from_exception(exc)
        finally:
            con.close()


@app.post("/api/admin/bodies/{body_id}/pois")
def create_poi(body_id: int, req: PoiCreate) -> Dict[str, Any]:
    with WRITE_LOCK:
        con = connect_write()
        try:
            get_body_row_or_404(con, body_id)
            now = dbmod.utc_now()
            cur = con.execute(
                """
                INSERT INTO body_pois(body_id, name, lat, lon, description, is_public, review_status, submitter_name, reviewed_at_utc, reviewed_by, created_at_utc, updated_at_utc, created_by, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (body_id, req.name.strip(), req.lat, req.lon, req.description.strip(), 1 if req.is_public else 0, req.review_status, req.submitter_name.strip(), now if req.review_status == "approved" else None, req.actor if req.review_status == "approved" else None, now, now, req.actor, req.actor),
            )
            poi_id = int(cur.lastrowid)
            row = get_poi_row_or_404(con, poi_id)
            insert_audit(con, "poi", poi_id, "api_create_poi", None, poi_public_dict(row), req.actor)
            con.commit()
            return poi_public_dict(row)
        finally:
            con.close()


@app.patch("/api/admin/pois/{poi_id}")
def patch_poi(poi_id: int, req: PoiPatch) -> Dict[str, Any]:
    with WRITE_LOCK:
        con = connect_write()
        try:
            old = get_poi_row_or_404(con, poi_id)
            values = row_to_dict(old)
            patch = req.dict(exclude_unset=True)
            actor = str(patch.pop("actor", "website-admin") or "website-admin")
            for key in ("name", "description"):
                if key in patch and patch[key] is not None:
                    patch[key] = str(patch[key]).strip()
            if "lat" in patch and patch["lat"] is not None and (patch["lat"] < -90 or patch["lat"] > 90):
                raise HTTPException(status_code=400, detail="lat must be between -90 and 90")
            if "lon" in patch and patch["lon"] is not None and (patch["lon"] < -180 or patch["lon"] > 180):
                raise HTTPException(status_code=400, detail="lon must be between -180 and 180")
            if "review_status" in patch and patch["review_status"] is not None:
                status = str(patch["review_status"]).strip().lower()
                if status not in ALLOWED_REVIEW and status != "hidden":
                    raise HTTPException(status_code=400, detail="invalid POI review_status")
                patch["review_status"] = status
            for key, val in patch.items():
                if key in {"name", "lat", "lon", "description", "is_public", "review_status", "submitter_name"}:
                    values[key] = val
            now = dbmod.utc_now()
            reviewed_at = values.get("reviewed_at_utc")
            reviewed_by = values.get("reviewed_by")
            if values.get("review_status") in {"approved", "rejected", "hidden"}:
                reviewed_at = now
                reviewed_by = actor
            con.execute(
                """
                UPDATE body_pois
                   SET name = ?, lat = ?, lon = ?, description = ?, is_public = ?, review_status = ?, submitter_name = ?, reviewed_at_utc = ?, reviewed_by = ?, updated_at_utc = ?, updated_by = ?
                 WHERE id = ?
                """,
                (values["name"], values["lat"], values["lon"], values.get("description") or "", 1 if values.get("is_public") else 0, values.get("review_status") or "approved", values.get("submitter_name") or "", reviewed_at, reviewed_by, now, actor, poi_id),
            )
            row = get_poi_row_or_404(con, poi_id)
            insert_audit(con, "poi", poi_id, "api_patch_poi", poi_public_dict(old), poi_public_dict(row), actor)
            con.commit()
            return poi_public_dict(row)
        finally:
            con.close()


@app.delete("/api/admin/pois/{poi_id}")
def delete_poi(poi_id: int, actor: str = "website-admin") -> Dict[str, Any]:
    with WRITE_LOCK:
        con = connect_write()
        try:
            row = get_poi_row_or_404(con, poi_id)
            old = poi_public_dict(row)
            con.execute("DELETE FROM body_pois WHERE id = ?", (poi_id,))
            insert_audit(con, "poi", poi_id, "api_delete_poi", old, None, actor)
            con.commit()
            return {"deleted": True, "poi_id": poi_id}
        finally:
            con.close()


@app.get("/api/admin/observations")
def list_observations(
    status: Optional[str] = Query(None, description="new/approved/rejected/needs_check/corrected"),
    body_id: Optional[int] = None,
    body_name: Optional[str] = None,
    system_id: Optional[int] = None,
    system_name: Optional[str] = None,
    observer_name: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    con = connect()
    try:
        clauses = []
        params: List[Any] = []
        if status and str(status).strip().lower() != "all":
            clauses.append("o.review_status = ?")
            params.append(status)
        if body_id is not None:
            clauses.append("o.body_id = ?")
            params.append(body_id)
        if body_name:
            clauses.append("lower(b.name) LIKE lower(?)")
            params.append(f"%{body_name.strip()}%")
        if system_id is not None:
            clauses.append("o.system_id = ?")
            params.append(system_id)
        if system_name:
            clauses.append("lower(s.name) LIKE lower(?)")
            params.append(f"%{system_name.strip()}%")
        if observer_name:
            clauses.append("lower(coalesce(o.observer_name, '')) LIKE lower(?)")
            params.append(f"%{observer_name.strip()}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = con.execute(
            f"""
            SELECT o.*, s.name AS system_name, b.name AS body_name,
                   af.id AS approved_fit_id,
                   afo.altitude_error_deg AS approved_altitude_error_deg,
                   afo.heading_error_deg AS approved_heading_error_deg,
                   afo.effective_weight AS approved_effective_weight,
                   pf.id AS provisional_fit_id,
                   pfo.altitude_error_deg AS provisional_altitude_error_deg,
                   pfo.heading_error_deg AS provisional_heading_error_deg,
                   pfo.effective_weight AS provisional_effective_weight
              FROM observations o
              JOIN systems s ON s.id = o.system_id
              JOIN bodies b ON b.id = o.body_id
              LEFT JOIN fits af ON af.id = (
                    SELECT id FROM fits
                     WHERE body_id = o.body_id
                       AND fit_mode = 'approved'
                       AND is_active = 1
                       AND fit_status = 'ok'
                     ORDER BY id DESC LIMIT 1
              )
              LEFT JOIN fit_observations afo ON afo.fit_id = af.id AND afo.observation_id = o.id
              LEFT JOIN fits pf ON pf.id = (
                    SELECT id FROM fits
                     WHERE body_id = o.body_id
                       AND fit_mode = 'provisional'
                       AND is_active = 1
                       AND fit_status = 'ok'
                     ORDER BY id DESC LIMIT 1
              )
              LEFT JOIN fit_observations pfo ON pfo.fit_id = pf.id AND pfo.observation_id = o.id
              {where}
             ORDER BY o.created_at_utc DESC, o.id DESC LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
    finally:
        con.close()
    return {"results": [observation_public_dict(r) for r in rows], "limit": limit, "offset": offset}


@app.get("/api/admin/observations/{observation_id}")
def get_admin_observation(observation_id: int) -> Dict[str, Any]:
    con = connect()
    try:
        row = con.execute(
            """
            SELECT o.*, s.name AS system_name, b.name AS body_name
              FROM observations o JOIN systems s ON s.id = o.system_id JOIN bodies b ON b.id = o.body_id
             WHERE o.id = ?
            """,
            (observation_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Observation not found")
        return observation_public_dict(row)
    finally:
        con.close()


@app.patch("/api/admin/observations/{observation_id}")
def patch_observation(observation_id: int, req: ObservationPatch) -> Dict[str, Any]:
    with WRITE_LOCK:
        con = connect_write()
        try:
            old = con.execute(
                """
                SELECT o.*, s.name AS system_name, b.name AS body_name
                  FROM observations o JOIN systems s ON s.id = o.system_id JOIN bodies b ON b.id = o.body_id
                 WHERE o.id = ?
                """,
                (observation_id,),
            ).fetchone()
            if not old:
                raise HTTPException(status_code=404, detail="Observation not found")
            values = row_to_dict(old)
            patch = req.dict(exclude_unset=True)
            if "timestamp_utc" in patch:
                patch["timestamp_utc"] = model.format_utc(model.parse_utc(str(patch["timestamp_utc"])))
            if "observation" in patch:
                patch["observation"] = str(patch["observation"]).strip().lower()
                if patch["observation"] not in ALLOWED_OBSERVATIONS:
                    raise HTTPException(status_code=400, detail="unsupported observation type")
            if "quality" in patch:
                patch["quality"] = str(patch["quality"]).strip().lower()
                if patch["quality"] not in ALLOWED_QUALITY:
                    raise HTTPException(status_code=400, detail="quality must be high, medium, or low")
            if "review_status" in patch:
                patch["review_status"] = str(patch["review_status"]).strip().lower()
                if patch["review_status"] not in ALLOWED_REVIEW:
                    raise HTTPException(status_code=400, detail="invalid review_status")
            for key, val in patch.items():
                values[key] = val
            if values["lat"] < -90 or values["lat"] > 90 or values["lon"] < -180 or values["lon"] > 180:
                raise HTTPException(status_code=400, detail="lat/lon out of range")
            if values.get("heading") is not None and (values["heading"] < 0 or values["heading"] >= 360):
                raise HTTPException(status_code=400, detail="heading out of range")
            if values["observation"] in {"elevation", "altitude", "sun_altitude", "alt"} and values.get("elevation") is None:
                raise HTTPException(status_code=400, detail="Elevation observations need an elevation value")
            new_hash = dbmod.observation_hash(int(values["system_id"]), int(values["body_id"]), values)
            con.execute(
                """
                UPDATE observations
                   SET obs_hash = ?, observer_name = ?, timestamp_utc = ?, lat = ?, lon = ?, observation = ?,
                       elevation = ?, heading = ?, quality = ?, note = ?, review_status = ?, updated_at_utc = ?
                 WHERE id = ?
                """,
                (new_hash, values.get("observer_name"), values["timestamp_utc"], values["lat"], values["lon"], values["observation"],
                 values.get("elevation"), values.get("heading"), values.get("quality"), values.get("note"), values.get("review_status"),
                 dbmod.utc_now(), observation_id),
            )
            row = con.execute(
                """
                SELECT o.*, s.name AS system_name, b.name AS body_name
                  FROM observations o JOIN systems s ON s.id = o.system_id JOIN bodies b ON b.id = o.body_id
                 WHERE o.id = ?
                """,
                (observation_id,),
            ).fetchone()
            insert_audit(con, "observation", observation_id, "api_patch_observation", observation_public_dict(old), observation_public_dict(row), "api-admin")
            con.commit()
            return observation_public_dict(row)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"Patch conflicts with an existing observation: {exc}")
        finally:
            con.close()


@app.post("/api/admin/observations/{observation_id}/review")
def set_observation_review(observation_id: int, req: ReviewStatusUpdate) -> Dict[str, Any]:
    with WRITE_LOCK:
        con = connect_write()
        try:
            old = con.execute("SELECT * FROM observations WHERE id = ?", (observation_id,)).fetchone()
            if not old:
                raise HTTPException(status_code=404, detail="Observation not found")
            con.execute("UPDATE observations SET review_status = ?, updated_at_utc = ? WHERE id = ?", (req.review_status, dbmod.utc_now(), observation_id))
            row = con.execute(
                """
                SELECT o.*, s.name AS system_name, b.name AS body_name
                  FROM observations o JOIN systems s ON s.id = o.system_id JOIN bodies b ON b.id = o.body_id
                 WHERE o.id = ?
                """,
                (observation_id,),
            ).fetchone()
            insert_audit(con, "observation", observation_id, f"set_review_{req.review_status}", row_to_dict(old), observation_public_dict(row), req.actor)
            con.commit()
            return observation_public_dict(row)
        finally:
            con.close()


@app.post("/api/admin/bodies/{body_id}/refit")
def refit_body(body_id: int, req: RefitRequest) -> Dict[str, Any]:
    con = connect()
    try:
        body = get_body_row_or_404(con, body_id)
        system_name = body["system_name"]
        body_name = body["name"]
    finally:
        con.close()
    try:
        with WRITE_LOCK:
            fit_id = dbmod.fit_body(DB_PATH, system_name, body_name, req.use_heading, req.time_weighting, req.time_half_life_hours, req.include_unreviewed, force_refit=req.force_refit)
    except Exception as exc:
        raise http_error_from_exception(exc)
    con = connect()
    try:
        fit = con.execute("SELECT * FROM fits WHERE id = ?", (fit_id,)).fetchone()
    finally:
        con.close()
    return row_to_dict(fit)


@app.delete("/api/admin/observations/{observation_id}")
def delete_observation(observation_id: int, actor: str = "api-admin") -> Dict[str, Any]:
    """Hard-delete an observation. Prefer review_status='rejected' for traceability."""
    with WRITE_LOCK:
        con = connect_write()
        try:
            row = con.execute("SELECT * FROM observations WHERE id = ?", (observation_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Observation not found")
            old = row_to_dict(row)
            con.execute("DELETE FROM observations WHERE id = ?", (observation_id,))
            insert_audit(con, "observation", observation_id, "api_delete_observation", old, None, actor)
            con.commit()
            return {"deleted": True, "observation_id": observation_id}
        finally:
            con.close()


@app.get("/api/admin/audit")
def list_audit_log(
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    con = connect()
    try:
        clauses = []
        params: List[Any] = []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type.strip())
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if actor:
            clauses.append("lower(actor) LIKE lower(?)")
            params.append(f"%{actor.strip()}%")
        if action:
            clauses.append("lower(action) LIKE lower(?)")
            params.append(f"%{action.strip()}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = con.execute(
            f"""
            SELECT id, entity_type, entity_id, action, old_json, new_json, created_at_utc, actor
              FROM audit_log
              {where}
             ORDER BY created_at_utc DESC, id DESC
             LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        results = []
        for row in rows:
            item = row_to_dict(row)
            for key in ("old_json", "new_json"):
                raw = item.get(key)
                if raw:
                    try:
                        item[key.replace("_json", "")] = json.loads(raw)
                    except Exception:
                        item[key.replace("_json", "")] = raw
                else:
                    item[key.replace("_json", "")] = None
            results.append(item)
        return {"results": results, "limit": limit, "offset": offset}
    finally:
        con.close()
