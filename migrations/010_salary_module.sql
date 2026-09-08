BEGIN;

CREATE TABLE IF NOT EXISTS salary_configs (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    branch_code VARCHAR(100) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    default_vat_rate DOUBLE PRECISION NOT NULL DEFAULT 0.10,
    deduct_shipping_fee BOOLEAN NOT NULL DEFAULT TRUE,
    stop_ads_threshold DOUBLE PRECISION NULL,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS salary_kpi_tiers (
    id BIGSERIAL PRIMARY KEY,
    salary_config_id BIGINT NOT NULL REFERENCES salary_configs(id) ON DELETE CASCADE,
    min_ads_per_order DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_ads_per_order DOUBLE PRECISION NULL,
    kpi_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_stop_run BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS salary_employee_settings (
    id BIGSERIAL PRIMARY KEY,
    employee_code VARCHAR(100) NOT NULL UNIQUE,
    employee_name VARCHAR(200) NOT NULL,
    branch_code VARCHAR(100) NOT NULL,
    base_salary DOUBLE PRECISION NOT NULL DEFAULT 0,
    allowance DOUBLE PRECISION NOT NULL DEFAULT 0,
    config_id BIGINT NOT NULL REFERENCES salary_configs(id) ON DELETE RESTRICT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS salary_period_results (
    id BIGSERIAL PRIMARY KEY,
    period_key VARCHAR(20) NOT NULL,
    employee_code VARCHAR(100) NOT NULL,
    employee_name VARCHAR(200) NOT NULL,
    branch_code VARCHAR(100) NOT NULL,
    revenue_gross DOUBLE PRECISION NOT NULL DEFAULT 0,
    vat_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    revenue_net DOUBLE PRECISION NOT NULL DEFAULT 0,
    shipping_fee DOUBLE PRECISION NOT NULL DEFAULT 0,
    revenue_for_kpi DOUBLE PRECISION NOT NULL DEFAULT 0,
    ads_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    orders_count INT NOT NULL DEFAULT 0,
    ads_per_order DOUBLE PRECISION NOT NULL DEFAULT 0,
    kpi_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
    kpi_salary DOUBLE PRECISION NOT NULL DEFAULT 0,
    base_salary DOUBLE PRECISION NOT NULL DEFAULT 0,
    allowance DOUBLE PRECISION NOT NULL DEFAULT 0,
    penalty DOUBLE PRECISION NOT NULL DEFAULT 0,
    advance DOUBLE PRECISION NOT NULL DEFAULT 0,
    final_salary DOUBLE PRECISION NOT NULL DEFAULT 0,
    stop_ads BOOLEAN NOT NULL DEFAULT FALSE,
    calc_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_salary_configs_branch_active ON salary_configs(branch_code, is_active);
CREATE INDEX IF NOT EXISTS idx_salary_tiers_config_sort ON salary_kpi_tiers(salary_config_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_salary_emp_branch_active ON salary_employee_settings(branch_code, is_active);
CREATE INDEX IF NOT EXISTS idx_salary_result_period_branch ON salary_period_results(period_key, branch_code);

COMMIT;
