## V0.212

- Ported the v0.213 calculation performance work onto the V0.211 UI baseline.
- Added adaptive coarse/fine scanning for the normal prediction window.
- Added optional multiprocessing for long extended fallback searches.
- Added optional multiprocessing for model fitting orientation branches.
- The background fit queue remains single-job-at-a-time; only the CPU-heavy branch search inside one fit job is parallelized.
- Fit metadata/report now records parallel fitting status, worker count, evaluated branch count, and elapsed fit seconds.
- Added private API run-script defaults for prediction search and model-fit performance settings.
- Kept the V0.211 templates and static website styling unchanged.

## V0.211

- Public prediction API responses now include the local `body_id` in the `target` object.
- Public prediction API responses now include the local `poi_id` for POI/race-key predictions, or `null` for manual coordinate predictions.
- Ambiguous public API POI/race/body lookup responses now include IDs where available to help callers disambiguate.

## V0.210

- Fixed the systems overview health badges so they use the same current model-confidence and model-note logic as body predictions.
- Systems with short observation coverage or stale observations now show as needing observations instead of healthy.
- Added a Medium confidence filter/status bucket to the systems overview.
- Body fit confidence without an explicit prediction now uses the current UTC time for freshness, reducing before/after prediction inconsistencies.

## V0.209

- Added public read-only prediction API namespace under `/public/api/v1`.
- Added `/public/api/v1/health`, `/public/api/v1/prediction`, and `/public/api/v1/docs`.
- Public prediction lookup now supports system/body/coordinates, public POI name, and Razz Racing race key.
- Public prediction responses include compact prediction data, model confidence, a single short model note, and a needs-observations flag.
- Added `PUBLIC_API.md` with usage examples and response/error formats.
- Body pages now show the single short model note instead of long confidence warning lists.

## V0.208.1

- Added automation environment defaults to the private API run scripts.
- `run_private_api.bat` and `run_private_api.sh` now set `ELITE_DAYNIGHT_AUTOMATION_MODE=shadow` and `ELITE_DAYNIGHT_AUTOMATION_BATCH_LIMIT=200` when those variables are not already defined.

## V0.208

- Added conservative observation-automation shadow mode.
- New observations are analysed against the active reviewed model when available, but review status is not changed automatically.
- Added automation metadata fields for observations: status, reason, reviewed model id, altitude residual, threshold, confidence score, and analysis timestamp.
- Added future-facing fit metadata fields: `fit_origin` and `auto_fit_reason`.
- Added reviewer-visible automation recommendations on the observation review list and edit page.
- Added automation filters for unanalysed, would-auto-approve, auto-candidate, needs-check, duplicate/near-duplicate, blocked, and large-residual observations.
- Added a reviewer/admin batch action to analyse matching observations in shadow mode.
- Added audit-log entries for automation shadow decisions.
- Added `ELITE_DAYNIGHT_AUTOMATION_MODE=off|shadow|candidate|active`; V0.208 implements shadow/candidate analysis only and does not auto-approve.

## V0.207

- Reviewers can now queue reviewed and provisional model refits from the hidden control area.
- Super-admin-only restrictions remain for account management and destructive actions.
- Removed the manual half-life field from the refit UI.
- Replaced old time-weighting wording with optional recent-observation boosting.
- Recent boosting keeps old observations at their normal quality weight and boosts newer observations up to 2x.
- The recent-boost time scale is calculated automatically from the estimated day period, then orbital period, then rotation period, with a safe fallback.
- Stored fit metadata now records the weighting mode and boost-scale details for audit/API use.

## V0.206.2

- Fixed the V0.199+ fit regression for bodies whose parent chain contains a Null/barycentre before the illumination star.
- Added automatic sun-geometry selection:
  - recursive source geometry for clean star/planet/moon parent chains and explicit illumination-source overrides.
  - v15-compatible fitted sun-direction geometry for Null/barycentre chains and old fits without geometry metadata.
- Made legacy geometry v15-compatible: direct star-orbiting bodies still use the parent-star orbital vector, while moons/Null cases use a fitted distant effective sun direction.
- Stored a `sun_geometry_reason` in fit metadata to make the selected geometry mode easier to audit.
- Updated admin fit help text to explain when auto uses recursive vs v15-compatible geometry.

## V0.206.1

- Fixed a fitting regression introduced by the V0.199 recursive illumination-source path for bodies where the available orbital geometry does not match Elite's effective daylight behavior.
- Existing fits without stored geometry metadata now load with the legacy empirical distant-star geometry, preserving old accurate V0.194-era models.
- New refits automatically try the legacy distant-star geometry first and use it when it clearly explains the observations better.
- Stored fit parameters now include `sun_geometry_mode` so future predictions use the same geometry mode that was used during fitting.
- Marked heading constraints as experimental on the admin fit page and disabled browser autocomplete on refit forms to reduce accidental checked-state persistence.

## V0.206

- Added dynamic model-confidence freshness half-life based on estimated day period and fit accuracy.
- Excellent low-residual models now keep freshness confidence longer, while weak/high-residual models age faster.
- Limited the positive accuracy boost when a model has too few effective observations or poor time coverage, to reduce overfit confidence.
- Added freshness details to `model_confidence`, including base half-life, accuracy factor, final half-life, and boost-limiting reason.
- Added freshness half-life details to the body and admin fit confidence displays.

## V0.205

