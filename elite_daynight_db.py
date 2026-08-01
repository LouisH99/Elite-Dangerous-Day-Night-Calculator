#!/usr/bin/env python3
"""
Elite Dangerous Day/Night Calculator - compact local SQLite database and Spansh importer

This is the local database prototype for the v16 model split.
It intentionally stores only the fields needed for:
  * system/body selection
  * day/night fitting and prediction
  * observation review and residual auditing
  * later parent/neighbor modelling

It does NOT store full source/Spansh JSON, journal events, factions,
materials, station data, raw Status.json snapshots, or full raw body blobs.

Keep this file in the same folder as elite_daynight_model_v16.py.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


SQLITE_TIMEOUT_SECONDS = float(os.environ.get("ELITE_DAYNIGHT_SQLITE_TIMEOUT", "10"))
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_TIMEOUT_SECONDS * 1000)
# Number of inactive old fits to keep per body after creating a new active fit.
# Default 0 keeps the database tidy; set e.g. ELITE_DAYNIGHT_KEEP_INACTIVE_FITS=3
# if you want a small fit history while testing.
KEEP_INACTIVE_FITS = int(os.environ.get("ELITE_DAYNIGHT_KEEP_INACTIVE_FITS", "0"))
DB_WRITE_RETRIES = int(os.environ.get("ELITE_DAYNIGHT_DB_WRITE_RETRIES", "5"))
DB_WRITE_RETRY_BASE_SECONDS = float(os.environ.get("ELITE_DAYNIGHT_DB_WRITE_RETRY_BASE_SECONDS", "0.08"))
DB_WRITE_LOCK = threading.RLock()


def sqlite_connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return con



def begin_write_transaction(con: sqlite3.Connection) -> None:
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

try:
    import elite_daynight_model_v16 as model
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import elite_daynight_model_v16.py. Put this script in the same folder as the model module.\n"
        f"Import error: {exc}"
    ) from exc

SCHEMA_VERSION = 5
MODEL_VERSION = "v16"

SPANSH_API_BASE = "https://www.spansh.co.uk/api"
EDSM_SYSTEM_API_BASE = "https://www.edsm.net/api-v1"
EDSM_API_SYSTEM_BASE = "https://www.edsm.net/api-system-v1"
AU_METRES = 149_597_870_700.0
DAY_SECONDS = 86_400.0
KM_METRES = 1_000.0
STANDARD_GRAVITY = 9.80665


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def parse_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def get_any(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


def as_bool_int(value: Any) -> Optional[int]:
    if value is True:
        return 1
    if value is False:
        return 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "yes", "1"}:
            return 1
        if v in {"false", "no", "0"}:
            return 0
    return None


def body_name_from_json(body: Dict[str, Any]) -> str:
    return str(get_any(body, "BodyName", "bodyName", "Name", "name", default="")).strip()


def body_type_from_json(body: Dict[str, Any]) -> str:
    return str(get_any(body, "BodyType", "type", default="")).strip()


def body_subtype_from_json(body: Dict[str, Any]) -> str:
    return str(get_any(body, "SubType", "subType", "PlanetClass", "StarType", default="")).strip()


def system_name_from_json(system: Dict[str, Any]) -> str:
    return str(get_any(system, "name", "Name", "systemName", "StarSystem", default="")).strip()


def system_address_from_json(system: Dict[str, Any]) -> Optional[int]:
    return parse_optional_int(get_any(system, "systemAddress", "SystemAddress", "id64", default=None))


def bodies_from_system(system: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(system.get("bodies"), list):
        return system["bodies"]
    if isinstance(system.get("Bodies"), list):
        return system["Bodies"]
    return []


def parent_refs(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = body.get("Parents") or body.get("parents") or []
    return p if isinstance(p, list) else []


def first_parent_info(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    refs = parent_refs(body)
    if not refs or not isinstance(refs[0], dict):
        return None, None
    p = refs[0]
    for key in ("Star", "Planet", "Null"):
        if key in p:
            return key, parse_optional_int(p.get(key))
    ptype = p.get("type") or p.get("Type") or p.get("BodyType")
    return str(ptype) if ptype else None, parse_optional_int(p.get("BodyID") or p.get("bodyId") or p.get("id64") or p.get("id"))


def relevant_bodies(system: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Keep only real stars/planets. Skip belt clusters and non-orbital helper bodies."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for b in bodies_from_system(system):
        typ = body_type_from_json(b).lower()
        if typ not in {"star", "planet"}:
            continue
        name = body_name_from_json(b)
        bid = parse_optional_int(get_any(b, "BodyID", "bodyId", "body_id", default=None))
        key = (bid, name.lower())
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


# ------------------------------ Spansh import ------------------------------

class SpanshImportError(RuntimeError):
    pass


def http_get_json(url: str, timeout: float = 30.0, retries: int = 2, pause: float = 0.6) -> Any:
    """Small stdlib-only JSON GET helper.

    Spansh can be briefly busy, so we retry transient HTTP/network failures a
    couple of times. This avoids adding requests/aiohttp as dependencies.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EliteDayNightDB/0.199"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            # Do not retry obvious not-found/invalid requests.
            if isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 401, 403, 404}:
                break
            if attempt < retries:
                time.sleep(pause * (attempt + 1))
    raise SpanshImportError(f"GET failed: {url}\n{last_error}")


def unwrap_spansh_record(payload: Any) -> Dict[str, Any]:
    """Spansh endpoints usually return {record: {...}}, but keep this robust."""
    if isinstance(payload, dict):
        rec = payload.get("record") or payload.get("system") or payload.get("body")
        if isinstance(rec, dict):
            return rec
        return payload
    raise SpanshImportError(f"Unexpected Spansh payload type: {type(payload).__name__}")


def spansh_search_systems(query: str, limit: int = 20) -> List[Any]:
    url = f"{SPANSH_API_BASE}/systems?q={urllib.parse.quote_plus(query)}"
    data = http_get_json(url)
    if isinstance(data, list):
        return data[:limit]
    if isinstance(data, dict):
        for key in ("results", "systems", "matches", "records"):
            if isinstance(data.get(key), list):
                return data[key][:limit]
    raise SpanshImportError(f"Unexpected /api/systems?q= response: {type(data).__name__}")


def edsm_resolve_system_address(system_name: str) -> Tuple[Optional[int], str]:
    """Resolve a system name to the game system address/id64 using EDSM.

    Spansh full-system lookup needs /api/system/{id64}. Spansh autocomplete
    sometimes returns only names, so this is a fallback resolver only; the
    actual system/body import still comes from Spansh after id64 is known.

    EDSM's estimated-value endpoint includes an id64 field for known systems.
    As a second fallback, the traffic endpoint also includes id64 for systems
    with traffic data.
    """
    encoded = urllib.parse.quote_plus(system_name)
    urls = [
        f"{EDSM_API_SYSTEM_BASE}/estimated-value?systemName={encoded}",
        f"{EDSM_API_SYSTEM_BASE}/traffic?systemName={encoded}",
    ]
    errors: List[str] = []
    for url in urls:
        try:
            data = http_get_json(url, retries=0)
            if isinstance(data, dict):
                # Some EDSM errors are JSON like {msgnum: 302, msg: ...}
                if data.get("msgnum") not in (None, 100):
                    errors.append(f"{url}: {data.get('msgnum')} {data.get('msg')}")
                    continue
                sid64 = parse_optional_int(data.get("id64") or data.get("systemId64") or data.get("SystemAddress"))
                name = str(data.get("name") or system_name).strip()
                if sid64 is not None:
                    return sid64, name
                errors.append(f"{url}: no id64 in response")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise SpanshImportError(
        "Could not resolve system address/id64 from Spansh autocomplete or EDSM fallback. "
        "Pass --system-address manually if you know it.\n" + "\n".join(errors)
    )


def candidate_name(candidate: Any) -> str:
    if isinstance(candidate, str):
        return candidate.strip()
    if isinstance(candidate, dict):
        return str(candidate.get("name") or candidate.get("Name") or candidate.get("system_name") or candidate.get("systemName") or "").strip()
    return ""


def candidate_address(candidate: Any) -> Optional[int]:
    if not isinstance(candidate, dict):
        return None
    return parse_optional_int(candidate.get("id64") or candidate.get("systemAddress") or candidate.get("SystemAddress") or candidate.get("address") or candidate.get("id"))


def resolve_spansh_system_address(system_name: str, explicit_address: Optional[int] = None) -> Tuple[Optional[int], str, List[Any]]:
    """Resolve a system name to id64/system address.

    Spansh full system fetch is stable by id64: /api/system/{id64}. The
    /api/systems?q= autocomplete endpoint may return either dicts with id64 or
    just names. If no id64 is present, fall back to EDSM only to resolve the
    address, then fetch the full data from Spansh.
    """
    if explicit_address is not None:
        return explicit_address, system_name, []

    candidates: List[Any] = []
    try:
        candidates = spansh_search_systems(system_name)
    except Exception:
        # Keep going; the EDSM id64 fallback may still let the Spansh import work.
        candidates = []

    exact = [c for c in candidates if candidate_name(c).lower() == system_name.strip().lower()]
    chosen = exact[0] if exact else (candidates[0] if candidates else None)
    if chosen is not None:
        addr = candidate_address(chosen)
        nm = candidate_name(chosen) or system_name
        if addr is not None:
            return addr, nm, candidates
        # If Spansh only gave a name, resolve id64 elsewhere but preserve the
        # Spansh-normalized name for display.
        addr2, nm2 = edsm_resolve_system_address(nm)
        return addr2, nm2 or nm, candidates

    addr, nm = edsm_resolve_system_address(system_name)
    return addr, nm or system_name, candidates


def fetch_spansh_system_by_address(system_address: int) -> Dict[str, Any]:
    return unwrap_spansh_record(http_get_json(f"{SPANSH_API_BASE}/system/{int(system_address)}"))


