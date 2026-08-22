-- Multifamily tract scorer schema (database: realestate)
-- Idempotent. Safe to re-run.

CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================ demographics
-- One row per tract per ACS 5-year vintage. Wide format for query simplicity.
-- Suppressed Census sentinels (-666666666 et al) are stored as NULL, never as 0.
CREATE TABLE IF NOT EXISTS acs_tract (
    geoid                   TEXT    NOT NULL,   -- 11-digit: state(2)+county(3)+tract(6)
    vintage                 INT     NOT NULL,   -- ACS 5-yr end year
    state_fips              TEXT    NOT NULL,
    county_fips             TEXT    NOT NULL,
    name                    TEXT,
    population              INT,
    median_hh_income        INT,
    median_gross_rent       INT,
    median_home_value       INT,
    median_re_taxes         INT,
    tenure_total            INT,
    tenure_renter           INT,
    edu_total               INT,
    edu_bachelors           INT,
    edu_masters             INT,
    edu_professional        INT,
    edu_doctorate           INT,
    vacancy_total           INT,
    below_poverty           INT,
    median_rent_pct_income  NUMERIC(6,2),
    ingested_at             TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (geoid, vintage)
);
CREATE INDEX IF NOT EXISTS idx_acs_vintage  ON acs_tract (vintage);
CREATE INDEX IF NOT EXISTS idx_acs_state    ON acs_tract (state_fips);

-- ============================================================ geometry
CREATE TABLE IF NOT EXISTS tract_geom (
    geoid       TEXT PRIMARY KEY,
    vintage     INT NOT NULL,            -- boundary vintage (2020 tracts)
    state_fips  TEXT,
    county_fips TEXT,
    name        TEXT,
    aland       BIGINT,
    awater      BIGINT,
    intptlat    DOUBLE PRECISION,
    intptlon    DOUBLE PRECISION,
    geom        GEOMETRY(MultiPolygon, 4326)
);
CREATE INDEX IF NOT EXISTS idx_tract_geom_gist ON tract_geom USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_tract_geom_state ON tract_geom (state_fips);

-- ============================================================ opportunity zones
-- OZ designations are on 2010 tract boundaries. ACS/geocoder return 2020 tracts.
-- Matching requires the crosswalk below or you get silent false pos/negatives.
CREATE TABLE IF NOT EXISTS opportunity_zones (
    geoid_2010  TEXT PRIMARY KEY,
    tract_type  TEXT,                    -- 'Low-Income Community' / 'Non-LIC Contiguous'
    state_fips  TEXT,
    county_fips TEXT
);

CREATE TABLE IF NOT EXISTS tract_xwalk_2010_2020 (
    geoid_2010  TEXT NOT NULL,
    geoid_2020  TEXT NOT NULL,
    area_weight NUMERIC(8,6),            -- share of the 2010 tract inside the 2020 tract
    PRIMARY KEY (geoid_2010, geoid_2020)
);
CREATE INDEX IF NOT EXISTS idx_xwalk_2020 ON tract_xwalk_2010_2020 (geoid_2020);

-- ============================================================ ground truth (Phase 3)
CREATE TABLE IF NOT EXISTS fhfa_hpi_tract (
    geoid       TEXT NOT NULL,
    year        INT  NOT NULL,
    hpi         NUMERIC(12,4),
    annual_pct  NUMERIC(8,4),
    PRIMARY KEY (geoid, year)
);

