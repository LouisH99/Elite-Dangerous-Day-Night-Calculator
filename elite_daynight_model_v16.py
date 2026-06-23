#!/usr/bin/env python3
"""
Elite Dangerous day/night calculation core - v16 separated model module.

This file intentionally contains no tkinter GUI code. It can be imported by:
  * the desktop GUI wrapper
  * a CLI wrapper
  * a future website/backend API

The model is based on v16 model: moon-safe distant-star handling for
non-star parents, direct orbital vectors for direct star-orbiting planets,
and compact sunrise/sunset/day-period prediction helpers.
"""

from __future__ import annotations

import concurrent.futures
import csv
import json
import math
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import numpy as np
except Exception as exc:
    raise SystemExit("This module needs numpy. Install with: pip install numpy") from exc

try:
    from scipy.optimize import differential_evolution, minimize
except Exception:
    differential_evolution = None
    minimize = None

# ------------------------------ basic helpers ------------------------------


def parse_utc(value: str) -> datetime:
    """Parse UTC timestamps used by the website/API.

    Besides full ISO timestamps, accept user-friendly full-hour inputs such as:
      2026-06-02 18
      2026-06-02 18:00
      2026-06-02T18
      2026-06-02T18:00
    Missing minutes/seconds are normalized to :00. Naive values are treated as UTC.
    """
    import re

    s = str(value).strip()
    if not s:
        raise ValueError("empty UTC timestamp")

    # Normalize common UTC suffix and the first date/time separator.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)

    # Allow full-hour or minute-only input while preserving optional timezone suffixes.
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{1,2})(?:(?::(\d{1,2}))(?::(\d{1,2}))?)?(.+)?$", s)
    if m:
        date, hour, minute, second, suffix = m.groups()
        # Only treat a suffix as timezone when it looks like one. Otherwise leave
        # the original string for datetime.fromisoformat to validate.
        suffix = suffix or ""
        if suffix and not (suffix.startswith("+") or suffix.startswith("-")):
            pass
        else:
            s = f"{date}T{int(hour):02d}:{int(minute or 0):02d}:{int(second or 0):02d}{suffix}"

    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fmt_dur(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def wrap180(x: float) -> float:
    return (x + 180.0) % 360.0 - 180.0


def norm(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n == 0:
        return v
    return v / n


def deg(x: float) -> float:
    return math.degrees(x)


def rad(x: float) -> float:
    return math.radians(x)


def safe_asin(x: float) -> float:
    return math.asin(max(-1.0, min(1.0, x)))


LIGHT_SECOND_METRES = 299_792_458.0
SOLAR_RADIUS_METRES = 695_700_000.0
# Below this apparent radius, the visual disc edge crossing is practically
# indistinguishable from the centre-horizon crossing, so keep the report clean.
VISUAL_DISC_MIN_RADIUS_DEG = 1.0
# Prediction fallback defaults. The normal user-selected window is respected for
# the crossing list. If that window has no day/night transition, the next few
# transitions are searched up to this safety limit. The same extended search is
# used to calculate sunlight duration/day period when the selected window does
# not contain a complete cycle.
DEFAULT_MIN_FALLBACK_TRANSITIONS = 2
DEFAULT_MAX_EXTENDED_PREDICTION_HOURS = 30.0 * 24.0


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled", ""}


def _env_float(name: str, default: float, low: Optional[float] = None, high: Optional[float] = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except Exception:
        value = float(default)
    if low is not None:
        value = max(float(low), value)
    if high is not None:
        value = min(float(high), value)
    return float(value)


def _env_int(name: str, default: int, low: Optional[int] = None, high: Optional[int] = None) -> int:
    try:
        value = int(float(os.environ.get(name, str(default))))
    except Exception:
        value = int(default)
    if low is not None:
        value = max(int(low), value)
    if high is not None:
        value = min(int(high), value)
    return int(value)


# Extended fallback search optimisation.  Long no-transition searches can be
# expensive on Raspberry Pi systems with seasonal 400-700 h day/night cycles.
# The selected display window remains accurate; the coarse search is refined by
# bisection whenever a transition is detected.
PARALLEL_EXTENDED_SEARCH = _env_bool("ELITE_DAYNIGHT_PARALLEL_EXTENDED_SEARCH", True)
PREDICTION_WORKERS = _env_int("ELITE_DAYNIGHT_PREDICTION_WORKERS", 3, 1, 8)
PARALLEL_EXTENDED_MIN_HOURS = _env_float("ELITE_DAYNIGHT_PARALLEL_EXTENDED_MIN_HOURS", 168.0, 1.0, 8760.0)
PARALLEL_CHUNK_OVERLAP_HOURS = _env_float("ELITE_DAYNIGHT_PARALLEL_CHUNK_OVERLAP_HOURS", 2.0, 0.0, 24.0)
LONG_SEARCH_STEP_SECONDS = _env_float("ELITE_DAYNIGHT_LONG_SEARCH_STEP_SECONDS", 300.0, 30.0, 3600.0)

# Normal prediction window optimisation.  The default 30 s scanner is preserved
# for very short cycles; slower cycles use a larger coarse step and binary
# refinement around detected crossings.
ADAPTIVE_NORMAL_SEARCH = _env_bool("ELITE_DAYNIGHT_ADAPTIVE_NORMAL_SEARCH", True)
NORMAL_SEARCH_MIN_STEP_SECONDS = _env_float("ELITE_DAYNIGHT_NORMAL_SEARCH_MIN_STEP_SECONDS", 30.0, 1.0, 3600.0)
NORMAL_SEARCH_MAX_STEP_SECONDS = _env_float("ELITE_DAYNIGHT_NORMAL_SEARCH_MAX_STEP_SECONDS", 300.0, 1.0, 3600.0)
NORMAL_SEARCH_EARLY_STOP = _env_bool("ELITE_DAYNIGHT_NORMAL_SEARCH_EARLY_STOP", False)
NORMAL_SEARCH_EARLY_STOP_CROSSINGS = _env_int("ELITE_DAYNIGHT_NORMAL_SEARCH_EARLY_STOP_CROSSINGS", 8, 2, 48)

# Model fitting optimisation.  The 8 spin/lon/orbit-flip orientation branches
# are independent, so a single background fit job can evaluate them across a few
# CPU processes while the main process keeps all SQLite writes serialized.
PARALLEL_FIT = _env_bool("ELITE_DAYNIGHT_PARALLEL_FIT", True)
FIT_WORKERS = _env_int("ELITE_DAYNIGHT_FIT_WORKERS", 3, 1, 8)
FIT_PARALLEL_MIN_COMBOS = _env_int("ELITE_DAYNIGHT_FIT_PARALLEL_MIN_COMBOS", 4, 1, 8)

# Fitting evaluates the same observation timestamps thousands of times. Cache the
# recursive inertial sun vectors by system/body/time/orbit convention so the
# multi-star/moon fix does not make fitting painfully slow on a Raspberry Pi.
_ILLUMINATION_VECTOR_CACHE: Dict[Tuple[int, str, str, int, str], Tuple[Optional[np.ndarray], str, str]] = {}
_ILLUMINATION_VECTOR_CACHE_LIMIT = 50000


def rotz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def rotx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def quality_weight(q: str) -> float:
    q = (q or "").strip().lower()
    if q in ("high", "h", "good", "1"):
        return 1.0
    if q in ("medium", "med", "m", "2"):
        return 0.55
    if q in ("low", "l", "rough", "3"):
        return 0.25
    return 0.7


def observation_time_reference(observations: List["Observation"], explicit_ref: Optional[datetime] = None) -> Optional[datetime]:
    """Return the time used as the center of time weighting.

    If the user supplies an explicit reference, use it. Otherwise use the newest
    calibration observation. This makes the latest observation most influential,
    which is useful when the model has small unmodelled drift over days.
    """
    if explicit_ref is not None:
        return explicit_ref
    if not observations:
        return None
    return max(o.timestamp_utc for o in observations)


def recency_time_weight(
    obs_time: datetime,
    ref_time: Optional[datetime],
    half_life_hours: float = 24.0,
    minimum: float = 0.05,
) -> float:
    """Exponential time weight around ref_time.

    A half-life of 24 h means an observation 24 h away from the reference counts
    50% as much, 48 h away counts 25% as much, etc., before the minimum floor.
    """
    if ref_time is None or half_life_hours <= 0:
        return 1.0
    age_hours = abs((ref_time - obs_time).total_seconds()) / 3600.0
    w = math.exp(-math.log(2.0) * age_hours / half_life_hours)
    return max(float(minimum), float(w))


def fit_recent_boost_scale_details(body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the automatic time scale used for recent-observation boosting.

    V0.207 removes manual half-life tuning from the reviewer UI.  When a
    reviewer enables recent boosting, old observations keep their normal quality
    weight and newer observations receive a bonus.  The boost fades over a
    period derived from the body's estimated local day cycle, falling back to
    orbital period, rotation period, then 24 h when metadata is incomplete.
    """
    period_hours = 24.0
    basis = "fallback_24h"

    if body is not None:
        try:
            diff_p, sum_p = apparent_mean_periods(body)
            day_candidates = []
            for value in (diff_p, sum_p):
                v = float(value)
                if math.isfinite(v) and v > 0:
                    day_candidates.append(v / 3600.0)
            if day_candidates:
                period_hours = max(1.0 / 60.0, min(day_candidates))
                basis = "estimated_day_period"
            else:
                orbital = abs(float(body.get("OrbitalPeriod", 0.0) or 0.0)) / 3600.0
                rotation = abs(float(body.get("RotationPeriod", 0.0) or 0.0)) / 3600.0
                if orbital > 0 and math.isfinite(orbital):
                    period_hours = orbital
                    basis = "orbital_period"
                elif rotation > 0 and math.isfinite(rotation):
                    period_hours = rotation
                    basis = "rotation_period"
        except Exception:
            pass

    scale_hours = clamp_value(2.0 * float(period_hours), 12.0, 24.0 * 30.0)
    return {
        "characteristic_period_hours": float(period_hours),
        "boost_scale_hours": float(scale_hours),
        "basis": basis,
        "minimum_scale_hours": 12.0,
        "maximum_scale_hours": 24.0 * 30.0,
    }


def recent_observation_boost(
    obs_time: datetime,
    ref_time: Optional[datetime],
    body: Optional[Dict[str, Any]] = None,
    max_boost: float = 2.0,
    boost_scale_hours: Optional[float] = None,
) -> float:
    """Return a multiplicative boost for newer observations.

    The newest observation receives up to ``max_boost``.  Older observations
    smoothly return to 1.0x, so good old observations are never discarded.
    """
    if ref_time is None:
        return 1.0
    try:
        max_boost = max(1.0, float(max_boost))
    except Exception:
        max_boost = 2.0
    if max_boost <= 1.0:
        return 1.0
    if boost_scale_hours is None or boost_scale_hours <= 0:
        boost_scale_hours = float(fit_recent_boost_scale_details(body)["boost_scale_hours"])
    age_hours = max(0.0, (ref_time - obs_time).total_seconds() / 3600.0)
    boost = 1.0 + (max_boost - 1.0) * math.exp(-age_hours / max(float(boost_scale_hours), 1e-9))
    return max(1.0, min(max_boost, float(boost)))


def combined_observation_weight(
    obs: "Observation",
    time_weighting: bool = False,
    time_ref: Optional[datetime] = None,
    time_half_life_hours: float = 24.0,
    time_min_weight: float = 0.05,
    body: Optional[Dict[str, Any]] = None,
    time_weighting_mode: str = "recent_boost",
    recent_boost_max: float = 2.0,
    recent_boost_scale_hours: Optional[float] = None,
) -> float:
    w = quality_weight(obs.quality)
    if time_weighting:
        mode = (time_weighting_mode or "recent_boost").strip().lower()
        if mode in {"decay", "legacy_decay", "half_life"}:
            # Backwards-compatible support for old stored fits that were created
            # with the former half-life decay weighting.  New V0.207 fits use
            # recent_boost instead.
            w *= recency_time_weight(obs.timestamp_utc, time_ref, time_half_life_hours, time_min_weight)
        else:
            w *= recent_observation_boost(
                obs.timestamp_utc,
                time_ref,
                body=body,
                max_boost=recent_boost_max,
                boost_scale_hours=recent_boost_scale_hours,
            )
    return w


# ------------------------------ data loading ------------------------------


@dataclass
class Observation:
    timestamp_utc: datetime
    lat: float
    lon: float
    observation: str
    elevation: Optional[float]
    heading: Optional[float]
    quality: str = "medium"
    note: str = ""

    @property
    def target_altitude(self) -> Optional[float]:
        o = self.observation.lower().strip()
        if o in {"sunrise", "sunset", "horizon", "rise", "set"}:
            return 0.0
        if o in {"elevation", "altitude", "sun_altitude", "alt"}:
            return float(self.elevation if self.elevation is not None else 0.0)
        return None


def load_system_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def bodies(system: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(system.get("bodies"), list):
        return system["bodies"]
    if isinstance(system.get("Bodies"), list):
        return system["Bodies"]
    return []


def body_name(body: Dict[str, Any]) -> str:
    return str(body.get("BodyName") or body.get("bodyName") or body.get("Name") or body.get("name") or "")


def body_id(body: Dict[str, Any]) -> Optional[int]:
    v = body.get("BodyID", body.get("bodyId"))
    try:
        return int(v)
    except Exception:
        return None


def body_display_name(body: Dict[str, Any]) -> str:
    typ = body.get("BodyType") or body.get("type") or ""
    sub = body.get("PlanetClass") or body.get("StarType") or body.get("SubType") or body.get("subType") or ""
    nm = body_name(body)
    extra = " / ".join([x for x in [typ, sub] if x])
    return f"{nm} [{extra}]" if extra else nm


def find_body(system: Dict[str, Any], name: Optional[str]) -> Dict[str, Any]:
    bs = bodies(system)
    if not bs:
        raise ValueError("No bodies found in system JSON")
    if not name:
        # Prefer currently selected body/status body if present, else first planet.
        status_body = (system.get("status") or {}).get("BodyName")
        if status_body:
            try:
                return find_body(system, status_body)
            except Exception:
                pass
        for b in bs:
            if str(b.get("BodyType") or b.get("type") or "").lower() == "planet":
                return b
        return bs[0]
    lname = name.strip().lower()
    for b in bs:
        if body_name(b).lower() == lname:
            return b
    for b in bs:
        if lname in body_name(b).lower():
            return b
    raise ValueError(f"Body not found: {name}")


def load_observations_csv(path: str) -> List[Observation]:
    out: List[Observation] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            try:
                ts = parse_utc(row.get("timestamp_utc") or row.get("time") or row.get("timestamp") or "")
                lat = float(row.get("lat") or row.get("latitude") or 0.0)
                lon = float(row.get("lon") or row.get("longitude") or 0.0)
                obs = (row.get("observation") or row.get("obs") or "horizon").strip().lower()
                elev_raw = row.get("elevation")
                if elev_raw in (None, ""):
                    elev_raw = row.get("altitude_deg")
                if elev_raw in (None, ""):
                    elev_raw = row.get("sun_altitude")
                elevation = float(elev_raw) if elev_raw not in (None, "") else None
                heading_raw = row.get("heading")
                heading = float(heading_raw) % 360.0 if heading_raw not in (None, "") else None
                q = row.get("quality") or "medium"
                note = row.get("note") or ""
                out.append(Observation(ts, lat, lon, obs, elevation, heading, q, note))
            except Exception as exc:
                raise ValueError(f"Bad observation CSV row {i}: {exc}\nRow: {row}") from exc
    if not out:
        raise ValueError("No observations found in CSV")
    return out


# ------------------------------ orbital model ------------------------------


def get_required_float(body: Dict[str, Any], key: str) -> float:
    if key not in body:
        raise ValueError(f"Body {body_name(body)} is missing required field {key}")
    return float(body[key])


def scan_epoch(body: Dict[str, Any]) -> datetime:
    return parse_utc(str(body.get("timestamp") or body.get("Timestamp") or "1970-01-01T00:00:00Z"))


def kepler_eccentric_anomaly(mean_anomaly_rad: float, eccentricity: float) -> float:
    M = (mean_anomaly_rad + math.pi) % (2.0 * math.pi) - math.pi
    e = eccentricity
    E = M if e < 0.8 else math.pi
    for _ in range(50):
        f = E - e * math.sin(E) - M
        fp = 1.0 - e * math.cos(E)
        if fp == 0:
            break
        d = f / fp
        E -= d
        if abs(d) < 1e-13:
            break
    return E


def orbital_position_parent_to_body(body: Dict[str, Any], t: datetime) -> np.ndarray:
    """Approximate parent->body inertial vector from journal orbital elements."""
    epoch = scan_epoch(body)
    dt = (t - epoch).total_seconds()
    a = get_required_float(body, "SemiMajorAxis")
    e = float(body.get("Eccentricity", 0.0))
    P = get_required_float(body, "OrbitalPeriod")
    M0 = rad(float(body.get("MeanAnomaly", 0.0)))
    M = M0 + (2.0 * math.pi / P) * dt
    E = kepler_eccentric_anomaly(M, e)
    true_anom = 2.0 * math.atan2(math.sqrt(1 + e) * math.sin(E / 2.0), math.sqrt(1 - e) * math.cos(E / 2.0))
    r = a * (1.0 - e * math.cos(E))
    perifocal = np.array([r * math.cos(true_anom), r * math.sin(true_anom), 0.0])
    Om = rad(float(body.get("AscendingNode", 0.0)))
    inc = rad(float(body.get("OrbitalInclination", 0.0)))
    argp = rad(float(body.get("Periapsis", 0.0)))
    # Standard perifocal -> inertial transform. The global inertial axes are arbitrary;
    # the fitted body frame absorbs the unknown ED orientation convention.
    return rotz(Om) @ rotx(inc) @ rotz(argp) @ perifocal


def parent_entries(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    parents = body.get("Parents") or body.get("parents") or []
    return [p for p in parents if isinstance(p, dict)]


def direct_parent_kind(body: Dict[str, Any]) -> str:
    """Return the direct parent kind: star, planet, null, or unknown.

    Journal exports often use keys like {"Star": 0} or {"Planet": 17}.
    Spansh-derived records often use {"type": "Star", "id64": ...}.
    """
    parents = parent_entries(body)
    if not parents:
        return "unknown"
    p = parents[0]
    if "Star" in p:
        return "star"
    if "Planet" in p:
        return "planet"
    if "Null" in p:
        return "null"
    typ = str(p.get("type") or p.get("BodyType") or "").strip().lower()
    if typ in {"star", "planet", "null"}:
        return typ
    return "unknown"


def body_orbits_star_directly(body: Dict[str, Any]) -> bool:
    return direct_parent_kind(body) == "star"


def is_star_body(body: Dict[str, Any]) -> bool:
    return str(body.get("BodyType") or body.get("type") or "").strip().lower() == "star"


def body_ref_values(body: Dict[str, Any]) -> set:
    vals = set()
    for key in ("BodyID", "bodyId", "body_id", "bodyId64", "id64", "SystemAddress"):
        v = body.get(key)
        if v not in (None, ""):
            vals.add(str(v))
            try:
                vals.add(str(int(v)))
            except Exception:
                pass
    name = body_name(body)
    if name:
        vals.add(str(name).strip().lower())
    return vals


def parent_ref_value(p: Dict[str, Any]) -> Optional[Any]:
    if "Star" in p:
        return p.get("Star")
    if "Planet" in p:
        return p.get("Planet")
    for key in ("BodyID", "bodyId", "body_id", "id64", "bodyId64", "name", "Name"):
        if p.get(key) not in (None, ""):
            return p.get(key)
    return None


def find_body_by_ref(system: Dict[str, Any], ref: Any) -> Optional[Dict[str, Any]]:
    if system is None or ref is None:
        return None
    ref_s = str(ref).strip()
    ref_l = ref_s.lower()
    for b in bodies(system):
        vals = body_ref_values(b)
        if ref_s in vals or ref_l in vals:
            return b
    return None


def direct_parent_body(system: Dict[str, Any], body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    parents = parent_entries(body)
    if not parents or system is None:
        return None
    p = parents[0]
    if "Null" in p or str(p.get("type") or "").strip().lower() == "null":
        return None
    return find_body_by_ref(system, parent_ref_value(p))


def parent_body_name(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> Optional[str]:
    if system is None:
        parents = parent_entries(body)
        if parents:
            return str(parents[0].get("name") or parents[0].get("Name") or "") or None
        return None
    pb = direct_parent_body(system, body)
    if pb is not None:
        return body_name(pb)
    parents = parent_entries(body)
    if parents:
        p = parents[0]
        return str(p.get("name") or p.get("Name") or p.get("type") or next(iter(p.keys()), "")) or None
    return None


def find_star_by_name(system: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    needle = str(name or "").strip().lower()
    if not needle:
        return None
    for b in bodies(system):
        if is_star_body(b) and body_name(b).strip().lower() == needle:
            return b
    return None


def system_name_value(system: Optional[Dict[str, Any]]) -> str:
    if not system:
        return ""
    return str(system.get("name") or system.get("Name") or system.get("systemName") or system.get("StarSystem") or "").strip()


def find_star_by_letter(system: Dict[str, Any], letter: str) -> Optional[Dict[str, Any]]:
    sysname = system_name_value(system)
    candidates = []
    wanted = str(letter).strip().upper()
    if not wanted:
        return None
    exact = f"{sysname} {wanted}".strip().lower()
    for b in bodies(system):
        if not is_star_body(b):
            continue
        name = body_name(b).strip()
        if name.lower() == exact or name.upper().endswith(" " + wanted):
            candidates.append(b)
    return candidates[0] if candidates else None


def body_designator(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> str:
    name = body_name(body).strip()
    sysname = system_name_value(system)
    if sysname and name.lower().startswith(sysname.lower()):
        return name[len(sysname):].strip()
    return name


def combined_name_star_default(system: Dict[str, Any], body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Infer Elite's likely single illumination source for AB/BC/ABC barycentre names.

    Elite appears to light a body from one star only. For combined-name orbits we
    avoid treating a Null/barycentre as the light source and choose a star rule
    that can be manually overridden later.
    """
    designator = body_designator(system, body)
    token = designator.split()[0].upper() if designator.split() else ""
    if token.startswith("ABC"):
        st = find_star_by_letter(system, "A") or find_main_star(system)
        return st, "combined-name ABC rule -> primary star A"
    if token.startswith("AB"):
        st = find_star_by_letter(system, "A") or find_main_star(system)
        return st, "combined-name AB rule -> star A"
    if token.startswith("BC"):
        st = find_star_by_letter(system, "B") or find_main_star(system)
        return st, "combined-name BC rule -> star B (uncertain; override if needed)"
    return None, None


def find_parent_star(system: Dict[str, Any], body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the first real star in the resolved parent chain."""
    if system is None:
        return None
    seen = set()
    cur = body
    for _ in range(12):
        parent = direct_parent_body(system, cur)
        if parent is None:
            return None
        key = body_name(parent).lower() or str(parent.get("BodyID") or parent.get("bodyId") or id(parent))
        if key in seen:
            return None
        seen.add(key)
        if is_star_body(parent):
            return parent
        cur = parent
    return None


def find_main_star(system: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for b in bodies(system):
        if is_star_body(b) and bool((b.get("rawSpanshBody") or {}).get("is_main_star")):
            return b
    # Prefer a star named "<system> A" when Spansh did not set is_main_star.
    st = find_star_by_letter(system, "A")
    if st is not None:
        return st
    for b in bodies(system):
        if is_star_body(b):
            return b
    return None


def illumination_source_info(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """Return (star, mode, reason) for the light source.

    mode is one of: explicit, inferred, fallback.
    """
    explicit = str(body.get("illumination_source_star_name") or body.get("IlluminationSourceStarName") or "").strip()
    if explicit.lower() == "auto":
        explicit = ""
    if system and explicit:
        st = find_star_by_name(system, explicit)
        if st is not None:
            return st, "explicit", f"manual override: {body_name(st)}"
        return None, "fallback", f"manual override star not found: {explicit}"

    if system:
        st = find_parent_star(system, body)
        if st is not None:
            return st, "inferred", f"parent chain contains {body_name(st)}"
        st, reason = combined_name_star_default(system, body)
        if st is not None:
            return st, "inferred", reason or f"combined-name rule -> {body_name(st)}"
        st = find_main_star(system)
        if st is not None:
            return st, "inferred", f"system primary/main star fallback: {body_name(st)}"
    return None, "fallback", "fitted distant-star vector; no resolvable illumination star"


def illumination_source_name(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> str:
    st, _mode, _reason = illumination_source_info(system, body)
    return body_name(st) if st is not None else "unknown"


def sun_source_mode_label(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> str:
    _st, mode, _reason = illumination_source_info(system, body)
    return mode


def sun_vector_source_label(body: Dict[str, Any], system: Optional[Dict[str, Any]] = None) -> str:
    st, mode, reason = illumination_source_info(system, body)
    if st is not None:
        return f"{mode} illumination source {body_name(st)} ({reason})"
    return f"fallback fitted distant-star vector ({reason})"


def orbit_context_label(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> str:
    kind = direct_parent_kind(body)
    pname = parent_body_name(system, body) or "unknown parent"
    if kind == "star":
        return f"target orbits star {pname}"
    if kind == "planet":
        return f"target is moon of {pname}"
    if kind == "null":
        return "target orbits a Null/barycentre parent"
    return f"target parent is {pname}"


def normalize_sun_geometry_mode(value: Optional[str]) -> str:
    """Normalize stored/user-facing sun geometry mode aliases.

    recursive_source / recursive_star_vector uses the V0.199+ recursive
    parent-chain illumination vector.

    legacy_distant / legacy_fitted_sun_direction preserves the pre-V0.199
    v15 behaviour: direct star-orbiting bodies use their parent-star orbital
    vector, while moons/null-parent bodies fit a stable distant effective sun
    direction from observations.
    """
    mode = str(value or "auto").strip().lower()
    aliases = {
        "recursive": "recursive_source",
        "recursive_star_vector": "recursive_source",
        "recursive-source": "recursive_source",
        "legacy": "legacy_distant",
        "legacy_fitted_sun_direction": "legacy_distant",
        "legacy-fitted-sun-direction": "legacy_distant",
        "legacy_fitted_distant_star": "legacy_distant",
        "legacy-distant": "legacy_distant",
        "fitted_distant_star": "legacy_distant",
        "v15": "legacy_distant",
        "v15_legacy": "legacy_distant",
    }
    mode = aliases.get(mode, mode)
    if mode in {"auto", "recursive_source", "legacy_distant"}:
        return mode
    return "auto"


def body_parent_chain_has_null(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> bool:
    """Return True when the resolved ancestor chain contains a Null/barycentre.

    This is important because many Spansh/Journal exports can describe a moon as
    moon -> planet -> Null/barycentre -> star.  The recursive physical vector
    can be useful for clean star/planet/moon chains, but the Null/barycentre
    branch has caused large regressions on bodies that were accurately handled
    by the old empirical v15 distant-sun fit.
    """
    cur = body
    seen = set()
    for _ in range(16):
        # Stop once a real star is reached. Many real stars themselves orbit a
        # Null/barycentre, but that does not make the planet/moon-to-star
        # illumination vector uncertain. The problematic case is a Null between
        # the target body and the star, such as moon -> planet -> Null.
        if is_star_body(cur):
            return False
        if direct_parent_kind(cur) == "null":
            return True
        key = body_name(cur).lower() or str(cur.get("BodyID") or cur.get("bodyId") or id(cur))
        if key in seen:
            return False
        seen.add(key)
        if system is None:
            return False
        parent = direct_parent_body(system, cur)
        if parent is None:
            return False
        cur = parent
    return False


def recommended_sun_geometry_mode(system: Optional[Dict[str, Any]], body: Dict[str, Any]) -> Tuple[str, str]:
    """Choose a safe default geometry mode for fitting.

    The rule intentionally preserves v15 empirical behaviour for uncertain
    Null/barycentre chains while allowing the V0.199+ recursive source geometry
    where it is known to help, such as clean moon-of-planet-around-star systems
    and explicit illumination-source overrides.
    """
    explicit = str(body.get("illumination_source_star_name") or body.get("IlluminationSourceStarName") or "").strip()
    if explicit and explicit.lower() != "auto":
        return "recursive_source", "explicit illumination-source override"
    if system is None:
        return "legacy_distant", "no system context; preserve empirical v15 geometry"
    if body_parent_chain_has_null(system, body):
        return "legacy_distant", "parent chain contains Null/barycentre"
    kind = direct_parent_kind(body)
    if kind == "star":
        return "recursive_source", "direct star parent"
    if find_parent_star(system, body) is not None:
        return "recursive_source", "clean parent chain contains real star"
    return "legacy_distant", "illumination geometry uncertain; preserve empirical v15 geometry"


def body_position_inertial(system: Dict[str, Any], body: Dict[str, Any], t: datetime, orbit_flip: int = 1, _seen: Optional[set] = None) -> Optional[np.ndarray]:
    """Approximate barycentric inertial position for a body.

    Null parents are treated as a local inertial root. For star/planet parents we
    recursively add parent->child orbital vectors. The global axes are still
    arbitrary; the fitted body frame absorbs that convention.
    """
    if _seen is None:
        _seen = set()
    key = body_name(body).lower() or str(body.get("BodyID") or body.get("bodyId") or id(body))
    if key in _seen:
        return None
    _seen.add(key)
    try:
        own = float(orbit_flip) * orbital_position_parent_to_body(body, t)
    except Exception:
        own = np.zeros(3)
    parent = direct_parent_body(system, body)
    if parent is None:
        return own
    ppos = body_position_inertial(system, parent, t, orbit_flip=orbit_flip, _seen=_seen)
    if ppos is None:
        return own
    return ppos + own


def _illumination_vector_inertial_uncached(system: Optional[Dict[str, Any]], body: Dict[str, Any], t: datetime, orbit_flip: int = 1) -> Tuple[Optional[np.ndarray], str, str]:
    if system is None:
        return None, "fallback", "no system context attached to model"
    star, mode, reason = illumination_source_info(system, body)
    if star is None:
        return None, mode, reason
    spos = body_position_inertial(system, star, t, orbit_flip=orbit_flip)
    bpos = body_position_inertial(system, body, t, orbit_flip=orbit_flip)
    if spos is None or bpos is None:
        return None, "fallback", "could not resolve recursive orbital positions"
    vec = spos - bpos
    if float(np.linalg.norm(vec)) <= 0:
        return None, "fallback", "zero-length illumination vector"
    return norm(vec), mode, reason


def illumination_vector_inertial(system: Optional[Dict[str, Any]], body: Dict[str, Any], t: datetime, orbit_flip: int = 1) -> Tuple[Optional[np.ndarray], str, str]:
    if system is None:
        return None, "fallback", "no system context attached to model"
    explicit = str(body.get("illumination_source_star_name") or body.get("IlluminationSourceStarName") or "").strip()
    key = (id(system), body_name(body), format_utc(t), int(orbit_flip), explicit)
    cached = _ILLUMINATION_VECTOR_CACHE.get(key)
    if cached is not None:
        return cached
    value = _illumination_vector_inertial_uncached(system, body, t, orbit_flip=orbit_flip)
    if len(_ILLUMINATION_VECTOR_CACHE) > _ILLUMINATION_VECTOR_CACHE_LIMIT:
        _ILLUMINATION_VECTOR_CACHE.clear()
    _ILLUMINATION_VECTOR_CACHE[key] = value
    return value


def star_radius_metres(star: Dict[str, Any]) -> Optional[float]:
    for key in ("Radius", "radius"):
        try:
            value = star.get(key)
            if value not in (None, "") and float(value) > 0:
                return float(value)
        except Exception:
            pass
    try:
        solar_r = (star.get("rawSpanshBody") or {}).get("solar_radius")
        if solar_r not in (None, "") and float(solar_r) > 0:
            return float(solar_r) * SOLAR_RADIUS_METRES
    except Exception:
        pass
    return None


def star_distance_metres(system: Dict[str, Any], body: Dict[str, Any], t: datetime) -> Optional[float]:
    vec, _mode, _reason = illumination_vector_inertial(system, body, t, orbit_flip=1)
    if vec is not None:
        # Recompute unnormalised vector for distance.
        star, _m, _r = illumination_source_info(system, body)
        if star is not None:
            spos = body_position_inertial(system, star, t, orbit_flip=1)
            bpos = body_position_inertial(system, body, t, orbit_flip=1)
            if spos is not None and bpos is not None:
                d = float(np.linalg.norm(spos - bpos))
                if d > 0:
                    return d

    # Fallback to Spansh/Journal distance-to-arrival when recursive geometry is incomplete.
    for key in ("DistanceFromArrivalLS", "distanceToArrival", "distance_to_arrival"):
        try:
            value = body.get(key)
            if value not in (None, "") and float(value) > 0:
                return float(value) * LIGHT_SECOND_METRES
        except Exception:
            pass
    try:
        raw = body.get("rawSpanshBody") or {}
        value = raw.get("distance_to_arrival")
        if value not in (None, "") and float(value) > 0:
            return float(value) * LIGHT_SECOND_METRES
    except Exception:
        pass
    return None


def star_angular_radius_deg(system: Optional[Dict[str, Any]], body: Dict[str, Any], t: datetime) -> Optional[float]:
    if not system:
        return None
    star, _mode, _reason = illumination_source_info(system, body)
    if not star:
        return None
    R = star_radius_metres(star)
    d = star_distance_metres(system, body, t)
    if R is None or d is None or d <= 0 or R <= 0:
        return None
    return deg(safe_asin(min(0.999999, R / d)))


@dataclass
class FittedModel:
    body: Dict[str, Any]
    params: Tuple[float, float, float, float]
    spin_sign: int
    lon_sign: int
    orbit_flip: int
    score: float
    rms_altitude: float
    rms_heading: Optional[float]
    observations: List[Observation]
    time_weighting: bool = False
    time_ref: Optional[datetime] = None
    time_half_life_hours: float = 24.0
    time_min_weight: float = 0.05
    system: Optional[Dict[str, Any]] = None
    time_weighting_mode: str = "recent_boost"
    recent_boost_max: float = 2.0
    recent_boost_scale_hours: Optional[float] = None
    fit_parallel_enabled: bool = False
    fit_workers: int = 1
    fit_evaluated_combos: int = 0
    fit_elapsed_seconds: Optional[float] = None
    # "recursive_source" uses the V0.199+ physical/recursive star-vector path.
    # "legacy_distant" keeps the pre-V0.199 empirical fitted distant-star vector.
    sun_geometry_mode: str = "recursive_source"
    def predict(self, t: datetime, lat_deg: float, lon_deg: float) -> Tuple[float, float]:
        return predict_alt_az(self, t, lat_deg, lon_deg)


def body_frame_matrix(alpha: float, beta: float, gamma: float) -> np.ndarray:
    # Euler-like orientation. Gamma and phase are partly degenerate, but this makes
    # fitting stable and absorbs ED's unknown body-axis convention.
    return rotz(alpha) @ rotx(beta) @ rotz(gamma)


def sun_vector_body(model: FittedModel, t: datetime) -> np.ndarray:
    alpha, beta, gamma, phase = model.params
    if getattr(model, "sun_geometry_mode", "recursive_source") == "legacy_distant":
        # Pre-V0.199/v15-compatible mode. Direct star-orbiting bodies use the
        # parent-star orbital vector, while moons and Null/barycentre cases fit
        # a stable distant effective sun direction from observations.
        if body_orbits_star_directly(model.body):
            r_parent_to_body = orbital_position_parent_to_body(model.body, t)
            s_inertial = norm(-float(model.orbit_flip) * r_parent_to_body)
        else:
            s_inertial = np.array([float(model.orbit_flip), 0.0, 0.0])
    else:
        s_inertial, _mode, _reason = illumination_vector_inertial(model.system, model.body, t, orbit_flip=model.orbit_flip)
        if s_inertial is None:
            # Last-resort legacy fallback. This keeps older/incomplete data usable,
            # but reports should show Sun-source mode: fallback.
            s_inertial = np.array([float(model.orbit_flip), 0.0, 0.0])
    epoch = scan_epoch(model.body)
    dt = (t - epoch).total_seconds()
    rot_period = abs(get_required_float(model.body, "RotationPeriod"))
    spin = float(model.spin_sign) * (2.0 * math.pi / rot_period) * dt + phase
    R0 = body_frame_matrix(alpha, beta, gamma)
    return norm(rotz(-spin) @ (R0.T @ s_inertial))


def local_vectors(lat_deg: float, lon_deg: float, lon_sign: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    lat = rad(lat_deg)
    lon = rad(float(lon_sign) * lon_deg)
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    return norm(up), norm(north), norm(east)


def predict_alt_az(model: FittedModel, t: datetime, lat_deg: float, lon_deg: float) -> Tuple[float, float]:
    s = sun_vector_body(model, t)
    up, north, east = local_vectors(lat_deg, lon_deg, model.lon_sign)
    alt = deg(safe_asin(float(np.dot(up, s))))
    az = deg(math.atan2(float(np.dot(s, east)), float(np.dot(s, north)))) % 360.0
    return alt, az


def daylight_sun_peak(
    model: FittedModel,
    target_time: datetime,
    lat_deg: float,
    lon_deg: float,
    current_altitude_deg: float,
    current_heading_deg: float,
    sun_altitude_trend: str,
    next_sunset: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    if current_altitude_deg <= 0.0:
        return None

    trend = (sun_altitude_trend or "").strip().lower()
    if "falling" in trend:
        return {
            "status": "already_passed",
            "time_utc": format_utc(target_time),
            "seconds_from_target": 0.0,
            "elevation_deg": float(current_altitude_deg),
            "heading_deg": float(current_heading_deg),
        }
    if "level" in trend:
        return {
            "status": "near_peak",
            "time_utc": format_utc(target_time),
            "seconds_from_target": 0.0,
            "elevation_deg": float(current_altitude_deg),
            "heading_deg": float(current_heading_deg),
        }
    if next_sunset is None:
        return {
            "status": "unknown",
            "time_utc": None,
            "seconds_from_target": None,
            "elevation_deg": None,
            "heading_deg": None,
        }

    total_seconds = (next_sunset - target_time).total_seconds()
    if total_seconds <= 0.0:
        return {
            "status": "already_passed",
            "time_utc": format_utc(target_time),
            "seconds_from_target": 0.0,
            "elevation_deg": float(current_altitude_deg),
            "heading_deg": float(current_heading_deg),
        }

    hours = total_seconds / 3600.0
    intervals = int(_clamp_float(max(24.0, hours * 4.0), 24.0, 160.0))
    samples: List[Tuple[float, float]] = []
    for i in range(intervals + 1):
        offset = total_seconds * (float(i) / float(intervals))
        alt, _az = model.predict(target_time + timedelta(seconds=offset), lat_deg, lon_deg)
        samples.append((offset, alt))

    best_index = max(range(len(samples)), key=lambda idx: samples[idx][1])
    if best_index == 0:
        peak_time = target_time
    else:
        lo = samples[max(0, best_index - 1)][0]
        hi = samples[min(len(samples) - 1, best_index + 1)][0]
        if hi <= lo:
            peak_time = target_time + timedelta(seconds=samples[best_index][0])
        else:
            # Golden-section search on the best coarse bracket.  The sun
            # altitude over one daylight arc is expected to be locally unimodal.
            gr = (math.sqrt(5.0) - 1.0) / 2.0
            x1 = hi - gr * (hi - lo)
            x2 = lo + gr * (hi - lo)
            y1 = model.predict(target_time + timedelta(seconds=x1), lat_deg, lon_deg)[0]
            y2 = model.predict(target_time + timedelta(seconds=x2), lat_deg, lon_deg)[0]
            for _ in range(32):
                if y1 < y2:
                    lo = x1
                    x1 = x2
                    y1 = y2
                    x2 = lo + gr * (hi - lo)
                    y2 = model.predict(target_time + timedelta(seconds=x2), lat_deg, lon_deg)[0]
                else:
                    hi = x2
                    x2 = x1
                    y2 = y1
                    x1 = hi - gr * (hi - lo)
                    y1 = model.predict(target_time + timedelta(seconds=x1), lat_deg, lon_deg)[0]
            peak_time = target_time + timedelta(seconds=(lo + hi) / 2.0)

    peak_alt, peak_heading = model.predict(peak_time, lat_deg, lon_deg)
    seconds_from_target = max(0.0, (peak_time - target_time).total_seconds())
    return {
        "status": "near_peak" if seconds_from_target <= 300.0 else "upcoming",
        "time_utc": format_utc(peak_time),
        "seconds_from_target": float(seconds_from_target),
        "elevation_deg": float(peak_alt),
        "heading_deg": float(peak_heading),
    }


def make_model(
    body: Dict[str, Any],
    params: Tuple[float, float, float, float],
    spin_sign: int,
    lon_sign: int,
    orbit_flip: int,
    observations: List[Observation],
    score: float = 0.0,
    time_weighting: bool = False,
    time_ref: Optional[datetime] = None,
    time_half_life_hours: float = 24.0,
    time_min_weight: float = 0.05,
    system: Optional[Dict[str, Any]] = None,
    time_weighting_mode: str = "recent_boost",
    recent_boost_max: float = 2.0,
    recent_boost_scale_hours: Optional[float] = None,
    sun_geometry_mode: Optional[str] = None,
) -> FittedModel:
    effective_geometry_mode = normalize_sun_geometry_mode(sun_geometry_mode)
    if effective_geometry_mode == "auto":
        effective_geometry_mode, _geometry_reason = recommended_sun_geometry_mode(system, body)
    return FittedModel(
        body=body,
        params=params,
        spin_sign=spin_sign,
        lon_sign=lon_sign,
        orbit_flip=orbit_flip,
        score=score,
        rms_altitude=0.0,
        rms_heading=None,
        observations=observations,
        time_weighting=time_weighting,
        time_ref=time_ref,
        time_half_life_hours=time_half_life_hours,
        time_min_weight=time_min_weight,
        system=system,
        time_weighting_mode=time_weighting_mode,
        recent_boost_max=recent_boost_max,
        recent_boost_scale_hours=recent_boost_scale_hours,
        sun_geometry_mode=effective_geometry_mode,
    )


def model_loss(model: FittedModel, use_heading: bool = True, horizon_for_night_deg: float = 0.0) -> Tuple[float, float, Optional[float]]:
    alt_ss = 0.0
    alt_w = 0.0
    head_ss = 0.0
    head_w = 0.0
    total = 0.0
    total_w = 0.0
    for obs in model.observations:
        w = combined_observation_weight(
            obs,
            time_weighting=model.time_weighting,
            time_ref=model.time_ref,
            time_half_life_hours=model.time_half_life_hours,
            time_min_weight=model.time_min_weight,
            body=model.body,
            time_weighting_mode=getattr(model, "time_weighting_mode", "recent_boost"),
            recent_boost_max=getattr(model, "recent_boost_max", 2.0),
            recent_boost_scale_hours=getattr(model, "recent_boost_scale_hours", None),
        )
        alt, az = model.predict(obs.timestamp_utc, obs.lat, obs.lon)
        target = obs.target_altitude
        o = obs.observation.lower().strip()
        if target is not None:
            # Elevation observations are compared directly to calculated sun-centre altitude.
            err = alt - target
            alt_ss += w * err * err
            alt_w += w
            total += w * err * err
            total_w += w
        elif o == "night":
            # Weak inequality. Night usually means the sun centre is below horizon.
            if alt > horizon_for_night_deg:
                err = alt - horizon_for_night_deg + 2.0
                total += w * err * err
                total_w += w
        elif o == "day":
            if alt < horizon_for_night_deg:
                err = alt - horizon_for_night_deg - 2.0
                total += w * err * err
                total_w += w
        if use_heading and obs.heading is not None:
            # Heading readings are useful, but softer than altitude/horizon observations.
            # A 9 degree heading error counts roughly like a 3 degree altitude error.
            herr = wrap180(az - obs.heading)
            scaled = herr / 3.0
            head_ss += w * herr * herr
            head_w += w
            total += w * scaled * scaled
            total_w += w
    score = math.sqrt(total / max(total_w, 1e-9))
    rms_alt = math.sqrt(alt_ss / alt_w) if alt_w > 0 else 0.0
    rms_head = math.sqrt(head_ss / head_w) if head_w > 0 else None
    return score, rms_alt, rms_head



def _split_fit_candidate(x: Iterable[float]) -> Tuple[float, float, float, float]:
    vals = [float(v) for v in x]
    return tuple(vals[:4])  # type: ignore[return-value]


def _fit_combo_worker(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Fit one independent spin/lon/orbit orientation branch.

    This worker is intentionally pure model code: it receives body/system and
    observations as serialisable objects and returns the best candidate for this
    branch.  It never opens or writes SQLite, which keeps background refits safe
    when ProcessPoolExecutor is used on a Raspberry Pi.
    """
    body: Dict[str, Any] = payload["body"]
    observations: List[Observation] = payload["observations"]
    system: Optional[Dict[str, Any]] = payload.get("system")
    use_heading = bool(payload.get("use_heading", True))
    seed = int(payload.get("seed", 42))
    combo_index = int(payload.get("combo_index", 0))
    spin_sign, lon_sign, orbit_flip = tuple(payload["combo"])
    time_weighting = bool(payload.get("time_weighting", False))
    time_half_life_hours = float(payload.get("time_half_life_hours", 24.0))
    time_ref: Optional[datetime] = payload.get("time_ref")
    time_min_weight = float(payload.get("time_min_weight", 0.05))
    time_weighting_mode = str(payload.get("time_weighting_mode") or "recent_boost")
    recent_boost_max = float(payload.get("recent_boost_max", 2.0))
    recent_boost_scale_hours: Optional[float] = payload.get("recent_boost_scale_hours")
    sun_geometry_mode = str(payload.get("sun_geometry_mode") or "recursive_source")
    bounds = [(-math.pi, math.pi), (-math.pi, math.pi), (-math.pi, math.pi), (-math.pi, math.pi)]

    def evaluate_tuple(x: Iterable[float]) -> float:
        params = _split_fit_candidate(x)
        m = make_model(
            body, params, int(spin_sign), int(lon_sign), int(orbit_flip), observations,
            time_weighting=time_weighting,
            time_ref=time_ref,
            time_half_life_hours=time_half_life_hours,
            time_min_weight=time_min_weight,
            system=system,
            time_weighting_mode=time_weighting_mode,
            recent_boost_max=recent_boost_max,
            recent_boost_scale_hours=recent_boost_scale_hours,
            sun_geometry_mode=sun_geometry_mode,
        )
        return model_loss(m, use_heading=use_heading)[0]

    if differential_evolution is not None:
        result = differential_evolution(
            lambda x: evaluate_tuple(x),
            bounds,
            seed=seed,
            tol=1e-7,
            popsize=8,
            maxiter=220,
            polish=True,
            workers=1,
        )
        params = _split_fit_candidate(result.x)
    else:
        # Pure-Python fallback: random global search + simple coordinate pattern search.
        rng = random.Random(seed + combo_index * 1009)
        candidates: List[Tuple[float, Tuple[float, float, float, float]]] = []
        ndim = len(bounds)
        for _ in range(2500):
            x = tuple(rng.uniform(bounds[i][0], bounds[i][1]) for i in range(ndim))
            candidates.append((evaluate_tuple(x), x))
        candidates.sort(key=lambda p: p[0])
        best_params = candidates[0][1]
        best_score = candidates[0][0]
        for _, start in candidates[:20]:
            x = list(start)
            step = math.pi / 3.0
            best = evaluate_tuple(x)
            while step > 1e-5:
                improved = False
                for j in range(len(bounds)):
                    for direction in (-1.0, 1.0):
                        y = x[:]
                        y[j] += direction * step
                        if j < 4:
                            y[j] = (y[j] + math.pi) % (2.0 * math.pi) - math.pi
                        else:
                            y[j] = max(bounds[j][0], min(bounds[j][1], y[j]))
                        val = evaluate_tuple(y)
                        if val < best:
                            x, best, improved = y, val, True
                if not improved:
                    step *= 0.55
            if best < best_score:
                best_score = best
                best_params = tuple(float(v) for v in x)  # type: ignore[assignment]
        params = _split_fit_candidate(best_params)

    m = make_model(
        body, params, int(spin_sign), int(lon_sign), int(orbit_flip), observations,
        time_weighting=time_weighting,
        time_ref=time_ref,
        time_half_life_hours=time_half_life_hours,
        time_min_weight=time_min_weight,
        system=system,
        time_weighting_mode=time_weighting_mode,
        recent_boost_max=recent_boost_max,
        recent_boost_scale_hours=recent_boost_scale_hours,
        sun_geometry_mode=sun_geometry_mode,
    )
    score, rms_alt, rms_head = model_loss(m, use_heading=use_heading)
    return {
        "score": float(score),
        "rms_altitude": float(rms_alt),
        "rms_heading": None if rms_head is None else float(rms_head),
        "params": tuple(float(v) for v in params),
        "spin_sign": int(spin_sign),
        "lon_sign": int(lon_sign),
        "orbit_flip": int(orbit_flip),
        "combo_index": int(combo_index),
    }


def _model_from_fit_combo_result(
    result: Dict[str, Any],
    *,
    body: Dict[str, Any],
    observations: List[Observation],
    time_weighting: bool,
    time_ref: Optional[datetime],
    time_half_life_hours: float,
    time_min_weight: float,
    system: Optional[Dict[str, Any]],
    time_weighting_mode: str,
    recent_boost_max: float,
    recent_boost_scale_hours: Optional[float],
    sun_geometry_mode: str,
) -> FittedModel:
    m = make_model(
        body,
        tuple(float(v) for v in result["params"]),  # type: ignore[arg-type]
        int(result["spin_sign"]),
        int(result["lon_sign"]),
        int(result["orbit_flip"]),
        observations,
        time_weighting=time_weighting,
        time_ref=time_ref,
        time_half_life_hours=time_half_life_hours,
        time_min_weight=time_min_weight,
        system=system,
        time_weighting_mode=time_weighting_mode,
        recent_boost_max=recent_boost_max,
        recent_boost_scale_hours=recent_boost_scale_hours,
        sun_geometry_mode=sun_geometry_mode,
    )
    m.score = float(result["score"])
    m.rms_altitude = float(result["rms_altitude"])
    m.rms_heading = None if result.get("rms_heading") is None else float(result["rms_heading"])
    return m


def fit_model(
    body: Dict[str, Any],
    observations: List[Observation],
    use_heading: bool = True,
    seed: int = 42,
    time_weighting: bool = False,
    time_half_life_hours: float = 24.0,
    time_ref: Optional[datetime] = None,
    time_min_weight: float = 0.05,
    system: Optional[Dict[str, Any]] = None,
    time_weighting_mode: str = "recent_boost",
    recent_boost_max: float = 2.0,
    sun_geometry_mode: str = "recursive_source",
) -> FittedModel:
    sun_geometry_mode = normalize_sun_geometry_mode(sun_geometry_mode)
    if sun_geometry_mode == "auto":
        selected_mode, selected_reason = recommended_sun_geometry_mode(system, body)
        selected = fit_model(
            body, observations, use_heading=use_heading, seed=seed,
            time_weighting=time_weighting, time_half_life_hours=time_half_life_hours,
            time_ref=time_ref, time_min_weight=time_min_weight, system=system,
            time_weighting_mode=time_weighting_mode, recent_boost_max=recent_boost_max,
            sun_geometry_mode=selected_mode,
        )
        # Safety valve: if the selected mode is very poor, try the alternative
        # and keep it only if it is a major improvement. This catches incomplete
        # or misleading orbital metadata without making every normal refit twice
        # as expensive.
        if selected.rms_altitude > 3.0:
            alternative_mode = "legacy_distant" if selected_mode == "recursive_source" else "recursive_source"
            try:
                alternative = fit_model(
                    body, observations, use_heading=use_heading, seed=seed,
                    time_weighting=time_weighting, time_half_life_hours=time_half_life_hours,
                    time_ref=time_ref, time_min_weight=time_min_weight, system=system,
                    time_weighting_mode=time_weighting_mode, recent_boost_max=recent_boost_max,
                    sun_geometry_mode=alternative_mode,
                )
                if alternative.score + 0.5 < selected.score:
                    return alternative
            except Exception:
                pass
        return selected
    if sun_geometry_mode not in {"recursive_source", "legacy_distant"}:
        sun_geometry_mode, _geometry_reason = recommended_sun_geometry_mode(system, body)

    fit_start_monotonic = time.perf_counter()

    # Validate required fields early.
    for key in ("timestamp", "RotationPeriod", "OrbitalPeriod", "SemiMajorAxis"):
        get_required_float(body, key) if key != "timestamp" else scan_epoch(body)

    best_model: Optional[FittedModel] = None
    effective_time_ref = observation_time_reference(observations, time_ref) if time_weighting else None
    recent_boost_scale_hours: Optional[float] = None
    if time_weighting and (time_weighting_mode or "recent_boost").strip().lower() not in {"decay", "legacy_decay", "half_life"}:
        recent_boost_scale_hours = float(fit_recent_boost_scale_details(body)["boost_scale_hours"])

    combos = [(sp, lo, of) for sp in (1, -1) for lo in (1, -1) for of in (1, -1)]
    payloads: List[Dict[str, Any]] = []
    for i, combo in enumerate(combos):
        payloads.append({
            "body": body,
            "observations": observations,
            "use_heading": bool(use_heading),
            "seed": int(seed),
            "combo_index": int(i),
            "combo": combo,
            "time_weighting": bool(time_weighting),
            "time_half_life_hours": float(time_half_life_hours),
            "time_ref": effective_time_ref,
            "time_min_weight": float(time_min_weight),
            "system": system,
            "time_weighting_mode": time_weighting_mode,
            "recent_boost_max": float(recent_boost_max),
            "recent_boost_scale_hours": recent_boost_scale_hours,
            "sun_geometry_mode": sun_geometry_mode,
        })

    fit_workers = min(int(FIT_WORKERS), len(payloads))
    parallel_requested = bool(PARALLEL_FIT and fit_workers > 1 and len(payloads) >= int(FIT_PARALLEL_MIN_COMBOS))
    used_parallel = False
    results: List[Dict[str, Any]] = []

    if parallel_requested:
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=fit_workers) as executor:
                results = list(executor.map(_fit_combo_worker, payloads))
            used_parallel = True
        except Exception as exc:
            print(f"Warning: parallel fit failed; falling back to single-process fit: {exc}")
            results = []
            used_parallel = False

    if not results:
        # Sequential fallback.  This is also used when parallel fitting is
        # disabled, SciPy is missing, or process creation is unavailable.
        results = [_fit_combo_worker(payload) for payload in payloads]

    for result in sorted(results, key=lambda r: float(r.get("score", 1e99))):
        candidate_model = _model_from_fit_combo_result(
            result,
            body=body,
            observations=observations,
            time_weighting=time_weighting,
            time_ref=effective_time_ref,
            time_half_life_hours=time_half_life_hours,
            time_min_weight=time_min_weight,
            system=system,
            time_weighting_mode=time_weighting_mode,
            recent_boost_max=recent_boost_max,
            recent_boost_scale_hours=recent_boost_scale_hours,
            sun_geometry_mode=sun_geometry_mode,
        )
        if best_model is None or candidate_model.score < best_model.score:
            best_model = candidate_model

    elapsed = time.perf_counter() - fit_start_monotonic
    if best_model is not None:
        best_model.fit_parallel_enabled = bool(used_parallel)
        best_model.fit_workers = int(fit_workers if used_parallel else 1)
        best_model.fit_evaluated_combos = int(len(payloads))
        best_model.fit_elapsed_seconds = float(elapsed)

    if best_model is None:
        raise RuntimeError("Fit failed")
    return best_model


# ------------------------------ reporting / prediction ------------------------------


def apparent_mean_periods(body: Dict[str, Any]) -> Tuple[float, float]:
    rot = abs(float(body.get("RotationPeriod", 0.0)))
    orb = abs(float(body.get("OrbitalPeriod", 0.0)))
    if rot <= 0 or orb <= 0:
        return (0.0, 0.0)
    diff = abs(1.0 / rot - 1.0 / orb)
    summ = abs(1.0 / rot + 1.0 / orb)
    return (1.0 / diff if diff > 0 else float("inf"), 1.0 / summ if summ > 0 else float("inf"))


def residual_rows(model: FittedModel) -> List[Tuple[Observation, float, float, Optional[float], Optional[float], float]]:
    rows = []
    for obs in model.observations:
        alt, az = model.predict(obs.timestamp_utc, obs.lat, obs.lon)
        alt_err = alt - obs.target_altitude if obs.target_altitude is not None else None
        # Only show heading residuals when headings were actually used in the fit.
        head_err = wrap180(az - obs.heading) if (obs.heading is not None and model.rms_heading is not None) else None
        eff_w = combined_observation_weight(
            obs,
            model.time_weighting,
            model.time_ref,
            model.time_half_life_hours,
            model.time_min_weight,
            body=model.body,
            time_weighting_mode=getattr(model, "time_weighting_mode", "recent_boost"),
            recent_boost_max=getattr(model, "recent_boost_max", 2.0),
            recent_boost_scale_hours=getattr(model, "recent_boost_scale_hours", None),
        )
        rows.append((obs, alt, az, alt_err, head_err, eff_w))
    return rows


def crossing_type(model: FittedModel, t: datetime, lat: float, lon: float, threshold: float) -> str:
    before = model.predict(t - timedelta(seconds=10), lat, lon)[0] - threshold
    after = model.predict(t + timedelta(seconds=10), lat, lon)[0] - threshold
    if after > before:
        return "rise"
    return "set"


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _search_period_seconds(body: Dict[str, Any]) -> Optional[float]:
    """Best-effort local day/cycle period estimate used only for scan steps."""
    try:
        diff_p, sum_p = apparent_mean_periods(body)
        candidates = [float(v) for v in (diff_p, sum_p) if math.isfinite(float(v)) and float(v) > 0.0]
        if candidates:
            return min(candidates)
    except Exception:
        pass
    for key in ("RotationPeriod", "rotation_period_s", "OrbitalPeriod", "orbital_period_s"):
        try:
            value = abs(float(body.get(key) or 0.0))
            if math.isfinite(value) and value > 0.0:
                return value
        except Exception:
            continue
    return None


def normal_search_step_seconds(model: FittedModel, hours: float) -> float:
    if not ADAPTIVE_NORMAL_SEARCH:
        return 30.0
    period_s = _search_period_seconds(model.body)
    if period_s is not None:
        # ~120 samples per estimated cycle keeps short cycles safe while allowing
        # slow-season bodies to use the configured max step.
        step = period_s / 120.0
    else:
        # Fall back to roughly 1000 samples over the requested window.
        step = max(30.0, float(hours) * 3600.0 / 1000.0)
    return _clamp_float(step, NORMAL_SEARCH_MIN_STEP_SECONDS, NORMAL_SEARCH_MAX_STEP_SECONDS)


def _scan_crossings_with_step(
    model: FittedModel,
    start: datetime,
    lat: float,
    lon: float,
    threshold: float = 0.0,
    hours: float = 16.0,
    max_crossings: int = 8,
    step_seconds: float = 30.0,
) -> Tuple[List[Tuple[datetime, str]], Dict[str, Any]]:
    out: List[Tuple[datetime, str]] = []
    step = max(1.0, float(step_seconds))
    end = start + timedelta(hours=float(hours))
    t0 = start
    f0 = model.predict(t0, lat, lon)[0] - threshold
    t = start + timedelta(seconds=step)
    samples = 1
    refinements = 0
    while t <= end and len(out) < max_crossings:
        f1 = model.predict(t, lat, lon)[0] - threshold
        samples += 1
        if f0 == 0.0 or f0 * f1 < 0.0:
            lo = t - timedelta(seconds=step)
            hi = t
            flo = f0
            for _ in range(40):
                mid = lo + (hi - lo) / 2
                fm = model.predict(mid, lat, lon)[0] - threshold
                refinements += 1
                if flo == 0.0 or flo * fm <= 0.0:
                    hi = mid
                    f1 = fm
                else:
                    lo = mid
                    flo = fm
            ct = crossing_type(model, hi, lat, lon, threshold)
            out.append((hi, ct))
        t0, f0 = t, f1
        t = t + timedelta(seconds=step)
    meta = {
        "step_seconds": float(step),
        "samples": int(samples),
        "refinements": int(refinements),
    }
    return out, meta


def find_crossings(
    model: FittedModel,
    start: datetime,
    lat: float,
    lon: float,
    threshold: float = 0.0,
    hours: float = 16.0,
    max_crossings: int = 8,
    step_seconds: Optional[float] = None,
) -> List[Tuple[datetime, str]]:
    step = 30.0 if step_seconds is None else float(step_seconds)
    crossings, _meta = _scan_crossings_with_step(
        model, start, lat, lon, threshold=threshold,
        hours=hours, max_crossings=max_crossings, step_seconds=step,
    )
    return crossings


def find_crossings_with_metadata(
    model: FittedModel,
    start: datetime,
    lat: float,
    lon: float,
    threshold: float = 0.0,
    hours: float = 16.0,
    max_crossings: int = 8,
    step_seconds: Optional[float] = None,
) -> Tuple[List[Tuple[datetime, str]], Dict[str, Any]]:
    step = 30.0 if step_seconds is None else float(step_seconds)
    return _scan_crossings_with_step(
        model, start, lat, lon, threshold=threshold,
        hours=hours, max_crossings=max_crossings, step_seconds=step,
    )


def find_crossings_adaptive(
    model: FittedModel,
    start: datetime,
    lat: float,
    lon: float,
    threshold: float = 0.0,
    hours: float = 16.0,
    max_crossings: int = 8,
    max_steps: int = 30000,
    step_seconds: Optional[float] = None,
) -> List[Tuple[datetime, str]]:
    crossings, _meta = find_crossings_adaptive_with_metadata(
        model, start, lat, lon, threshold=threshold, hours=hours,
        max_crossings=max_crossings, max_steps=max_steps, step_seconds=step_seconds,
    )
    return crossings


def find_crossings_adaptive_with_metadata(
    model: FittedModel,
    start: datetime,
    lat: float,
    lon: float,
    threshold: float = 0.0,
    hours: float = 16.0,
    max_crossings: int = 8,
    max_steps: int = 30000,
    step_seconds: Optional[float] = None,
) -> Tuple[List[Tuple[datetime, str]], Dict[str, Any]]:
    """Find crossings over long windows without doing millions of 30 s samples."""
    total_seconds = max(1.0, float(hours) * 3600.0)
    if step_seconds is None:
        step = max(30.0, total_seconds / max(1000, int(max_steps)))
    else:
        step = max(1.0, float(step_seconds))
    return _scan_crossings_with_step(
        model, start, lat, lon, threshold=threshold,
        hours=hours, max_crossings=max_crossings, step_seconds=step,
    )


def _extended_chunk_worker(args: Tuple[FittedModel, datetime, float, float, float, float, int, float]) -> Tuple[List[Tuple[datetime, str]], Dict[str, Any]]:
    model_obj, chunk_start, lat, lon, threshold, hours, max_crossings, step_seconds = args
    return find_crossings_adaptive_with_metadata(
        model_obj, chunk_start, lat, lon, threshold=threshold,
        hours=hours, max_crossings=max_crossings, step_seconds=step_seconds,
    )


def find_crossings_extended_with_metadata(
    model: FittedModel,
    start: datetime,
    lat: float,
    lon: float,
    threshold: float = 0.0,
    hours: float = 16.0,
    max_crossings: int = 8,
) -> Tuple[List[Tuple[datetime, str]], Dict[str, Any]]:
    """Long fallback crossing search, optionally split across processes."""
    hours = max(0.1, float(hours))
    step = float(LONG_SEARCH_STEP_SECONDS)
    base_meta = {
        "step_seconds": step,
        "parallel_enabled": False,
        "workers": 1,
        "samples": 0,
        "refinements": 0,
        "chunks": 1,
        "parallel_error": None,
    }
    use_parallel = bool(PARALLEL_EXTENDED_SEARCH and PREDICTION_WORKERS > 1 and hours >= PARALLEL_EXTENDED_MIN_HOURS)
    if not use_parallel:
        crossings, meta = find_crossings_adaptive_with_metadata(
            model, start, lat, lon, threshold=threshold,
            hours=hours, max_crossings=max_crossings, step_seconds=step,
        )
        base_meta.update(meta)
        return crossings, base_meta

    workers = min(max(1, int(PREDICTION_WORKERS)), max(1, int(math.ceil(hours))))
    chunk_hours = hours / workers
    overlap = min(float(PARALLEL_CHUNK_OVERLAP_HOURS), max(0.0, chunk_hours / 2.0))
    tasks = []
    for i in range(workers):
        nominal_start_h = i * chunk_hours
        nominal_end_h = hours if i == workers - 1 else (i + 1) * chunk_hours
        chunk_start_h = max(0.0, nominal_start_h - (overlap if i > 0 else 0.0))
        chunk_end_h = min(hours, nominal_end_h + (overlap if i < workers - 1 else 0.0))
        chunk_start = start + timedelta(hours=chunk_start_h)
        chunk_span_h = max(0.1, chunk_end_h - chunk_start_h)
        tasks.append((model, chunk_start, lat, lon, threshold, chunk_span_h, max_crossings, step))

    all_crossings: List[Tuple[datetime, str]] = []
    total_samples = 0
    total_refinements = 0
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            for crossings, meta in executor.map(_extended_chunk_worker, tasks):
                all_crossings.extend(crossings)
                total_samples += int(meta.get("samples") or 0)
                total_refinements += int(meta.get("refinements") or 0)
        merged = unique_crossings(all_crossings)[:max_crossings]
        base_meta.update({
            "parallel_enabled": True,
            "workers": int(workers),
            "samples": int(total_samples),
            "refinements": int(total_refinements),
            "chunks": int(workers),
        })
        return merged, base_meta
    except Exception as exc:
        # Keep prediction reliable if multiprocessing is unavailable on a host.
        crossings, meta = find_crossings_adaptive_with_metadata(
            model, start, lat, lon, threshold=threshold,
            hours=hours, max_crossings=max_crossings, step_seconds=step,
        )
        base_meta.update(meta)
        base_meta["parallel_error"] = str(exc)[:500]
        return crossings, base_meta


def unique_crossings(crossings: List[Tuple[datetime, str]]) -> List[Tuple[datetime, str]]:
    out: List[Tuple[datetime, str]] = []
    for t, kind in sorted(crossings, key=lambda x: x[0]):
        if out and abs((t - out[-1][0]).total_seconds()) <= 2.0 and kind == out[-1][1]:
            continue
        out.append((t, kind))
    return out


def find_crossings_for_prediction(
    model: FittedModel,
    start: datetime,
    lat: float,
    lon: float,
    threshold: float = 0.0,
    prediction_hours: float = 72.0,
    max_crossings: int = 24,
    min_fallback_transitions: int = DEFAULT_MIN_FALLBACK_TRANSITIONS,
    max_extended_hours: float = DEFAULT_MAX_EXTENDED_PREDICTION_HOURS,
) -> Tuple[
    List[Tuple[datetime, str]],
    List[Tuple[datetime, str]],
    List[Tuple[datetime, str]],
    float,
    bool,
    bool,
    str,
    Dict[str, Any],
]:
    """Return crossings for display plus enough data for cycle summaries.

    V0.212 keeps the V0.200 behaviour but optimises the scanners:
      * The selected prediction window uses an adaptive coarse-to-fine step.
      * Long extended fallback searches use a coarse scan and can be split
        across multiple processes.
      * Every detected crossing is refined by bisection.
    """
    prediction_hours = max(0.1, float(prediction_hours))
    min_fallback_transitions = max(1, int(min_fallback_transitions))
    max_extended_hours = max(prediction_hours, float(max_extended_hours))

    normal_step = normal_search_step_seconds(model, prediction_hours)
    effective_max_crossings = max_crossings
    early_stop_used = False
    if NORMAL_SEARCH_EARLY_STOP and max_crossings > NORMAL_SEARCH_EARLY_STOP_CROSSINGS:
        # Keep enough transitions for next rise/set plus a complete cycle while
        # avoiding very long normal-window scans on slow seasonal bodies.
        effective_max_crossings = int(NORMAL_SEARCH_EARLY_STOP_CROSSINGS)
        early_stop_used = True

    window_crossings, normal_meta = find_crossings_with_metadata(
        model, start, lat, lon, threshold=threshold,
        hours=prediction_hours, max_crossings=effective_max_crossings,
        step_seconds=normal_step,
    )
    normal_meta["early_stop"] = bool(early_stop_used)
    normal_meta["adaptive_enabled"] = bool(ADAPTIVE_NORMAL_SEARCH)

    window_summary = daylight_cycle_summary(window_crossings)
    need_display_fallback = len(window_crossings) == 0
    need_cycle_fallback = window_summary is None

    if not need_display_fallback and not need_cycle_fallback:
        search_meta = {
            "normal": normal_meta,
            "extended": {
                "step_seconds": None,
                "parallel_enabled": False,
                "workers": 0,
                "samples": 0,
                "refinements": 0,
                "chunks": 0,
                "parallel_error": None,
            },
        }
        return (
            window_crossings,
            window_crossings,
            window_crossings,
            prediction_hours,
            False,
            False,
            "window",
            search_meta,
        )

    extended, extended_meta = find_crossings_extended_with_metadata(
        model, start, lat, lon, threshold=threshold,
        hours=max_extended_hours,
        max_crossings=max(max_crossings, min_fallback_transitions, 8),
    )
    all_crossings = unique_crossings(window_crossings + extended)

    if need_display_fallback:
        display_crossings = all_crossings[:min_fallback_transitions]
    else:
        display_crossings = window_crossings

    cycle_crossings = all_crossings
    cycle_summary = daylight_cycle_summary(cycle_crossings)
    cycle_search_extended = bool(need_cycle_fallback and cycle_summary is not None)
    daylight_source = "extended" if cycle_search_extended else ("window" if window_summary is not None else "unknown")
    listed_extended = any(
        (ct - start).total_seconds() > prediction_hours * 3600.0 + 1.0
        for ct, _kind in display_crossings
    )

    search_meta = {
        "normal": normal_meta,
        "extended": extended_meta,
    }
    return (
        display_crossings,
        window_crossings,
        cycle_crossings,
        max_extended_hours,
        bool(listed_extended),
        bool(cycle_search_extended),
        daylight_source,
        search_meta,
    )


def find_crossings_for_prediction_from_cache(
    cached_crossings: List[Tuple[datetime, str]],
    start: datetime,
    prediction_hours: float = 72.0,
    max_crossings: int = 24,
    min_fallback_transitions: int = DEFAULT_MIN_FALLBACK_TRANSITIONS,
    cached_search_hours: Optional[float] = None,
    cached_search_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[
    List[Tuple[datetime, str]],
    List[Tuple[datetime, str]],
    List[Tuple[datetime, str]],
    float,
    bool,
    bool,
    str,
    Dict[str, Any],
]:
    """Build prediction crossing data from precomputed POI transitions.

    The live sun altitude/heading is still calculated separately.  This helper
    only replaces the expensive horizon-crossing scan when the API has a valid
    POI transition cache for the active fit and coordinates.
    """
    prediction_hours = max(0.1, float(prediction_hours))
    min_fallback_transitions = max(1, int(min_fallback_transitions))
    horizon_seconds = prediction_hours * 3600.0
    end = start + timedelta(hours=prediction_hours)
    future_crossings = unique_crossings([
        (ct, kind)
        for ct, kind in cached_crossings
        if (ct - start).total_seconds() >= -1.0
    ])
    window_crossings = [
        (ct, kind)
        for ct, kind in future_crossings
        if ct <= end + timedelta(seconds=1.0)
    ][:max_crossings]

    window_summary = daylight_cycle_summary(window_crossings)
    need_display_fallback = len(window_crossings) == 0
    need_cycle_fallback = window_summary is None

    if need_display_fallback:
        display_crossings = future_crossings[:min_fallback_transitions]
    else:
        display_crossings = window_crossings

    cycle_crossings = future_crossings[:max(max_crossings, min_fallback_transitions, 8)]
    cycle_summary = daylight_cycle_summary(cycle_crossings)
    cycle_search_extended = bool(need_cycle_fallback and cycle_summary is not None)
    daylight_source = "cache_extended" if cycle_search_extended else ("cache_window" if window_summary is not None else "cache_unknown")
    listed_extended = any((ct - start).total_seconds() > horizon_seconds + 1.0 for ct, _kind in display_crossings)

    normal_meta = {
        "step_seconds": None,
        "samples": 0,
        "refinements": 0,
        "early_stop": False,
        "adaptive_enabled": bool(ADAPTIVE_NORMAL_SEARCH),
        "cache_hit": True,
    }
    extended_meta = {
        "step_seconds": None,
        "parallel_enabled": False,
        "workers": 0,
        "samples": 0,
        "refinements": 0,
        "chunks": 0,
        "parallel_error": None,
        "cache_hit": True,
    }
    if cached_search_meta:
        normal_meta["cached_normal"] = cached_search_meta.get("normal", {})
        extended_meta["cached_extended"] = cached_search_meta.get("extended", {})
    search_meta = {
        "normal": normal_meta,
        "extended": extended_meta,
        "transition_cache": "hit",
    }
    return (
        display_crossings,
        window_crossings,
        cycle_crossings,
        float(cached_search_hours if cached_search_hours is not None else prediction_hours),
        bool(listed_extended),
        bool(cycle_search_extended),
        daylight_source,
        search_meta,
    )


def daylight_cycle_rows(crossings: List[Tuple[datetime, str]]) -> List[Tuple[datetime, Optional[datetime], Optional[float], Optional[datetime], Optional[float]]]:
    """Return sunrise-based daylight/day-cycle summaries.

    Each row is:
      sunrise_time, next_sunset_time, daylight_seconds, next_sunrise_time, total_day_seconds

    Daylight time is sunrise -> following sunset.
    Total day period is sunrise -> following sunrise.
    """
    rows: List[Tuple[datetime, Optional[datetime], Optional[float], Optional[datetime], Optional[float]]] = []
    for i, (rise_time, kind) in enumerate(crossings):
        if kind != "rise":
            continue
        next_set: Optional[datetime] = None
        next_rise: Optional[datetime] = None
        for later_time, later_kind in crossings[i + 1:]:
            if next_set is None and later_kind == "set":
                next_set = later_time
            if later_kind == "rise":
                next_rise = later_time
                break
        daylight_seconds = (next_set - rise_time).total_seconds() if next_set else None
        total_day_seconds = (next_rise - rise_time).total_seconds() if next_rise else None
        rows.append((rise_time, next_set, daylight_seconds, next_rise, total_day_seconds))
    return rows


def daylight_cycle_summary(crossings: List[Tuple[datetime, str]]) -> Optional[Tuple[float, float]]:
    """Return the first complete daylight duration and full cycle period.

    Prefer the classic sunrise -> sunset -> sunrise sequence. If the checked
    window starts during daylight and only contains sunset -> sunrise -> sunset,
    use that set-to-set cycle instead. This lets V0.200 calculate daylight
    duration/day period from the next complete cycle even when the user-selected
    prediction window begins in the middle of a cycle.
    """
    crossings = sorted(crossings, key=lambda x: x[0])

    # Rise -> set -> rise.
    for i, (t0, kind0) in enumerate(crossings):
        if kind0 != "rise":
            continue
        next_set: Optional[datetime] = None
        next_rise: Optional[datetime] = None
        for t1, kind1 in crossings[i + 1:]:
            if next_set is None and kind1 == "set":
                next_set = t1
            if next_set is not None and kind1 == "rise":
                next_rise = t1
                break
        if next_set is not None and next_rise is not None:
            return (next_set - t0).total_seconds(), (next_rise - t0).total_seconds()

    # Set -> rise -> set.  This is the same full cycle shifted by half a day;
    # daylight is the middle rise->following set and the period is set->set.
    for i, (t0, kind0) in enumerate(crossings):
        if kind0 != "set":
            continue
        next_rise: Optional[datetime] = None
        next_set: Optional[datetime] = None
        for t1, kind1 in crossings[i + 1:]:
            if next_rise is None and kind1 == "rise":
                next_rise = t1
            if next_rise is not None and kind1 == "set":
                next_set = t1
                break
        if next_rise is not None and next_set is not None:
            return (next_set - next_rise).total_seconds(), (next_set - t0).total_seconds()

    return None


def calibration_source_label(calibration_path: Optional[str]) -> str:
    if calibration_path:
        return os.path.basename(calibration_path)
    return "manual observation table / unsaved CSV"


def model_sun_source_mode_label(fitted: FittedModel) -> str:
    if getattr(fitted, "sun_geometry_mode", "recursive_source") == "legacy_distant":
        return "legacy_distant"
    return sun_source_mode_label(fitted.system, fitted.body)


def model_illumination_source_name(fitted: FittedModel) -> str:
    if getattr(fitted, "sun_geometry_mode", "recursive_source") == "legacy_distant":
        return "fitted distant-star vector"
    return illumination_source_name(fitted.system, fitted.body)


def model_orbit_context_label(fitted: FittedModel) -> str:
    return orbit_context_label(fitted.system, fitted.body)


def model_sun_vector_source_label(fitted: FittedModel) -> str:
    if getattr(fitted, "sun_geometry_mode", "recursive_source") == "legacy_distant":
        return "v15-compatible fitted sun direction"
    return sun_vector_source_label(fitted.body, fitted.system)


def make_report(
    model: FittedModel,
    system: Optional[Dict[str, Any]] = None,
    target_time: Optional[datetime] = None,
    target_lat: Optional[float] = None,
    target_lon: Optional[float] = None,
    calibration_path: Optional[str] = None,
    prediction_hours: float = 72.0,
) -> str:
    body = model.body
    lines: List[str] = []
    lines.append("Elite Dangerous orbital day/night model v16")
    lines.append("============================================")
    lines.append(f"Body: {body_name(body)}")
    lines.append(f"Calibration CSV: {calibration_source_label(calibration_path)}")
    lines.append(f"Scan/orbit epoch: {format_utc(scan_epoch(body))}")
    rot = abs(float(body.get("RotationPeriod", 0.0)))
    orb = abs(float(body.get("OrbitalPeriod", 0.0)))
    lines.append(f"Rotation period: {rot:.3f} s ({rot/3600.0:.4f} h)")
    lines.append(f"Orbital period:  {orb:.3f} s ({orb/3600.0:.4f} h)")
    lines.append(f"Illumination source: {model_illumination_source_name(model)}")
    lines.append(f"Orbit context: {model_orbit_context_label(model)}")
    lines.append(f"Sun-source mode: {model_sun_source_mode_label(model)}")
    lines.append(f"Sun-vector source: {model_sun_vector_source_label(model)}")
    diff_p, sum_p = apparent_mean_periods(body)
    lines.append(f"Mean rotation-orbit beat period: {diff_p:.1f} s ({diff_p/3600.0:.4f} h)")
    lines.append(f"Mean rotation+orbit period:     {sum_p:.1f} s ({sum_p/3600.0:.4f} h)")
    lines.append(f"Fit signs: spin {model.spin_sign:+d}, longitude {model.lon_sign:+d}, orbit-vector {model.orbit_flip:+d}")
    a, b, g, p = model.params
    lines.append(f"Fitted frame angles: alpha {deg(a):+.3f}°, beta {deg(b):+.3f}°, gamma {deg(g):+.3f}°, phase {deg(p):+.3f}°")
    lines.append(f"RMS altitude residual: {model.rms_altitude:.3f}°")
    if model.time_weighting:
        ref_s = format_utc(model.time_ref) if model.time_ref else "latest observation"
        mode = getattr(model, "time_weighting_mode", "recent_boost")
        if str(mode).lower() in {"decay", "legacy_decay", "half_life"}:
            lines.append(f"Legacy time weighting: ON, reference {ref_s}, half-life {model.time_half_life_hours:.2f} h, minimum multiplier {model.time_min_weight:.2f}")
        else:
            details = fit_recent_boost_scale_details(model.body)
            scale_h = getattr(model, "recent_boost_scale_hours", None) or details["boost_scale_hours"]
            lines.append(f"Recent-observation boost: ON, reference {ref_s}, newest observations up to {getattr(model, 'recent_boost_max', 2.0):.2f}x, scale {scale_h:.2f} h ({details['basis']})")
    else:
        lines.append("Recent-observation boost: OFF; all observation ages use only quality weights.")
    if model.rms_heading is not None:
        lines.append(f"RMS heading residual:  {model.rms_heading:.3f}°")
    if getattr(model, "fit_parallel_enabled", False):
        lines.append(f"Fit parallelization: ON, {getattr(model, 'fit_workers', 1)} workers, {getattr(model, 'fit_evaluated_combos', 0)} orientation branches, {getattr(model, 'fit_elapsed_seconds', 0.0):.2f} s")
    else:
        lines.append(f"Fit parallelization: OFF, {getattr(model, 'fit_evaluated_combos', 0) or 8} orientation branches, {getattr(model, 'fit_elapsed_seconds', 0.0):.2f} s")
    lines.append(f"Fit score: {model.score:.3f}")
    lines.append("")
    lines.append("Observation residuals")
    lines.append("---------------------")
    lines.append("time UTC              obs          lat        lon       target  sun_alt  alt_err  pred_head  head_err  eff_w")
    for obs, alt, az, alt_err, head_err, eff_w in residual_rows(model):
        target = obs.target_altitude
        target_s = "" if target is None else f"{target:7.2f}"
        alt_err_s = "" if alt_err is None else f"{alt_err:7.2f}"
        head_err_s = "" if head_err is None else f"{head_err:8.2f}"
        lines.append(
            f"{format_utc(obs.timestamp_utc):20s} {obs.observation[:10]:10s} "
            f"{obs.lat:9.4f} {obs.lon:9.4f} {target_s:>7s} {alt:8.2f} {alt_err_s:>8s} {az:10.2f} {head_err_s:>9s} {eff_w:6.3f}"
        )

    if target_time is not None and target_lat is not None and target_lon is not None:
        alt, az = model.predict(target_time, target_lat, target_lon)
        radius = star_angular_radius_deg(system, body, target_time) if (system and getattr(model, "sun_geometry_mode", "recursive_source") != "legacy_distant") else None
        centre_crossings = find_crossings(
            model,
            target_time,
            target_lat,
            target_lon,
            threshold=0.0,
            hours=prediction_hours,
            max_crossings=24,
        )
        daylight_summary = daylight_cycle_summary(centre_crossings)

        lines.append("")
        lines.append("Target prediction")
        lines.append("-----------------")
        lines.append(f"Time: {format_utc(target_time)}")
        lines.append(f"Latitude / longitude: {target_lat:.6f}, {target_lon:.6f}")
        lines.append(f"Sun centre altitude: {alt:.2f}°")
        lines.append(f"Sun centre heading:  {az:.2f}°")
        lines.append(f"Centre state: {'DAY' if alt > 0 else 'NIGHT'}")
        if daylight_summary is not None:
            daylight_seconds, total_day_seconds = daylight_summary
            lines.append(f"Sunlight duration: {fmt_dur(daylight_seconds)} | Day period: {fmt_dur(total_day_seconds)}")

        lines.append(f"Next centre-horizon crossings ({prediction_hours/24.0:.1f} days):")
        if centre_crossings:
            for ct, kind in centre_crossings:
                lines.append(f"  {kind:4s} {format_utc(ct)}  in {fmt_dur((ct-target_time).total_seconds())}")
        else:
            lines.append("  no centre-horizon crossing found in this window")

        if radius is not None and radius >= VISUAL_DISC_MIN_RADIUS_DEG:
            lines.append(f"Approx parent-star angular radius: {radius:.2f}°")
            lines.append(f"Disc visibility: {'VISIBLE' if alt > -radius else 'HIDDEN'}  (disc visible if centre altitude > {-radius:.2f}°)")
            disc_crossings = find_crossings(
                model,
                target_time,
                target_lat,
                target_lon,
                threshold=-radius,
                hours=prediction_hours,
                max_crossings=24,
            )
            lines.append("Next visual-disc edge crossings:")
            if disc_crossings:
                for ct, kind in disc_crossings:
                    lines.append(f"  disc-{kind:4s} {format_utc(ct)}  in {fmt_dur((ct-target_time).total_seconds())}")
            else:
                lines.append("  no visual-disc edge crossing found in this window")
    return "\n".join(lines)


# ------------------------------ structured API helpers ------------------------------


def model_summary_dict(model: FittedModel) -> Dict[str, Any]:
    """Return a JSON-serialisable summary of a fitted model.

    This is intended for the future web/API layer. It does not include the full
    body JSON or observation rows, only the active fit metadata and parameters.
    """
    a, b, g, p = model.params
    return {
        "body_name": body_name(model.body),
        "model_version": "v16",
        "score": float(model.score),
        "rms_altitude_deg": float(model.rms_altitude),
        "rms_heading_deg": None if model.rms_heading is None else float(model.rms_heading),
        "spin_sign": int(model.spin_sign),
        "lon_sign": int(model.lon_sign),
        "orbit_flip": int(model.orbit_flip),
        "params": {
            "alpha_rad": float(a),
            "beta_rad": float(b),
            "gamma_rad": float(g),
            "phase_rad": float(p),
            "alpha_deg": float(deg(a)),
            "beta_deg": float(deg(b)),
            "gamma_deg": float(deg(g)),
            "phase_deg": float(deg(p)),
        },
        "time_weighting": bool(model.time_weighting),
        "time_weighting_mode": getattr(model, "time_weighting_mode", "recent_boost"),
        "recent_observation_boost": bool(model.time_weighting and str(getattr(model, "time_weighting_mode", "recent_boost")).lower() not in {"decay", "legacy_decay", "half_life"}),
        "time_ref_utc": None if model.time_ref is None else format_utc(model.time_ref),
        "time_half_life_hours": None if str(getattr(model, "time_weighting_mode", "recent_boost")).lower() not in {"decay", "legacy_decay", "half_life"} else float(model.time_half_life_hours),
        "time_min_weight": float(model.time_min_weight),
        "recent_boost_max": float(getattr(model, "recent_boost_max", 2.0)),
        "recent_boost_scale_hours": None if getattr(model, "recent_boost_scale_hours", None) is None else float(getattr(model, "recent_boost_scale_hours")),
        "recent_boost_scale": fit_recent_boost_scale_details(model.body),
        "sun_geometry_mode": getattr(model, "sun_geometry_mode", "recursive_source"),
        "sun_geometry_reason": recommended_sun_geometry_mode(model.system, model.body)[1],
        "illumination_source": model_illumination_source_name(model),
        "sun_source_mode": model_sun_source_mode_label(model),
        "orbit_context": model_orbit_context_label(model),
        "sun_vector_source": model_sun_vector_source_label(model),
        "fit_parallel_enabled": bool(getattr(model, "fit_parallel_enabled", False)),
        "fit_workers": int(getattr(model, "fit_workers", 1) or 1),
        "fit_evaluated_combos": int(getattr(model, "fit_evaluated_combos", 0) or 0),
        "fit_elapsed_seconds": None if getattr(model, "fit_elapsed_seconds", None) is None else float(getattr(model, "fit_elapsed_seconds")),
    }


def residuals_as_dicts(model: FittedModel) -> List[Dict[str, Any]]:
    """Return fit residuals as JSON-serialisable dictionaries."""
    out: List[Dict[str, Any]] = []
    for obs, alt, az, alt_err, head_err, eff_w in residual_rows(model):
        out.append({
            "timestamp_utc": format_utc(obs.timestamp_utc),
            "lat": float(obs.lat),
            "lon": float(obs.lon),
            "observation": obs.observation,
            "target_altitude_deg": None if obs.target_altitude is None else float(obs.target_altitude),
            "predicted_sun_altitude_deg": float(alt),
            "predicted_sun_heading_deg": float(az),
            "altitude_error_deg": None if alt_err is None else float(alt_err),
            "heading_error_deg": None if head_err is None else float(head_err),
            "effective_weight": float(eff_w),
            "quality": obs.quality,
            "note": obs.note,
        })
    return out



def model_horizon_check_hours(model: FittedModel, prediction_hours: float = 72.0) -> float:
    """Return the period to sample when no sunrise/sunset is found.

    This is intentionally a checked model period, not a promise that a body
    literally never has a transition. For moons/non-star parents v16 uses a
    fitted distant-star direction, so one rotation is the relevant cycle. For
    direct star-orbiting bodies we include the apparent beat period and, when
    reasonable, the orbital period so polar-season cases can be detected.
    """
    rot = abs(float(model.body.get("RotationPeriod", 0.0) or 0.0))
    orb = abs(float(model.body.get("OrbitalPeriod", 0.0) or 0.0))
    max_seconds = 365.0 * 24.0 * 3600.0
    candidates = [max(float(prediction_hours) * 3600.0, 3600.0)]

    if rot > 0:
        candidates.append(rot)

    if body_orbits_star_directly(model.body):
        diff_p, sum_p = apparent_mean_periods(model.body)
        for p in (diff_p, sum_p, orb):
            if p and math.isfinite(p) and p > 0:
                candidates.append(min(float(p), max_seconds))

    # Check at least the visible prediction window, but cap the background
    # analysis so one request cannot become expensive for century-long orbits.
    seconds = min(max(candidates), max_seconds)
    return float(seconds / 3600.0)


def horizon_status_analysis(
    model: FittedModel,
    target_time: datetime,
    lat: float,
    lon: float,
    window_crossings: List[Tuple[datetime, str]],
    checked_crossings: List[Tuple[datetime, str]],
    current_altitude_deg: float,
    prediction_hours: float = 72.0,
    check_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Classify no-crossing cases as continuous day/night or outside-window.

    The result is suitable for website/API display. It avoids saying "never"
    and instead reports what was found inside the checked model period.
    """
    if window_crossings:
        return {
            "horizon_mode": "normal",
            "horizon_message": "Horizon transitions found in the prediction window.",
            "horizon_check_hours": float(prediction_hours),
            "cycle_min_sun_altitude_deg": None,
            "cycle_max_sun_altitude_deg": None,
            "has_transition_in_checked_period": True,
        }

    check_hours = float(check_hours if check_hours is not None else model_horizon_check_hours(model, prediction_hours))
    state = "day" if current_altitude_deg > 0 else "night"
    wanted = "sunset" if state == "day" else "sunrise"
    window = f"the next {float(prediction_hours):.0f} hours"
    checked = f"the checked model period ({check_hours:.1f} hours)"

    if checked_crossings:
        return {
            "horizon_mode": f"no_{wanted}_in_window",
            "horizon_message": f"No {wanted} was found in {window}. The next transition was found later within {checked}.",
            "horizon_check_hours": float(check_hours),
            "cycle_min_sun_altitude_deg": None,
            "cycle_max_sun_altitude_deg": None,
            "has_transition_in_checked_period": True,
        }

    check_seconds = max(3600.0, check_hours * 3600.0)
    # Enough samples to detect broad seasonal/polar behaviour without making
    # very long orbital periods expensive.
    samples = 1440
    min_alt = float("inf")
    max_alt = -float("inf")
    for i in range(samples + 1):
        t = target_time + timedelta(seconds=check_seconds * i / samples)
        a = model.predict(t, lat, lon)[0]
        min_alt = min(min_alt, float(a))
        max_alt = max(max_alt, float(a))

    eps = 0.02
    if min_alt > eps:
        return {
            "horizon_mode": "continuous_day_checked",
            "horizon_message": f"Continuous daylight in {checked}; no sunset was found.",
            "horizon_check_hours": float(check_hours),
            "cycle_min_sun_altitude_deg": float(min_alt),
            "cycle_max_sun_altitude_deg": float(max_alt),
            "has_transition_in_checked_period": False,
        }
    if max_alt < -eps:
        return {
            "horizon_mode": "continuous_night_checked",
            "horizon_message": f"Continuous night in {checked}; no sunrise was found.",
            "horizon_check_hours": float(check_hours),
            "cycle_min_sun_altitude_deg": float(min_alt),
            "cycle_max_sun_altitude_deg": float(max_alt),
            "has_transition_in_checked_period": False,
        }

    return {
        "horizon_mode": f"no_{wanted}_in_window",
        "horizon_message": f"No {wanted} was found in {window}. No transition was detected in {checked}, but sampled sun altitude crosses the horizon range; try a longer prediction later if needed.",
        "horizon_check_hours": float(check_hours),
        "cycle_min_sun_altitude_deg": float(min_alt),
        "cycle_max_sun_altitude_deg": float(max_alt),
        "has_transition_in_checked_period": False,
    }

def calculate_prediction(
    model: FittedModel,
    system: Optional[Dict[str, Any]],
    target_time: datetime,
    lat: float,
    lon: float,
    prediction_hours: float = 72.0,
    min_fallback_transitions: int = DEFAULT_MIN_FALLBACK_TRANSITIONS,
    max_extended_prediction_hours: float = DEFAULT_MAX_EXTENDED_PREDICTION_HOURS,
    cached_crossings: Optional[List[Tuple[datetime, str]]] = None,
    cached_crossing_search_hours: Optional[float] = None,
    cached_search_meta: Optional[Dict[str, Any]] = None,
    include_sun_peak: bool = True,
) -> Dict[str, Any]:
    """Return a JSON-serialisable prediction for a body/lat/lon/time.

    This is the main non-GUI API entry point for a future web backend.
    """
    alt, az = model.predict(target_time, lat, lon)
    # Local sun trend for the website elevation view.  A centred 120-second
    # sample is stable enough for display without changing the fit itself.
    trend_sample_seconds = 60.0
    alt_before = model.predict(target_time - timedelta(seconds=trend_sample_seconds), lat, lon)[0]
    alt_after = model.predict(target_time + timedelta(seconds=trend_sample_seconds), lat, lon)[0]
    sun_altitude_rate_deg_per_min = (alt_after - alt_before) / (2.0 * trend_sample_seconds / 60.0)
    if sun_altitude_rate_deg_per_min > 0.002:
        sun_altitude_trend = "rising"
    elif sun_altitude_rate_deg_per_min < -0.002:
        sun_altitude_trend = "falling"
    else:
        sun_altitude_trend = "nearly level"

    if cached_crossings is not None:
        (
            centre_crossings,
            window_crossings,
            cycle_crossings,
            crossing_search_hours,
            crossings_extended,
            cycle_search_extended,
            daylight_summary_source,
            search_meta,
        ) = find_crossings_for_prediction_from_cache(
            cached_crossings,
            target_time,
            prediction_hours=prediction_hours,
            max_crossings=24,
            min_fallback_transitions=min_fallback_transitions,
            cached_search_hours=cached_crossing_search_hours,
            cached_search_meta=cached_search_meta,
        )
    else:
        (
            centre_crossings,
            window_crossings,
            cycle_crossings,
            crossing_search_hours,
            crossings_extended,
            cycle_search_extended,
            daylight_summary_source,
            search_meta,
        ) = find_crossings_for_prediction(
            model,
            target_time,
            lat,
            lon,
            threshold=0.0,
            prediction_hours=prediction_hours,
            max_crossings=24,
            min_fallback_transitions=min_fallback_transitions,
            max_extended_hours=max_extended_prediction_hours,
        )
    daylight_summary = daylight_cycle_summary(cycle_crossings)
    next_sunrise = next((t for t, kind in cycle_crossings if kind == "rise"), None)
    next_sunset = next((t for t, kind in cycle_crossings if kind == "set"), None)
    sun_peak = daylight_sun_peak(model, target_time, lat, lon, alt, az, sun_altitude_trend, next_sunset) if include_sun_peak else None

    radius = star_angular_radius_deg(system, model.body, target_time) if system else None
    visual_disc_crossings: List[Dict[str, Any]] = []
    disc_visible: Optional[bool] = None
    if radius is not None and radius >= VISUAL_DISC_MIN_RADIUS_DEG:
        disc_visible = bool(alt > -radius)
        for ct, kind in find_crossings(
            model,
            target_time,
            lat,
            lon,
            threshold=-radius,
            hours=prediction_hours,
            max_crossings=24,
            step_seconds=normal_search_step_seconds(model, prediction_hours),
        ):
            visual_disc_crossings.append({
                "time_utc": format_utc(ct),
                "kind": kind,
                "seconds_from_target": float((ct - target_time).total_seconds()),
            })

    horizon_info = horizon_status_analysis(
        model,
        target_time,
        lat,
        lon,
        window_crossings,
        cycle_crossings,
        alt,
        prediction_hours=prediction_hours,
        check_hours=crossing_search_hours,
    )

    result = {
        "body_name": body_name(model.body),
        "target_time_utc": format_utc(target_time),
        "lat": float(lat),
        "lon": float(lon),
        "sun_altitude_deg": float(alt),
        "sun_heading_deg": float(az),
        "sun_altitude_trend": sun_altitude_trend,
        "sun_altitude_rate_deg_per_min": float(sun_altitude_rate_deg_per_min),
        "sun_peak": sun_peak,
        "centre_state": "DAY" if alt > 0 else "NIGHT",
        "next_sunrise_utc": None if next_sunrise is None else format_utc(next_sunrise),
        "next_sunrise_seconds": None if next_sunrise is None else float((next_sunrise - target_time).total_seconds()),
        "next_sunset_utc": None if next_sunset is None else format_utc(next_sunset),
        "next_sunset_seconds": None if next_sunset is None else float((next_sunset - target_time).total_seconds()),
        "sunlight_duration_sec": None if daylight_summary is None else float(daylight_summary[0]),
        "day_period_sec": None if daylight_summary is None else float(daylight_summary[1]),
        "prediction_window_hours": float(prediction_hours),
        "crossing_search_hours": float(crossing_search_hours),
        "crossings_extended_beyond_window": bool(crossings_extended),
        "daylight_cycle_extended_beyond_window": bool(cycle_search_extended),
        "daylight_summary_source": daylight_summary_source,
        "min_fallback_transitions": int(min_fallback_transitions),
        "max_extended_prediction_hours": float(max_extended_prediction_hours),
        "normal_search_step_seconds": None if search_meta.get("normal", {}).get("step_seconds") is None else float(search_meta.get("normal", {}).get("step_seconds")),
        "normal_search_adaptive_enabled": bool(search_meta.get("normal", {}).get("adaptive_enabled", False)),
        "normal_search_early_stop": bool(search_meta.get("normal", {}).get("early_stop", False)),
        "normal_search_samples": int(search_meta.get("normal", {}).get("samples") or 0),
        "normal_search_refinements": int(search_meta.get("normal", {}).get("refinements") or 0),
        "extended_search_step_seconds": None if search_meta.get("extended", {}).get("step_seconds") is None else float(search_meta.get("extended", {}).get("step_seconds")),
        "extended_search_parallel_enabled": bool(search_meta.get("extended", {}).get("parallel_enabled", False)),
        "extended_search_workers": int(search_meta.get("extended", {}).get("workers") or 0),
        "extended_search_chunks": int(search_meta.get("extended", {}).get("chunks") or 0),
        "extended_search_samples": int(search_meta.get("extended", {}).get("samples") or 0),
        "extended_search_refinements": int(search_meta.get("extended", {}).get("refinements") or 0),
        "extended_search_parallel_error": search_meta.get("extended", {}).get("parallel_error"),
        "transition_cache_status": str(search_meta.get("transition_cache") or "miss"),
        "window_horizon_crossing_count": int(len(window_crossings)),
        "cycle_horizon_crossing_count": int(len(cycle_crossings)),
        "centre_horizon_crossings": [
            {
                "time_utc": format_utc(ct),
                "kind": kind,
                "seconds_from_target": float((ct - target_time).total_seconds()),
                "inside_prediction_window": bool((ct - target_time).total_seconds() <= float(prediction_hours) * 3600.0 + 1.0),
            }
            for ct, kind in centre_crossings
        ],
        "star_angular_radius_deg": None if radius is None else float(radius),
        "visual_disc_threshold_deg": float(VISUAL_DISC_MIN_RADIUS_DEG),
        "disc_visible": disc_visible,
        "visual_disc_crossings": visual_disc_crossings,
    }
    result.update(horizon_info)
    result["model_confidence"] = model_confidence_dict(model, target_time=target_time)
    return result



# ------------------------------ model confidence ------------------------------


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def confidence_level(score: float) -> str:
    if score >= 85.0:
        return "high"
    if score >= 65.0:
        return "medium"
    if score >= 40.0:
        return "low"
    return "very low"


def clamp_value(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def confidence_base_half_life_hours(estimated_day_period_hours: Optional[float]) -> Tuple[float, str]:
    """Return the day-period based freshness half-life used by confidence.

    The old confidence freshness used a broad fixed minimum.  V0.206 keeps the
    idea that longer local day cycles should age more slowly, but makes the
    chosen value explicit for website/API consumers.
    """
    if estimated_day_period_hours is None or estimated_day_period_hours <= 0:
        return 24.0 * 7.0, "fallback_7_days"
    base = clamp_value(3.0 * float(estimated_day_period_hours), 24.0 * 3.0, 24.0 * 30.0)
    return base, "3x_estimated_day_period_clamped_3_to_30_days"


def confidence_accuracy_half_life_factor(
    rms_altitude_deg: float,
    max_altitude_error_deg: Optional[float],
    effective_observations: float,
    observation_coverage_ratio: Optional[float],
) -> Tuple[float, str, bool]:
    """Return how much fit accuracy should stretch/shrink freshness half-life.

    Very accurate models should stay trustworthy for longer because observed
    drift is smaller.  However, a tiny residual from only a couple of close
    observations can simply be overfit, so the positive boost is capped until
    the model has enough effective observations and time coverage.
    """
    rms = float(rms_altitude_deg)
    max_err = None if max_altitude_error_deg is None else float(max_altitude_error_deg)

    if rms <= 0.25 and (max_err is None or max_err <= 1.0):
        factor, basis = 3.0, "excellent_fit"
    elif rms <= 0.5 and (max_err is None or max_err <= 2.0):
        factor, basis = 2.0, "very_good_fit"
    elif rms <= 1.0:
        factor, basis = 1.5, "good_fit"
    elif rms <= 2.0:
        factor, basis = 1.0, "usable_fit"
    elif rms <= 4.0:
        factor, basis = 0.6, "weak_fit"
    else:
        factor, basis = 0.35, "bad_fit"

    boost_limited_by_data = False
    has_enough_data = float(effective_observations) >= 4.0
    has_enough_coverage = observation_coverage_ratio is not None and float(observation_coverage_ratio) >= 0.30
    if factor > 1.2 and not (has_enough_data and has_enough_coverage):
        factor = 1.2
        boost_limited_by_data = True
        basis = basis + "_limited_by_observation_data"

    return float(factor), basis, bool(boost_limited_by_data)


def confidence_freshness_half_life_hours(
    estimated_day_period_hours: Optional[float],
    rms_altitude_deg: float,
    max_altitude_error_deg: Optional[float],
    effective_observations: float,
    observation_coverage_ratio: Optional[float],
) -> Dict[str, Any]:
    """Return V0.206 freshness half-life details for model confidence.

    Formula:
      base = clamp(3 × estimated day period, 3 days, 30 days)
      final = clamp(base × accuracy_factor, 1 day, 90 days)

    The accuracy factor lets excellent low-residual models age more slowly and
    weak/high-residual models age faster.
    """
    base_hours, base_basis = confidence_base_half_life_hours(estimated_day_period_hours)
    factor, accuracy_basis, boost_limited = confidence_accuracy_half_life_factor(
        rms_altitude_deg,
        max_altitude_error_deg,
        effective_observations,
        observation_coverage_ratio,
    )
    half_life_hours = clamp_value(base_hours * factor, 24.0, 24.0 * 90.0)
    return {
        "day_period_hours": None if estimated_day_period_hours is None else float(estimated_day_period_hours),
        "base_half_life_hours": float(base_hours),
        "base_basis": base_basis,
        "accuracy_factor": float(factor),
        "accuracy_basis": accuracy_basis,
        "boost_limited_by_observation_data": bool(boost_limited),
        "half_life_hours": float(half_life_hours),
        "basis": "day_period_and_fit_accuracy",
        "minimum_half_life_hours": 24.0,
        "maximum_half_life_hours": 24.0 * 90.0,
    }


def estimated_model_day_period_seconds(model: FittedModel) -> Optional[float]:
    """Return a conservative estimate for the model's relevant day/night period.

    This is used only for confidence/coverage scoring.  When possible we use the
    shorter apparent rotation/orbit period because that usually matches the
    repeating local light cycle better for Elite bodies.  If the orbital fields
    are incomplete, rotation period is used as a safe fallback.
    """
    try:
        rot = abs(float(model.body.get("RotationPeriod", 0.0) or 0.0))
    except Exception:
        rot = 0.0
    candidates: List[float] = []
    diff_p, sum_p = apparent_mean_periods(model.body)
    for value in (diff_p, sum_p, rot):
        try:
            v = float(value)
            if math.isfinite(v) and v > 0:
                candidates.append(v)
        except Exception:
            pass
    if not candidates:
        return None
    # Avoid selecting tiny artefacts while still supporting short moon cycles.
    return max(60.0, min(candidates))


def model_confidence_dict(
    model: FittedModel,
    target_time: Optional[datetime] = None,
    model_mode: str = "approved",
    includes_unreviewed: bool = False,
) -> Dict[str, Any]:
    """Return a human/API friendly confidence estimate for this fitted model.

    The score is intentionally heuristic.  It combines residual quality,
    observation count, time coverage, freshness and review status.  It should be
    read as a practical confidence indicator, not as a formal probability.
    """
    residuals = residuals_as_dicts(model)
    alt_errors = [abs(float(r["altitude_error_deg"])) for r in residuals if r.get("altitude_error_deg") is not None]
    max_alt_error = max(alt_errors) if alt_errors else None
    rms_alt = float(model.rms_altitude)

    # 1) Fit quality: RMS is the main factor, but a single large outlier hurts.
    rms_score = clamp01(1.0 - (rms_alt / 5.0))
    if rms_alt <= 1.0:
        rms_score = 1.0
    elif rms_alt <= 2.0:
        rms_score = 0.85
    elif rms_alt <= 4.0:
        rms_score = 0.55
    outlier_score = 1.0
    if max_alt_error is not None:
        if max_alt_error <= 2.0:
            outlier_score = 1.0
        elif max_alt_error <= 5.0:
            outlier_score = 0.65
        elif max_alt_error <= 10.0:
            outlier_score = 0.30
        else:
            outlier_score = 0.0
    fit_quality_score = 0.75 * rms_score + 0.25 * outlier_score

    # 2) Observation amount, weighted by quality/time weight.
    used_observations = len(model.observations)
    effective_observations = sum(float(r.get("effective_weight", 0.0) or 0.0) for r in residuals)
    observation_count_score = clamp01((effective_observations - 2.0) / 6.0)
    if used_observations >= 10 and effective_observations >= 6.0:
        observation_count_score = max(observation_count_score, 0.95)
    elif used_observations >= 6 and effective_observations >= 4.0:
        observation_count_score = max(observation_count_score, 0.75)
    elif used_observations >= 3:
        observation_count_score = max(observation_count_score, 0.45)

    # 3) Observation time coverage relative to the estimated cycle.
    timestamps = [obs.timestamp_utc for obs in model.observations]
    newest = max(timestamps) if timestamps else None
    oldest = min(timestamps) if timestamps else None
    span_hours: Optional[float] = None
    estimated_day_period_sec = estimated_model_day_period_seconds(model)
    estimated_day_period_hours = None if estimated_day_period_sec is None else estimated_day_period_sec / 3600.0
    if newest and oldest:
        span_hours = max(0.0, (newest - oldest).total_seconds() / 3600.0)
    if span_hours is not None and estimated_day_period_hours and estimated_day_period_hours > 0:
        coverage_ratio = span_hours / estimated_day_period_hours
        time_coverage_score = clamp01(coverage_ratio / 0.8)
        if coverage_ratio >= 1.0:
            time_coverage_score = 1.0
        elif coverage_ratio >= 0.5:
            time_coverage_score = max(time_coverage_score, 0.8)
        elif coverage_ratio >= 0.2:
            time_coverage_score = max(time_coverage_score, 0.55)
    elif used_observations >= 3:
        coverage_ratio = None
        time_coverage_score = 0.45
    else:
        coverage_ratio = None
        time_coverage_score = 0.2

    # 4) Freshness: predictions far from the newest observation become less certain.
    # V0.206 makes the half-life dynamic: local day period gives the base value,
    # then fit accuracy stretches or shrinks it. Excellent, well-covered models
    # age more slowly; weak residuals age faster.
    prediction_distance_hours: Optional[float] = None
    if target_time is not None and newest is not None:
        prediction_distance_hours = abs((target_time - newest).total_seconds()) / 3600.0
    freshness_details = confidence_freshness_half_life_hours(
        estimated_day_period_hours,
        rms_alt,
        max_alt_error,
        effective_observations,
        coverage_ratio,
    )
    half_life_hours = float(freshness_details["half_life_hours"])
    if prediction_distance_hours is None:
        freshness_score = 0.75
    else:
        freshness_score = max(0.25, 0.5 ** (prediction_distance_hours / half_life_hours))
    freshness_details["prediction_distance_hours"] = None if prediction_distance_hours is None else float(prediction_distance_hours)
    freshness_details["score"] = round(freshness_score * 100.0, 1)

    # 5) Review/model safety. Provisional data is flagged and mildly penalized,
    # but the final score should still move with fit quality and coverage.
    mode = (model_mode or "approved").strip().lower()
    review_safety_score = 1.0
    if includes_unreviewed or mode == "provisional":
        review_safety_score = 0.75

    # 6) Geometry/source complexity. Explicit/inferred recursive star source is OK;
    # last-resort fallback is more risky.
    source_mode = model_sun_source_mode_label(model)
    if source_mode == "fallback":
        geometry_score = 0.55
    elif source_mode == "legacy_distant":
        geometry_score = 0.75
    elif source_mode == "explicit":
        geometry_score = 0.95
    else:
        geometry_score = 0.85

    raw_score = (
        0.40 * fit_quality_score
        + 0.20 * observation_count_score
        + 0.15 * time_coverage_score
        + 0.15 * freshness_score
        + 0.10 * ((review_safety_score * 0.7) + (geometry_score * 0.3))
    ) * 100.0

    score = int(round(max(0.0, min(100.0, raw_score))))

    warnings: List[str] = []
    strengths: List[str] = []
    if rms_alt <= 1.0:
        strengths.append("good altitude fit")
    elif rms_alt > 4.0:
        warnings.append("high altitude residuals")
    if max_alt_error is not None and max_alt_error > 5.0:
        warnings.append("one or more large residuals")
    if used_observations < 4:
        warnings.append("few observations")
    elif effective_observations >= 6.0:
        strengths.append("good observation count")
    coverage_too_short = bool(
        span_hours is not None
        and estimated_day_period_hours
        and estimated_day_period_hours > 0
        and span_hours < 0.2 * estimated_day_period_hours
    )
    if coverage_too_short:
        warnings.append("observations cover a short part of the cycle")
    prediction_old = bool(prediction_distance_hours is not None and prediction_distance_hours > half_life_hours)
    prediction_very_old = bool(prediction_distance_hours is not None and prediction_distance_hours > 2.0 * half_life_hours)
    if prediction_very_old:
        warnings.append("prediction is far from latest observation")
    if float(freshness_details.get("accuracy_factor") or 1.0) > 1.2:
        strengths.append("accurate fit extends freshness")
    elif float(freshness_details.get("accuracy_factor") or 1.0) < 1.0:
        warnings.append("fit accuracy shortens freshness")
    if bool(freshness_details.get("boost_limited_by_observation_data")):
        warnings.append("freshness boost limited by observation coverage")
    if includes_unreviewed or mode == "provisional":
        warnings.append("includes unreviewed observations")
    if source_mode == "fallback":
        warnings.append("sun-source geometry uses fallback mode")
    elif source_mode == "legacy_distant":
        warnings.append("sun-source geometry uses v15-compatible fitted sun-direction mode")

    # Single short user-facing note for website/public API.  Keep this empty for
    # healthy models so normal users are not overwhelmed by diagnostic details.
    note = ""
    if rms_alt > 4.0 or (max_alt_error is not None and max_alt_error > 5.0):
        note = "Fit residuals are high."
    elif used_observations < 4:
        note = f"Only {used_observations} reviewed observation{'s' if used_observations != 1 else ''}."
    elif coverage_too_short:
        note = "Observation time coverage is too short."
    elif prediction_old and prediction_distance_hours is not None:
        age_days = prediction_distance_hours / 24.0
        if age_days >= 2.0:
            note = f"Newest observation was {age_days:.0f} days ago."
        else:
            note = f"Newest observation was {prediction_distance_hours:.0f} hours ago."
    elif source_mode == "fallback":
        note = "Sun-source geometry is uncertain."

    return {
        "score": score,
        "level": confidence_level(float(score)),
        "model_mode": mode,
        "fit_rms_altitude_deg": rms_alt,
        "max_altitude_residual_deg": None if max_alt_error is None else float(max_alt_error),
        "used_observations": int(used_observations),
        "effective_observations": float(effective_observations),
        "observation_time_span_hours": None if span_hours is None else float(span_hours),
        "estimated_day_period_hours": None if estimated_day_period_hours is None else float(estimated_day_period_hours),
        "observation_coverage_ratio": None if coverage_ratio is None else float(coverage_ratio),
        "newest_observation_utc": None if newest is None else format_utc(newest),
        "oldest_observation_utc": None if oldest is None else format_utc(oldest),
        "prediction_distance_hours": None if prediction_distance_hours is None else float(prediction_distance_hours),
        "freshness_half_life_hours": float(half_life_hours),
        "freshness": freshness_details,
        "includes_unreviewed": bool(includes_unreviewed or mode == "provisional"),
        "sun_source_mode": source_mode,
        "sun_geometry_mode": getattr(model, "sun_geometry_mode", "recursive_source"),
        "sun_geometry_reason": recommended_sun_geometry_mode(model.system, model.body)[1],
        "illumination_source": model_illumination_source_name(model),
        "orbit_context": model_orbit_context_label(model),
        "note": note,
        "component_scores": {
            "fit_quality": round(fit_quality_score * 100.0, 1),
            "observation_count": round(observation_count_score * 100.0, 1),
            "time_coverage": round(time_coverage_score * 100.0, 1),
            "freshness": round(freshness_score * 100.0, 1),
            "review_safety": round(review_safety_score * 100.0, 1),
            "geometry": round(geometry_score * 100.0, 1),
        },
        "warnings": warnings,
        "strengths": strengths,
    }

def fit_model_from_files(
    system_path: str,
    observations_path: str,
    body_name_value: Optional[str] = None,
    use_heading: bool = False,
    time_weighting: bool = False,
    time_half_life_hours: float = 24.0,
    time_ref: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Observation], FittedModel]:
    """Load files and fit a model. Useful for CLI tests and backend prototypes."""
    system = load_system_json(system_path)
    body = find_body(system, body_name_value)
    observations = load_observations_csv(observations_path)
    model = fit_model(
        body,
        observations,
        use_heading=use_heading,
        time_weighting=time_weighting,
        time_half_life_hours=time_half_life_hours,
        time_ref=time_ref,
        system=system,
        sun_geometry_mode="auto",
    )
    return system, body, observations, model


# ------------------------------ live Status.json ------------------------------


def candidate_status_paths() -> List[str]:
    r"""Return likely Elite Dangerous Status.json locations.

    Main Windows path:
      C:\Users\<user>\Saved Games\Frontier Developments\Elite Dangerous\Status.json

    A few Linux/Wine/Proton-style locations are included as fallbacks.
    """
    out: List[str] = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        out.append(os.path.join(userprofile, "Saved Games", "Frontier Developments", "Elite Dangerous", "Status.json"))

    home = os.path.expanduser("~")
    out.append(os.path.join(home, "Saved Games", "Frontier Developments", "Elite Dangerous", "Status.json"))
    out.append(os.path.join(home, "saved games", "Frontier Developments", "Elite Dangerous", "Status.json"))

    # Common Steam Proton prefixes. We keep this conservative to avoid slow disk walks.
    steam_roots = [
        os.path.join(home, ".steam", "steam", "steamapps", "compatdata"),
        os.path.join(home, ".local", "share", "Steam", "steamapps", "compatdata"),
    ]
    suffix = os.path.join(
        "pfx", "drive_c", "users", "steamuser", "Saved Games", "Frontier Developments", "Elite Dangerous", "Status.json"
    )
    for root in steam_roots:
        try:
            if os.path.isdir(root):
                for appid in os.listdir(root):
                    out.append(os.path.join(root, appid, suffix))
        except Exception:
            pass

    # Deduplicate preserving order.
    seen = set()
    uniq: List[str] = []
    for item in out:
        if item not in seen:
            uniq.append(item)
            seen.add(item)
    return uniq


def find_status_json() -> Optional[str]:
    for path in candidate_status_paths():
        if os.path.isfile(path):
            return path
    return None


def read_status_json(path: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    if not path:
        path = find_status_json()
    if not path:
        raise FileNotFoundError(
            "Could not find Status.json automatically. Expected Windows path like:\n"
            r"C:\Users\<you>\Saved Games\Frontier Developments\Elite Dangerous\Status.json"
        )
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read().strip()
    if not raw:
        raise ValueError(f"Status.json is empty: {path}")
    # Elite writes a single JSON object. If there are accidental extra lines, use the last non-empty one.
    if "\n" in raw:
        raw = [line for line in raw.splitlines() if line.strip()][-1]
    data = json.loads(raw)
    return data, path


def write_observations_csv(path: str, observations: List[Observation]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp_utc", "lat", "lon", "observation", "elevation", "heading", "quality", "note"],
        )
        writer.writeheader()
        for obs in observations:
            writer.writerow(
                {
                    "timestamp_utc": format_utc(obs.timestamp_utc).replace("T", " "),
                    "lat": f"{obs.lat:.8f}",
                    "lon": f"{obs.lon:.8f}",
                    "observation": obs.observation,
                    "elevation": "" if obs.elevation is None else f"{obs.elevation:.4f}",
                    "heading": "" if obs.heading is None else f"{obs.heading:.2f}",
                    "quality": obs.quality,
                    "note": obs.note,
                }
            )
