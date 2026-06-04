# Elite Dangerous Day/Night Calculator Public API v1

The public API is a read-only prediction API. It is intended for tools that want to know the local day/night state, sun altitude, next sunrise/sunset, and model confidence for a known body coordinate, saved POI, or Razz Racing race key.

It is not a general system/body information API. For full planetary data, use specialist sources such as Spansh or EDSM.

Base path:

```text
/public/api/v1
```

The public API runs on the website service, normally port `8080`. The private internal API on port `8000` should remain localhost/private.

---

## Health check

```text
GET /public/api/v1/health
```

Example response:

```json
{
  "api_version": "v1",
  "ok": true,
  "service": "Elite Dangerous Day/Night Calculator public prediction API"
}
```

---

## Prediction endpoint

```text
GET /public/api/v1/prediction
```

The endpoint supports exactly one target type:

1. manual system/body/coordinates
2. saved POI name
3. Razz Racing race key

Default values:

```text
model_mode=approved
prediction_hours=72
time=current UTC time
```

`prediction_hours` is limited to 1–168 hours.

---

## 1. Predict by system/body and coordinates

Use this when you already know the coordinates.

Required query parameters:

```text
system
body
lat
lon
```

Optional query parameters:

```text
time
prediction_hours
model_mode
```

Example:

```text
/public/api/v1/prediction?system=Col%20285%20Sector%20XK-O%20d6-90&body=2%20a&lat=-12.345&lon=67.89&time=2026-06-04T12:00:00Z
```

The body can be the full body name or the short suffix if it is unambiguous inside the system, for example `2 a`.

---

## 2. Predict by POI name

Use this when the location is saved as a public approved POI.

Required query parameter:

```text
poi
```

Optional disambiguation parameters:

```text
system
body
```

Optional prediction parameters:

```text
time
prediction_hours
model_mode
```

Example:

```text
/public/api/v1/prediction?poi=Race%20Start&system=Example%20System&body=Example%20System%201%20a
```

If multiple public POIs have the same name, the API returns an `ambiguous_poi` error with possible matches.

---

## 3. Predict by Razz Racing race key

Use this for imported Razz Racing starts.

Required query parameter:

```text
race_key
```

Example:

```text
/public/api/v1/prediction?race_key=RAZZAFRAG03&time=2026-06-04T12:00:00Z
```

Race-key lookup uses approved public POIs imported from the Razz Racing source metadata.

---

## Successful prediction response

Example shape:

```json
{
  "api_version": "v1",
  "query": {
    "type": "race_key",
    "system": null,
    "body": null,
    "poi": null,
    "race_key": "RAZZAFRAG03",
    "time_utc": "2026-06-04T12:00:00Z",
    "prediction_hours": 72.0,
    "model_mode": "approved"
  },
  "target": {
    "system_name": "Col 285 Sector XK-O d6-90",
    "body_name": "Col 285 Sector XK-O d6-90 2 a",
    "poi_name": "Race Start",
    "race_key": "RAZZAFRAG03",
    "lat": -12.345,
    "lon": 67.89
  },
  "prediction": {
    "time_utc": "2026-06-04T12:00:00Z",
    "sun_altitude_deg": 23.41,
    "sun_heading_deg": 145.2,
    "is_day": true,
    "state": "day",
    "sun_motion": "rising",
    "next_sunrise_utc": "2026-06-05T01:20:00Z",
    "next_sunset_utc": "2026-06-04T18:44:00Z",
    "sunlight_duration_seconds": 21600.0,
    "day_period_seconds": 86400.0
  },
  "model_confidence": {
    "score": 86,
    "level": "high",
    "fit_rms_altitude_deg": 0.42,
    "max_altitude_residual_deg": 0.91,
    "used_observations": 8,
    "newest_observation_utc": "2026-05-30T09:39:06Z",
    "prediction_time_from_newest_observation_hours": 14.2,
    "model_mode": "approved",
    "note": ""
  },
  "observation_need": {
    "needs_observations": false,
    "level": "none"
  },
  "warnings": []
}
```

`model_confidence.note` is intentionally a single short note. It is empty when the model is healthy. When the model needs attention, it gives one short reason such as:

```text
Newest observation was 22 days ago.
Only 2 reviewed observations.
Observation time coverage is too short.
Fit residuals are high.
```

---

## Model mode

Default:

```text
model_mode=approved
```

Optional:

```text
model_mode=provisional
```

A provisional model may include unreviewed observations. Public tools should normally use the approved model unless they intentionally want provisional data.

---

## Error response format

All public API errors use this JSON shape:

```json
{
  "api_version": "v1",
  "error": {
    "code": "not_found",
    "message": "POI not found."
  }
}
```

Common error codes:

```text
bad_request
not_found
ambiguous_poi
ambiguous_body
ambiguous_race_key
missing_coordinates
invalid_coordinates
invalid_time
no_reviewed_model
model_unavailable
prediction_failed
```

Ambiguous lookup errors can include a `matches` list to help the caller disambiguate.

---

## Notes for hosting

Expose the website service, normally port `8080`, to users.

Keep the private API service, normally port `8000`, bound to localhost/private network only.

The public prediction endpoint is read-only. It does not create observations, change models, expose audit logs, or expose reviewer/admin data.
