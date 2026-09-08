"""PostgreSQL DDL and one-time legacy archive for salary phase-1 (nghiepvu.md)."""

from __future__ import annotations

from typing import Any


def _legacy_salary_configs_is_old(cur: Any) -> bool:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'salary_configs'
        """
    )
    cols = {str(r[0]) for r in cur.fetchall()}
    if not cols:
        return False
    if "config_code" in cols:
        return False
    return "code" in cols


def archive_legacy_salary_tables_if_needed(cur: Any) -> None:
    """Rename v1 salary tables if present so phase-1 names are free."""
    cur.execute("SELECT to_regclass('public.employees')")
    if cur.fetchone()[0] is not None:
        return
    cur.execute("SELECT to_regclass('public.salary_configs')")
    if cur.fetchone()[0] is None:
        return
    if not _legacy_salary_configs_is_old(cur):
        return
    # Drop FK order: children first, then salary_configs
    renames = [
        ("salary_kpi_tiers", "salary_kpi_tiers_legacy_v1"),
        ("salary_employee_settings", "salary_employee_settings_legacy_v1"),
        ("salary_period_results", "salary_period_results_legacy_v1"),
        ("salary_configs", "salary_configs_legacy_v1"),
    ]
    for old, new in renames:
        cur.execute("SELECT to_regclass(%s)", (f"public.{old}",))
        if cur.fetchone()[0] is not None:
            cur.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')


def create_phase1_tables(cur: Any) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id BIGSERIAL PRIMARY KEY,
            employee_code VARCHAR(100) NOT NULL UNIQUE,
            employee_name VARCHAR(255) NOT NULL,
            branch_code VARCHAR(50),
            team_code VARCHAR(100),
            leader_code VARCHAR(100),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_employees_branch_code ON employees(branch_code);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_configs (
            id BIGSERIAL PRIMARY KEY,
            config_code VARCHAR(100) NOT NULL UNIQUE,
            config_name VARCHAR(255) NOT NULL,
            branch_code VARCHAR(50),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            deduct_vat BOOLEAN NOT NULL DEFAULT TRUE,
            deduct_shipping_fee BOOLEAN NOT NULL DEFAULT TRUE,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_salary_configs_branch_code ON salary_configs(branch_code);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_cpqc_tiers (
            id BIGSERIAL PRIMARY KEY,
            salary_config_id BIGINT NOT NULL REFERENCES salary_configs(id) ON DELETE CASCADE,
            min_cpqc_percent NUMERIC(10,2) NOT NULL,
            max_cpqc_percent NUMERIC(10,2),
            commission_percent NUMERIC(10,4) NOT NULL,
            require_import_price_lte NUMERIC(18,2),
            stop_run BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order INT NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_salary_cpqc_tiers_config_id ON salary_cpqc_tiers(salary_config_id);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_salary_settings (
            id BIGSERIAL PRIMARY KEY,
            employee_code VARCHAR(100) NOT NULL UNIQUE
                REFERENCES employees(employee_code) ON DELETE RESTRICT,
            base_salary NUMERIC(18,2) NOT NULL DEFAULT 0,
            allowance NUMERIC(18,2) NOT NULL DEFAULT 0,
            default_penalty NUMERIC(18,2) NOT NULL DEFAULT 0,
            default_advance NUMERIC(18,2) NOT NULL DEFAULT 0,
            salary_config_id BIGINT REFERENCES salary_configs(id) ON DELETE SET NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_employee_salary_settings_employee_code
            ON employee_salary_settings(employee_code);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_salary_configs (
            id BIGSERIAL PRIMARY KEY,
            product_code VARCHAR(100) NOT NULL UNIQUE,
            product_name VARCHAR(255) NOT NULL,
            import_price NUMERIC(18,2) NOT NULL DEFAULT 0,
            cpqc_standard_per_order NUMERIC(18,2) NOT NULL,
            vat_rate NUMERIC(6,4),
            is_commission_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_product_salary_configs_product_code
            ON product_salary_configs(product_code);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_period_product_lines (
            id BIGSERIAL PRIMARY KEY,
            period_key VARCHAR(20) NOT NULL,
            employee_code VARCHAR(100) NOT NULL,
            employee_name_snapshot VARCHAR(255) NOT NULL,
            branch_code VARCHAR(50),
            product_code VARCHAR(100) NOT NULL,
            product_name_snapshot VARCHAR(255) NOT NULL,
            revenue_gross NUMERIC(18,2) NOT NULL DEFAULT 0,
            quantity NUMERIC(18,2) NOT NULL DEFAULT 0,
            orders_count INT NOT NULL DEFAULT 0,
            ads_cost NUMERIC(18,2) NOT NULL DEFAULT 0,
            shipping_fee NUMERIC(18,2) NOT NULL DEFAULT 0,
            vat_rate NUMERIC(6,4) NOT NULL DEFAULT 0,
            revenue_net NUMERIC(18,2) NOT NULL DEFAULT 0,
            revenue_for_commission NUMERIC(18,2) NOT NULL DEFAULT 0,
            cpqc_standard_per_order NUMERIC(18,2) NOT NULL DEFAULT 0,
            ads_per_order NUMERIC(18,2) NOT NULL DEFAULT 0,
            cpqc_percent NUMERIC(10,2) NOT NULL DEFAULT 0,
            commission_percent NUMERIC(10,4) NOT NULL DEFAULT 0,
            commission_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
            import_price_snapshot NUMERIC(18,2) NOT NULL DEFAULT 0,
            stop_run BOOLEAN NOT NULL DEFAULT FALSE,
            calc_note TEXT,
            salary_config_id BIGINT REFERENCES salary_configs(id) ON DELETE SET NULL,
            calc_snapshot_json JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (period_key, employee_code, product_code)
        );
        CREATE INDEX IF NOT EXISTS idx_sppl_period_employee
            ON salary_period_product_lines(period_key, employee_code);
        CREATE INDEX IF NOT EXISTS idx_sppl_period_product
            ON salary_period_product_lines(period_key, product_code);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_period_input_lines (
            id BIGSERIAL PRIMARY KEY,
            period_key VARCHAR(20) NOT NULL,
            employee_code VARCHAR(100) NOT NULL
                REFERENCES employees(employee_code) ON DELETE CASCADE,
            product_code VARCHAR(100) NOT NULL,
            revenue_gross NUMERIC(18,2) NOT NULL DEFAULT 0,
            quantity NUMERIC(18,2) NOT NULL DEFAULT 0,
            orders_count INT NOT NULL DEFAULT 1,
            ads_cost NUMERIC(18,2) NOT NULL DEFAULT 0,
            shipping_fee NUMERIC(18,2) NOT NULL DEFAULT 0,
            returned_revenue_gross NUMERIC(18,2) NOT NULL DEFAULT 0,
            returned_quantity NUMERIC(18,2) NOT NULL DEFAULT 0,
            returned_orders_count INT NOT NULL DEFAULT 0,
            shipping_fee_return_delta NUMERIC(18,2) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (period_key, employee_code, product_code)
        );
        CREATE INDEX IF NOT EXISTS idx_spil_period
            ON salary_period_input_lines(period_key);
        CREATE INDEX IF NOT EXISTS idx_spil_period_employee
            ON salary_period_input_lines(period_key, employee_code);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_period_employee_results (
            id BIGSERIAL PRIMARY KEY,
            period_key VARCHAR(20) NOT NULL,
            employee_code VARCHAR(100) NOT NULL,
            employee_name_snapshot VARCHAR(255) NOT NULL,
            branch_code VARCHAR(50),
            total_revenue_gross NUMERIC(18,2) NOT NULL DEFAULT 0,
            total_revenue_net NUMERIC(18,2) NOT NULL DEFAULT 0,
            total_shipping_fee NUMERIC(18,2) NOT NULL DEFAULT 0,
            total_ads_cost NUMERIC(18,2) NOT NULL DEFAULT 0,
            total_commission_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
            base_salary NUMERIC(18,2) NOT NULL DEFAULT 0,
            allowance NUMERIC(18,2) NOT NULL DEFAULT 0,
            penalty NUMERIC(18,2) NOT NULL DEFAULT 0,
            advance_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
            final_salary NUMERIC(18,2) NOT NULL DEFAULT 0,
            stop_run_flag_count INT NOT NULL DEFAULT 0,
            calc_snapshot_json JSONB,
            status VARCHAR(50) NOT NULL DEFAULT 'draft',
            finalized_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (period_key, employee_code)
        );
        CREATE INDEX IF NOT EXISTS idx_sper_period_employee
            ON salary_period_employee_results(period_key, employee_code);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_config_audit_logs (
            id BIGSERIAL PRIMARY KEY,
            config_type VARCHAR(50) NOT NULL,
            config_ref_id BIGINT NOT NULL,
            action_type VARCHAR(50) NOT NULL,
            old_data_json JSONB,
            new_data_json JSONB,
            changed_by VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def _ensure_salary_periods_registry(cur: Any) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_periods (
            period_key VARCHAR(20) PRIMARY KEY,
            status VARCHAR(50) NOT NULL DEFAULT 'draft',
            finalized_at TIMESTAMPTZ,
            finalized_by VARCHAR(100),
            calc_snapshot_json JSONB,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'salary_config_audit_logs'
          AND column_name = 'period_key'
        LIMIT 1
        """
    )
    if not cur.fetchone():
        cur.execute(
            """
            ALTER TABLE salary_config_audit_logs
            ADD COLUMN period_key VARCHAR(20)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_salary_audit_period_key
            ON salary_config_audit_logs(period_key)
            """
        )


def _ensure_spil_return_columns(cur: Any) -> None:
    """DB cũ: thêm cột hoàn hàng cho salary_period_input_lines."""
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'salary_period_input_lines'
          AND column_name = 'returned_revenue_gross'
        LIMIT 1
        """
    )
    if cur.fetchone():
        return
    cur.execute(
        """
        ALTER TABLE salary_period_input_lines
            ADD COLUMN returned_revenue_gross NUMERIC(18,2) NOT NULL DEFAULT 0,
            ADD COLUMN returned_quantity NUMERIC(18,2) NOT NULL DEFAULT 0,
            ADD COLUMN returned_orders_count INT NOT NULL DEFAULT 0,
            ADD COLUMN shipping_fee_return_delta NUMERIC(18,2) NOT NULL DEFAULT 0
        """
    )


def ensure_phase1_schema(cur: Any) -> None:
    archive_legacy_salary_tables_if_needed(cur)
    create_phase1_tables(cur)
    _ensure_spil_return_columns(cur)
    _ensure_salary_periods_registry(cur)
