# Elite Dangerous Day/Night Calculator

Current package version: **V0.202**.

A community tool for predicting local daylight, sunrise, sunset, and sun elevation on planets and moons in **Elite Dangerous**.

The project lets players add systems/bodies, submit sun observations, review submitted data, fit a prediction model, and use saved Points of Interest (POIs) to quickly check local light conditions.

> **Project status:** early community tool, currently around `V0.202`.
>
> **Transparency note:** this is a **vibe-coded / AI-assisted project**. A large part of the design, code structure, debugging, and documentation was created with help from ChatGPT. The model, outputs, and implementation should be treated as experimental and should be validated with real observations.

---

## What the tool does

For a selected planet or moon, the website can show:

- current local sun altitude
- day/night state at a latitude and longitude
- next sunrise and sunset
- sunlight duration and day period when available
- a 2D local sun elevation view
- whether the sun is rising or falling
- saved POIs for common locations
- reviewed and provisional prediction models
- model confidence level and score

Players can help by submitting surface observations. Reviewers can approve, reject, edit, or inspect observations before they are used in the reviewed model.

---

## Current features

### Public website

- Search systems already in the local database
- Import systems/bodies from Spansh
- View predictable bodies only
- Predict day/night for manual coordinates
- Select POIs on a body page
- Submit observations for review
- Submit POIs for review, if public POI submissions are enabled
- Try a provisional model when unreviewed observations exist
- Beginner-friendly help text for new users

### Hidden control area

- Reviewer/super-admin login
- Safe password storage using salted password hashes
- Manage reviewer accounts
- Review, approve, reject, edit, and delete observations
- Filter observations by system, body, submitter, and status
- View residuals next to observations
- Manage POIs
- Import Razz Racing race starts as POIs from the hidden control area
- Refit reviewed and provisional models separately
- View audit log

### Backend / database

- Private local API, intended to run on `127.0.0.1`
- SQLite database with WAL mode and busy timeout
- Serialized write handling and audit logging
- Compact storage of relevant system/body/orbital data
- Reviewed and provisional fits stored separately
- Old fit cleanup
- Heuristic model-confidence output for website/API use

---

## Running locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the private API:

```bash
./run_private_api.sh
```

Start the public website:

```bash
./run_website.sh
```

Open the website:

```text
http://127.0.0.1:8080
```

Hidden control login:

```text
http://127.0.0.1:8080/control/login
```

On Windows, use the provided `.bat` files if present.

---

## Important environment variables

Set these before hosting:

```bash
ELITE_DAYNIGHT_SESSION_SECRET="a-long-random-secret"
ELITE_DAYNIGHT_SUPERUSER="admin"
ELITE_DAYNIGHT_SUPERUSER_PASSWORD="your-long-initial-password"
```

Optional settings:

```bash
ELITE_DAYNIGHT_PUBLIC_POI_SUBMISSIONS_ENABLED=1
ELITE_DAYNIGHT_SQLITE_TIMEOUT=10
ELITE_DAYNIGHT_DB_WRITE_RETRIES=5
ELITE_DAYNIGHT_DB_WRITE_RETRY_BASE_SECONDS=0.08
ELITE_DAYNIGHT_KEEP_INACTIVE_FITS=0
ELITE_DAYNIGHT_PUBLIC_REFIT_COOLDOWN=60
```

To disable public POI submissions:

```bash
ELITE_DAYNIGHT_PUBLIC_POI_SUBMISSIONS_ENABLED=0
ELITE_DAYNIGHT_ADMIN_FIT_REFRESH_SECONDS=5
```

---


## Fresh install database

The package includes an empty template database:

```text
elite_daynight_template.db
```

The run scripts automatically copy it to `elite_daynight.db` if no database exists yet. For manual setup:

```bash
cp elite_daynight_template.db elite_daynight.db
```

On Windows:

```bat
copy elite_daynight_template.db elite_daynight.db
```

## Database safety

Before updating the code, back up the SQLite database:

```bash
cp elite_daynight.db elite_daynight_BACKUP.db
```

On Windows:

```bat
copy elite_daynight.db elite_daynight_BACKUP.db
```


---

## How observations work

A useful sun observation normally contains:

- observer / commander name
- body being observed
- UTC observation time
- latitude and longitude from the in-game surface HUD
- sun altitude / elevation
- optional sun heading
- quality estimate

Recommended measuring method:

1. Target the sun in-game.
2. Aim as closely as possible at the centre of the targeted sun marker.
3. Read the elevation / altitude value from the HUD.
4. Submit that value as the sun altitude.

Altitude meaning:

```text
positive = sun above the horizon
negative = sun below the horizon
0°       = sunrise or sunset, using sun-centre altitude
```

Quality guideline:

```text
high   = accurate, about ±1°
medium = good estimate, about ±2–3°
low    = rough, about ±5° or worse
```

---

## Reviewed vs provisional models

The tool keeps two model types separate.

### Reviewed model

The default model on first page load.

It uses only reviewed/approved observations and is the normal public prediction.

