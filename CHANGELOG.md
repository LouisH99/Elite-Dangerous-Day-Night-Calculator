# Changelog

## V0.191

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
