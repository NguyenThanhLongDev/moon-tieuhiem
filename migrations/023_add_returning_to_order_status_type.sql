-- Migration 023: Add 'returning' value to order_status_type enum
-- Fixes: sync_orders_order_items_to_db.py crash when Pancake status=4 (returning)

ALTER TYPE order_status_type ADD VALUE IF NOT EXISTS 'returning';
ALTER TYPE order_status_type ADD VALUE IF NOT EXISTS 'shipped';