def fetch_spansh_system_by_name_fallback(system_name: str) -> Dict[str, Any]:
    """Try name-based Spansh endpoints.

    The documented/stable endpoint is /api/system/{id64}. These fallbacks are
    here so the tool can work if Spansh accepts names in the path/query.
    """
    quoted = urllib.parse.quote(system_name, safe="")
    urls = [
        f"{SPANSH_API_BASE}/system/{quoted}",
        f"{SPANSH_API_BASE}/system?name={urllib.parse.quote_plus(system_name)}",
        f"{SPANSH_API_BASE}/systems/{quoted}",
    ]
    errors: List[str] = []
    for url in urls:
        try:
            return unwrap_spansh_record(http_get_json(url, retries=0))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise SpanshImportError(
        "Could not fetch the full system by name from Spansh. "
        "Pass --system-address if /api/systems?q= only returns names on your machine.\n" + "\n".join(errors)
    )


def spansh_body_id64(system_address: Optional[int], body: Dict[str, Any]) -> Optional[int]:
    """Return Spansh body id64, deriving it from body_id when possible."""
    direct = parse_optional_int(body.get("id64") or body.get("bodyId64") or body.get("id"))
    if direct is not None:
        return direct
    bid = parse_optional_int(body.get("body_id") or body.get("bodyId") or body.get("BodyID"))
    if system_address is not None and bid is not None:
        return (int(bid) << 55) + int(system_address)
    return None


def fetch_spansh_body_detail(body_id64: int) -> Dict[str, Any]:
    return unwrap_spansh_record(http_get_json(f"{SPANSH_API_BASE}/body/{int(body_id64)}"))


def _float_or_none(value: Any) -> Optional[float]:
    return parse_optional_float(value)


def _axis_tilt_radians_to_degrees(value: Any) -> Optional[float]:
    """Convert Spansh axis_tilt values to degrees.

    Spansh body detail uses radians for ``axis_tilt`` while Journal/ED UI
    values are shown in degrees. Older imports accidentally stored the Spansh
    value directly in the ``axial_tilt_deg`` database column. Keep Journal-style
    ``AxialTilt`` values as degrees, but convert the Spansh key explicitly.
    """
    v = parse_optional_float(value)
    if v is None:
        return None
    # Be defensive for hand-made/test data: a value outside the valid radian
    # range but inside a plausible degree range is already degrees. Real Spansh
    # values should be radians here.
    if abs(v) > (2.0 * math.pi) and abs(v) <= 360.0:
        return v
    return math.degrees(v)


def _body_axial_tilt_degrees(body: Dict[str, Any]) -> Optional[float]:
    """Return axial tilt in degrees from Journal-like or Spansh-like body data."""
    direct = get_any(body, "AxialTilt", "axial_tilt_deg", default=None)
    if direct not in (None, ""):
        return parse_optional_float(direct)
    raw = body.get("rawSpanshBody") or {}
    if isinstance(raw, dict) and raw.get("axis_tilt") not in (None, ""):
        return _axis_tilt_radians_to_degrees(raw.get("axis_tilt"))
    if body.get("axis_tilt") not in (None, ""):
        return _axis_tilt_radians_to_degrees(body.get("axis_tilt"))
    return None


def _body_surface_gravity_ms2(body: Dict[str, Any]) -> Optional[float]:
    """Return surface gravity in m/s2 from Journal-like or Spansh-like body data."""
    direct = get_any(body, "SurfaceGravity", "gravity_ms2", default=None)
    if direct not in (None, ""):
        return parse_optional_float(direct)
    raw = body.get("rawSpanshBody") or {}
    if isinstance(raw, dict) and raw.get("gravity") not in (None, ""):
        value = parse_optional_float(raw.get("gravity"))
        return None if value is None else value * STANDARD_GRAVITY
    if body.get("gravity") not in (None, ""):
        value = parse_optional_float(body.get("gravity"))
        return None if value is None else value * STANDARD_GRAVITY
    return None