### Provisional model

Optional model using approved observations plus unreviewed observations.

It is useful when new data was added but has not been checked yet. The site marks it clearly as provisional. Provisional fits are cached, so they do not need to be refitted if no observation data changed.

---

## How the prediction model works

The model is an empirical fit built from player observations. It does **not** try to perfectly simulate every detail of Elite Dangerous orbital mechanics. Instead, it combines available body/orbital data with surface observations to produce a practical local daylight prediction.

### 1. Body and orbit data

For each imported body, the database stores compact model-relevant fields such as:

- system address and body name
- parent body information
- radius
- rotation period
- orbital period
- semi-major axis
- orbital inclination
- eccentricity
- periapsis
- mean anomaly
- axial tilt when available
- landable / tidally locked flags

### 2. Surface observation conversion

Each observation gives the model:

```text
time + latitude + longitude + measured sun altitude
```

Optionally it can also include sun heading.

The model converts the latitude/longitude into a local surface direction and compares the predicted sun vector against the measured sun altitude.

### 3. Fitting

The fitter searches for model parameters that minimize the residuals between observed and predicted sun altitude.

Residual example:

```text
altitude residual = predicted sun altitude - submitted sun altitude
```

Heading residuals can also be calculated when heading observations are present, but heading is treated more carefully because it is usually less reliable than altitude.

Observation quality affects weight:

```text
high   = strongest weight
medium = medium weight
low    = weak weight
```

Optional time weighting can reduce the influence of old observations, but it is not always enabled.

### 4. Illumination source and multi-star systems

Elite Dangerous can show multiple stars in a system, but a planet/moon appears to be lit by one effective illumination source. The model stores an optional explicit field:

```text
illumination_source_star_name
```

Examples:

```text
Sosong B 1 a     -> Sosong B
Sosong ABC 1 a   -> Sosong A
```

When this field is empty, the model tries to infer the light source:

```text
body around star A                -> use A
moon around planet around star B  -> use B
combined-name ABC/AB bodies       -> default to A
combined-name BC bodies           -> default to B, but treat as uncertain
```

Admins can override the inferred source on the body fit page with:

```text
Illumination source star: auto / system star list
```

The fit report and model metadata show:

```text
Illumination source
Orbit context
Sun-source mode: explicit / inferred / fallback
```

For moons, the model now uses recursive parent/body positions where possible, so a moon around a planet orbiting a secondary star can use the correct moving star direction instead of treating the moon's direct parent planet as the sun.

### 5. Prediction

After fitting, the model can predict sun altitude at a requested time and location. It scans forward to find horizon crossings:

```text
sun altitude crosses 0° upward   → sunrise
sun altitude crosses 0° downward → sunset
```

The website first uses the selected prediction window. If there is no day/night change in that window, the model searches farther ahead up to 30 days and shows the next fallback transition(s). If the selected window does not contain a complete day/night cycle, the same extended search is used to calculate sunlight duration and day period when possible.

The website then displays:

- current sun altitude
- whether it is day or night
- whether the sun is rising or falling
- next sunrise/sunset
- sunlight duration
- day period
- POI-specific predictions if a POI is selected

Website pages display times in a readable UTC format such as `2026-06-02 18:36:04 UTC`. Internal/API timestamps remain ISO-style UTC strings for machine use.

### Model confidence

Each prediction includes a practical confidence estimate. The score is not a formal probability; it is a heuristic indicator based on:

- fit RMS and maximum altitude residual
- number and quality-weighted count of observations
- observation time spread compared with the estimated day cycle
- time distance between the prediction and the newest observation
- reviewed vs provisional data
- sun-source/orbit geometry mode

The website shows a simple label such as:

```text
High · 88%
Medium · 67%
Low · 42%
```

The detailed confidence object is also returned in prediction responses for future public API use.

---

## Data review workflow

Public submissions start as unreviewed.

Reviewers can:

- approve good observations
- reject bad observations
- edit obvious mistakes
- compare residuals against reviewed and provisional fits
- refit reviewed or provisional models

The audit log records important reviewer/admin actions.

---

## Security notes

- Do not expose the private API port publicly.
- Use HTTPS when hosting.
- Set a strong `ELITE_DAYNIGHT_SESSION_SECRET`.
- Create individual reviewer accounts instead of sharing one password.
- Back up the database before updating the code.
- Keep public submissions reviewed before they become part of the reviewed model.

---

## Roadmap ideas

Possible future improvements:

- automatic observation quality checks
- auto-approval candidates for observations matching a trusted model
- body-name autocomplete on import
- improved sun path graphs
- 2D planet terminator map
- parent/neighbor body rise/set prediction
- eclipse / partial eclipse prediction
- PostgreSQL support for larger deployments

---

## Credits

Created for Elite Dangerous players who want better local day/night predictions on planets and moons.

This project was developed as a vibe-coded, AI-assisted project with significant help from ChatGPT. Human testing, player observations, and reviewer validation are essential parts of making the predictions useful.
