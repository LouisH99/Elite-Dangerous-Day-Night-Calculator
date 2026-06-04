#!/usr/bin/env python3
"""
Elite Dangerous Day/Night Calculator - public website

The public website calls the local calculation service server-side.
Run the calculation service locally, then run this app for the website.
"""
from __future__ import annotations

import json
import os
import time as _time
import secrets
import sqlite3
import threading
import hashlib
import hmac
import base64

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode, quote

import httpx
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

API_URL = os.environ.get("ELITE_DAYNIGHT_API_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("ELITE_DAYNIGHT_WEB_TIMEOUT", "25"))
PUBLIC_REFIT_COOLDOWN_SECONDS = int(os.environ.get("ELITE_DAYNIGHT_PUBLIC_REFIT_COOLDOWN", "60"))
PUBLIC_REFIT_LAST_RUN: Dict[int, float] = {}
DB_PATH = os.environ.get(
    "ELITE_DAYNIGHT_DB",
    os.path.join(os.path.dirname(__file__), "elite_daynight.db"),
)
SQLITE_TIMEOUT_SECONDS = float(os.environ.get("ELITE_DAYNIGHT_SQLITE_TIMEOUT", "10"))
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_TIMEOUT_SECONDS * 1000)
SESSION_SECRET = os.environ.get("ELITE_DAYNIGHT_SESSION_SECRET", "dev-change-me-please")
SUPERUSER_NAME = os.environ.get("ELITE_DAYNIGHT_SUPERUSER", "admin").strip() or "admin"
SUPERUSER_INITIAL_PASSWORD = os.environ.get(
    "ELITE_DAYNIGHT_SUPERUSER_PASSWORD",
    os.environ.get("ELITE_DAYNIGHT_ADMIN_PASSWORD", "admin"),
)
PASSWORD_HASH_ITERATIONS = int(os.environ.get("ELITE_DAYNIGHT_PASSWORD_HASH_ITERATIONS", "600000"))
DB_WRITE_RETRIES = int(os.environ.get("ELITE_DAYNIGHT_DB_WRITE_RETRIES", "5"))
DB_WRITE_RETRY_BASE_SECONDS = float(os.environ.get("ELITE_DAYNIGHT_DB_WRITE_RETRY_BASE_SECONDS", "0.08"))
WEB_WRITE_LOCK = threading.RLock()