def _set_if_value(d: Dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        d[key] = value


def _spansh_body_name(raw: Dict[str, Any]) -> str:
    return str(raw.get("name") or raw.get("Name") or raw.get("bodyName") or raw.get("BodyName") or "").strip()


def _spansh_body_type(raw: Dict[str, Any]) -> str:
    return str(raw.get("type") or raw.get("BodyType") or "").strip().title()


def _spansh_body_subtype(raw: Dict[str, Any]) -> str:
    return str(raw.get("subtype") or raw.get("SubType") or raw.get("PlanetClass") or raw.get("StarType") or "").strip()


def parent_refs_from_spansh(raw: Dict[str, Any], id64_to_body_id: Dict[int, int]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    parents = raw.get("parents") or raw.get("Parents") or []
    if not isinstance(parents, list):
        return out
    for p in parents:
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("type") or p.get("BodyType") or "").strip().lower()
        pid64 = parse_optional_int(p.get("id64") or p.get("id") or p.get("bodyId64"))
        # Some journal-style records already contain Star/Planet/Null keys.
        for key in ("Star", "Planet", "Null"):
            if key in p:
                out.append({key: parse_optional_int(p.get(key))})
                break
        else:
            body_id_value = id64_to_body_id.get(int(pid64)) if pid64 is not None else None
            if ptype == "star":
                out.append({"Star": body_id_value if body_id_value is not None else pid64})
            elif ptype == "planet":
                out.append({"Planet": body_id_value if body_id_value is not None else pid64})
            elif ptype == "null":
                out.append({"Null": body_id_value if body_id_value is not None else pid64})
    return out


def convert_spansh_body_to_journal(raw: Dict[str, Any], system_address: Optional[int], id64_to_body_id: Dict[int, int]) -> Dict[str, Any]:
    """Convert a Spansh body record into the compact Journal-like shape.

    This keeps only fields used by the local database/model and performs the
    Spansh unit conversions: days->seconds, AU->metres, km->metres and G->m/s².
    """
    name = _spansh_body_name(raw)
    bid = parse_optional_int(raw.get("body_id") or raw.get("bodyId") or raw.get("BodyID"))
    bid64 = spansh_body_id64(system_address, raw)
    btype = _spansh_body_type(raw)
    subtype = _spansh_body_subtype(raw)
    d: Dict[str, Any] = {
        "name": name, "Name": name, "bodyName": name, "BodyName": name,
        "BodyID": bid, "bodyId": bid,
        "bodyId64": bid64,
        "type": btype, "BodyType": btype,
        "subType": subtype, "SubType": subtype,
        "timestamp": raw.get("updated_at") or raw.get("timestamp") or raw.get("Timestamp") or "1970-01-01T00:00:00Z",
        "Parents": parent_refs_from_spansh(raw, id64_to_body_id),
        "StarSystem": raw.get("system_name") or raw.get("StarSystem") or raw.get("systemName") or "",
        "SystemAddress": system_address,
    }
    if btype.lower() == "planet":
        d["PlanetClass"] = subtype
    if btype.lower() == "star":
        d["StarType"] = raw.get("spectral_class") or raw.get("star_type") or subtype

    _set_if_value(d, "DistanceFromArrivalLS", _float_or_none(raw.get("distance_to_arrival") or raw.get("DistanceFromArrivalLS")))
    # Units: Spansh body detail uses km for radius, AU for semi-major axis,
    # days for periods and G for gravity. Journal-like rows use SI-ish units.
    if _float_or_none(raw.get("radius")) is not None:
        d["Radius"] = float(raw["radius"]) * KM_METRES
    else:
        _set_if_value(d, "Radius", _float_or_none(raw.get("Radius")))
    if _float_or_none(raw.get("gravity")) is not None:
        d["SurfaceGravity"] = float(raw["gravity"]) * STANDARD_GRAVITY
    else:
        _set_if_value(d, "SurfaceGravity", _float_or_none(raw.get("SurfaceGravity")))
    if _float_or_none(raw.get("rotational_period")) is not None:
        d["RotationPeriod"] = float(raw["rotational_period"]) * DAY_SECONDS
    else:
        _set_if_value(d, "RotationPeriod", _float_or_none(raw.get("RotationPeriod")))
    if _float_or_none(raw.get("orbital_period")) is not None:
        d["OrbitalPeriod"] = float(raw["orbital_period"]) * DAY_SECONDS
    else:
        _set_if_value(d, "OrbitalPeriod", _float_or_none(raw.get("OrbitalPeriod")))
    if _float_or_none(raw.get("semi_major_axis")) is not None:
        d["SemiMajorAxis"] = float(raw["semi_major_axis"]) * AU_METRES
    else:
        _set_if_value(d, "SemiMajorAxis", _float_or_none(raw.get("SemiMajorAxis")))

    field_map = {
        "Eccentricity": "orbital_eccentricity",
        "OrbitalInclination": "orbital_inclination",
        "Periapsis": "arg_of_periapsis",
        "MeanAnomaly": "mean_anomaly",
        "AscendingNode": "ascending_node",
        "MassEM": "earth_masses",
        "SurfaceTemperature": "surface_temperature",
        "SurfacePressure": "surface_pressure",
        "StellarMass": "solar_masses",
        "AbsoluteMagnitude": "absolute_magnitude",
        "Age_MY": "age",
    }
    for journal_key, spansh_key in field_map.items():
        _set_if_value(d, journal_key, _float_or_none(raw.get(spansh_key) if spansh_key in raw else raw.get(journal_key)))
    # Spansh axis_tilt is radians; Journal/ED UI AxialTilt is degrees.
    if raw.get("axis_tilt") not in (None, ""):
        _set_if_value(d, "AxialTilt", _axis_tilt_radians_to_degrees(raw.get("axis_tilt")))
    else:
        _set_if_value(d, "AxialTilt", _float_or_none(raw.get("AxialTilt")))
    _set_if_value(d, "Atmosphere", raw.get("atmosphere") or raw.get("Atmosphere") or "")
    _set_if_value(d, "Luminosity", raw.get("luminosity_class") or raw.get("Luminosity") or "")
    landable = raw.get("is_landable") if "is_landable" in raw else raw.get("Landable")
    tidal = raw.get("is_rotational_period_tidally_locked") if "is_rotational_period_tidally_locked" in raw else raw.get("TidalLock")
    if landable is not None:
        d["Landable"] = bool(landable)
    if tidal is not None:
        d["TidalLock"] = bool(tidal)
    return d


def convert_spansh_system_to_journal(record: Dict[str, Any], body_details: List[Dict[str, Any]]) -> Dict[str, Any]:
    name = str(record.get("name") or record.get("system_name") or record.get("Name") or "").strip()
    address = parse_optional_int(record.get("id64") or record.get("system_id64") or record.get("SystemAddress") or record.get("systemAddress"))
    id64_to_body_id: Dict[int, int] = {}
    for raw in body_details:
        bid64 = spansh_body_id64(address, raw)
        bid = parse_optional_int(raw.get("body_id") or raw.get("bodyId") or raw.get("BodyID"))
        if bid64 is not None and bid is not None:
            id64_to_body_id[int(bid64)] = int(bid)
    bodies_out = [convert_spansh_body_to_journal(raw, address, id64_to_body_id) for raw in body_details]
    bodies_out = [b for b in bodies_out if body_type_from_json(b).lower() in {"star", "planet"} and body_name_from_json(b)]
    return {
        "name": name, "Name": name, "systemName": name, "StarSystem": name,
        "systemAddress": address, "SystemAddress": address,
        "coords": {
            "x": _float_or_none(record.get("x") or record.get("system_x")),
            "y": _float_or_none(record.get("y") or record.get("system_y")),
            "z": _float_or_none(record.get("z") or record.get("system_z")),
        },
        "source": {
            "createdBy": "elite_daynight_db.py",
            "createdAtUtc": utc_now(),
            "dataSource": "Spansh API compact import",
            "note": "Only compact fields required for day/night fitting are preserved.",
        },
        "bodies": bodies_out,
    }


def fetch_full_spansh_system(system_name: str, system_address: Optional[int] = None, fetch_body_details: bool = True) -> Tuple[Dict[str, Any], List[Any]]:
    address, resolved_name, candidates = resolve_spansh_system_address(system_name, system_address)
    if address is not None:
        record = fetch_spansh_system_by_address(address)
    else:
        record = fetch_spansh_system_by_name_fallback(resolved_name or system_name)
        address = parse_optional_int(record.get("id64") or record.get("system_id64") or record.get("SystemAddress") or record.get("systemAddress"))

    raw_bodies = record.get("bodies") or record.get("Bodies") or []
    if not isinstance(raw_bodies, list):
        raw_bodies = []
    body_details: List[Dict[str, Any]] = []
    for i, summary in enumerate(raw_bodies, start=1):
        if not isinstance(summary, dict):
            continue
        typ = str(summary.get("type") or summary.get("BodyType") or "").strip().lower()
        if typ not in {"star", "planet"}:
            continue
        detail = summary
        if fetch_body_details:
            bid64 = spansh_body_id64(address, summary)
            if bid64 is not None:
                try:
                    detail = fetch_spansh_body_detail(bid64)
                except Exception as exc:
                    print(f"Warning: could not fetch body detail for {summary.get('name') or summary.get('Name')} ({bid64}): {exc}")
        body_details.append(detail)
    system = convert_spansh_system_to_journal(record, body_details)
    return system, candidates


def normalize_body_token(value: str) -> str:
    return "".join(ch.lower() for ch in str(value).strip() if ch.isalnum())


def body_suffix(full_body_name: str, system_name: str) -> str:
    full = str(full_body_name).strip()
    sysname = str(system_name).strip()
    if full.lower().startswith(sysname.lower()):
        return full[len(sysname):].strip()
    return full


def find_body_name_match(system: Dict[str, Any], requested_body_name: str) -> str:
    """Find a body by full name or by short suffix such as '2 a' / '2a'.

    This lets the website import form accept either:
      - Col 285 Sector XK-O d6-90 2 a
      - 2 a
      - 2a
    without exposing users to long repeated system prefixes.
    """
    system_name = system_name_from_json(system)
    names = [body_name_from_json(b) for b in bodies_from_system(system) if body_name_from_json(b)]
    req = str(requested_body_name).strip()
    req_l = req.lower()
    req_n = normalize_body_token(req)

    exact = [n for n in names if n.lower() == req_l]
    if len(exact) == 1:
        return exact[0]

    suffix_exact = [n for n in names if body_suffix(n, system_name).lower() == req_l]
    if len(suffix_exact) == 1:
        return suffix_exact[0]

    compact = [n for n in names if normalize_body_token(body_suffix(n, system_name)) == req_n]
    if len(compact) == 1:
        return compact[0]

    # Last fallback: unique suffix ending match, e.g. '7 a' can match 'A 7 a'
    # only if there is no ambiguity.
    ending = [n for n in names if normalize_body_token(body_suffix(n, system_name)).endswith(req_n)] if req_n else []
    if len(ending) == 1:
        return ending[0]

    partial = [n for n in names if req_l and req_l in n.lower()]
    reason = "ambiguous" if (len(compact) > 1 or len(suffix_exact) > 1 or len(ending) > 1) else "not found"
    raise SpanshImportError(
        f"Fetched system '{system_name}', but body '{requested_body_name}' was {reason}.\n"
        f"Partial body matches: {partial[:20]}\n"
        f"Available real bodies: {names[:80]}"
    )


def import_spansh_system(
    db_path: str,
    system_name: str,
    body_name: str,
    system_address: Optional[int] = None,
    fetch_body_details: bool = True,
) -> Tuple[int, int, str]:
    """Fetch a system from Spansh, compact it, import it, and confirm body exists."""
    system, candidates = fetch_full_spansh_system(system_name, system_address, fetch_body_details)
    names = [body_name_from_json(b) for b in bodies_from_system(system)]
    matched_body = find_body_name_match(system, body_name)
    # Write a minimal temporary JSON because import_system_json already contains
    # the database upsert logic. The raw Spansh payload is not stored in the DB.
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(system, f, ensure_ascii=False, indent=2)
            tmp_path = f.name
        system_id = import_system_json(db_path, tmp_path)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    con = sqlite_connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    begin_write_transaction(con)
    body_pk = lookup_system_body(con, system_name_from_json(system), matched_body)[1]
    now = utc_now()
    con.execute(
        "UPDATE systems SET source = ?, source_json_path = ?, source_created_at_utc = ?, updated_at_utc = ? WHERE id = ?",
        ("Spansh API compact import", f"spansh://system/{system.get('SystemAddress') or system.get('systemAddress')}", now, now, system_id),
    )
    con.execute(
        "INSERT INTO audit_log(entity_type, entity_id, action, new_json, created_at_utc) VALUES (?, ?, ?, ?, ?)",
        ("system", system_id, "import_spansh_system", json_dumps({"requested_system": system_name, "requested_body": body_name, "matched_body": matched_body, "stored_bodies": len(names)}), now),
    )
    con.commit(); con.close()
    return system_id, body_pk, matched_body


def init_db(db_path: str) -> None:
    con = sqlite_connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA wal_autocheckpoint = 1000")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS systems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_address INTEGER UNIQUE,
            name TEXT NOT NULL,
            x REAL,
            y REAL,
            z REAL,
            source TEXT,
            source_json_path TEXT,
            source_created_at_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_systems_name ON systems(name COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS bodies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
            body_id INTEGER,
            body_id64 INTEGER,
            name TEXT NOT NULL,
            body_type TEXT,
            subtype TEXT,
            parents_json TEXT,
            parent_type TEXT,
            parent_body_id INTEGER,
            parent_name TEXT,
            illumination_source_star_name TEXT,
            is_landable INTEGER,
            is_tidally_locked INTEGER,
            radius_m REAL,
            mass_em REAL,
            gravity_ms2 REAL,
            distance_from_arrival_ls REAL,
            surface_temperature_k REAL,
            surface_pressure_pa REAL,
            atmosphere TEXT,
            atmosphere_type TEXT,
            star_type TEXT,
            stellar_mass REAL,
            absolute_magnitude REAL,
            age_my REAL,
            luminosity TEXT,
            rotation_period_s REAL,
            orbital_period_s REAL,
            semi_major_axis_m REAL,
            eccentricity REAL,
            orbital_inclination_deg REAL,
            periapsis_deg REAL,
            mean_anomaly_deg REAL,
            ascending_node_deg REAL,
            axial_tilt_deg REAL,
            scan_timestamp_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            UNIQUE(system_id, name),
            UNIQUE(system_id, body_id)
        );
        CREATE INDEX IF NOT EXISTS idx_bodies_system_name ON bodies(system_id, name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_bodies_parent ON bodies(system_id, parent_body_id);

        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            obs_hash TEXT NOT NULL UNIQUE,
            system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
            body_id INTEGER NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            observer_name TEXT,
            timestamp_utc TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            observation TEXT NOT NULL,
            elevation REAL,
            heading REAL,
            quality TEXT NOT NULL DEFAULT 'medium',
            note TEXT,
            source TEXT,
            source_file TEXT,
            target_type TEXT NOT NULL DEFAULT 'sun',
            target_body_id INTEGER REFERENCES bodies(id) ON DELETE SET NULL,
            review_status TEXT NOT NULL DEFAULT 'new',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_observations_body_status ON observations(body_id, review_status);
        CREATE INDEX IF NOT EXISTS idx_observations_time ON observations(timestamp_utc);

        CREATE TABLE IF NOT EXISTS fits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
            body_id INTEGER NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            model_version TEXT NOT NULL,
            fit_status TEXT NOT NULL,
            fit_score REAL,
            rms_altitude_deg REAL,
            rms_heading_deg REAL,
            use_heading INTEGER NOT NULL DEFAULT 0,
            time_weighting INTEGER NOT NULL DEFAULT 0,
            params_json TEXT,
            report_text TEXT,
            observation_count INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 0,
            fit_mode TEXT NOT NULL DEFAULT 'approved',
            observation_fingerprint TEXT,
            includes_unreviewed INTEGER NOT NULL DEFAULT 0,
            used_statuses_json TEXT,
            created_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fits_body_active ON fits(body_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_fits_body_mode_active ON fits(body_id, fit_mode, is_active);
        CREATE INDEX IF NOT EXISTS idx_fits_body_mode_fingerprint ON fits(body_id, fit_mode, observation_fingerprint);

        CREATE TABLE IF NOT EXISTS fit_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fit_id INTEGER NOT NULL REFERENCES fits(id) ON DELETE CASCADE,
            observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            altitude_error_deg REAL,
            heading_error_deg REAL,
            effective_weight REAL,
            used_in_fit INTEGER NOT NULL DEFAULT 1,
            UNIQUE(fit_id, observation_id)
        );

        CREATE TABLE IF NOT EXISTS prediction_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body_id INTEGER NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            fit_id INTEGER REFERENCES fits(id) ON DELETE CASCADE,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            target_time_utc TEXT NOT NULL,
            prediction_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS poi_transition_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poi_id INTEGER NOT NULL,
            body_id INTEGER NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            fit_id INTEGER NOT NULL REFERENCES fits(id) ON DELETE CASCADE,
            model_mode TEXT NOT NULL DEFAULT 'approved',
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            threshold_deg REAL NOT NULL DEFAULT 0.0,
            cache_version TEXT NOT NULL,
            window_start_utc TEXT NOT NULL,
            window_end_utc TEXT NOT NULL,
            checked_until_utc TEXT NOT NULL,
            requested_lookahead_hours REAL NOT NULL,
            max_extended_hours REAL NOT NULL,
            transitions_json TEXT NOT NULL,
            search_meta_json TEXT,
            created_at_utc TEXT NOT NULL,
            refreshed_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_poi_transition_cache_lookup ON poi_transition_cache(poi_id, fit_id, model_mode, cache_version);
        CREATE INDEX IF NOT EXISTS idx_poi_transition_cache_body_fit ON poi_transition_cache(body_id, fit_id);
        CREATE INDEX IF NOT EXISTS idx_poi_transition_cache_checked ON poi_transition_cache(checked_until_utc);

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            old_json TEXT,
            new_json TEXT,
            created_at_utc TEXT NOT NULL,
            actor TEXT
        );
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at_utc DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action)")
    ensure_illumination_columns(con)
    ensure_fit_mode_columns(con)
    ensure_automation_columns(con)
    dedupe_duplicate_bodies(con)
    dedupe_active_fits(con)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bodies_system_name_norm_unique ON bodies(system_id, lower(trim(name)))")
    con.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)", ("schema_version", str(SCHEMA_VERSION)))
    con.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)", ("model_version", MODEL_VERSION))
    con.commit()
    con.close()


