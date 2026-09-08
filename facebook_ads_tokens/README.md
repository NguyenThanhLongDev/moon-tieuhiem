# Facebook Ads tokens (isolated module)

Self-contained tooling to store **long-lived user access tokens**, exchange short→long, refresh before expiry, and smoke-test Graph endpoints.

**Tích hợp controlled (dashboard + sync):**

- **Cài đặt → tab “FB token kho”** (`/settings?tab=facebook_tokens`, chỉ admin): xem danh sách (token đã mask), thêm/sửa dòng, exchange, refresh, test Graph. Lỗi token store không làm crash cả trang Cài đặt.
- **`scripts/sync_facebook_ads_to_db.py`**: với mỗi mapping, nếu kho JSON có token khớp `shop_key` + `ad_account_id` thì dùng token đó; **không có** thì fallback `FACEBOOK_ACCESS_TOKEN` / `config.json` như trước. File kho trống → hành vi cũ.
- **`facebook_ads_tokens/resolve.py`**: hàm `try_store_access_token` + `fb_ads_tokens_store_path()` dùng chung cho sync và (gián tiếp) web.

## Requirements

- Python 3.10+
- `requests` (already in project `requirements.txt`)
- Run commands from the **repository root** (`posbot/`) so `python -m facebook_ads_tokens` resolves the package.

## Setup

1. Export app credentials (never hardcode `FACEBOOK_APP_SECRET`):

   ```bash
   export FACEBOOK_APP_ID="your_app_id"
   export FACEBOOK_APP_SECRET="your_app_secret"
   ```

2. Optional: custom store path (default: `facebook_ads_tokens_store.json` in repo root):

   ```bash
   export FB_ADS_TOKENS_STORE="/secure/path/tokens.json"
   ```

3. See repo root `.env.example` for variable names.

4. Copy `facebook_ads_tokens/tokens.sample.json` to your real store path and replace placeholders, **or** use `exchange` CLI to create rows.

5. Add the real store file to deployment secrets; the default filename `facebook_ads_tokens_store.json` is listed in `.gitignore`.

## CLI overview

| Command | Purpose |
|--------|---------|
| `exchange` | Short-lived → long-lived token; upsert row by `shop_key` + `ad_account_id` |
| `refresh` | Force refresh one row |
| `refresh-all` | Refresh rows that are expired or expire within `--within-days` |
| `status` | Print health for all rows |
| `test-adaccounts` | `GET /me/adaccounts` |
| `test-insights` | `GET /act_{id}/insights` for one day |

Global flags: `--store PATH`, `-v` / `--verbose`. Place globals **before** the subcommand, e.g.  
`python3 -m facebook_ads_tokens --store /path/tokens.json status`.

## Health statuses

- `valid` — not expired, outside the expiring window
- `expiring_soon` — `expires_at` within `--within-days` (default 7)
- `expired` — past `expires_at` (or missing token)
- `refresh_failed` — row has `refresh_status` `refresh_failed` / `failed` (checked before expiry logic)

Rows **without** `expires_at` are skipped by `refresh-all` (nothing to schedule); use manual `refresh` or re-run `exchange` after obtaining `expires_in` from the API.

## Logging and security

- Logs use **masked** tokens only (`mask_token`).
- `FACEBOOK_APP_SECRET` is read from the environment, not from the JSON store.

## CLI usage examples

```bash
cd /home/admin/posbot
export FACEBOOK_APP_ID="..." FACEBOOK_APP_SECRET="..."

# Short-lived → long-lived and save row
python3 -m facebook_ads_tokens exchange \
  --shop-key shop_1 \
  --short-token "SHORT_TOKEN_FROM_LOGIN" \
  --facebook-user-id "USER_ID" \
  --ad-account-id "123456789" \
  --note "optional"

# Refresh one row
python3 -m facebook_ads_tokens refresh --shop-key shop_1 --ad-account-id "123456789"

# Scheduler-style: refresh all due rows
python3 -m facebook_ads_tokens refresh-all --within-days 14

# Health listing
python3 -m facebook_ads_tokens status --within-days 7

# Smoke tests (token from env arg or store row)
python3 -m facebook_ads_tokens test-adaccounts --shop-key shop_1 --ad-account-id "123456789"
python3 -m facebook_ads_tokens test-insights --ad-account-id "123456789" --date 2026-03-28 \
  --shop-key shop_1
```

## Tự động refresh định kỳ (trên VPS)

Trong repo có sẵn (không đụng cron job `run_auto_refresh_all_data`):

1. **`run_facebook_token_refresh.sh`** — nạp `deploy/pos-dashboard.env` (hoặc `FB_TOKEN_REFRESH_ENV_FILE`), gọi `refresh-all` với `--within-days` (mặc định **14** qua `FB_TOKEN_REFRESH_WITHIN_DAYS`). Ghi log vào `logs/fb_token_refresh.log`. Nếu thiếu App ID/Secret → ghi SKIP, thoát 0.
2. **`install_fb_token_refresh_cron.sh`** — thêm **một dòng cron** (mặc định **03:15 mỗi ngày**). Đổi lịch:  
   `FB_TOKEN_REFRESH_CRON='15 3 * * 1' ./install_fb_token_refresh_cron.sh`

Cài một lần trên server:

```bash
cd /home/admin/posbot
./install_fb_token_refresh_cron.sh
```

Chỉ các dòng trong kho có `expires_at` và đang **hết hạn hoặc trong cửa sổ `--within-days`** mới được gọi API refresh (tránh spam).

## Cron example (thủ công)

```cron
15 3 * * * cd /home/admin/posbot && /bin/bash ./run_facebook_token_refresh.sh
```

## Sau khi deploy

- **Restart Flask / gunicorn** sau khi đổi `web_app.py` (production).
- Đặt `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` trên server nếu dùng Exchange / Refresh từ tab **FB token kho**.
