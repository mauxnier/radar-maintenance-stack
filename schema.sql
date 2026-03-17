-- ============================================================
--  Radar Maintenance — Schéma TimescaleDB
--  PostgreSQL 15+ avec extension TimescaleDB
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ------------------------------------------------------------
--  Table principale : toutes les métriques du radar
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS radar_metrics (
    time                TIMESTAMPTZ     NOT NULL,
    radar_id            TEXT            NOT NULL DEFAULT 'RADAR-01',

    -- Rotation antenne
    rpm                 FLOAT,          -- tours/minute (0 si fixe)
    azimuth_deg         FLOAT,          -- angle courant 0-360°
    rotation_state      TEXT,           -- 'rotating' | 'fixed' | 'stby'
    cumul_rotations     BIGINT,         -- compteur absolu depuis mise en service

    -- Vibrations
    vibration_g         FLOAT,          -- accélération en g (RMS)

    -- Thermique
    temp_cabin          FLOAT,          -- °C — baie électronique
    temp_motor          FLOAT,          -- °C — moteur entraînement
    temp_transmitter    FLOAT,          -- °C — transmetteur RF
    temp_external       FLOAT,          -- °C — ambiance extérieure

    -- Hygrométrie
    humidity_internal   FLOAT,          -- % HR intérieur
    humidity_external   FLOAT,          -- % HR extérieur

    -- Alimentation — générateur 1
    gen1_power_w        FLOAT,          -- puissance consommée W
    gen1_voltage_v      FLOAT,          -- tension V
    gen1_current_a      FLOAT,          -- courant A
    gen1_freq_hz        FLOAT,          -- fréquence Hz
    gen1_state          TEXT,           -- 'running' | 'standby' | 'fault'

    -- Alimentation — générateur 2 (optionnel, NULL si absent)
    gen2_power_w        FLOAT,
    gen2_voltage_v      FLOAT,
    gen2_current_a      FLOAT,
    gen2_freq_hz        FLOAT,
    gen2_state          TEXT,

    -- RF
    rf_power_peak_w     FLOAT,          -- puissance émise crête W
    rf_power_avg_w      FLOAT,          -- puissance moyenne W
    vswr                FLOAT,          -- taux onde stationnaire (idéal ~1.0)

    -- Supervision
    operational_state   TEXT,           -- 'OPERATIONAL' | 'DEGRADED' | 'FAULT' | 'MAINTENANCE'
    active_alarms       INTEGER,        -- nombre d'alarmes actives
    uptime_hours        FLOAT           -- heures depuis dernière maintenance
);

-- Conversion en hypertable TimescaleDB (partition par 1h)
SELECT create_hypertable('radar_metrics', 'time',
    chunk_time_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- Index pour filtrage par radar (utile si on étend à N radars plus tard)
CREATE INDEX IF NOT EXISTS idx_radar_metrics_radar_id
    ON radar_metrics (radar_id, time DESC);

-- Compression automatique des chunks > 7 jours
ALTER TABLE radar_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'radar_id',
    timescaledb.compress_orderby   = 'time DESC'
);

SELECT add_compression_policy('radar_metrics',
    compress_after => INTERVAL '7 days',
    if_not_exists  => TRUE
);

-- Rétention : garder 90 jours
SELECT add_retention_policy('radar_metrics',
    drop_after    => INTERVAL '90 days',
    if_not_exists => TRUE
);

-- ------------------------------------------------------------
--  Table des alarmes (historique)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS radar_alarms (
    time            TIMESTAMPTZ NOT NULL,
    radar_id        TEXT        NOT NULL DEFAULT 'RADAR-01',
    alarm_code      TEXT        NOT NULL,   -- ex: 'TEMP_CABIN_HIGH'
    severity        TEXT        NOT NULL,   -- 'WARNING' | 'CRITICAL'
    metric_name     TEXT,                   -- métrique en cause
    metric_value    FLOAT,                  -- valeur au moment de l'alarme
    threshold       FLOAT,                  -- seuil dépassé
    message         TEXT,
    resolved_at     TIMESTAMPTZ             -- NULL = alarme active
);

