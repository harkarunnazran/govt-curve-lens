CREATE TABLE IF NOT EXISTS boe_yield_curve (
    curve_type VARCHAR(20) NOT NULL CHECK (curve_type IN ('spot', 'forward')),
    curve_date DATE NOT NULL,
    maturity_years DOUBLE NOT NULL,
    rate_pct DOUBLE NOT NULL,  -- percent per annum, e.g. 4.25 means 4.25%
    published_at DATE NOT NULL,
    published_at_source VARCHAR(20) NOT NULL CHECK (published_at_source IN ('observed', 'estimated')),
    fetched_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (curve_type, curve_date, maturity_years)
);
