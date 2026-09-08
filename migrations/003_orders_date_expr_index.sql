-- Performance patch: speed up dashboard order-status by date + scope.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_orders_shop_created_date_expr
    ON orders (shop_id, (DATE(created_at_pos)));

COMMIT;