def ensure_system(con: sqlite3.Connection, name: str, system_address: Optional[int] = None) -> int:
    now = utc_now()
    row = None
    if system_address is not None:
        row = con.execute("SELECT id FROM systems WHERE system_address = ?", (system_address,)).fetchone()
    if row is None:
        row = con.execute("SELECT id FROM systems WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return int(row[0])
    cur = con.execute(
        "INSERT INTO systems(system_address, name, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?)",
        (system_address, name, now, now),
    )
    return int(cur.lastrowid)


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _table_columns(con: sqlite3.Connection, table_name: str) -> List[str]:
    if not _table_exists(con, table_name):
        return []
    return [str(r[1]) for r in con.execute(f"PRAGMA table_info({table_name})").fetchall()]


def _missing_body_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _body_reference_targets(con: sqlite3.Connection) -> List[Tuple[str, str]]:
    candidates = [
        ("observations", "body_id"),
        ("observations", "target_body_id"),
        ("fits", "body_id"),
        ("prediction_cache", "body_id"),
        ("poi_transition_cache", "body_id"),
        ("background_fit_jobs", "body_id"),
        ("body_pois", "body_id"),
    ]
    out: List[Tuple[str, str]] = []
    for table, column in candidates:
        if column in _table_columns(con, table):
            out.append((table, column))
    return out


def _body_link_count(con: sqlite3.Connection, body_pk: int) -> int:
    total = 0
    for table, column in _body_reference_targets(con):
        row = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (body_pk,)).fetchone()
        total += 0 if row is None else int(row[0])
    return total


def _body_merge_score(con: sqlite3.Connection, row: sqlite3.Row) -> Tuple[int, int, int, int, str, int]:
    data = dict(row)
    completeness = sum(0 if _missing_body_value(v) else 1 for v in data.values())
    updated = str(data.get("updated_at_utc") or data.get("created_at_utc") or "")
    return (
        _body_link_count(con, int(data["id"])),
        1 if not _missing_body_value(data.get("body_id")) else 0,
        1 if not _missing_body_value(data.get("body_id64")) else 0,
        completeness,
        updated,
        -int(data["id"]),
    )


def _deactivate_extra_active_fits_for_body(con: sqlite3.Connection, body_pk: int) -> None:
    dedupe_active_fits(con, body_pk)


def dedupe_active_fits(con: sqlite3.Connection, body_pk: Optional[int] = None) -> int:
    """Keep only the newest active fit per body/mode.

    Older versions and interrupted jobs can leave more than one active fit for
    the same body/mode.  List pages join active fits, so duplicate active rows
    can render a body more than once even when the body table is clean.
    """
    fit_cols = set(_table_columns(con, "fits"))
    if not {"body_id", "fit_mode", "is_active", "id"}.issubset(fit_cols):
        return 0
    params: List[Any] = []
    where = "WHERE is_active = 1"
    if body_pk is not None:
        where += " AND body_id = ?"
        params.append(body_pk)
    rows = con.execute(
        f"""
        SELECT body_id, fit_mode, MAX(id) AS keep_fit_id, COUNT(*) AS active_count
          FROM fits
          {where}
         GROUP BY body_id, fit_mode
        HAVING COUNT(*) > 1
        """,
        params,
    ).fetchall()
    deactivated = 0
    for row in rows:
        con.execute(
            "UPDATE fits SET is_active = 0 WHERE body_id = ? AND fit_mode = ? AND is_active = 1 AND id <> ?",
            (int(row[0]), row[1], int(row[2])),
        )
        deactivated += int(row[3]) - 1
    return deactivated


