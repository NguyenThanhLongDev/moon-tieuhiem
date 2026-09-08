# CLAUDE.md — Moon (CPQC) · Phần mềm Chi phí Quảng cáo

Tài liệu cho AI agent / dev tiếp quản. Đọc kỹ trước khi sửa.

> **Bản chất**: Đây là **bản CLONE từ dự án Tiểu Hiềm** (`/home/admin1/tieuhiemsoft/posbottieuhiem`), cắt gọn lại chỉ còn **Chi phí Quảng cáo + Cài đặt + Quản lý nhân sự (Chấm công/HR)**. Dùng để **bán/triển khai cho công ty khác** (mỗi khách 1 bản riêng).

---

## 0. ⚠️ NGUYÊN TẮC BẤT DI BẤT DỊCH
1. **KHÔNG ĐỤNG tieuhiem.com** — dự án gốc (`posbottieuhiem`), DB `tieuhiem_pos`, service `pos-dashboard`/`pos-scheduler`, cron của tieuhiem. Tất cả phải GIỮ NGUYÊN. Chỉ đọc (SELECT) để tham khảo, KHÔNG ghi/sửa.
2. **Lock file, Redis DB, /tmp** phải RIÊNG (không share với tieuhiem) — xem §6.
3. **Phân tích → trình bày → chờ OK** mới sửa logic nghiệp vụ (map ads, tính chi phí).

---

## 1. Thông tin hệ thống
| | |
|---|---|
| **Thư mục** | `/home/admin1/cpqc` (chủ `admin1`; user `dev3` có ACL rwx) |
| **DB** | `db_cpqc` — PostgreSQL 16, `localhost:5432`, role `tieuhiem` (nối THẲNG 5432, **KHÔNG qua PgBouncer**) |
| **Redis** | `redis://127.0.0.1:6379/**9**` (DB 9 — tách khỏi tieuhiem dùng DB 0) |
| **Web** | gunicorn `127.0.0.1:**5060**`, systemd `cpqc.service` |
| **Domain** | `https://moon.tieuhiem.com` (Cloudflare Tunnel) |
| **Login** | `admin` / `cpqc@2026` (đặt tạm, nên đổi) |
| **Env** | `deploy/cpqc.env` |

**Quản lý service**: `sudo systemctl {status|restart|stop} cpqc.service`
**Sudo**: dev3 không có; chạy quyền cao qua user `admin1` (có sudo).

---

## 2. Khác biệt với bản gốc (đã cắt gọn)
- **web_app.py**: ĐÃ comment `auto_bootstrap_if_empty()` + `register_kho_vat_ly_module()` (kho fail `init_wh_tables` trên DB mới + ngoài phạm vi). Các module khác vẫn đăng ký.
- **Navbar** (`app_constants.py`): chỉ còn **Doanh thu · Quản lý nhân sự (Chấm công + Hồ sơ HR) · Quảng cáo · Cài đặt**. Đã bỏ: Báo cáo, Báo NS, Teams, Hướng dẫn, Kho, Lương 2B/B1, Marketing/Leader Brain, Kiểm tra số liệu. Đổi tên "TK 6.1% VAT" → **"Tài khoản quảng cáo (VAT)"**. Bỏ tab "Ngân sách TK", "Chi tiết", "Lịch sử chốt".
- **Scheduler tắt** (`WEB_SCHEDULER_DISABLED=1`), **kho bg-sync tắt** (`WEB_KHO_BG_SYNC=0`).
- **Dữ liệu**: đã wipe sạch data Tiểu Hiềm. File rò rỉ (config.json chứa token/cookie/telegram/fb secret, shops.json, users.json, data_*.json, stock_*) đã chuyển ra `/home/admin1/cpqc_LEAK_backup` (700).

---

## 3. DB (db_cpqc) — ~100 bảng
Schema dựng bằng: `auto_migrate()` (file SQL) + migration lúc module load (pa_db, cc_db...) + `pg_dump --schema-only` các bảng "mồ côi" từ `tieuhiem_pos` (app_config, vat_options, via_accounts, fb_ad_account_*, pos_page_daily_metrics...).

