-- POS-origin avg profit per order (from analytics JSON result.avg_profit via bootstrap).
ALTER TABLE daily_shop_metrics
    ADD COLUMN IF NOT EXISTS pos_avg_profit_per_order NUMERIC(18,2) NOT NULL DEFAULT 0;