SELECT create_hypertable('radar_alarms', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- ------------------------------------------------------------
--  Vues continues (continuous aggregates) pour les dashboards
-- ------------------------------------------------------------

-- Agrégat par minute — pour les graphes temps réel
CREATE MATERIALIZED VIEW IF NOT EXISTS radar_metrics_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    radar_id,
    AVG(rpm)                AS avg_rpm,
    AVG(temp_cabin)         AS avg_temp_cabin,
    MAX(temp_cabin)         AS max_temp_cabin,
    AVG(temp_motor)         AS avg_temp_motor,
    AVG(temp_transmitter)   AS avg_temp_transmitter,
    AVG(humidity_internal)  AS avg_humidity,
    SUM(gen1_power_w + COALESCE(gen2_power_w, 0)) / COUNT(*) AS avg_total_power_w,
    AVG(rf_power_peak_w)    AS avg_rf_peak,
    AVG(vswr)               AS avg_vswr,
    AVG(vibration_g)        AS avg_vibration,
    MAX(active_alarms)      AS max_alarms
FROM radar_metrics
GROUP BY bucket, radar_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('radar_metrics_1min',
    start_offset => INTERVAL '10 minutes',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE
);

-- Agrégat par heure — pour les graphes historiques
CREATE MATERIALIZED VIEW IF NOT EXISTS radar_metrics_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    radar_id,
    AVG(rpm)                AS avg_rpm,
    AVG(temp_cabin)         AS avg_temp_cabin,
    MAX(temp_cabin)         AS max_temp_cabin,
    AVG(temp_motor)         AS avg_temp_motor,
    AVG(humidity_internal)  AS avg_humidity,
    AVG(gen1_power_w)       AS avg_gen1_power,
    AVG(gen2_power_w)       AS avg_gen2_power,
    MAX(active_alarms)      AS max_alarms,
    COUNT(*)                AS sample_count
FROM radar_metrics
GROUP BY bucket, radar_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('radar_metrics_1h',
    start_offset => INTERVAL '2 hours',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- ------------------------------------------------------------
--  Seuils d'alerte (référence pour le simulateur et Grafana)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS radar_thresholds (
    metric_name     TEXT PRIMARY KEY,
    warn_low        FLOAT,
    warn_high       FLOAT,
    crit_low        FLOAT,
    crit_high       FLOAT,
    unit            TEXT,
    description     TEXT
);

INSERT INTO radar_thresholds VALUES
    ('temp_cabin',        NULL,  55.0,  NULL,  70.0, '°C',  'Température baie électronique'),
    ('temp_motor',        NULL,  70.0,  NULL,  90.0, '°C',  'Température moteur'),
    ('temp_transmitter',  NULL,  60.0,  NULL,  75.0, '°C',  'Température transmetteur RF'),
    ('humidity_internal',  5.0,  80.0,   2.0,  95.0, '%',   'Hygrométrie interne'),
    ('vibration_g',       NULL,   0.8,  NULL,   1.5, 'g',   'Vibrations moteur RMS'),
    ('vswr',              NULL,   2.0,  NULL,   3.0, '',    'Taux onde stationnaire'),
    ('gen1_voltage_v',   210.0, 235.0, 200.0, 245.0, 'V',   'Tension générateur 1'),
    ('gen1_freq_hz',      49.0,  51.0,  48.0,  52.0, 'Hz',  'Fréquence générateur 1')
ON CONFLICT (metric_name) DO NOTHING;

-- ------------------------------------------------------------
--  Commandes d'injection de pannes (fault injector)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS radar_commands (
    id          SERIAL      PRIMARY KEY,
    radar_id    TEXT        NOT NULL DEFAULT 'RADAR-01',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    command     TEXT        NOT NULL,   -- 'gen1_fault' | 'gen2_fault' | 'blackout' | 'overheat' | 'vswr_spike' | 'antenna_stop' | 'maintenance' | 'reset'
    duration_s  INTEGER     NOT NULL DEFAULT 60,
    description TEXT,
    applied_at  TIMESTAMPTZ,           -- NULL = pas encore traité par le simulateur
    expires_at  TIMESTAMPTZ            -- calculé à l'application
);

CREATE INDEX IF NOT EXISTS idx_radar_commands_pending
    ON radar_commands (radar_id, applied_at)
    WHERE applied_at IS NULL;

-- ------------------------------------------------------------
--  Vérification
-- ------------------------------------------------------------
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE tablename IN ('radar_metrics', 'radar_alarms', 'radar_thresholds')
ORDER BY tablename;
