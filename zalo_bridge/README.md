# Zalo Bridge — đọc tin Zalo group, forward sang Flask

NV vẫn dùng Zalo bình thường, không cần đăng nhập web. Account "Trợ lý Lan"
nằm im trong group team, đọc mọi tin → POST sang `/api/zalo-bridge/inbound`
→ pipeline parse + Lan reply giống như tin gửi từ web chat.

## 1. Chuẩn bị tài khoản

- Mua **1 SIM rác** đăng ký Zalo riêng (tài khoản "Trợ lý Lan").
- KHÔNG dùng SĐT cá nhân của anh — bị ban là mất số.
- Add account Lan vào tất cả Zalo group team đang chạy.

## 2. Lấy cookie + IMEI + UA

1. Cài extension Chrome **ZaloDataExtractor**:
   https://github.com/JustKemForFun/ZaloDataExtractor
2. Đăng nhập Zalo Web (`chat.zalo.me`) bằng account Lan.
3. Bấm extension → copy 3 giá trị: `cookie` (JSON array), `imei`, `userAgent`.
4. Cookie sống 30-60 ngày. Khi bridge log lỗi auth → lặp lại bước 2-3.

## 3. Cấu hình

```bash
cd /home/admin1/tieuhiemsoft/zalo_bridge
cp .env.example .env
nano .env
# Paste ZALO_COOKIES (1 dòng JSON), ZALO_IMEI, ZALO_USER_AGENT
# Đặt BRIDGE_SECRET = random string dài (cùng với env web)
```

Bên Flask, set env `ZALO_BRIDGE_SECRET` trong `pos-dashboard.env` cùng giá trị:
```
ZALO_BRIDGE_SECRET=...
```

## 4. Map Zalo group → team_code

1. Chạy bridge thử (xem mục 5).
2. Cho 1 NV trong group gửi tin bất kỳ. Bridge sẽ log `📩 [<thread_id>] ...`.
3. Lưu mapping vào `app_config`:

```sql
INSERT INTO app_config (key, value)
VALUES ('zalo_thread_<thread_id>', 'team-nam')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

Lặp lại cho mỗi team.

## 5. Chạy thử (foreground)

```bash
cd /home/admin1/tieuhiemsoft/zalo_bridge
node bridge.js
```

Quan sát log:
- `✅ Đã đăng nhập. uid=...` → login OK
- `🔌 Listener connected` → đang nghe
- `📩 [<tid>] <NV>: <body>` mỗi khi có tin
- `[POST] 200 ...` → Flask đã nhận

Ctrl+C để dừng.

## 6. Cài systemd để chạy nền

```bash
sudo cp /home/admin1/tieuhiemsoft/zalo_bridge/zalo-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zalo-bridge
sudo systemctl status zalo-bridge --no-pager
tail -f /home/admin1/tieuhiemsoft/zalo_bridge/bridge.log
```

## 7. Match NV (sender → user_id)

Endpoint Flask thử 2 nguồn theo thứ tự:
1. `users.zalo_uid` = sender uid Zalo (cần migration để thêm cột này — chưa làm).
2. `users.full_name` hoặc `users.username` (case-insensitive, trim) = `dName` Zalo.

→ Bước nhanh: đặt `full_name` trong bảng `users` khớp đúng tên Zalo NV hiển thị.

Khi không match được → endpoint trả `ok:false, error:'sender not matched'` + log,
tin Zalo **không vào DB**. Bridge tiếp tục chạy, không crash.

## 8. Cảnh báo

- API Zalo này **không chính thức**. Có nguy cơ ban account Lan. Có 1 SIM dự phòng.
- `zca-js` có thể break khi Zalo update Web → update package theo GitHub.
- Account Lan **chỉ đọc**, KHÔNG chat trong group → giảm rủi ro flag bot.
- Khi cookie hết hạn, bridge sẽ log lỗi auth → restart sau khi update `.env`.

## 9. Trouble-shooting

| Triệu chứng | Cách kiểm |
|---|---|
| `❌ Login Zalo fail` | Cookie hết hạn → re-extract |
| `🔌 Listener closed` lặp lại | Zalo kick listener (đăng nhập trùng?). Đóng Zalo Web ở browser. |
| `[POST] 401` | `BRIDGE_SECRET` 2 bên không khớp |
| `[POST] 200 ok:false error:'thread not mapped'` | Chưa `INSERT app_config zalo_thread_<tid>` |
| `[POST] 200 ok:false error:'sender not matched'` | `users.full_name` không khớp `dName` Zalo |