def env_bool(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


PUBLIC_POI_SUBMISSIONS_ENABLED = env_bool("ELITE_DAYNIGHT_PUBLIC_POI_SUBMISSIONS_ENABLED", True)

app = FastAPI(
    title="Elite Dangerous Day/Night Calculator Website",
    version="0.208.1",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

BASE_DIR = os.path.dirname(__file__)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fmt_seconds(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    try:
        seconds = int(round(float(value)))
    except Exception:
        return "unknown"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def none_dash(value: Any) -> str:
    return "—" if value is None or value == "" else str(value)


def short_num(value: Any, digits: int = 3) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def readable_utc(value: Any) -> str:
    """Display UTC timestamps in a human-friendly form while keeping ISO internally."""
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return "—"
        # Common internal/API format is 2026-06-02T18:36:04Z.
        # Also accept SQLite-ish values and offsets.
        parse_text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(parse_text)
        except Exception:
            # Best-effort cosmetic fallback for already-normalized strings.
            if text.endswith("Z") and "T" in text:
                return text[:-1].replace("T", " ") + " UTC"
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["duration"] = fmt_seconds
templates.env.filters["dash"] = none_dash
templates.env.filters["num"] = short_num
templates.env.filters["utc"] = readable_utc


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


def api_detail(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, list):
            return "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
        if detail is not None:
            return str(detail)
    return str(payload)


async def api_request(method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{API_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.request(method, url, params=params, json=json_body)
        except httpx.RequestError as exc:
            raise ApiError(503, f"The calculation service is not reachable. Start the local API service first. Details: {exc}")
    if response.status_code >= 400:
        try:
            detail = api_detail(response.json())
        except Exception:
            detail = response.text
        raise ApiError(response.status_code, detail)
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


def render(request: Request, template: str, context: Dict[str, Any], status_code: int = 200) -> HTMLResponse:
    base = {
        "request": request,
        "api_url": API_URL,
        "now_utc": now_utc_iso(),
        "message": request.query_params.get("message"),
        "is_control_admin": bool(request.session.get("admin_ok")),
        "admin_user": request.session.get("admin_user", ""),
        "admin_role": request.session.get("admin_role", ""),
        "is_super_admin": request.session.get("admin_role") == "super",
        "public_poi_submissions_enabled": PUBLIC_POI_SUBMISSIONS_ENABLED,
    }
    base.update(context)

    # Starlette/FastAPI changed the recommended TemplateResponse call style.
    # Use keywords so newer versions do not interpret the context dict as the
    # template name. Fall back to the older positional style for old installs.
    try:
        return templates.TemplateResponse(
            request=request,
            name=template,
            context=base,
            status_code=status_code,
        )
    except TypeError:
        return templates.TemplateResponse(template, base, status_code=status_code)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> HTMLResponse:
    return render(request, "error.html", {"title": "API error", "status_code": exc.status_code, "detail": exc.detail}, status_code=502)


@app.middleware("http")
async def noindex_control_routes(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/control"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /control\n")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    health = await api_request("GET", "/health")
    summary = await api_request("GET", "/api/summary")
    observed_bodies = summary.get("bodies_with_observations", [])
    tracked_bodies = summary.get("tracked_bodies", observed_bodies)
    observed_body_count = int(summary.get("observed_body_count", len(observed_bodies)))
    return render(
        request,
        "index.html",
        {
            "health": health,
            "summary": summary,
            "observed_bodies": observed_bodies,
            "tracked_bodies": tracked_bodies,
            "observed_body_count": observed_body_count,
        },
    )


def system_open_url(system_row: Dict[str, Any]) -> str:
    try:
        tracked = int(system_row.get("tracked_body_count") or 0)
    except Exception:
        tracked = 0
    single_body_id = system_row.get("single_tracked_body_id")
    if tracked == 1 and single_body_id:
        return f"/bodies/{int(single_body_id)}"
    return f"/systems/{int(system_row['id'])}"


def query_flag(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "off"}


@app.get("/systems", response_class=HTMLResponse)
async def systems(
    request: Request,
    q: str = Query(""),
    hide_racing_only: str = Query("1"),
    has_reviewed_model: str = Query(""),
    needs_observations: str = Query(""),
    has_provisional_model: str = Query(""),
    has_observations: str = Query(""),
    confidence: str = Query(""),
) -> HTMLResponse:
    filters = {
        "hide_racing_only": query_flag(hide_racing_only, True),
        "has_reviewed_model": query_flag(has_reviewed_model),
        "needs_observations": query_flag(needs_observations),
        "has_provisional_model": query_flag(has_provisional_model),
        "has_observations": query_flag(has_observations),
        "confidence": (confidence or "").strip().lower(),
    }
    if filters["confidence"] not in {"", "all", "high", "low"}:
        filters["confidence"] = ""
    params = {"q": q, "limit": 100, **filters}
    data = await api_request("GET", "/api/systems/search", params=params)
    rows = data.get("results", [])
    for row in rows:
        row["open_url"] = system_open_url(row)
    return render(request, "systems.html", {"q": q, "systems": rows, "filters": filters})


@app.get("/systems/autocomplete")
async def systems_autocomplete(q: str = Query(""), limit: int = Query(8, ge=1, le=20)) -> Dict[str, Any]:
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    data = await api_request("GET", "/api/systems/search", params={"q": q, "limit": limit})
    results = []
    for row in data.get("results", []):
        tracked = int(row.get("tracked_body_count") or 0)
        observed_bodies = int(row.get("observed_body_count") or 0)
        observations = int(row.get("observation_count") or 0)
        models = int(row.get("approved_model_count") or 0)
        suffix_parts = []
        if tracked:
            suffix_parts.append(f"{tracked} tracked")
        if observations:
            suffix_parts.append(f"{observations} obs")
        elif observed_bodies:
            suffix_parts.append(f"{observed_bodies} observed")
        if models:
            suffix_parts.append(f"{models} model{'s' if models != 1 else ''}")
        suffix = " · ".join(suffix_parts) if suffix_parts else "no tracked bodies yet"
        results.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "tracked_body_count": tracked,
            "observed_body_count": observed_bodies,
            "observation_count": observations,
            "approved_model_count": models,
            "url": system_open_url(row),
            "label": f"{row.get('name')} — {suffix}",
        })
    return {"results": results}


@app.get("/systems/import", response_class=HTMLResponse)
async def import_system_form(request: Request) -> HTMLResponse:
    return render(request, "import_system.html", {"form": {"system_name": "", "body_name": "", "system_address": ""}})


@app.post("/systems/import", response_class=HTMLResponse)
async def import_system_submit(
    request: Request,
    system_name: str = Form(...),
    body_name: str = Form(...),
    system_address: str = Form(""),
    fetch_body_details: str = Form("on"),
    fit: str = Form(""),
) -> HTMLResponse:
    payload: Dict[str, Any] = {
        "system_name": system_name.strip(),
        "body_name": body_name.strip(),
        "fetch_body_details": bool(fetch_body_details),
        "fit": bool(fit),
    }
    if system_address.strip():
        payload["system_address"] = int(system_address.strip())
    result = await api_request("POST", "/api/systems/import", json_body=payload)
    system_id = result.get("system", {}).get("id")
    body_id = result.get("selected_body", {}).get("id")
    if body_id:
        return RedirectResponse(f"/bodies/{body_id}?message=Imported%20system/body", status_code=303)
    if system_id:
        return RedirectResponse(f"/systems/{system_id}?message=Imported%20system", status_code=303)
    return render(request, "import_result.html", {"result": result})


@app.get("/pois/submit", response_class=HTMLResponse)
async def submit_poi_form(request: Request, body_id: str = Query("")) -> HTMLResponse:
    if not PUBLIC_POI_SUBMISSIONS_ENABLED:
        return render(
            request,
            "submit_poi.html",
            {
                "bodies": [],
                "selected_body_id": str(body_id or ""),
                "form": {"name": "", "lat": "", "lon": "", "description": "", "submitter_name": ""},
                "poi_submissions_disabled": True,
            },
        )
    summary = await api_request("GET", "/api/summary")
    bodies = summary.get("tracked_bodies", summary.get("bodies_with_observations", []))
    return render(
        request,
        "submit_poi.html",
        {
            "bodies": bodies,
            "selected_body_id": str(body_id or ""),
            "form": {"name": "", "lat": "", "lon": "", "description": "", "submitter_name": ""},
            "poi_submissions_disabled": False,
        },
    )


@app.post("/pois/submit", response_class=HTMLResponse)
async def submit_poi_post(
    request: Request,
    body_id: int = Form(...),
    submitter_name: str = Form(...),
    name: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    description: str = Form(""),
) -> RedirectResponse:
    if not PUBLIC_POI_SUBMISSIONS_ENABLED:
        return RedirectResponse("/pois/submit?message=POI%20submissions%20are%20currently%20disabled", status_code=303)
    submitter = submitter_name.strip()
    if len(submitter) < 2:
        return RedirectResponse("/pois/submit?message=Submitter%20name%20is%20required", status_code=303)
    payload = {
        "name": name.strip(),
        "lat": lat,
        "lon": lon,
        "description": description.strip(),
        "submitter_name": submitter,
        "actor": submitter,
        "is_public": False,
        "review_status": "new",
    }
    await api_request("POST", f"/api/bodies/{body_id}/pois/submit", json_body=payload)
    return RedirectResponse(f"/bodies/{body_id}?lat={lat}&lon={lon}&message=POI%20submitted%20for%20review", status_code=303)


@app.get("/systems/{system_id}/open")
async def system_open(system_id: int) -> RedirectResponse:
    # Normal system links can use this convenience route. If the system only has
    # one tracked/predictable body, jump straight to that body. Otherwise show
    # the system overview.
    bodies = await api_request("GET", f"/api/systems/{system_id}/bodies")
    summary = await api_request("GET", "/api/summary")
    tracked_body_ids = {int(row["body_pk"]) for row in summary.get("tracked_bodies", summary.get("bodies_with_observations", []))}
    target_bodies = [b for b in bodies.get("results", []) if int(b.get("id", -1)) in tracked_body_ids]
    if len(target_bodies) == 1:
        return RedirectResponse(f"/bodies/{int(target_bodies[0]['id'])}", status_code=303)
    return RedirectResponse(f"/systems/{system_id}", status_code=303)


@app.get("/systems/{system_id}", response_class=HTMLResponse)
async def system_detail(request: Request, system_id: int) -> HTMLResponse:
    system = await api_request("GET", f"/api/systems/{system_id}")
    bodies = await api_request("GET", f"/api/systems/{system_id}/bodies")
    summary = await api_request("GET", "/api/summary")
    tracked_body_ids = {int(row["body_pk"]) for row in summary.get("tracked_bodies", summary.get("bodies_with_observations", []))}
    observed_bodies = [b for b in bodies.get("results", []) if int(b.get("id", -1)) in tracked_body_ids]
    return render(
        request,
        "system_detail.html",
        {
            "system": system,
            "bodies": observed_bodies,
            "total_body_count": len(bodies.get("results", [])),
            "observed_system_body_count": len(observed_bodies),
        },
    )


def parse_optional_query_float(value: Optional[str]) -> Optional[float]:
    """Parse an optional float from query strings.

    FastAPI returns a 422 before our route runs when an Optional[float] query
    parameter is present as an empty string, for example ?lat=&lon=. Public
    template links can legitimately have no selected coordinates yet, so accept
    empty strings here and treat them as missing values.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def parse_prediction_hours(value: str) -> float:
    try:
        hours = float(str(value).strip() or "72")
    except Exception:
        hours = 72.0
    return max(1.0, min(168.0, hours))


@app.get("/bodies/{body_id}", response_class=HTMLResponse)
async def body_detail(
    request: Request,
    body_id: int,
    lat: Optional[str] = Query(None),
    lon: Optional[str] = Query(None),
    time: str = Query(""),
    prediction_hours: str = Query("72.0"),
    poi: Optional[int] = Query(None),
    model_mode: str = Query("approved"),
) -> HTMLResponse:
    body = await api_request("GET", f"/api/bodies/{body_id}")
    poi_data = await api_request("GET", f"/api/bodies/{body_id}/pois", params={"public_only": True})
    pois = poi_data.get("results", [])
    selected_poi = None
    lat_value = parse_optional_query_float(lat)
    lon_value = parse_optional_query_float(lon)
    prediction_hours_value = parse_prediction_hours(prediction_hours)
    if poi is not None:
        for item in pois:
            try:
                if int(item.get("id")) == int(poi):
                    selected_poi = item
                    break
            except Exception:
                pass
        if selected_poi is not None:
            lat_value = float(selected_poi["lat"])
            lon_value = float(selected_poi["lon"])
    model_mode = (model_mode or "approved").strip().lower()
    if model_mode not in {"approved", "provisional"}:
        model_mode = "approved"

    counts = body.get("observation_counts") or {}
    pending_obs = int(counts.get("new", 0) or 0) + int(counts.get("needs_check", 0) or 0)
    provisional_status = None
    if pending_obs > 0 or model_mode == "provisional":
        try:
            # This queues a background provisional fit when needed, but returns
            # immediately so slow Raspberry Pi fitting does not block the page.
            provisional_status = await api_request(
                "GET",
                f"/api/bodies/{body_id}/provisional/status",
                params={"auto_enqueue": True},
            )
        except ApiError as exc:
            provisional_status = {"ready": False, "error": exc.detail}

    if model_mode == "provisional" and not (provisional_status or {}).get("ready"):
        # Always fall back to the reviewed model until the provisional fit is ready.
        model_mode = "approved"

    prediction = None
    fit = None
    fit_error = None
    try:
        fit = await api_request("GET", f"/api/bodies/{body_id}/fit", params={"include_residuals": False, "model_mode": model_mode})
    except ApiError as exc:
        fit_error = exc.detail
    target_time = time.strip() or now_utc_iso()
    if lat_value is not None and lon_value is not None and fit is not None:
        prediction = await api_request(
            "GET",
            f"/api/bodies/{body_id}/prediction",
            params={"lat": lat_value, "lon": lon_value, "time": target_time, "prediction_hours": prediction_hours_value, "model_mode": model_mode},
        )
    prediction_json = json.dumps(prediction or {}, ensure_ascii=False)
    return render(
        request,
        "body_detail.html",
        {
            "body": body,
            "fit": fit,
            "fit_error": fit_error,
            "prediction": prediction,
            "prediction_json": prediction_json,
            "lat": "" if lat_value is None else lat_value,
            "lon": "" if lon_value is None else lon_value,
            "target_time": target_time,
            "prediction_hours": prediction_hours_value,
            "pois": pois,
            "selected_poi": selected_poi,
            "model_mode": model_mode,
            "provisional_status": provisional_status,
        },
    )


@app.post("/bodies/{body_id}/observations")
async def submit_observation(
    body_id: int,
    observer_name: str = Form(...),
    timestamp_utc: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    elevation: float = Form(...),
    heading: str = Form(""),
    quality: str = Form("medium"),
    note: str = Form(""),
) -> RedirectResponse:
    observer_name_clean = observer_name.strip()
    if len(observer_name_clean) < 2:
        return RedirectResponse(f"/bodies/{body_id}?lat={lat}&lon={lon}&time={timestamp_utc}&message=Observer%20name%20is%20required", status_code=303)

    payload: Dict[str, Any] = {
        "observer_name": observer_name_clean,
        "timestamp_utc": timestamp_utc.strip(),
        "lat": lat,
        "lon": lon,
        "observation": "elevation",
        "elevation": elevation,
        "heading": None,
        "quality": quality.strip().lower(),
        "note": note.strip(),
        "review_status": "new",
        "source": "website",
    }
    if heading.strip():
        payload["heading"] = float(heading.strip())
    await api_request("POST", f"/api/bodies/{body_id}/observations", json_body=payload)
    # Keep the submitted coordinate as prediction coordinate after redirect.
    return RedirectResponse(
        f"/bodies/{body_id}?lat={lat}&lon={lon}&time={timestamp_utc}&message=Observation%20submitted%20for%20review.%20Provisional%20model%20will%20be%20prepared%20in%20the%20background",
        status_code=303,
    )



@app.post("/bodies/{body_id}/provisional-refit")
async def public_provisional_refit(
    request: Request,
    body_id: int,
    lat: str = Form(""),
    lon: str = Form(""),
    target_time: str = Form(""),
    prediction_hours: str = Form("72"),
) -> RedirectResponse:
    """Compatibility route: queue provisional fitting in the background.

    Users no longer wait for fitting. The body page normally auto-queues this
    when unreviewed observations exist, but this route remains safe if older
    forms/bookmarks call it.
    """
    query = {}
    if lat.strip():
        query["lat"] = lat.strip()
    if lon.strip():
        query["lon"] = lon.strip()
    if target_time.strip():
        query["time"] = target_time.strip()
    if prediction_hours.strip():
        query["prediction_hours"] = prediction_hours.strip()

    try:
        result = await api_request("POST", f"/api/bodies/{body_id}/provisional/ensure", params={"reason": "public_request"})
        if result.get("ready"):
            query["model_mode"] = "provisional"
            query["message"] = "Provisional model is ready"
        elif result.get("queued") or result.get("job_id"):
            query["message"] = "Provisional model is being prepared in the background"
        else:
            query["message"] = "Provisional model is not available yet"
    except ApiError as exc:
        query["message"] = f"Could not prepare provisional model: {exc.detail}"
    return RedirectResponse(f"/bodies/{body_id}?{urlencode(query)}", status_code=303)


# Hidden reviewer/admin controls. No public navigation links point here.


def utc_now() -> str:
    return now_utc_iso()


def admin_db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return con




def begin_admin_write(con: sqlite3.Connection) -> None:
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
            _time.sleep(DB_WRITE_RETRY_BASE_SECONDS * (2 ** attempt))


def admin_db_connect_write() -> sqlite3.Connection:
    con = admin_db_connect()
    begin_admin_write(con)
    return con


def insert_control_audit(con: sqlite3.Connection, entity_type: str, entity_id: Optional[int], action: str, old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]], actor: str) -> None:
    con.execute(
        "INSERT INTO audit_log(entity_type, entity_id, action, old_json, new_json, created_at_utc, actor) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, action, None if old is None else json.dumps(old, sort_keys=True), None if new is None else json.dumps(new, sort_keys=True), utc_now(), actor),
    )

def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_HASH_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(dk).decode("ascii").rstrip("="),
    )


def _b64decode_nopad(value: str) -> bytes:
    value = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value.encode("ascii"))


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iter_s, salt_s, hash_s = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = _b64decode_nopad(salt_s)
        expected = _b64decode_nopad(hash_s)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def ensure_admin_user_table() -> None:
    with WEB_WRITE_LOCK:
        con = admin_db_connect_write()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    action TEXT NOT NULL,
                    old_json TEXT,
                    new_json TEXT,
                    created_at_utc TEXT NOT NULL,
                    actor TEXT
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at_utc DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action)")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'reviewer',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    last_login_at_utc TEXT,
                    created_by TEXT,
                    updated_by TEXT
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_admin_users_username ON admin_users(username)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_admin_users_active ON admin_users(is_active)")
            now = utc_now()
            row = con.execute("SELECT id FROM admin_users WHERE username = ? COLLATE NOCASE", (SUPERUSER_NAME,)).fetchone()
            if row is None:
                con.execute(
                    """
                    INSERT INTO admin_users(username, password_hash, role, is_active, must_change_password,
                                            created_at_utc, updated_at_utc, created_by, updated_by)
                    VALUES (?, ?, 'super', 1, 0, ?, ?, 'env-bootstrap', 'env-bootstrap')
                    """,
                    (SUPERUSER_NAME, password_hash(SUPERUSER_INITIAL_PASSWORD), now, now),
                )
                new_user = con.execute("SELECT id, username, role, is_active FROM admin_users WHERE username = ? COLLATE NOCASE", (SUPERUSER_NAME,)).fetchone()
                insert_control_audit(con, "admin_user", int(new_user["id"]), "bootstrap_superuser", None, dict(new_user), "env-bootstrap")
            else:
                before = dict(con.execute("SELECT id, username, role, is_active FROM admin_users WHERE id = ?", (row["id"],)).fetchone())
                con.execute(
                    "UPDATE admin_users SET role = 'super', is_active = 1, updated_at_utc = ?, updated_by = 'env-bootstrap' WHERE id = ?",
                    (now, row["id"]),
                )
                after = dict(con.execute("SELECT id, username, role, is_active FROM admin_users WHERE id = ?", (row["id"],)).fetchone())
                if before != after:
                    insert_control_audit(con, "admin_user", int(row["id"]), "ensure_superuser", before, after, "env-bootstrap")
            con.commit()
        finally:
            con.close()


@app.on_event("startup")
def startup_admin_users() -> None:
    ensure_admin_user_table()


def get_admin_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    con = admin_db_connect()
    try:
        row = con.execute("SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE", (username.strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def get_admin_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    con = admin_db_connect()
    try:
        row = con.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def verify_admin_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    ensure_admin_user_table()
    user = get_admin_user_by_username(username)
    if not user or not int(user.get("is_active") or 0):
        return None
    if not verify_password(password, str(user.get("password_hash") or "")):
        return None
    with WEB_WRITE_LOCK:
        con = admin_db_connect_write()
        try:
            now = utc_now()
            con.execute("UPDATE admin_users SET last_login_at_utc = ? WHERE id = ?", (now, user["id"]))
            insert_control_audit(con, "admin_user", int(user["id"]), "login", None, {"username": user.get("username"), "role": user.get("role")}, user.get("username", "control-user"))
            con.commit()
            user["last_login_at_utc"] = now
        finally:
            con.close()
    return user


def admin_logged_in(request: Request) -> bool:
    if not request.session.get("admin_ok") or not request.session.get("admin_user_id"):
        return False
    try:
        user = get_admin_user_by_id(int(request.session.get("admin_user_id")))
    except Exception:
        request.session.clear()
        return False
    if not user or not int(user.get("is_active") or 0):
        request.session.clear()
        return False
    # Refresh role/name in case a super admin changed them.
    request.session["admin_user"] = user.get("username", "")
    request.session["admin_role"] = user.get("role", "")
    return True


def current_admin_user(request: Request) -> Optional[Dict[str, Any]]:
    if not admin_logged_in(request):
        return None
    user_id = request.session.get("admin_user_id")
    if not user_id:
        return None
    return get_admin_user_by_id(int(user_id))


def is_super_admin(request: Request) -> bool:
    user = current_admin_user(request)
    return bool(user and user.get("role") == "super")


def require_admin(request: Request) -> Optional[RedirectResponse]:
    if not current_admin_user(request):
        return admin_redirect(request)
    return None


def require_super_admin(request: Request) -> Optional[RedirectResponse]:
    redirect = require_admin(request)
    if redirect:
        return redirect
    if not is_super_admin(request):
        return RedirectResponse("/control?message=Super%20admin%20permission%20required", status_code=303)
    return None


def admin_redirect(request: Request) -> RedirectResponse:
    next_url = quote(str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""), safe="")
    return RedirectResponse(f"/control/login?next={next_url}", status_code=303)


def review_badge_class(status: str) -> str:
    return {
        "approved": "badge-ok",
        "new": "badge-new",
        "needs_check": "badge-warn",
        "rejected": "badge-danger",
        "corrected": "badge-muted",
    }.get((status or "").lower(), "badge-muted")


templates.env.filters["review_badge"] = review_badge_class


def auto_review_badge_class(status: str) -> str:
    return {
        "shadow_auto_approve": "badge-ok",
        "auto_candidate": "badge-warn",
        "needs_check": "badge-danger",
        "duplicate_or_near_duplicate": "badge-warn",
        "blocked": "badge-muted",
    }.get((status or "").lower(), "badge-muted")


templates.env.filters["auto_review_badge"] = auto_review_badge_class


def validate_new_password(password: str, confirm: str) -> Optional[str]:
    if password != confirm:
        return "Passwords do not match."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if password.lower() in {"password", "admin", "changeme", "12345678"}:
        return "Please choose a stronger password."
    return None


def validate_username(username: str) -> Optional[str]:
    username = username.strip()
    if len(username) < 2:
        return "Username must be at least 2 characters long."
    if len(username) > 40:
        return "Username must be at most 40 characters long."
    if any(ch in username for ch in ":,/"):
        return "Username may not contain ':', ',' or '/'."
    return None


def list_admin_users() -> list[Dict[str, Any]]:
    con = admin_db_connect()
    try:
        rows = con.execute("SELECT id, username, role, is_active, must_change_password, created_at_utc, updated_at_utc, last_login_at_utc, created_by, updated_by FROM admin_users ORDER BY username COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def create_admin_user(username: str, password: str, role: str, is_active: bool, must_change_password: bool, actor: str) -> Optional[str]:
    username = username.strip()
    err = validate_username(username)
    if err:
        return err
    if role not in {"reviewer", "super"}:
        return "Invalid role."
    now = utc_now()
    with WEB_WRITE_LOCK:
        con = admin_db_connect_write()
        try:
            cur = con.execute(
                """
                INSERT INTO admin_users(username, password_hash, role, is_active, must_change_password,
                                        created_at_utc, updated_at_utc, created_by, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (username, password_hash(password), role, 1 if is_active else 0, 1 if must_change_password else 0, now, now, actor, actor),
            )
            user_id = int(cur.lastrowid)
            new_row = con.execute("SELECT id, username, role, is_active, must_change_password, created_by FROM admin_users WHERE id = ?", (user_id,)).fetchone()
            insert_control_audit(con, "admin_user", user_id, "create_admin_user", None, dict(new_row), actor)
            con.commit()
            return None
        except sqlite3.IntegrityError:
            con.rollback()
            return "A user with that name already exists."
        finally:
            con.close()


def update_admin_user(user_id: int, username: str, role: str, is_active: bool, must_change_password: bool, new_password: str, actor: str) -> Optional[str]:
    username = username.strip()
    err = validate_username(username)
    if err:
        return err
    if role not in {"reviewer", "super"}:
        return "Invalid role."
    now = utc_now()
    with WEB_WRITE_LOCK:
        con = admin_db_connect_write()
        try:
            existing_super_count = con.execute("SELECT COUNT(*) AS c FROM admin_users WHERE role = 'super' AND is_active = 1 AND id <> ?", (user_id,)).fetchone()["c"]
            current = con.execute("SELECT id, username, role, is_active, must_change_password FROM admin_users WHERE id = ?", (user_id,)).fetchone()
            if not current:
                con.rollback()
                return "User not found."
            before = dict(current)
            if current["role"] == "super" and int(current["is_active"] or 0) == 1 and (role != "super" or not is_active) and existing_super_count < 1:
                con.rollback()
                return "At least one active super admin must remain."
            fields = ["username = ?", "role = ?", "is_active = ?", "must_change_password = ?", "updated_at_utc = ?", "updated_by = ?"]
            params: list[Any] = [username, role, 1 if is_active else 0, 1 if must_change_password else 0, now, actor]
            password_reset = bool(new_password.strip())
            if password_reset:
                fields.append("password_hash = ?")
                params.append(password_hash(new_password.strip()))
            params.append(user_id)
            con.execute(f"UPDATE admin_users SET {', '.join(fields)} WHERE id = ?", params)
            after_row = con.execute("SELECT id, username, role, is_active, must_change_password FROM admin_users WHERE id = ?", (user_id,)).fetchone()
            after = dict(after_row)
            after["password_reset"] = password_reset
            insert_control_audit(con, "admin_user", user_id, "update_admin_user", before, after, actor)
            con.commit()
            return None
        except sqlite3.IntegrityError:
            con.rollback()
            return "A user with that name already exists."
        finally:
            con.close()


def change_admin_password(user_id: int, current_password: str, new_password: str, actor: str) -> Optional[str]:
    user = get_admin_user_by_id(user_id)
    if not user:
        return "User not found."
    if not verify_password(current_password, user["password_hash"]):
        return "Current password is wrong."
    now = utc_now()
    with WEB_WRITE_LOCK:
        con = admin_db_connect_write()
        try:
            con.execute(
                "UPDATE admin_users SET password_hash = ?, must_change_password = 0, updated_at_utc = ?, updated_by = ? WHERE id = ?",
                (password_hash(new_password), now, actor, user_id),
            )
            insert_control_audit(con, "admin_user", user_id, "change_own_password", None, {"username": user.get("username")}, actor)
            con.commit()
            return None
        finally:
            con.close()


@app.get("/control/login", response_class=HTMLResponse)
async def admin_login_form(request: Request, next: str = "/control") -> HTMLResponse:
    return render(request, "admin_login.html", {"next": next, "bad_login": False})


@app.post("/control/login", response_class=HTMLResponse)
async def admin_login_submit(request: Request, username: str = Form(""), password: str = Form(...), next: str = Form("/control")):
    admin_user = verify_admin_login(username, password)
    if admin_user:
        request.session["admin_ok"] = True
        request.session["admin_user_id"] = int(admin_user["id"])
        request.session["admin_user"] = admin_user["username"]
        request.session["admin_role"] = admin_user["role"]
        if int(admin_user.get("must_change_password") or 0):
            return RedirectResponse("/control/account/password?message=Please%20change%20your%20temporary%20password", status_code=303)
        return RedirectResponse(next or "/control", status_code=303)
    return render(request, "admin_login.html", {"next": next or "/control", "bad_login": True, "username": username}, status_code=401)


@app.post("/control/logout")
async def admin_logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/?message=Logged%20out", status_code=303)


@app.get("/control/account/password", response_class=HTMLResponse)
async def admin_change_password_form(request: Request):
    redirect = require_admin(request)
    if redirect:
        return redirect
    return render(request, "admin_change_password.html", {"bad_message": None})


@app.post("/control/account/password", response_class=HTMLResponse)
async def admin_change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    redirect = require_admin(request)
    if redirect:
        return redirect
    err = validate_new_password(new_password, confirm_password)
    if not err:
        err = change_admin_password(int(request.session["admin_user_id"]), current_password, new_password, request.session.get("admin_user", "control-user"))
    if err:
        return render(request, "admin_change_password.html", {"bad_message": err}, status_code=400)
    return RedirectResponse("/control?message=Password%20changed", status_code=303)



@app.get("/control/audit", response_class=HTMLResponse)
async def admin_audit_log(
    request: Request,
    actor: str = Query(""),
    entity_type: str = Query(""),
    action: str = Query(""),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if actor.strip():
        params["actor"] = actor.strip()
    if entity_type.strip():
        params["entity_type"] = entity_type.strip()
    if action.strip():
        params["action"] = action.strip()
    data = await api_request("GET", "/api/admin/audit", params=params)
    return render(
        request,
        "admin_audit.html",
        {
            "entries": data.get("results", []),
            "actor": actor,
            "entity_type": entity_type,
            "action": action,
            "limit": limit,
            "offset": offset,
        },
    )


@app.get("/control/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    return render(request, "admin_users.html", {"users": list_admin_users()})


@app.get("/control/users/new", response_class=HTMLResponse)
async def admin_new_user_form(request: Request):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    return render(request, "admin_user_edit.html", {"mode": "new", "user": {}, "bad_message": None})


@app.post("/control/users/new", response_class=HTMLResponse)
async def admin_new_user_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form("reviewer"),
    is_active: str = Form("on"),
    must_change_password: str = Form("on"),
):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    err = validate_new_password(password, confirm_password)
    if not err:
        err = create_admin_user(username, password, role, bool(is_active), bool(must_change_password), request.session.get("admin_user", "control-super"))
    if err:
        user = {"username": username, "role": role, "is_active": 1 if is_active else 0, "must_change_password": 1 if must_change_password else 0}
        return render(request, "admin_user_edit.html", {"mode": "new", "user": user, "bad_message": err}, status_code=400)
    return RedirectResponse("/control/users?message=User%20created", status_code=303)


@app.get("/control/users/{user_id}/edit", response_class=HTMLResponse)
async def admin_edit_user_form(request: Request, user_id: int):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    user = get_admin_user_by_id(user_id)
    if not user:
        return RedirectResponse("/control/users?message=User%20not%20found", status_code=303)
    return render(request, "admin_user_edit.html", {"mode": "edit", "user": user, "bad_message": None})


@app.post("/control/users/{user_id}/edit", response_class=HTMLResponse)
async def admin_edit_user_submit(
    request: Request,
    user_id: int,
    username: str = Form(...),
    role: str = Form("reviewer"),
    is_active: str = Form(""),
    must_change_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    err = None
    if new_password.strip():
        err = validate_new_password(new_password.strip(), confirm_password.strip())
    if not err:
        err = update_admin_user(user_id, username, role, bool(is_active), bool(must_change_password), new_password.strip(), request.session.get("admin_user", "control-super"))
    if err:
        user = get_admin_user_by_id(user_id) or {"id": user_id}
        user.update({"username": username, "role": role, "is_active": 1 if is_active else 0, "must_change_password": 1 if must_change_password else 0})
        return render(request, "admin_user_edit.html", {"mode": "edit", "user": user, "bad_message": err}, status_code=400)
    # Refresh own session if the super admin edited themselves.
    if int(request.session.get("admin_user_id", 0)) == user_id:
        user = get_admin_user_by_id(user_id)
        if user:
            request.session["admin_user"] = user["username"]
            request.session["admin_role"] = user["role"]
    return RedirectResponse("/control/users?message=User%20saved", status_code=303)


@app.get("/control", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not admin_logged_in(request):
        return admin_redirect(request)
    summary = await api_request("GET", "/api/summary")
    observations = await api_request("GET", "/api/admin/observations", params={"status": "new", "limit": 10})
    return render(request, "admin_dashboard.html", {"summary": summary, "new_observations": observations.get("results", [])})


@app.get("/control/observations", response_class=HTMLResponse)
async def admin_observations(
    request: Request,
    status: str = Query("new"),
    system_name: str = Query(""),
    body_name: str = Query(""),
    observer_name: str = Query(""),
    automation: str = Query("all"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    if not admin_logged_in(request):
        return admin_redirect(request)
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if status and status != "all":
        params["status"] = status
    if system_name.strip():
        params["system_name"] = system_name.strip()
    if body_name.strip():
        params["body_name"] = body_name.strip()
    if observer_name.strip():
        params["observer_name"] = observer_name.strip()
    if automation and automation != "all":
        params["automation"] = automation.strip().lower()
    data = await api_request("GET", "/api/admin/observations", params=params)
    summary = await api_request("GET", "/api/summary")
    filter_qs = urlencode({
        "status": status,
        "system_name": system_name,
        "body_name": body_name,
        "observer_name": observer_name,
        "automation": automation,
        "limit": limit,
        "offset": offset,
    })
    base_filter_qs = urlencode({
        "status": status,
        "system_name": system_name,
        "body_name": body_name,
        "observer_name": observer_name,
        "automation": automation,
        "limit": limit,
    })
    return render(
        request,
        "admin_observations.html",
        {
            "observations": data.get("results", []),
            "status": status,
            "system_name": system_name,
            "body_name": body_name,
            "observer_name": observer_name,
            "automation": automation,
            "limit": limit,
            "offset": offset,
            "summary": summary,
            "filter_qs": filter_qs,
            "base_filter_qs": base_filter_qs,
        },
    )


@app.post("/control/observations/automation/analyze")
async def admin_analyze_observations(
    request: Request,
    status: str = Form("new"),
    system_name: str = Form(""),
    body_name: str = Form(""),
    observer_name: str = Form(""),
    limit: int = Form(100),
    next_url: str = Form("/control/observations"),
):
    if not admin_logged_in(request):
        return admin_redirect(request)
    params: Dict[str, Any] = {"status": status, "limit": max(1, min(int(limit), 500)), "actor": request.session.get("admin_user", "automation")}
    if system_name.strip():
        params["system_name"] = system_name.strip()
    if body_name.strip():
        params["body_name"] = body_name.strip()
    if observer_name.strip():
        params["observer_name"] = observer_name.strip()
    result = await api_request("POST", "/api/admin/observations/automation/analyze", params=params)
    analysed = int(result.get("analysed") or 0)
    sep = "&" if "?" in next_url else "?"
    return RedirectResponse(f"{next_url}{sep}message=Automation%20analysed%20{analysed}%20observations", status_code=303)


@app.post("/control/observations/{observation_id}/review")
async def admin_review_observation(
    request: Request,
    observation_id: int,
    review_status: str = Form(...),
    next_url: str = Form("/control/observations"),
):
    if not admin_logged_in(request):
        return admin_redirect(request)
    await api_request("POST", f"/api/admin/observations/{observation_id}/review", json_body={"review_status": review_status, "actor": request.session.get("admin_user", "control-admin")})
    sep = "&" if "?" in next_url else "?"
    return RedirectResponse(f"{next_url}{sep}message=Observation%20updated", status_code=303)


@app.get("/control/observations/{observation_id}/edit", response_class=HTMLResponse)
async def admin_edit_observation_form(request: Request, observation_id: int):
    if not admin_logged_in(request):
        return admin_redirect(request)
    obs = await api_request("GET", f"/api/admin/observations/{observation_id}")
    return render(request, "admin_observation_edit.html", {"obs": obs})


@app.post("/control/observations/{observation_id}/edit", response_class=HTMLResponse)
async def admin_edit_observation_submit(
    request: Request,
    observation_id: int,
    observer_name: str = Form(""),
    timestamp_utc: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    observation: str = Form("elevation"),
    elevation: str = Form(""),
    heading: str = Form(""),
    quality: str = Form("medium"),
    review_status: str = Form("new"),
    note: str = Form(""),
):
    if not admin_logged_in(request):
        return admin_redirect(request)
    payload: Dict[str, Any] = {
        "observer_name": observer_name.strip(),
        "timestamp_utc": timestamp_utc.strip(),
        "lat": lat,
        "lon": lon,
        "observation": observation.strip().lower(),
        "elevation": None if not elevation.strip() else float(elevation.strip()),
        "heading": None if not heading.strip() else float(heading.strip()),
        "quality": quality.strip().lower(),
        "review_status": review_status.strip().lower(),
        "note": note.strip(),
    }
    await api_request("PATCH", f"/api/admin/observations/{observation_id}", json_body=payload)
    return RedirectResponse(f"/control/observations/{observation_id}/edit?message=Saved", status_code=303)


@app.post("/control/observations/{observation_id}/delete")
async def admin_delete_observation(request: Request, observation_id: int):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    await api_request("DELETE", f"/api/admin/observations/{observation_id}", params={"actor": request.session.get("admin_user", "control-super")})
    return RedirectResponse("/control/observations?message=Observation%20deleted", status_code=303)




@app.get("/control/racing", response_class=HTMLResponse)
async def admin_racing_import_page(request: Request, limit: int = Query(25, ge=1, le=200), preview: str = Query("")):
    if not admin_logged_in(request):
        return admin_redirect(request)
    results = None
    if preview:
        results = await api_request("GET", "/api/admin/racing/preview", params={"limit": limit})
    return render(request, "admin_racing_import.html", {"limit": limit, "preview": preview, "results": None if results is None else results.get("results", [])})


@app.post("/control/racing/import")
async def admin_racing_import_submit(
    request: Request,
    limit: int = Form(100),
    import_missing_systems: str = Form(""),
    review_status: str = Form("needs_check"),
    make_public: str = Form(""),
):
    if not admin_logged_in(request):
        return admin_redirect(request)
    payload = {
        "limit": int(limit),
        "import_missing_systems": bool(import_missing_systems),
        "review_status": review_status.strip().lower(),
        "make_public": bool(make_public),
        "actor": request.session.get("admin_user", "control-admin"),
    }
    result = await api_request("POST", "/api/admin/racing/import", json_body=payload)
    return render(request, "admin_racing_result.html", {"result": result})


@app.get("/control/pois", response_class=HTMLResponse)
async def admin_pois(request: Request, body_name: str = Query(""), review_status: str = Query("")):
    if not admin_logged_in(request):
        return admin_redirect(request)
    params: Dict[str, Any] = {"limit": 500}
    if body_name.strip():
        params["body_name"] = body_name.strip()
    if review_status.strip():
        params["review_status"] = review_status.strip().lower()
    pois = await api_request("GET", "/api/admin/pois", params=params)
    summary = await api_request("GET", "/api/summary")
    bodies = summary.get("tracked_bodies", summary.get("bodies_with_observations", []))
    return render(request, "admin_pois.html", {"pois": pois.get("results", []), "bodies": bodies, "body_name": body_name, "review_status": review_status})


@app.post("/control/pois")
async def admin_create_poi(
    request: Request,
    body_id: int = Form(...),
    name: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    description: str = Form(""),
    is_public: str = Form(""),
):
    if not admin_logged_in(request):
        return admin_redirect(request)
    payload = {
        "name": name.strip(),
        "lat": lat,
        "lon": lon,
        "description": description.strip(),
        "is_public": bool(is_public),
        "review_status": "approved",
        "submitter_name": request.session.get("admin_user", "control-admin"),
        "actor": request.session.get("admin_user", "control-admin"),
    }
    await api_request("POST", f"/api/admin/bodies/{body_id}/pois", json_body=payload)
    return RedirectResponse("/control/pois?message=POI%20created", status_code=303)


@app.get("/control/pois/{poi_id}/edit", response_class=HTMLResponse)
async def admin_edit_poi_form(request: Request, poi_id: int):
    if not admin_logged_in(request):
        return admin_redirect(request)
    poi = await api_request("GET", f"/api/pois/{poi_id}", params={"public_only": False})
    return render(request, "admin_poi_edit.html", {"poi": poi})


@app.post("/control/pois/{poi_id}/edit")
async def admin_edit_poi_submit(
    request: Request,
    poi_id: int,
    name: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    description: str = Form(""),
    is_public: str = Form(""),
    review_status: str = Form("new"),
    submitter_name: str = Form(""),
):
    if not admin_logged_in(request):
        return admin_redirect(request)
    payload = {
        "name": name.strip(),
        "lat": lat,
        "lon": lon,
        "description": description.strip(),
        "is_public": bool(is_public),
        "review_status": review_status.strip().lower(),
        "submitter_name": submitter_name.strip(),
        "actor": request.session.get("admin_user", "control-admin"),
    }
    await api_request("PATCH", f"/api/admin/pois/{poi_id}", json_body=payload)
    return RedirectResponse(f"/control/pois/{poi_id}/edit?message=POI%20saved", status_code=303)


@app.post("/control/pois/{poi_id}/review")
async def admin_review_poi(request: Request, poi_id: int, review_status: str = Form(...), next_url: str = Form("/control/pois")):
    if not admin_logged_in(request):
        return admin_redirect(request)
    payload = {
        "review_status": review_status.strip().lower(),
        "is_public": review_status.strip().lower() == "approved",
        "actor": request.session.get("admin_user", "control-admin"),
    }
    await api_request("PATCH", f"/api/admin/pois/{poi_id}", json_body=payload)
    sep = "&" if "?" in next_url else "?"
    return RedirectResponse(f"{next_url}{sep}message=POI%20updated", status_code=303)


@app.post("/control/pois/{poi_id}/delete")
async def admin_delete_poi(request: Request, poi_id: int):
    redirect = require_super_admin(request)
    if redirect:
        return redirect
    await api_request("DELETE", f"/api/admin/pois/{poi_id}", params={"actor": request.session.get("admin_user", "control-super")})
    return RedirectResponse("/control/pois?message=POI%20deleted", status_code=303)


@app.get("/control/bodies", response_class=HTMLResponse)
async def admin_bodies(request: Request):
    if not admin_logged_in(request):
        return admin_redirect(request)
    summary = await api_request("GET", "/api/summary")
    bodies = summary.get("bodies_with_observations", [])
    return render(request, "admin_bodies.html", {"bodies": bodies})


@app.get("/control/bodies/{body_id}/fit", response_class=HTMLResponse)
async def admin_body_fit(request: Request, body_id: int):
    if not admin_logged_in(request):
        return admin_redirect(request)
    body = await api_request("GET", f"/api/bodies/{body_id}")
    stars_resp = await api_request("GET", f"/api/systems/{body['system_id']}/stars")
    stars = stars_resp.get("results", [])
    approved_fit = None
    approved_fit_error = None
    provisional_fit = None
    provisional_fit_error = None
    try:
        approved_fit = await api_request("GET", f"/api/bodies/{body_id}/fit", params={"include_residuals": True, "model_mode": "approved"})
    except ApiError as exc:
        approved_fit_error = exc.detail
    try:
        provisional_fit = await api_request("GET", f"/api/bodies/{body_id}/fit", params={"include_residuals": True, "model_mode": "provisional"})
    except ApiError as exc:
        provisional_fit_error = exc.detail
    fit_jobs = {"results": []}
    try:
        fit_jobs = await api_request("GET", f"/api/admin/bodies/{body_id}/fit-jobs", params={"limit": 8})
    except ApiError:
        fit_jobs = {"results": []}
    fit_job_rows = fit_jobs.get("results", [])
    fit_jobs_active = any(str(j.get("status", "")).lower() in {"queued", "running"} for j in fit_job_rows)
    return render(request, "admin_body_fit.html", {
        "body": body,
        "stars": stars,
        "approved_fit": approved_fit,
        "approved_fit_error": approved_fit_error,
        "provisional_fit": provisional_fit,
        "provisional_fit_error": provisional_fit_error,
        "fit_jobs": fit_job_rows,
        "fit_jobs_active": fit_jobs_active,
        "fit_refresh_seconds": int(os.getenv("ELITE_DAYNIGHT_ADMIN_FIT_REFRESH_SECONDS", "5")),
    })


@app.post("/control/bodies/{body_id}/refit")
async def admin_refit_body(
    request: Request,
    body_id: int,
    use_heading: str = Form(""),
    time_weighting: str = Form(""),
    include_unreviewed: str = Form(""),
    illumination_source_star_name: str = Form(""),
):
    redirect = require_admin(request)
    if redirect:
        return redirect
    payload = {
        "use_heading": bool(use_heading),
        "time_weighting": bool(time_weighting),
        "include_unreviewed": bool(include_unreviewed),
        "force_refit": True,
        "background": True,
        "illumination_source_star_name": illumination_source_star_name,
        "actor": request.session.get("admin_user", "control-admin"),
    }
    await api_request("POST", f"/api/admin/bodies/{body_id}/refit", json_body=payload)
    return RedirectResponse(f"/control/bodies/{body_id}/fit?message=Fit%20queued.%20Refresh%20this%20page%20to%20see%20progress.", status_code=303)


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request) -> HTMLResponse:
    return render(request, "about.html", {})