- Simplified the public `/systems` overview table.
- Removed system address and galactic coordinate columns from the systems overview.
- Added total system observation counts, with approved and pending observation subtotals.
- Kept the V0.204 filters while making the table rows easier to scan.
- Updated system autocomplete labels to use total observation counts when available.

## V0.204

- Added public `/systems` overview filters for reviewed models, provisional models, observations, needs-observations, and high/low confidence.
- Added a default-on "Hide racing-only systems" filter so systems imported only because of Razz Racing POIs do not overwhelm the public systems list.
- Added lightweight system health counters to `/api/systems/search`, including provisional model count, POI count, racing POI count, needs-observations count, and confidence buckets.
- Updated the systems table with model, health, and POI badges so filtered results are easier to scan.

## V0.203

- Added local system search autocomplete on the top search and systems page.
- Selecting/opening a system with exactly one tracked body now opens that body page directly.
- System search results now show tracked-body, observation, and reviewed-model counts.
- Prediction and observation timestamp parsing now accepts shorter UTC inputs such as `2026-06-02 18`, `2026-06-02 18:00`, and `2026-06-02T18`.
- No deploy directory is included in the release package.

## V0.202

- Changed website timestamp display from raw ISO strings like `2026-06-02T18:36:04Z` to a more readable UTC format like `2026-06-02 18:36:04 UTC`.
- Kept internal form values and API timestamps in ISO UTC format for compatibility.
- Removed the deploy examples directory from the release package.

## V0.201

- Added model confidence scoring for reviewed and provisional fits.
- Prediction responses now include a `model_confidence` object for API/website use.
- Body pages show confidence level, percent score, key inputs, and warning notes.
- Admin fit pages show confidence next to fit diagnostics.

## V0.200

- Improved prediction-window fallback behavior.
- The selected prediction window is now respected for the listed horizon crossings whenever at least one day/night change exists inside that window.
- If no day/night change exists inside the selected window, the model searches forward up to 30 days and lists the next fallback transitions.
- If the selected window does not contain a complete day/night cycle, the model searches up to 30 days for the next complete cycle and uses that to calculate sunlight duration and day period.
- Added prediction metadata for `daylight_summary_source`, `daylight_cycle_extended_beyond_window`, `min_fallback_transitions`, and `max_extended_prediction_hours`.

## V0.199

- Added explicit illumination-source support for multi-star systems.
- Added `illumination_source_star_name` to body/model data.
- Added recursive star-vector geometry so moons around planets orbiting secondary stars use the correct star path.
- Added automatic illumination inference:
  - direct planet around star A -> A
  - moon around planet around star B -> B
  - combined-name bodies like ABC/AB default to A, BC defaults to B and can be overridden.
- Added an admin fit-page selector for illumination source: auto or any star in the system.
- Fit reports and fit metadata now show illumination source, orbit context, and sun-source mode.

## V0.198

- Cleaned imported Razz Racing POI names by removing the `Race Start:` prefix.
- Cleaned imported Razz Racing POI descriptions so they contain only the race description text.
- Kept Razz race keys in POI source metadata for future public API lookup support.

# Changelog

## V0.197

- Added an admin-only Razz Racing POI importer at `/control/racing`.
- Racing import can preview races, parse surface start body/lat/lon from race waypoint data, and import starts as reviewed/hidden POIs for moderation.
- Added POI source metadata (`source`, `source_id`, `source_url`, `source_label`) so imported race POIs can be updated instead of duplicated.
- Improved the observation review layout: wider control pages and a second detail row for fit residuals and notes, so long descriptions are no longer squeezed into a tiny table cell.

## V0.196

- The body fit control page now auto-refreshes while a reviewed/provisional fit job is queued or running.
- Once the Raspberry Pi finishes fitting, the page reloads and shows the updated model without manual refresh.
- Added `ELITE_DAYNIGHT_ADMIN_FIT_REFRESH_SECONDS` to configure the refresh interval.

## V0.195

- Admin reviewed/provisional refits are now queued in the background instead of blocking the control page.
- This avoids website/API timeout errors on slower hosts such as Raspberry Pi.
- Added background fit-job status table on the body fit control page.

## V0.194

- Fixed the body page failing with HTTP 422 when the provisional-model link was clicked before latitude/longitude were selected.
- The body route now treats empty `lat`/`lon` query parameters as missing values instead of invalid floats.
- The reviewed/provisional model links no longer add empty coordinate parameters.

## V0.192

- Fixed provisional/refit failure on existing databases: `cannot start a transaction within a transaction`.
- No database migration needed; existing V0.191 databases can be reused.

## V0.192

Clean naming pass before publishing to GitHub.

- Removed old phase-style filenames from the main package.
- Renamed the local API to `elite_daynight_api.py`.
- Renamed the website entry point to `elite_daynight_website.py`.
- Renamed the database/import helper to `elite_daynight_db.py`.
- Kept `elite_daynight_model_v16.py` because the model version remains important for prediction behavior.
- Changed the default runtime database name to `elite_daynight.db`.
- Added `elite_daynight_template.db` as an empty template database for new installs.
- Updated run scripts and documentation for the clean names.

## V0.190

Review workflow improvements.

- Added observation filters by system, body, submitter, and review status.
- Added submitter display to the observation review table.
- Added reviewed/provisional residual columns next to observations.
- Updated the admin observation API to support these filters and residual fields.