def _merge_body_rows(con: sqlite3.Connection, keep_body_pk: int, duplicate_body_pk: int, reason: str) -> None:
    if keep_body_pk == duplicate_body_pk:
        return
    previous_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        keep = con.execute("SELECT * FROM bodies WHERE id = ?", (keep_body_pk,)).fetchone()
        duplicate = con.execute("SELECT * FROM bodies WHERE id = ?", (duplicate_body_pk,)).fetchone()
        if keep is None or duplicate is None:
            return
        keep_data = dict(keep)
        duplicate_data = dict(duplicate)
        now = utc_now()

        for table, column in _body_reference_targets(con):
            con.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (keep_body_pk, duplicate_body_pk))

        con.execute("DELETE FROM bodies WHERE id = ?", (duplicate_body_pk,))

        updates: Dict[str, Any] = {}
        body_cols = _table_columns(con, "bodies")
        skip = {"id", "system_id", "name", "created_at_utc"}
        for col in body_cols:
            if col in skip:
                continue
            if col == "updated_at_utc":
                updates[col] = now
                continue
            if _missing_body_value(keep_data.get(col)) and not _missing_body_value(duplicate_data.get(col)):
                updates[col] = duplicate_data.get(col)
        if updates:
            assignments = ", ".join(f"{col} = ?" for col in updates)
            con.execute(
                f"UPDATE bodies SET {assignments} WHERE id = ?",
                [*updates.values(), keep_body_pk],
            )
        _deactivate_extra_active_fits_for_body(con, keep_body_pk)
        if _table_exists(con, "audit_log"):
            con.execute(
                """
                INSERT INTO audit_log(entity_type, entity_id, action, old_json, new_json, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "body",
                    keep_body_pk,
                    reason,
                    json_dumps({"removed_duplicate_body_id": duplicate_body_pk, "duplicate_name": duplicate_data.get("name")}),
                    json_dumps({"kept_body_id": keep_body_pk, "kept_name": keep_data.get("name")}),
                    now,
                ),
            )
    finally:
        con.row_factory = previous_factory


def dedupe_duplicate_bodies(con: sqlite3.Connection, system_id: Optional[int] = None) -> int:
    """Merge same-system body rows with the same normalized name.

    Older imports or manual database work could leave case/spacing variants of
    the same body.  Keep the row with the most linked data, repoint children,
    and preserve missing body metadata from removed rows.
    """
    if not _table_exists(con, "bodies"):
        return 0
    previous_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        params: List[Any] = []
        where = "WHERE name IS NOT NULL AND trim(name) <> ''"
        if system_id is not None:
            where += " AND system_id = ?"
            params.append(system_id)
        groups = con.execute(
            f"""
            SELECT system_id, lower(trim(name)) AS norm_name, COUNT(*) AS c, group_concat(id) AS ids
              FROM bodies
              {where}
             GROUP BY system_id, lower(trim(name))
            HAVING COUNT(*) > 1
            """,
            params,
        ).fetchall()
        merged = 0
        for group in groups:
            ids = [int(x) for x in str(group["ids"]).split(",") if x]
            if len(ids) < 2:
                continue
            placeholders = ",".join("?" for _ in ids)
            rows = con.execute(f"SELECT * FROM bodies WHERE id IN ({placeholders})", ids).fetchall()
            keep = max(rows, key=lambda r: _body_merge_score(con, r))
            keep_id = int(keep["id"])
            for row in rows:
                duplicate_id = int(row["id"])
                if duplicate_id == keep_id:
                    continue
                _merge_body_rows(con, keep_id, duplicate_id, "merge_duplicate_body_name")
                merged += 1
        return merged
    finally:
        con.row_factory = previous_factory


def ensure_body(con: sqlite3.Connection, system_id: int, body_name: str, body_id_value: Optional[int] = None) -> int:
    now = utc_now()
    clean_name = str(body_name or "").strip()
    if not clean_name:
        raise ValueError("Body JSON has no body name")
    previous_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        name_row = con.execute(
            """
            SELECT id, body_id FROM bodies
             WHERE system_id = ? AND lower(trim(name)) = lower(trim(?))
             ORDER BY CASE WHEN body_id IS NULL THEN 1 ELSE 0 END, id
             LIMIT 1
            """,
            (system_id, clean_name),
        ).fetchone()
        id_row = None
        if body_id_value is not None:
            id_row = con.execute(
                "SELECT id, body_id FROM bodies WHERE system_id = ? AND body_id = ?",
                (system_id, body_id_value),
            ).fetchone()
        if name_row is not None and id_row is not None and int(name_row["id"]) != int(id_row["id"]):
            _merge_body_rows(con, int(name_row["id"]), int(id_row["id"]), "merge_body_import_identity")
            id_row = None
        row = name_row if name_row is not None else id_row
        if row:
            body_pk = int(row["id"])
            updates: Dict[str, Any] = {"updated_at_utc": now}
            current_body_id = row["body_id"]
            if body_id_value is not None and current_body_id is None:
                updates["body_id"] = body_id_value
            if name_row is None:
                updates["name"] = clean_name
            assignments = ", ".join(f"{col} = ?" for col in updates)
            con.execute(f"UPDATE bodies SET {assignments} WHERE id = ?", [*updates.values(), body_pk])
            return body_pk
        cur = con.execute(
            "INSERT INTO bodies(system_id, body_id, name, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?)",
            (system_id, body_id_value, clean_name, now, now),
        )
        return int(cur.lastrowid)
    finally:
        con.row_factory = previous_factory


def import_system_json(db_path: str, json_path: str) -> int:
    init_db(db_path)
    with open(json_path, "r", encoding="utf-8") as f:
        system = json.load(f)
    name = system_name_from_json(system)
    if not name:
        raise ValueError("System JSON has no system name")
    address = system_address_from_json(system)
    coords = system.get("coords") or {}
    source = system.get("source") or {}
    now = utc_now()

    con = sqlite_connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    begin_write_transaction(con)
    system_id = ensure_system(con, name, address)
    dedupe_duplicate_bodies(con, system_id)
    con.execute(
        """
        UPDATE systems
           SET system_address = COALESCE(?, system_address), name = ?, x = ?, y = ?, z = ?,
               source = ?, source_json_path = ?, source_created_at_utc = ?, updated_at_utc = ?
         WHERE id = ?
        """,
        (
            address,
            name,
            parse_optional_float(coords.get("x")),
            parse_optional_float(coords.get("y")),
            parse_optional_float(coords.get("z")),
            str(source.get("dataSource") or source.get("createdBy") or "compact system JSON"),
            os.path.basename(json_path),
            str(source.get("createdAtUtc") or source.get("created_at") or ""),
            now,
            system_id,
        ),
    )

    raw_bodies = relevant_bodies(system)
    # First pass so parent names can be resolved.
    for b in raw_bodies:
        bname = body_name_from_json(b)
        bid = parse_optional_int(get_any(b, "BodyID", "bodyId", "body_id", default=None))
        ensure_body(con, system_id, bname, bid)

    for b in raw_bodies:
        bname = body_name_from_json(b)
        bid = parse_optional_int(get_any(b, "BodyID", "bodyId", "body_id", default=None))
        bpk = ensure_body(con, system_id, bname, bid)
        ptype, parent_body_id = first_parent_info(b)
        parent_name = None
        if parent_body_id is not None:
            prow = con.execute("SELECT name FROM bodies WHERE system_id = ? AND body_id = ?", (system_id, parent_body_id)).fetchone()
            parent_name = None if prow is None else str(prow[0])
        raw = b.get("rawSpanshBody") or {}
        con.execute(
            """
            UPDATE bodies SET
                body_id = COALESCE(?, body_id), body_id64 = ?, name = ?, body_type = ?, subtype = ?,
                parents_json = ?, parent_type = ?, parent_body_id = ?, parent_name = ?,
                illumination_source_star_name = COALESCE(illumination_source_star_name, ?),
                is_landable = ?, is_tidally_locked = ?, radius_m = ?, mass_em = ?, gravity_ms2 = ?,
                distance_from_arrival_ls = ?, surface_temperature_k = ?, surface_pressure_pa = ?,
                atmosphere = ?, atmosphere_type = ?, star_type = ?, stellar_mass = ?, absolute_magnitude = ?,
                age_my = ?, luminosity = ?, rotation_period_s = ?, orbital_period_s = ?, semi_major_axis_m = ?,
                eccentricity = ?, orbital_inclination_deg = ?, periapsis_deg = ?, mean_anomaly_deg = ?,
                ascending_node_deg = ?, axial_tilt_deg = ?, scan_timestamp_utc = ?, updated_at_utc = ?
            WHERE id = ?
            """,
            (
                bid,
                parse_optional_int(get_any(b, "bodyId64", "id64", default=raw.get("id64"))),
                bname,
                body_type_from_json(b),
                body_subtype_from_json(b),
                json_dumps(parent_refs(b)),
                ptype,
                parent_body_id,
                parent_name,
                str(get_any(b, "illumination_source_star_name", "IlluminationSourceStarName", default="") or "") or None,
                as_bool_int(get_any(b, "Landable", "is_landable", default=raw.get("is_landable"))),
                as_bool_int(get_any(b, "TidalLock", "IsTidallyLocked", default=raw.get("is_rotational_period_tidally_locked"))),
                parse_optional_float(get_any(b, "Radius", default=raw.get("radius"))),
                parse_optional_float(get_any(b, "MassEM", default=raw.get("earth_masses"))),
                _body_surface_gravity_ms2(b),
                parse_optional_float(get_any(b, "DistanceFromArrivalLS", default=raw.get("distance_to_arrival"))),
                parse_optional_float(get_any(b, "SurfaceTemperature", default=raw.get("surface_temperature"))),
                parse_optional_float(get_any(b, "SurfacePressure", default=raw.get("surface_pressure"))),
                str(get_any(b, "Atmosphere", default=raw.get("atmosphere") or "") or ""),
                str(get_any(b, "AtmosphereType", default="") or ""),
                str(get_any(b, "StarType", default=raw.get("spectral_class") or "") or ""),
                parse_optional_float(get_any(b, "StellarMass", default=raw.get("solar_masses"))),
                parse_optional_float(get_any(b, "AbsoluteMagnitude", default=None)),
                parse_optional_float(get_any(b, "Age_MY", default=raw.get("age"))),
                str(get_any(b, "Luminosity", default=raw.get("luminosity_class") or "") or ""),
                parse_optional_float(get_any(b, "RotationPeriod", default=raw.get("rotational_period"))),
                parse_optional_float(get_any(b, "OrbitalPeriod", default=raw.get("orbital_period"))),
                parse_optional_float(get_any(b, "SemiMajorAxis", default=raw.get("semi_major_axis"))),
                parse_optional_float(get_any(b, "Eccentricity", default=raw.get("orbital_eccentricity"))),
                parse_optional_float(get_any(b, "OrbitalInclination", default=raw.get("orbital_inclination"))),
                parse_optional_float(get_any(b, "Periapsis", default=raw.get("arg_of_periapsis"))),
                parse_optional_float(get_any(b, "MeanAnomaly", default=None)),
                parse_optional_float(get_any(b, "AscendingNode", default=None)),
                _body_axial_tilt_degrees(b),
                str(get_any(b, "timestamp", "Timestamp", default="") or ""),
                now,
                bpk,
            ),
        )

    con.execute(
        "INSERT INTO audit_log(entity_type, entity_id, action, new_json, created_at_utc) VALUES (?, ?, ?, ?, ?)",
        ("system", system_id, "import_compact_system_json", json_dumps({"file": os.path.basename(json_path), "stored_bodies": len(raw_bodies)}), now),
    )
    dedupe_duplicate_bodies(con, system_id)
    con.commit()
    con.close()
    return system_id


def compact_body_dict(row: sqlite3.Row) -> Dict[str, Any]:
    def put(d: Dict[str, Any], key: str, value: Any) -> None:
        if value is not None and value != "":
            d[key] = value

    d: Dict[str, Any] = {
        "name": row["name"], "Name": row["name"], "bodyName": row["name"], "BodyName": row["name"],
        "BodyID": row["body_id"], "bodyId": row["body_id"],
        "BodyType": row["body_type"], "type": row["body_type"],
        "SubType": row["subtype"], "subType": row["subtype"],
        "timestamp": row["scan_timestamp_utc"] or "1970-01-01T00:00:00Z",
    }
    if "illumination_source_star_name" in row.keys() and row["illumination_source_star_name"]:
        d["illumination_source_star_name"] = row["illumination_source_star_name"]
        d["IlluminationSourceStarName"] = row["illumination_source_star_name"]
    if row["body_type"] == "Planet":
        d["PlanetClass"] = row["subtype"] or ""
    if row["body_type"] == "Star":
        d["StarType"] = row["star_type"] or row["subtype"] or ""
    try:
        parents = json.loads(row["parents_json"] or "[]")
    except Exception:
        parents = []
    # The v16 model only needs the direct parent for sun-vector mode selection.
    # Keeping distant ancestor stars in a moon's Parents list can make the
    # stellar-disc calculation accidentally use the moon-parent distance.
    d["Parents"] = parents[:1]
    numeric_map = {
        "DistanceFromArrivalLS": "distance_from_arrival_ls",
        "Radius": "radius_m",
        "MassEM": "mass_em",
        "SurfaceGravity": "gravity_ms2",
        "SurfaceTemperature": "surface_temperature_k",
        "SurfacePressure": "surface_pressure_pa",
        "RotationPeriod": "rotation_period_s",
        "OrbitalPeriod": "orbital_period_s",
        "SemiMajorAxis": "semi_major_axis_m",
        "Eccentricity": "eccentricity",
        "OrbitalInclination": "orbital_inclination_deg",
        "Periapsis": "periapsis_deg",
        "MeanAnomaly": "mean_anomaly_deg",
        "AscendingNode": "ascending_node_deg",
        "AxialTilt": "axial_tilt_deg",
        "StellarMass": "stellar_mass",
        "AbsoluteMagnitude": "absolute_magnitude",
        "Age_MY": "age_my",
    }
    for out_key, col in numeric_map.items():
        put(d, out_key, row[col])
    put(d, "Atmosphere", row["atmosphere"])
    put(d, "AtmosphereType", row["atmosphere_type"])
    put(d, "Luminosity", row["luminosity"])
    if row["is_landable"] is not None:
        d["Landable"] = bool(row["is_landable"])
    if row["is_tidally_locked"] is not None:
        d["TidalLock"] = bool(row["is_tidally_locked"])
    return d


def compact_system_from_db(con: sqlite3.Connection, system_id: int) -> Dict[str, Any]:
    con.row_factory = sqlite3.Row
    s = con.execute("SELECT * FROM systems WHERE id = ?", (system_id,)).fetchone()
    if not s:
        raise ValueError(f"System id not found: {system_id}")
    bodies = [compact_body_dict(r) for r in con.execute("SELECT * FROM bodies WHERE system_id = ? ORDER BY COALESCE(body_id, 999999), name", (system_id,))]
    return {
        "name": s["name"], "Name": s["name"], "systemName": s["name"], "StarSystem": s["name"],
        "systemAddress": s["system_address"], "SystemAddress": s["system_address"],
        "coords": {"x": s["x"], "y": s["y"], "z": s["z"]},
        "bodies": bodies,
    }


def raw_system_and_body(con: sqlite3.Connection, system_id: int, body_pk: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    system = compact_system_from_db(con, system_id)
    brow = con.execute("SELECT name FROM bodies WHERE id = ?", (body_pk,)).fetchone()
    if not brow:
        raise ValueError(f"Body id not found: {body_pk}")
    body = model.find_body(system, str(brow[0]))
    return system, body


def observation_hash(system_id: int, body_id: int, row: Dict[str, Any]) -> str:
    parts = [
        str(system_id), str(body_id), str(row.get("timestamp_utc") or ""),
        f"{float(row.get('lat')):.8f}", f"{float(row.get('lon')):.8f}",
        str(row.get("observation") or ""),
        "" if row.get("elevation") is None else f"{float(row.get('elevation')):.4f}",
        "" if row.get("heading") is None else f"{float(row.get('heading')):.4f}",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def import_observations_csv(
    db_path: str,
    csv_path: str,
    system_name: str,
    body_name: str,
    system_address: Optional[int] = None,
    review_status: str = "new",
    observer_name: str = "",
    source: str = "local_csv",
) -> int:
    init_db(db_path)
    con = sqlite_connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    begin_write_transaction(con)
    system_id = ensure_system(con, system_name, system_address)
    body_row = con.execute("SELECT id, body_id FROM bodies WHERE system_id = ? AND lower(name) = lower(?)", (system_id, body_name)).fetchone()
    body_pk = int(body_row[0]) if body_row else ensure_body(con, system_id, body_name)
    now = utc_now()
    count = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = model.format_utc(model.parse_utc(row.get("timestamp_utc") or row.get("time") or row.get("timestamp") or ""))
            lat = float(str(row.get("lat") or row.get("latitude") or 0.0).replace(",", "."))
            lon = float(str(row.get("lon") or row.get("longitude") or 0.0).replace(",", "."))
            obs_type = (row.get("observation") or row.get("obs") or "elevation").strip().lower()
            elev_raw = row.get("elevation") or row.get("altitude_deg") or row.get("sun_altitude") or ""
            head_raw = row.get("heading") or ""
            elevation = None if str(elev_raw).strip() == "" else float(str(elev_raw).replace(",", "."))
            heading = None if str(head_raw).strip() == "" else float(str(head_raw).replace(",", ".")) % 360.0
            quality = (row.get("quality") or "medium").strip().lower()
            note = row.get("note") or row.get("notes") or ""
            norm = {"timestamp_utc": ts, "lat": lat, "lon": lon, "observation": obs_type, "elevation": elevation, "heading": heading}
            ohash = observation_hash(system_id, body_pk, norm)
            con.execute(
                """
                INSERT INTO observations(
                    obs_hash, system_id, body_id, observer_name, timestamp_utc, lat, lon,
                    observation, elevation, heading, quality, note, source, source_file,
                    target_type, review_status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sun', ?, ?, ?)
                ON CONFLICT(obs_hash) DO UPDATE SET
                    observer_name = excluded.observer_name, quality = excluded.quality,
                    note = excluded.note, source = excluded.source, source_file = excluded.source_file,
                    review_status = excluded.review_status, updated_at_utc = excluded.updated_at_utc
                """,
                (ohash, system_id, body_pk, observer_name, ts, lat, lon, obs_type, elevation, heading, quality, note,
                 source, os.path.basename(csv_path), review_status, now, now),
            )
            count += 1
    con.execute(
        "INSERT INTO audit_log(entity_type, entity_id, action, new_json, created_at_utc) VALUES (?, ?, ?, ?, ?)",
        ("body", body_pk, "import_observations_csv", json_dumps({"file": os.path.basename(csv_path), "rows": count, "review_status": review_status}), now),
    )
    con.commit(); con.close()
    return count


def lookup_system_body(con: sqlite3.Connection, system_name: str, body_name: str) -> Tuple[int, int]:
    s = con.execute("SELECT id FROM systems WHERE lower(name) = lower(?)", (system_name,)).fetchone()
    if not s:
        raise ValueError(f"System not found in database: {system_name}")
    system_id = int(s[0])
    b = con.execute("SELECT id FROM bodies WHERE system_id = ? AND lower(name) = lower(?)", (system_id, body_name)).fetchone()
    if not b:
        raise ValueError(f"Body not found in database for {system_name}: {body_name}")
    return system_id, int(b[0])




def ensure_illumination_columns(con: sqlite3.Connection) -> None:
    """Add explicit illumination-source support to older databases."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(bodies)").fetchall()}
    if "illumination_source_star_name" not in cols:
        con.execute("ALTER TABLE bodies ADD COLUMN illumination_source_star_name TEXT")


