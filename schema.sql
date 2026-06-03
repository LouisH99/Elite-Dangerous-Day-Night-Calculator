BEGIN TRANSACTION;;
CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            old_json TEXT,
            new_json TEXT,
            created_at_utc TEXT NOT NULL,
            actor TEXT
        );;
CREATE TABLE background_fit_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body_id INTEGER NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            fit_mode TEXT NOT NULL DEFAULT 'provisional',
            status TEXT NOT NULL DEFAULT 'queued',
            reason TEXT,
            requested_at_utc TEXT NOT NULL,
            started_at_utc TEXT,
            finished_at_utc TEXT,
            fit_id INTEGER REFERENCES fits(id) ON DELETE SET NULL,
            error TEXT
        , use_heading INTEGER NOT NULL DEFAULT 0, time_weighting INTEGER NOT NULL DEFAULT 0, time_half_life_hours REAL NOT NULL DEFAULT 24.0, include_unreviewed INTEGER NOT NULL DEFAULT 1, force_refit INTEGER NOT NULL DEFAULT 0, requested_by TEXT);;
CREATE TABLE bodies (
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
            updated_at_utc TEXT NOT NULL, tracked_for_prediction INTEGER NOT NULL DEFAULT 0,
            UNIQUE(system_id, name),
            UNIQUE(system_id, body_id)
        );;
CREATE TABLE body_pois (
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
        , source TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '', source_label TEXT NOT NULL DEFAULT '');;
CREATE TABLE fit_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fit_id INTEGER NOT NULL REFERENCES fits(id) ON DELETE CASCADE,
            observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            altitude_error_deg REAL,
            heading_error_deg REAL,
            effective_weight REAL,
            used_in_fit INTEGER NOT NULL DEFAULT 1,
            UNIQUE(fit_id, observation_id)
        );;
CREATE TABLE fits (
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
        );;
CREATE TABLE observations (
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
        );;
CREATE TABLE prediction_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body_id INTEGER NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            fit_id INTEGER REFERENCES fits(id) ON DELETE CASCADE,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            target_time_utc TEXT NOT NULL,
            prediction_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );;
CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );;
CREATE TABLE systems (
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
        );;
CREATE INDEX idx_systems_name ON systems(name COLLATE NOCASE);;
CREATE INDEX idx_bodies_system_name ON bodies(system_id, name COLLATE NOCASE);;
CREATE INDEX idx_bodies_parent ON bodies(system_id, parent_body_id);;
CREATE INDEX idx_observations_body_status ON observations(body_id, review_status);;
CREATE INDEX idx_observations_time ON observations(timestamp_utc);;
CREATE INDEX idx_fits_body_active ON fits(body_id, is_active);;
CREATE INDEX idx_fits_body_mode_active ON fits(body_id, fit_mode, is_active);;
CREATE INDEX idx_fits_body_mode_fingerprint ON fits(body_id, fit_mode, observation_fingerprint);;
CREATE INDEX idx_audit_log_created ON audit_log(created_at_utc DESC);;
CREATE INDEX idx_audit_log_entity ON audit_log(entity_type, entity_id);;
CREATE INDEX idx_audit_log_actor ON audit_log(actor);;
CREATE INDEX idx_audit_log_action ON audit_log(action);;
CREATE INDEX idx_bodies_tracked ON bodies(tracked_for_prediction);;
CREATE INDEX idx_body_pois_source ON body_pois(source, source_id);;
CREATE INDEX idx_body_pois_body_public ON body_pois(body_id, is_public, review_status);;
CREATE INDEX idx_body_pois_name ON body_pois(name);;
CREATE INDEX idx_body_pois_review ON body_pois(review_status);;
CREATE INDEX idx_background_fit_jobs_body_created ON background_fit_jobs(body_id, requested_at_utc DESC);;
CREATE INDEX idx_background_fit_jobs_status ON background_fit_jobs(status);;
CREATE INDEX idx_background_fit_jobs_body_mode_status ON background_fit_jobs(body_id, fit_mode, status);;
DELETE FROM "sqlite_sequence";;
COMMIT;;
