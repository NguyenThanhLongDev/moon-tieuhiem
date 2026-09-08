# Phase 1 - Core Admin DB Migration

This phase creates only core admin/permission tables:

- `teams`
- `users`
- `shops`
- `user_shop_assignments`
- `webs`

No dashboard/telegram runtime read path is switched in this phase.

## Apply

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/pos_dashboard"
psql "$DATABASE_URL" -f migrations/001_core_admin.sql
python3 scripts/bootstrap_core_admin_from_json.py
```

## Verify

```bash
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM teams;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM users;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM shops;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM user_shop_assignments;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM webs;"
```

## Rollback

```bash
psql "$DATABASE_URL" -f migrations/001_core_admin_rollback.sql
```

## Phase B (priority business tables, no route switch)

```bash
psql "$DATABASE_URL" -f migrations/002_priority_metrics.sql
python3 scripts/bootstrap_daily_shop_metrics_from_json.py
```

Verify:

```bash
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM daily_shop_metrics;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM orders;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM order_items;"
```

Rollback Phase B:

```bash
psql "$DATABASE_URL" -f migrations/002_priority_metrics_rollback.sql
```

## Phase B.1 (orders + order_items sync job only)

No web route switch. Telegram/dashboard behavior remains unchanged.

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/pos_dashboard"
python3 scripts/sync_orders_order_items_to_db.py --date 2026-03-26
```

Optional:

```bash
python3 scripts/sync_orders_order_items_to_db.py --date 2026-03-26 --shop-key shop1
python3 scripts/sync_orders_order_items_to_db.py --date 2026-03-26 --dry-run
```

Quick verify:

```bash
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM orders;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM order_items;"
psql "$DATABASE_URL" -c \"SELECT s.shop_key, COUNT(*) FROM orders o JOIN shops s ON s.id=o.shop_id GROUP BY s.shop_key ORDER BY COUNT(*) DESC LIMIT 10;\"
```

## Phase B.1 parity checker (verification only)

Compare API vs DB by shop/day. No route switch.

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/pos_dashboard"
python3 scripts/verify_orders_api_vs_db.py --date 2026-03-26
```

One shop:

```bash
python3 scripts/verify_orders_api_vs_db.py --date 2026-03-26 --shop-key shop1
```

Show passing shops too:

```bash
python3 scripts/verify_orders_api_vs_db.py --date 2026-03-26 --show-matches
```