def set_body_illumination_source(con: sqlite3.Connection, body_pk: int, star_name: Optional[str], actor: str = "api-admin") -> None:
    ensure_illumination_columns(con)
    old = con.execute("SELECT illumination_source_star_name FROM bodies WHERE id = ?", (body_pk,)).fetchone()
    old_value = None if old is None else old[0]
    value = (star_name or "").strip()
    if value.lower() == "auto":
        value = ""
    new_value = value or None
    if old_value == new_value:
        return
    con.execute("UPDATE bodies SET illumination_source_star_name = ?, updated_at_utc = ? WHERE id = ?", (new_value, utc_now(), body_pk))
    con.execute(
        "INSERT INTO audit_log(entity_type, entity_id, action, old_json, new_json, actor, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("body", body_pk, "set_illumination_source", json_dumps({"illumination_source_star_name": old_value}), json_dumps({"illumination_source_star_name": new_value}), actor, utc_now()),
    )


def ensure_fit_mode_columns(con: sqlite3.Connection) -> None:
    """Add fit-variant columns for approved vs provisional model separation."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(fits)").fetchall()}
    if "fit_mode" not in cols:
        con.execute("ALTER TABLE fits ADD COLUMN fit_mode TEXT NOT NULL DEFAULT 'approved'")
    if "observation_fingerprint" not in cols:
        con.execute("ALTER TABLE fits ADD COLUMN observation_fingerprint TEXT")
    if "includes_unreviewed" not in cols:
        con.execute("ALTER TABLE fits ADD COLUMN includes_unreviewed INTEGER NOT NULL DEFAULT 0")
    if "used_statuses_json" not in cols:
        con.execute("ALTER TABLE fits ADD COLUMN used_statuses_json TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fits_body_mode_active ON fits(body_id, fit_mode, is_active)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fits_body_mode_fingerprint ON fits(body_id, fit_mode, observation_fingerprint)")
    # Older versions had one active fit per body. Classify old fits by the
    # statuses they actually used, so a previous provisional fit does not become
    # the reviewed/default model.
    con.execute(
        """
        UPDATE fits
           SET includes_unreviewed = CASE WHEN EXISTS (
                 SELECT 1 FROM fit_observations fo
                 JOIN observations o ON o.id = fo.observation_id
                 WHERE fo.fit_id = fits.id AND fo.used_in_fit = 1 AND o.review_status != 'approved'
               ) THEN 1 ELSE 0 END
        """
    )
    con.execute("UPDATE fits SET fit_mode = CASE WHEN includes_unreviewed = 1 THEN 'provisional' ELSE 'approved' END WHERE fit_mode IS NULL OR fit_mode = '' OR fit_mode = 'approved'")
    con.execute("UPDATE fits SET used_statuses_json = '[\"approved\",\"new\",\"needs_check\"]' WHERE includes_unreviewed = 1 AND used_statuses_json IS NULL")
    con.execute("UPDATE fits SET used_statuses_json = '[\"approved\"]' WHERE includes_unreviewed = 0 AND used_statuses_json IS NULL")


def fit_mode_from_include_unreviewed(include_unreviewed: bool) -> str:
    return "provisional" if include_unreviewed else "approved"


def statuses_for_fit_mode(fit_mode: str) -> Tuple[str, ...]:
    mode = (fit_mode or "approved").strip().lower()
    if mode == "provisional":
        return ("approved", "new", "needs_check")
    return ("approved",)


def ensure_automation_columns(con: sqlite3.Connection) -> None:
    """Add V0.208 automation metadata columns to older databases."""
    obs_cols = {r[1] for r in con.execute("PRAGMA table_info(observations)").fetchall()}
    if "auto_review_status" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_status TEXT")
    if "auto_review_reason" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_reason TEXT")
    if "auto_review_model_id" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_model_id INTEGER REFERENCES fits(id) ON DELETE SET NULL")
    if "auto_review_residual_altitude_deg" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_residual_altitude_deg REAL")
    if "auto_review_threshold_deg" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_threshold_deg REAL")
    if "auto_review_residual_heading_deg" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_residual_heading_deg REAL")
    if "auto_review_heading_threshold_deg" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_heading_threshold_deg REAL")
    if "auto_review_confidence_score" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_review_confidence_score REAL")
    if "auto_reviewed_at_utc" not in obs_cols:
        con.execute("ALTER TABLE observations ADD COLUMN auto_reviewed_at_utc TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_observations_auto_review ON observations(auto_review_status)")

    fit_cols = {r[1] for r in con.execute("PRAGMA table_info(fits)").fetchall()}
    if "fit_origin" not in fit_cols:
        con.execute("ALTER TABLE fits ADD COLUMN fit_origin TEXT NOT NULL DEFAULT 'manual'")
    if "auto_fit_reason" not in fit_cols:
        con.execute("ALTER TABLE fits ADD COLUMN auto_fit_reason TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fits_origin ON fits(fit_origin)")


def observation_fingerprint_for_body(
    con: sqlite3.Connection,
    body_pk: int,
    include_unreviewed: bool,
    use_heading: bool = False,
    time_weighting: bool = False,
    time_half_life_hours: float = 24.0,
) -> str:
    """Stable fingerprint of fitting inputs and important settings."""
    statuses = statuses_for_fit_mode(fit_mode_from_include_unreviewed(include_unreviewed))
    placeholders = ",".join("?" for _ in statuses)
    rows = con.execute(
        f"""
        SELECT id, review_status, updated_at_utc
          FROM observations
         WHERE body_id = ? AND review_status IN ({placeholders}) AND target_type = 'sun'
         ORDER BY id
        """,
        (body_pk, *statuses),
    ).fetchall()
    payload = {
        "body_id": int(body_pk),
        "statuses": list(statuses),
        "use_heading": bool(use_heading),
        "time_weighting": bool(time_weighting),
        "time_weighting_mode": "recent_boost" if time_weighting else "off",
        "fit_weighting_version": 2,
        "observations": [
            {
                "id": int(r["id"] if isinstance(r, sqlite3.Row) else r[0]),
                "status": str(r["review_status"] if isinstance(r, sqlite3.Row) else r[1]),
                "updated_at_utc": str(r["updated_at_utc"] if isinstance(r, sqlite3.Row) else r[2]),
            }
            for r in rows
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def observations_for_body(
    con: sqlite3.Connection,
    body_pk: int,
    include_unreviewed: bool = False,
) -> List[Tuple[int, model.Observation]]:
    """Return observations usable for fitting.

    Normal/public fits use approved observations only. Provisional fits may also
    include new/needs_check observations so a model can be tested before manual
    review is complete. Rejected/corrected observations are never used.
    """
    statuses = ("approved", "new", "needs_check") if include_unreviewed else ("approved",)
    placeholders = ",".join("?" for _ in statuses)
    rows = con.execute(
        f"""
        SELECT id, timestamp_utc, lat, lon, observation, elevation, heading, quality, note
          FROM observations
         WHERE body_id = ? AND review_status IN ({placeholders}) AND target_type = 'sun'
         ORDER BY timestamp_utc, id
        """, (body_pk, *statuses),
    ).fetchall()
    out: List[Tuple[int, model.Observation]] = []
    for r in rows:
        out.append((int(r[0]), model.Observation(model.parse_utc(r[1]), float(r[2]), float(r[3]), str(r[4]), None if r[5] is None else float(r[5]), None if r[6] is None else float(r[6]), str(r[7] or "medium"), str(r[8] or ""))))
    return out


def approved_observations_for_body(con: sqlite3.Connection, body_pk: int) -> List[Tuple[int, model.Observation]]:
    return observations_for_body(con, body_pk, include_unreviewed=False)


def observations_for_fit(con: sqlite3.Connection, fit_id: int) -> List[Tuple[int, model.Observation]]:
    rows = con.execute(
        """
        SELECT o.id, o.timestamp_utc, o.lat, o.lon, o.observation, o.elevation, o.heading, o.quality, o.note
          FROM fit_observations fo JOIN observations o ON o.id = fo.observation_id
         WHERE fo.fit_id = ? AND fo.used_in_fit = 1
         ORDER BY o.timestamp_utc, o.id
        """,
        (fit_id,),
    ).fetchall()
    out: List[Tuple[int, model.Observation]] = []
    for r in rows:
        out.append((int(r[0]), model.Observation(model.parse_utc(r[1]), float(r[2]), float(r[3]), str(r[4]), None if r[5] is None else float(r[5]), None if r[6] is None else float(r[6]), str(r[7] or "medium"), str(r[8] or ""))))
    return out


def fit_body(
    db_path: str,
    system_name: str,
    body_name: str,
    use_heading: bool = False,
    time_weighting: bool = False,
    time_half_life_hours: float = 24.0,
    include_unreviewed: bool = False,
    force_refit: bool = False,
) -> int:
    """Fit and store either the reviewed or provisional model.

    include_unreviewed=False creates/updates the approved model and is the
    default public model. include_unreviewed=True creates/updates a provisional
    model using approved + new + needs_check observations. The two model modes
    are stored separately; a provisional fit never replaces the reviewed model.

    Provisional fits are cached by an observation/settings fingerprint unless
    force_refit=True.
    """
    con = sqlite_connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    ensure_illumination_columns(con)
    ensure_fit_mode_columns(con)
    ensure_automation_columns(con)
    # ensure_* migration helpers may run ALTER/UPDATE statements for older DBs.
    # Commit that migration work before the later explicit BEGIN IMMEDIATE,
    # otherwise SQLite can raise: "cannot start a transaction within a transaction".
    con.commit()
    system_id, body_pk = lookup_system_body(con, system_name, body_name)
    system, body = raw_system_and_body(con, system_id, body_pk)
    fit_mode = fit_mode_from_include_unreviewed(include_unreviewed)
    statuses = statuses_for_fit_mode(fit_mode)
    fingerprint = observation_fingerprint_for_body(con, body_pk, include_unreviewed, use_heading, time_weighting, time_half_life_hours)

    if fit_mode == "provisional" and not force_refit:
        cached = con.execute(
            """
            SELECT id FROM fits
             WHERE body_id = ? AND fit_mode = 'provisional' AND fit_status = 'ok'
               AND observation_fingerprint = ? AND is_active = 1
             ORDER BY id DESC LIMIT 1
            """,
            (body_pk, fingerprint),
        ).fetchone()
        if cached:
            con.close()
            return int(cached["id"])

    obs_pairs = observations_for_body(con, body_pk, include_unreviewed=include_unreviewed)
    if not obs_pairs:
        if include_unreviewed:
            raise ValueError("No approved/new/needs_check observations for this body")
        raise ValueError("No approved observations for this body")
    observations = [o for _, o in obs_pairs]
    fitted = model.fit_model(
        body, observations, use_heading=use_heading, time_weighting=time_weighting,
        time_half_life_hours=time_half_life_hours, system=system,
        time_weighting_mode="recent_boost", recent_boost_max=2.0,
        sun_geometry_mode="auto",
    )
    summary = model.model_summary_dict(fitted)
    report_label = "database approved + unreviewed observations" if include_unreviewed else "database approved observations"
    report = model.make_report(fitted, system, calibration_path=report_label)
    now = utc_now()
    begin_write_transaction(con)
    con.execute("UPDATE fits SET is_active = 0 WHERE body_id = ? AND fit_mode = ?", (body_pk, fit_mode))
    cur = con.execute(
        """
        INSERT INTO fits(system_id, body_id, model_version, fit_status, fit_score, rms_altitude_deg,
            rms_heading_deg, use_heading, time_weighting, params_json, report_text,
            observation_count, is_active, fit_mode, observation_fingerprint, includes_unreviewed,
            used_statuses_json, created_at_utc)
        VALUES (?, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            system_id, body_pk, MODEL_VERSION, float(fitted.score), float(fitted.rms_altitude),
            None if fitted.rms_heading is None else float(fitted.rms_heading),
            1 if use_heading else 0, 1 if time_weighting else 0, json_dumps(summary), report,
            len(observations), fit_mode, fingerprint, 1 if include_unreviewed else 0,
            json_dumps(list(statuses)), now,
        ),
    )
    fit_id = int(cur.lastrowid)
    for (obs_id, _), res in zip(obs_pairs, model.residuals_as_dicts(fitted)):
        con.execute(
            "INSERT INTO fit_observations(fit_id, observation_id, altitude_error_deg, heading_error_deg, effective_weight, used_in_fit) VALUES (?, ?, ?, ?, ?, 1)",
            (fit_id, obs_id, res.get("altitude_error_deg"), res.get("heading_error_deg"), res.get("effective_weight")),
        )
    cleaned = cleanup_old_fits(con, body_pk)
    con.execute("INSERT INTO audit_log(entity_type, entity_id, action, new_json, created_at_utc) VALUES (?, ?, ?, ?, ?)",
                ("fit", fit_id, "fit_body", json_dumps({"body": body_name, "observations": len(observations), "score": fitted.score, "fit_mode": fit_mode, "include_unreviewed": include_unreviewed, "old_fits_cleaned": cleaned, "force_refit": force_refit}), now))
    con.commit(); con.close()
    return fit_id


