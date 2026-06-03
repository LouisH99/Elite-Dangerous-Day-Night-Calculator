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
- Once fitting finishes, the page reloads and shows the updated model without manual refresh.
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
- Kept `elite_daynight_model_v15.py` because the model version remains important for prediction behavior.
- Changed the default runtime database name to `elite_daynight.db`.
- Added `elite_daynight_template.db` as an empty template database for new installs.
- Updated run scripts and documentation for the clean names.

## V0.190

Review workflow improvements.

- Added observation filters by system, body, submitter, and review status.
- Added submitter display to the observation review table.
- Added reviewed/provisional residual columns next to observations.
- Updated the admin observation API to support these filters and residual fields.