CREATE TABLE IF NOT EXISTS zillow_series (
    region_id   TEXT NOT NULL,
    metric      TEXT NOT NULL,           -- 'zhvi' | 'zori'
    geo_level   TEXT NOT NULL,           -- 'zip' | 'metro'
    zip         TEXT,
    obs_date    DATE NOT NULL,
    value       NUMERIC(14,4),
    PRIMARY KEY (region_id, metric, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_zillow_zip ON zillow_series (zip, metric, obs_date);

-- ============================================================ spatial features
CREATE TABLE IF NOT EXISTS tract_poi_counts (
    geoid           TEXT NOT NULL,
    poi_class       TEXT NOT NULL,
    count_in_tract  INT DEFAULT 0,
    count_within_5k INT DEFAULT 0,
    nearest_m       DOUBLE PRECISION,
    PRIMARY KEY (geoid, poi_class)
);

CREATE TABLE IF NOT EXISTS osm_poi (
    osm_id      BIGINT,
    poi_class   TEXT,
    name        TEXT,
    geom        GEOMETRY(Point, 4326)
);
CREATE INDEX IF NOT EXISTS idx_osm_poi_gist  ON osm_poi USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_osm_poi_class ON osm_poi (poi_class);

-- ============================================================ supply & economy
-- Supply pipeline is the single biggest omission in the source paper. A market
-- with strong rent growth AND heavy deliveries gets crushed (Austin 2023-25).
CREATE TABLE IF NOT EXISTS bps_permits (
    county_fips     TEXT NOT NULL,
    year            INT  NOT NULL,
    units_total     INT,
    units_5plus     INT,                 -- multifamily specifically
    PRIMARY KEY (county_fips, year)
);

CREATE TABLE IF NOT EXISTS qcew_county (
    county_fips     TEXT NOT NULL,
    year            INT  NOT NULL,
    employment      INT,
    avg_weekly_wage INT,
    PRIMARY KEY (county_fips, year)
);

CREATE TABLE IF NOT EXISTS irs_migration (
    county_fips     TEXT NOT NULL,
    year            INT  NOT NULL,
    inflow_returns  INT,
    inflow_agi      BIGINT,
    outflow_returns INT,
    outflow_agi     BIGINT,
    net_agi         BIGINT,
    PRIMARY KEY (county_fips, year)
);

-- ============================================================ risk
CREATE TABLE IF NOT EXISTS crime_agency (
    ori             TEXT NOT NULL,
    year            INT  NOT NULL,
    county_fips     TEXT,
    state_abbr      TEXT,
    population      INT,
    violent_count   INT,
    property_count  INT,
    PRIMARY KEY (ori, year)
);

-- Hand-curated. No national API exists for these; ~50 rows, annual review.
-- State preemption of local rent control is a POSITIVE for a value-add thesis.
CREATE TABLE IF NOT EXISTS reg_flags (
    scope           TEXT NOT NULL,       -- 'state' | 'county' | 'place'
    fips            TEXT NOT NULL,
    label           TEXT,
    rent_control    BOOLEAN DEFAULT FALSE,
    state_preempts  BOOLEAN DEFAULT FALSE,
    adu_by_right    BOOLEAN DEFAULT FALSE,
    notes           TEXT,
    reviewed_on     DATE,
    PRIMARY KEY (scope, fips)
);

-- ============================================================ output
CREATE TABLE IF NOT EXISTS tract_scores (
    geoid               TEXT PRIMARY KEY,
    score               NUMERIC(6,2),
    -- component subscores, all 0-100 before weighting
    s_rent_momentum     NUMERIC(6,2),
    s_income_growth     NUMERIC(6,2),
    s_education_influx  NUMERIC(6,2),
    s_affordability     NUMERIC(6,2),
    s_spatial           NUMERIC(6,2),
    s_supply_risk       NUMERIC(6,2),
    s_safety            NUMERIC(6,2),
    s_regulatory        NUMERIC(6,2),
    -- diagnostics
    rent_cagr_5y        NUMERIC(8,4),
    income_cagr_5y      NUMERIC(8,4),
    rent_income_spread  NUMERIC(8,4),   -- the actual gentrification signal
    rent_to_income      NUMERIC(8,4),
    is_opportunity_zone BOOLEAN,
    moran_cluster       TEXT,           -- HH | LL | HL | LH | NS
    moran_p             NUMERIC(8,5),
    data_completeness   NUMERIC(5,3),   -- 0-1; scores below threshold are not published
    computed_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scores_score ON tract_scores (score DESC);
CREATE INDEX IF NOT EXISTS idx_scores_cluster ON tract_scores (moran_cluster);

-- Provenance: what ran, when, how many requests it cost.
CREATE TABLE IF NOT EXISTS ingest_log (
    id          SERIAL PRIMARY KEY,
    dataset     TEXT,
    status      TEXT,
    rows        INT,
    requests    INT,
    detail      TEXT,
    ran_at      TIMESTAMPTZ DEFAULT now()
);