def cleanup_old_fits(con: sqlite3.Connection, body_pk: int, keep_inactive: int = KEEP_INACTIVE_FITS) -> int:
    """Delete inactive fits for a body, keeping only the newest N if requested.

    Old fits are useful while debugging, but they quickly clutter the DB because
    every refit creates a new row. The active fit is always preserved. Deleting
    old fit rows also cleans fit_observations and prediction_cache via cascades.
    """
    keep = max(0, int(keep_inactive))
    rows = con.execute(
        "SELECT id FROM fits WHERE body_id = ? AND is_active = 0 ORDER BY id DESC",
        (body_pk,),
    ).fetchall()
    delete_ids = [int(r["id"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows[keep:]]
    if not delete_ids:
        return 0
    con.executemany("DELETE FROM fits WHERE id = ?", [(fid,) for fid in delete_ids])
    return len(delete_ids)


def model_from_active_fit(con: sqlite3.Connection, body_pk: int, fit_mode: str = "approved") -> Tuple[Dict[str, Any], model.FittedModel, int]:
    con.row_factory = sqlite3.Row
    ensure_illumination_columns(con)
    frow = con.execute(
        """
        SELECT id, system_id, params_json, fit_score, rms_altitude_deg, rms_heading_deg, time_weighting
          FROM fits WHERE body_id = ? AND fit_mode = ? AND is_active = 1 AND fit_status = 'ok'
         ORDER BY id DESC LIMIT 1
        """, (body_pk, (fit_mode or "approved").strip().lower()),
    ).fetchone()
    if not frow:
        raise ValueError(f"No active {fit_mode or 'approved'} fit for this body. Run the fit command first.")
    fit_id = int(frow["id"])
    system, body = raw_system_and_body(con, int(frow["system_id"]), body_pk)
    params = json.loads(frow["params_json"])
    p = params["params"]
    # Fits created before the V0.199 recursive illumination-source experiment did
    # not store a geometry mode and were calibrated with the empirical distant-star
    # vector.  Default missing metadata to legacy_distant so old accurate fits keep
    # predicting with the same model semantics.
    sun_geometry_mode = model.normalize_sun_geometry_mode(params.get("sun_geometry_mode") or params.get("illumination_geometry_mode") or "legacy_distant")
    if sun_geometry_mode == "auto":
        sun_geometry_mode = "legacy_distant"
    stored_time_weighting = bool(frow["time_weighting"])
    stored_weighting_mode = str(params.get("time_weighting_mode") or ("decay" if stored_time_weighting else "off"))
    recent_boost_scale = params.get("recent_boost_scale_hours")
    if recent_boost_scale is None and isinstance(params.get("recent_boost_scale"), dict):
        recent_boost_scale = params.get("recent_boost_scale", {}).get("boost_scale_hours")
    fit_observations = [o for _, o in observations_for_fit(con, fit_id)]
    fitted = model.make_model(
        body,
        (float(p["alpha_rad"]), float(p["beta_rad"]), float(p["gamma_rad"]), float(p["phase_rad"])),
        spin_sign=int(params["spin_sign"]), lon_sign=int(params["lon_sign"]), orbit_flip=int(params["orbit_flip"]),
        observations=fit_observations,
        score=float(frow["fit_score"] or 0.0), time_weighting=stored_time_weighting,
        time_ref=model.observation_time_reference(fit_observations) if stored_time_weighting else None,
        system=system,
        time_weighting_mode=stored_weighting_mode,
        recent_boost_max=float(params.get("recent_boost_max") or 2.0),
        recent_boost_scale_hours=None if recent_boost_scale in (None, "") else float(recent_boost_scale),
        sun_geometry_mode=sun_geometry_mode,
    )
    fitted.rms_altitude = float(frow["rms_altitude_deg"] or 0.0)
    fitted.rms_heading = None if frow["rms_heading_deg"] is None else float(frow["rms_heading_deg"])
    return system, fitted, fit_id


def predict_from_db(db_path: str, system_name: str, body_name: str, lat: float, lon: float, target_time: str) -> Dict[str, Any]:
    con = sqlite_connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    _, body_pk = lookup_system_body(con, system_name, body_name)
    system, fitted, fit_id = model_from_active_fit(con, body_pk)
    prediction = model.calculate_prediction(fitted, system, model.parse_utc(target_time), lat, lon)
    con.execute("INSERT INTO prediction_cache(body_id, fit_id, lat, lon, target_time_utc, prediction_json, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (body_pk, fit_id, lat, lon, prediction["target_time_utc"], json_dumps(prediction), utc_now()))
    con.commit(); con.close()
    return prediction


def db_summary(db_path: str) -> str:
    con = sqlite_connect(db_path)
    con.row_factory = sqlite3.Row
    lines = [f"Database: {db_path}"]
    for table in ("systems", "bodies", "observations", "fits", "fit_observations"):
        lines.append(f"{table}: {con.execute(f'SELECT COUNT(*) AS c FROM {table}').fetchone()['c']}")
    lines.append("")
    lines.append("Bodies with observations:")
    for r in con.execute(
        """
        SELECT s.name AS system_name, b.name AS body_name, COUNT(o.id) AS observations,
               SUM(CASE WHEN o.review_status = 'approved' THEN 1 ELSE 0 END) AS approved,
               MAX(CASE WHEN f.is_active = 1 THEN f.id END) AS active_fit_id,
               MAX(CASE WHEN f.is_active = 1 THEN f.fit_score END) AS active_fit_score
          FROM bodies b JOIN systems s ON s.id = b.system_id
          LEFT JOIN observations o ON o.body_id = b.id
          LEFT JOIN fits f ON f.body_id = b.id
         GROUP BY b.id HAVING observations > 0 ORDER BY s.name, b.name
        """
    ):
        suffix = f", active fit #{r['active_fit_id']} score={r['active_fit_score']:.3f}" if r["active_fit_id"] else ""
        lines.append(f"- {r['system_name']} / {r['body_name']}: {r['observations']} obs, {r['approved']} approved{suffix}")
    con.close()
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Compact SQLite database for Elite Dangerous day/night model")
    p.add_argument("--db", default="elite_daynight.db", help="SQLite database path")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Create/update the compact database schema")
    po = sub.add_parser("import-observations", help="Import observation CSV rows for a selected body")
    po.add_argument("--csv", required=True); po.add_argument("--system-name", required=True); po.add_argument("--body-name", required=True)
    po.add_argument("--system-address", type=int); po.add_argument("--review-status", default="new", choices=["new", "approved", "rejected", "needs_check", "corrected"])
    po.add_argument("--observer-name", default=""); po.add_argument("--source", default="local_csv")
    pf = sub.add_parser("fit", help="Fit an active v16 model from approved observations")
    pf.add_argument("--system-name", required=True); pf.add_argument("--body-name", required=True)
    pf.add_argument("--use-heading", action="store_true"); pf.add_argument("--time-weight", action="store_true"); pf.add_argument("--time-half-life-hours", type=float, default=24.0); pf.add_argument("--include-unreviewed", action="store_true", help="Use approved + new/needs_check observations for a provisional fit")
    pp = sub.add_parser("predict", help="Predict from the active stored fit")
    pp.add_argument("--system-name", required=True); pp.add_argument("--body-name", required=True); pp.add_argument("--lat", type=float, required=True); pp.add_argument("--lon", type=float, required=True); pp.add_argument("--time", required=True)
    pss = sub.add_parser("spansh-search", help="Search Spansh system autocomplete")
    pss.add_argument("--system-name", required=True)
    pss.add_argument("--limit", type=int, default=20)
    prs = sub.add_parser("resolve-system", help="Resolve a system name to the id64/SystemAddress used by Spansh")
    prs.add_argument("--system-name", required=True)
    pis = sub.add_parser("import-spansh", help="Fetch a system from Spansh and add compact fields to the database")
    pis.add_argument("--system-name", required=True)
    pis.add_argument("--body-name", required=True)
    pis.add_argument("--system-address", type=int, help="Optional id64/SystemAddress; recommended when Spansh autocomplete returns names only")
    pis.add_argument("--no-body-details", action="store_true", help="Do not call /api/body for each body; faster but less complete")
    pis.add_argument("--fit", action="store_true", help="Run a fit immediately after import, if approved observations already exist")
    pis.add_argument("--use-heading", action="store_true")
    pis.add_argument("--time-weight", action="store_true")
    pis.add_argument("--time-half-life-hours", type=float, default=24.0)
    sub.add_parser("summary", help="Print database summary")
    args = p.parse_args(argv)
    if args.cmd == "init":
        init_db(args.db); print(f"Initialised {args.db}")
    elif args.cmd == "import-observations":
        n = import_observations_csv(args.db, args.csv, args.system_name, args.body_name, args.system_address, args.review_status, args.observer_name, args.source)
        print(f"Imported/updated {n} observation rows from {args.csv}")
    elif args.cmd == "fit":
        fit_id = fit_body(args.db, args.system_name, args.body_name, args.use_heading, args.time_weight, args.time_half_life_hours)
        print(f"Created active fit id {fit_id}")
    elif args.cmd == "predict":
        print(json.dumps(predict_from_db(args.db, args.system_name, args.body_name, args.lat, args.lon, args.time), indent=2, ensure_ascii=False))
    elif args.cmd == "spansh-search":
        for item in spansh_search_systems(args.system_name, args.limit):
            if isinstance(item, dict):
                print(json.dumps(item, ensure_ascii=False))
            else:
                print(str(item))
    elif args.cmd == "resolve-system":
        addr, resolved_name, candidates = resolve_spansh_system_address(args.system_name)
        print(json.dumps({"system_name": resolved_name, "system_address": addr, "spansh_candidates": candidates}, indent=2, ensure_ascii=False))
    elif args.cmd == "import-spansh":
        system_id, body_pk, matched_body = import_spansh_system(
            args.db, args.system_name, args.body_name, args.system_address, fetch_body_details=not args.no_body_details
        )
        print(f"Imported Spansh system id {system_id}; selected body id {body_pk}: {matched_body}")
        if args.fit:
            fit_id = fit_body(args.db, args.system_name, matched_body, args.use_heading, args.time_weight, args.time_half_life_hours)
            print(f"Created active fit id {fit_id}")
    elif args.cmd == "summary":
        print(db_summary(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
