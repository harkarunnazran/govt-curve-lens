CREATE TABLE IF NOT EXISTS gilt_data (
    cob_date DATE,
    data_date DATE,
    isin VARCHAR(255),
    name VARCHAR(255),
    coupon_pct NUMERIC(10, 4),
    maturity_date DATE,
    first_issue_date DATE,
    dividend_dates VARCHAR(255),
    next_ex_div_date DATE,
    amount_in_issue_gbp_mn NUMERIC(15, 5),
    bucket VARCHAR(255),
    PRIMARY KEY (cob_date, isin)
);