**Bảng quan trọng**:
- `pa_fb_tokens` (token FB), `pa_business_managers`, `pa_pages`, `pa_ad_accounts` — module page_account
- `fb_pages` (page từ OAuth login, có `access_token`+`fb_user_id`), `fb_ad_account_mappings` (TK QC ↔ shop)
- `fb_ads_page_daily_spend` (chi phí FB theo page — báo cáo chi-phi-qc đọc cái này), `fb_ads_daily_metrics`
- `users`, `shops`, `wh_shops` (api_key Pancake), `daily_shop_metrics` (doanh thu/lợi nhuận POS)
- `cc_*` (chấm công), `hr_*` (hồ sơ HR)

**Gotcha đã fix**: `users` thiếu cột `resigned_at` → `load_all_users()` văng → fallback `users.json` (id lệch) → chấm công đá /login. Đã `ALTER TABLE users ADD COLUMN resigned_at date`.

---

## 4. Tích hợp Facebook (2 cách — như tieuhiem)
### 4.1 System User Token (CHÍNH — không cần App Review)
- **Page & Tài khoản → Thêm Token FB** → dán token → Sync.
- Khách tạo token vĩnh viễn trong BM của họ (scope `ads_read` + `business_management`), gán TK QC + Pages cho system user.
- `_sync_all` (`modules/page_account/__init__.py`): nếu `/me/businesses` rỗng (đặc thù system user) → kéo TRỰC TIẾP `/me/adaccounts` + `/me/accounts`, suy BM từ field `business` của TK. Backfill tên page qua `/act_<id>/promote_pages` (chỉ cần `ads_read`).

### 4.2 OAuth Login (PHỤ — cần App Review cho khách ngoài)
- **`/fb-pages/`** → "Đăng nhập Facebook". Module `fb_pages`. Dùng `FACEBOOK_APP_ID`/`SECRET` + `APP_BASE_URL` trong env.
- Callback `https://<domain>/fb-pages/auth/callback` phải whitelist trong FB App.
- Callback lưu: pages → `fb_pages`, ad accounts → `pa_ad_accounts`, **token → `pa_fb_tokens`** (để cron kéo chi phí). Token user hết hạn ~60 ngày → login lại.
- **Giới hạn FB**: app dev-mode chỉ admin/tester login; khách ngoài cần app **Live + App Review** (`ads_read`+`pages_read_engagement`). App Review **1 lần/1 app dùng chung**, không phải mỗi khách.

### 4.3 Tên/ảnh page — giới hạn
Token chỉ đọc được tên+ảnh page **mình có quyền** (admin/trong BM). Page không quản → **chỉ hiện ID** (`#10`/`#100`). **Chi phí vẫn tính ĐÚNG 100%**. Muốn ra tên: gán page vào BM, hoặc admin page login OAuth.

---

## 5. Đồng bộ dữ liệu (POS + Chi phí Meta)
- **Nút "Đồng bộ POS + Ads"** (Cài đặt→Shop&Web có ô Từ/Đến ngày; Chi phí QC có "Sync ngày/nhiều ngày"): chạy nền `run_auto_refresh_all_data.sh <date>` (endpoint `settings_bp.sync_pos_data`, chỉ admin/superadmin).
- `run_auto_refresh_all_data.sh` (8 bước): POS analytics(150d) · daily_shop_metrics · order status · orders · carrier pickup · ~~[6] sản phẩm (TẮT nếu `SKIP_SYNC_PRODUCTS=1`)~~ · **[7] FB ads** (`sync_facebook_ads_to_db`) · verify.
- **Chi phí page**: `scripts/sync_fb_ads_by_page.py` → `fb_ads_page_daily_spend`.
- **KHÔNG kéo sản phẩm** (`SKIP_SYNC_PRODUCTS=1`).

### 5.1 Cron tự động (admin1 crontab)
```
*/30 7-22 * * *  /home/admin1/cpqc/scripts/cron_sync.sh       # NHẸ: hôm nay, SYNC_DAYS=2 (~17s)
0 1 * * *        /home/admin1/cpqc/scripts/cron_sync.sh full  # ĐẦY: hôm nay+hôm qua, SYNC_DAYS=150
```
Log: `logs/cron_sync.log`.

### 5.2 Resolve token cho chi phí — FIX quan trọng
`sync_fb_ads_by_page._get_global_fb_token()`: đọc `fb_pages.access_token`, **fallback `pa_fb_tokens`** (token System User). Trước khi fix, xóa fb_pages/JSON store → "không có token sống". Đã thêm fallback.
`sync_facebook_ads_to_db`: resolve token qua JSON store `facebook_ads_tokens_store.json` theo `(shop_key, ad_account_id)` → fallback env `FACEBOOK_ACCESS_TOKEN`.

---

## 6. Tách biệt với tieuhiem (BẮT BUỘC khi clone/sửa)
- **Lock**: `sync_fb_ads_by_page.py` dùng `/tmp/cpqc_sync_fb_ads_by_page.lock` (KHÔNG phải `/tmp/sync_fb_ads_by_page.lock` của tieuhiem).
- **Redis**: DB 9.
- **DB/code/service/cron/domain**: tất cả riêng.

---

## 7. Triển khai cho khách mới (mô hình SaaS)
Mỗi khách = **1 bản moon riêng** (DB riêng, domain riêng, service+cron riêng), data cách ly.
1. **Bên Facebook khách**: tạo System User token trong BM của họ (Never expire, `ads_read`+`business_management`), gán TK QC + Pages. (Hoặc OAuth login nếu app đã review.)
2. **Bên server**: clone `/home/admin1/cpqc` → bản mới (DB `db_<khach>`, domain `<khach>.tieuhiem.com`, port mới, service+cron mới, wipe data, admin riêng). Thêm ingress domain trong **Cloudflare Tunnel (dashboard — remote-managed, KHÔNG sửa file local)**.
3. **Kết nối**: Thêm Token FB → Sync; thêm shop + Pancake API key → Đồng bộ POS.

> DB mới cần `CREATEDB` (role `tieuhiem` chưa có) → tạo qua `sudo -u postgres createdb -O tieuhiem db_<khach>`.

---

## 8. Tham khảo nhanh
- Restart: `sudo systemctl restart cpqc.service`
- Log: `journalctl -u cpqc.service -f` / `logs/cron_sync.log`
- DB: `PGPASSWORD=... psql -h localhost -p 5432 -U tieuhiem -d db_cpqc`
- Sync tay (admin1): `cd /home/admin1/cpqc && set -a; source deploy/cpqc.env; set +a; .venv/bin/python scripts/sync_fb_ads_by_page.py --date YYYY-MM-DD`
- Wipe data quảng cáo (giữ user/shop): TRUNCATE các bảng `pa_*`, `fb_*`, `ads_*`, `*ad_account*`, `pos_page_daily_metrics` (giữ `users`, `shops`, `wh_shops`, `daily_shop_metrics`, `cc_*`, `hr_*`).

---

## 9. Cấu trúc mã (kế thừa từ posbottieuhiem)
- `web_app.py` — entry, register blueprint (đã cắt kho)
- `app_constants.py` — PAGE_TEMPLATE/navbar (đã cắt menu)
- `blueprints/settings_bp.py` — Cài đặt + endpoint `sync_pos_data` (nút Đồng bộ POS)
- `modules/chi_phi_qc/` — báo cáo chi phí QC (đọc `fb_ads_page_daily_spend`)
- `modules/page_account/` — token FB + sync BM/Page/TKQC (System User)
- `modules/fb_pages/` — OAuth login Facebook
- `modules/cham_cong/`, `modules/hr/` — Quản lý nhân sự
- `scripts/sync_fb_ads_by_page.py`, `scripts/sync_facebook_ads_to_db.py`, `run_auto_refresh_all_data.sh`, `scripts/cron_sync.sh`

> Chi tiết kiến trúc Flask/DB/sync gốc: xem `posbottieuhiem/CLAUDE.md` (bản gốc) — nhưng nhớ moon đã cắt kho + đổi DB/Redis/lock.
