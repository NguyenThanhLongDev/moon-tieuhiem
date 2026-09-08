# LAN — Tài liệu nhận diện tin nhắn (audit 2026-05-18, updated sau commit `6c43700f`)

> Cập nhật 2026-05-18: 9/10 gaps trong §5 đã được fix ở commit `6c43700f`
> (`feat(lan-detection): fix 9/10 gaps trong LAN_DETECTION audit`). Mỗi gap đã
> giải quyết được đánh dấu `[RESOLVED 2026-05-18 commit 6c43700f]` ở tiêu đề
> + có dòng "Fix:" tóm tắt cách fix kèm file:line. Chỉ còn 5.9 (order check
> image branch) là quyết định nghiệp vụ — không phải bug.

Trợ lý Zalo "Lan" nhận tin qua webhook `POST /api/zalo-bridge/inbound`
(`posbottieuhiem/web_app.py:787`). Tài liệu này map từng nhánh decision
+ detector + prompt AI để dev jump thẳng vào sửa.

Tin được phân vào 4 luồng:
- **NS** (`budget_chat`) — báo ngân sách TK QC.
- **CP CTY** (`expense_chat`, group inbox `zalo_expense_inbox`).
- **CP RIÊNG** (1-1 với Lan → group `zalo_expense_private_inbox`,
  default `638544760243854625`).
- **Im lặng** — chit chat không liên quan.

---

## 1. Decision tree (entry point: `zalo_bridge_inbound`)

File: `posbottieuhiem/web_app.py:830-1237`.

**Cooldown gate** — mọi nhánh push reply gọi qua `_lan_cooldown_check(uid,
tid)` / `_lan_cooldown_mark(uid, tid)` (`web_app.py:790-827`). Window mặc
định 3.0s/sender per thread (env `ZALO_LAN_REPLY_COOLDOWN_SEC`). Trong
cooldown → SKIP reply, **vẫn save data** (vd expense). Map in-memory mỗi
worker, sweep entries >60s khi map >500 keys.

```
[INBOUND]
  ├─ Auth X-Bridge-Secret  (w:836)
  ├─ Lookup sender_name qua zalo_uid nếu rỗng  (w:855)
  ├─ Dedup zalo_msg_id qua budget_chat_messages ∪ company_expense_items  (w:868)
  │
  ├─ if kind == "image":  (w:885)
  │   ├─ caption + KHÔNG private + parse_budget_message(caption).items > 0
  │   │     OR is_budget_intent(caption)  → COI caption LÀ TEXT NS (kind='text')  (w:890)
  │   ├─ caption + _parse_expense_text(caption).amount > 0
  │   │     → handle_expense_text(caption)  ← KHÔNG OCR  (w:904)
  │   └─ ELSE → handle_expense_image(image_url, caption)  (w:916)
  │       (Gemini trả thêm `is_ad_account_dashboard` — reject về NS flow)
  │
  ├─ if missing tid/body → 400
  │
  ├─ if is_private (thread_type='user'):  (w:925)
  │   ├─ NS detection TRƯỚC (parse_budget_message + is_budget_intent)  (w:935-952)
  │   │     items > 0 OR is_budget_intent → push hint "NS phải báo trong NHÓM TEAM"
  │   │     + cooldown check + return (KHÔNG persist expense, KHÔNG persist NS)
  │   ├─ has_dup_pending (load `dup_pending_<uid>`) → handle_dup_pending_reply  (w:956-966)
  │   │     [BYPASS relevance gate — user trả lời rất ngắn "đúng/khác"]
  │   ├─ _is_relevant_to_lan(body) == False → IM LẶNG return  (w:969-971)
  │   ├─ _parse_expense_text(body).amount > 0 → handle_expense_text(is_private=True)
  │   │     [vẫn save dù cooldown — mark cooldown trước push]  (w:973-982)
  │   ├─ handle_expense_follow_up(is_private=True)  (w:984-987)
  │   └─ ELSE → cooldown check → push "Lan chưa thấy số tiền rõ"  (w:989-997)
  │
  ├─ team_code = cfg.get("zalo_thread_<tid>")  (w:1005)
  │
  ├─ if NOT team_code  (thread không map team NS):  (w:1008)
  │   ├─ handle_dup_pending_reply  (w:1015)
  │   ├─ _parse_expense_text → handle_expense_text + cooldown mark  (w:1019-1024)
  │   ├─ handle_expense_follow_up  (w:1026-1029)
  │   └─ ELSE → IM LẶNG return ok=false thread-not-mapped  (w:1035)
  │
  ├─ Match sender → users.zalo_uid / full_name; not matched → log
  │   zalo_pending_senders + return  (w:1037-1091)
  │
  ├─ insert_message vào budget_chat_messages  (w:1118)
  ├─ extract_fill_date_intent → fill_missing_date_for_user  (w:1122)
  ├─ parsed = parse_budget_message(body); items_count  (w:1152)
  │
  ├─ if items_count == 0 AND _is_ad_account_context(body):  (w:1162)
  │     → cooldown check → push "gõ chuẩn `tk <Tên> chạy <Số> ngày <ngày>`"
  │     (KHÔNG rẽ expense)
  │
  ├─ if items_count == 0 AND NOT is_budget_intent:  (w:1178)
  │     → thử expense (dup_reply / parse + cooldown mark / follow_up)
  │
  ├─ save_parse_result
  │   ├─ items==0 AND is_budget_intent → ai_reply_parse_fail  (w:1207)
  │   └─ items>0 AND not for_date → ai_reply_ask_date  (w:1211)
  └─ cooldown check → push lan_text về team chat  (w:1223-1232)
```

**Lưu ý TK QC hint khi amount=0** (text flow group team): trong
`handle_expense_text` (`__init__.py:1301-1309`) — nếu Gemini/DeepSeek
parse được `amount=0` NHƯNG body khớp `_is_ad_account_context` → push
"Lan thấy bạn nhắc TK QC. Số tiền chạy bao nhiêu ạ?" (đúng luồng NS),
KHÔNG hỏi mơ hồ "ghi rõ chi VPP".

---

## 2. Detectors

### 2.1 TK QC context — `_is_ad_account_context(text)`
File: `posbottieuhiem/modules/expense_chat/__init__.py:1029-1043`.

- Keywords (norm bỏ dấu, lower) — `_AD_ACCOUNT_PATTERNS`
  (`__init__.py:1005-1015`): "tài khoản quảng cáo", "tk quảng cáo",
  "tài khoản ads", "fb ads", "facebook ads", "meta ads", "google ads",
  "ad account", "ads account", "ads acc", "acc ads", "acc qc", "tk qc".
  *(4 alias cuối thêm ở commit `6c43700f` — gap 5.6.)*
- Regex `_AD_ACCOUNT_REGEX` (`__init__.py:1017-1026`, IGNORECASE):
  - `\b(?:tk|tài\s*khoản|tai\s*khoan)\s*[:\-]?\s*(?:[a-zA-Z]+\s*){1,3}\d`
    → "tk Long15", "tk linh tk16", "tài khoản Long 15"
  - `\b[a-zA-Z]+TH\s*\d` → "LinhTH16", "Linh TH16/4"
  - `\b[a-zA-Z]{3,}\d+\.\d+\b` → "Long15.3", "Hai10.5"
  - **Mới (gap 5.6)**: `\b(?!(?:phòng|phong|version|ver|build|chương|chuong)\b)[a-zA-Z]{2,15}\s+(?:[12]?\d|3[01])[\./](?:1[0-2]|[1-9])\b`
    → bắt "Linh 16.4", "Long 15/3" (tên 2-15 ký tự + space + DD[./]M).
    Negative lookahead loại "phòng/phong/version/ver/build/chương/chuong"
    để không nuốt "phòng 12/4", "version 1.2", "build 16/4".
- Edge cases còn miss (chưa fix):
  - Tên TK kiểu thuần số (vd "TK 884") → MISS — pattern cần ≥1 chữ cái.
  - Tên 1 ký tự + số dấu chấm ("A16.4") → MISS (regex thứ 3 cần ≥3
    chữ, regex thứ 4 cần ≥2 chữ + SPACE).

### 2.2 NS intent — `is_budget_intent(body)`
File: `posbottieuhiem/modules/budget_chat/lan_personality.py:112-121`.

- Keywords (`_BUDGET_KEYWORDS`, line 64-70): "ngân sách", "ngan sach",
  "ng sách", "ns ngày", "ns mai", "ns hôm", "báo ns", "báo ngân",
  "ngày mai", "hôm nay chạy", "mai chạy", "thẻ ", "chạy ".
- `_TK_PATTERN` (line 71): `\btk\s*[:\-]?\s*\w` (case-insensitive).
- Edge cases:
  - "thẻ " và "chạy " match cả tin không phải NS (vd "chạy ra ngoài
    mua").
  - Câu chỉ có tên TK không có "tk" prefix vd "Long15.3 nạp 500k" →
    MISS keyword + miss `_TK_PATTERN`, nhưng `parse_budget_message`
    DeepSeek vẫn có thể bắt được items (path `items_count > 0`
    KHÔNG cần `is_budget_intent`).
- **Quyết định NS vs expense**: ở `web_app.py:1076` —
  `items_count == 0 AND NOT is_budget_intent` → mới rẽ sang expense.
  `items_count > 0` → luôn coi là NS bất kể.

### 2.3 Money signal — `_is_relevant_to_lan(body)`
File: `posbottieuhiem/modules/expense_chat/__init__.py:1066-1097`.

Trả True khi (norm bỏ dấu, lower):
1. Có `\blan\b` (gọi tên Lan).
2. `_is_ad_account_context(body) == True`.
3. `_RE_MONEY_TOKEN` (`__init__.py:1046-1049`): `\d+\s*(k|tr|triệu|trieu|ngàn|ngan|nghìn|nghin|đồng|dong|vnd|vnđ|đ)\b`.
4. `_RE_BIG_NUMBER` (`__init__.py:1054-1058`) — **đã thắt ở commit
   `6c43700f` (gap 5.5)**, không còn match `\d{4,}` trần:
   ```
   \b\d{1,3}(?:[.,]\d{3})+\s*(?:đ|vnd|vnđ|đồng|dong)?\b   # 1.000.000 / 200,000
   |
   \b\d{4,}\s*(?:đ|vnd|vnđ|đồng|dong)\b                   # 5000đ / 200000 vnd
   ```
   → SĐT (`0987654321`), mã đơn (`#12345`), ID đơn POS, mã vận đơn
   KHÔNG còn false-positive.
5. `_MONEY_KEYWORDS`: "tiền", "tien", "chi phí", "thanh toán",
   "chuyển khoản", "lương", "hoá đơn", "vpp", "ngân sách", "phí",
   "mua", "trả" (line 1059-1063).

Dùng trong `handle_expense_text` (line 1300-1303): nếu `amount<=0`
và `_is_relevant_to_lan(body)==False` → **IM LẶNG**
(`reason=irrelevant_silent`). Nếu relevant nhưng không có số → reply
"Lan chưa hiểu được số tiền".

### 2.4 Non-ads sender whitelist — `_is_non_ads_sender(name)`
File: `posbottieuhiem/modules/expense_chat/__init__.py:1099-1124`.

- Config: `app_config.expense_non_ads_senders` (default `"Vợ, Huyền"`).
- Norm bỏ dấu uppercase, substring match.
- Tác dụng: nếu Gemini/DeepSeek phân `category=ads` mà sender thuộc
  whitelist → demote `ads → other` (line 1296, 1530).

### 2.5 Owner transfer force-ads — `_force_ads_if_owner_transfer(note, body)`
File: `posbottieuhiem/modules/expense_chat/__init__.py:1127-1151`.

- Config: `app_config.expense_ads_payers` (mặc định rỗng → no-op).
- Nếu tên trong note/body khớp → ép `category=ads`.
- Áp dụng ở line 1292 (text) và 1525 (image).

### 2.6 Duplicate detection — `_find_duplicate_item`
File: `posbottieuhiem/modules/expense_chat/__init__.py:665-691`.

- Trigger: trong `handle_expense_text:1332` + `handle_expense_image:1544`.
- Điều kiện: cùng `zalo_sender_id`, `status='pending'`,
  `amount BETWEEN amount×0.95 AND amount×1.05`, `created_at` trong
  10 phút gần đây.
- Nếu hit → `_save_dup_pending` (TTL 300s, lưu qua `app_config`,
  key `dup_pending_<uid>`) và push hỏi:
  `"Khoản này là 1 hay 2 khoản khác nhau?"`.
- Reply user → `handle_dup_pending_reply` (line 828) →
  `_classify_dup_intent` (line 765-825):
  - Fast keyword match ("1"/"đúng"/"ok" → same; "2"/"khác"/"không" → different).
  - Fallback DeepSeek với prompt `__init__.py:798-808` (output JSON
    `{label: same|different|unclear}`, temperature 0).

### 2.7 Follow-up — `handle_expense_follow_up`
File: `posbottieuhiem/modules/expense_chat/__init__.py:955-1014`.

- Tin không có số tiền + sender có item pending < 15 phút.
- `_detect_category_from_text` (line 651-662): match từ khoá thuộc
  `_CATEGORY_KEYWORDS` (line 639-648) → cập nhật category.
- Append note nếu body 4-200 ký tự.
- Reply qua `_ai_lan_reply` (budget_chat lan_personality).

---

## 3. Prompts AI

### 3.1 DeepSeek — parse expense text (`_TEXT_PROMPT`)
File: `posbottieuhiem/modules/expense_chat/__init__.py:1155-1199`.
Hàm `_parse_expense_text` (line 1202-1255), model `deepseek-chat`,
temperature 0, JSON object mode.

- Output schema: `{amount_vnd, category, note, occurred_date, confidence}`.
- Quy ước số tiền: 300k=300000, 1tr=1000000, 4tr5=4500000, 1tr2=1200000.
- **Category rules** (đọc ở dòng `__init__.py:1175-1198`):
  - `ads`: chỉ khi có chữ "quảng cáo"/"ads"/"QC"/"FB ads"/"Google ads"
    hoặc tên cty chuyên QC. Mặc định KHÔNG ads.
  - `salary`: chỉ khi có "lương"/"thưởng"/"phụ cấp" hoặc kỳ
    "T1"-"T12"/"tháng N". Cấm suy từ tên người.
  - `office`: "VPP"/"giấy"/"in"/"máy"/"sửa"/"mua đồ"/"văn phòng phẩm".
  - `utility`: "điện"/"nước"/"EVN"/"internet"/"FPT"/"VNPT"/"viettel"/"wifi".
  - `other`: default. "Chuyển khoản"/"thanh toán" cho người không rõ
    mục đích → other.
- Anti-AI-leak: cấm note nhắc Gemini/DeepSeek/GPT/Claude/OpenAI/OCR/API.
- HÔM NAY inject runtime (line 1219) — model nào trả năm cũ thì coerce
  về năm hiện tại nếu DD-MM ≤ today (line 1238-1251).

### 3.2 Gemini Vision — parse expense image (`vprompt`)
File: `posbottieuhiem/modules/expense_chat/__init__.py:1428-1476`.
Hàm `handle_expense_image:1382`, model qua `get_gemini_model()`,
endpoint `v1beta/models/{model}:generateContent`, temperature 0.1.

- Output schema = DeepSeek text **+ field mới (gap 5.2)**:
  `is_ad_account_dashboard: true|false`.
  - `true` KHI ảnh là dashboard / trình quản lý Facebook Ads Manager /
    Meta Business Suite / Google Ads (có cột **Spend / Impressions /
    Reach / CPM / CPC / Cost per result**, `account_id` dạng `act_...`,
    layout chart spend theo ngày).
  - `false` cho SMS bank, hoá đơn giấy, biên lai chuyển khoản thường.
- USD → VND tỷ giá ~25000.
- Category rules y hệt prompt text.
- Cấm note nhắc tên AI/OCR.
- Coerce năm tương tự text flow.
- **Reject về NS flow** (`__init__.py:1506-1524`): nếu
  `is_ad_account_dashboard=true` HOẶC `_is_ad_account_context(caption|note)`
  HOẶC `category=ads` không kèm transfer-signal → push hint NS, KHÔNG
  ghi vào `company_expense_items`.

### 3.3 DeepSeek — classify dup intent
File: `posbottieuhiem/modules/expense_chat/__init__.py:798-808`. JSON
object, temperature 0, max_tokens 30.

### 3.4 DeepSeek — parse budget message (`_SYSTEM_PROMPT_TEMPLATE`)
File: `posbottieuhiem/modules/budget_chat/ai_parser.py:36-64`. Model
`deepseek-v4-flash` (env `DEEPSEEK_MODEL`), temperature 0, JSON object.

- Output `{for_date, items[{tk_name, card_last4, amount_vnd}]}`.
- Quy ước for_date: "ngày mai"→tomorrow, "hôm nay"→today,
  "DD/M"→năm hiện tại.
- Nếu không phải tin báo NS → trả `items=[]`.
- Fallback regex `_regex_fallback_parse` (line 106-141) khi API fail.

### 3.5 Lan personality replies
File: `posbottieuhiem/modules/budget_chat/lan_personality.py:164+`
(`_ai_lan_reply`, `ai_reply_parse_fail`, `ai_reply_ask_date`,
`ai_reply_fill_date`). Dùng DeepSeek sinh câu, fallback `PHRASES_*`
ngẫu nhiên (line 28-59).

---

## 4. Forward & notify

| Trường hợp | Đích | Hàm / điều kiện |
|---|---|---|
| Expense ghi nhận xong (text, group team NS) | `app_config.zalo_expense_inbox` | `_forward_to_inbox(is_private=False)` — `expense_chat/__init__.py:551-554` |
| Expense 1-1 (chat riêng với Lan) | `app_config.zalo_expense_private_inbox` (default `638544760243854625`) | `_forward_to_inbox(is_private=True)` — `__init__.py:520, 551` |
| Item `category=ads` | **SKIP forward** | `__init__.py:563-565` — kế toán xem qua web `/chi-phi/khai-bao` |
| Source thread trùng inbox tid | SKIP (không lặp) | `__init__.py:557` |
| Reply tại nguồn (group hoặc 1-1) | thread gốc | `_push_to_zalo` — text khác nhau group vs `🔒` private (`__init__.py:1368, 1578`) |
| NS đã lưu | team chat (group NS) | `_push_lan_to_zalo(team_code, ...)` — `web_app.py:1049, 1119` |
| Webhook approve/decline khoản qua web | inbox tương ứng (private hay group) | `_notify_admin_decision` — `__init__.py:216-269` |

Header inbox:
- `"💵 Chi phí mới ghi nhận"` (group).
- `"🔒 Chi phí RIÊNG (NV báo qua 1-1)"` (private).
Body: NV (mention @), Tiền, Loại, Nội dung, Ngày, Nguồn 📝 text /
🖼 ảnh + tên group nguồn, link `/chi-phi/khai-bao (#id)`.

Push driver: `_push_to_zalo` (group) / `_push_to_zalo_raw` (raw +
mentions) — bridge HTTP `POST <ZALO_BRIDGE_OUTBOUND_URL>/send` (default
`http://127.0.0.1:5051/send`) với 3 retry, backoff 1.5×attempt.

---

## 5. Gaps & TODO

> 9/10 đã fix ở commit `6c43700f` (2026-05-18). Mô tả gốc giữ nguyên
> để tham chiếu lịch sử + reproduce nếu cần. Chỉ 5.9 là quyết định
> nghiệp vụ — không phải bug.

### 5.1 [RESOLVED 2026-05-18 commit 6c43700f] TK QC text không có số tiền
**Fix**: `handle_expense_text` (`__init__.py:1301-1309`) khi
`amount<=0` AND `_is_ad_account_context(body)` → push "Lan thấy bạn
nhắc TK QC. Số tiền chạy bao nhiêu ạ?" + gợi ý format `tk LinhTH16.4
chạy 500k ngày mai`, return `reason=ad_account_no_amount`. Áp dụng
cho mọi flow (group team, non-team, 1-1).

- Flow đúng: tin "tk LinhTH16.4" trong group NS (có team_code) →
  `parse_budget_message` items=0 → nhánh `_is_ad_account_context`
  (`web_app.py:1065`) ask format → OK.
- Flow group KHÔNG map team NS (vd inbox CP) `web_app.py:916-938`:
  KHÔNG check `_is_ad_account_context`, chỉ thử expense parse rồi im
  lặng. Nếu NV vô tình gõ "tk LinhTH16.4 chạy 500k ngày mai" vào
  inbox CP → Lan im, không hint sang group NS.
- Flow 1-1 (`web_app.py:882-908`): tương tự, không có guard TK QC.
  Tin "tk Long15 chạy 1tr" gửi riêng cho Lan → push "Lan chưa thấy
  số tiền" (DeepSeek trả amount=0 vì prompt ép "ads → mặc định
  KHÔNG dùng ads") rồi dừng. Đáng lẽ phải rẽ NS hoặc hint format.

### 5.2 [RESOLVED 2026-05-18 commit 6c43700f] Ảnh dashboard QC không caption
**Fix**: Gemini `vprompt` thêm field `is_ad_account_dashboard`
(`__init__.py:1441-1449`) + heuristic Spend/Impressions/Reach/CPM/CPC/
`act_…`. Trong `handle_expense_image:1506-1524`, ảnh có
`is_ad_account_dashboard=true` (hoặc category=ads không có transfer
signal) → reject về NS flow, push hint format `tk ... chạy ... ngày
...`, KHÔNG INSERT `company_expense_items`.

- `handle_expense_image` gọi Gemini, prompt ép `ads` cần "bằng chứng
  trực tiếp" → nếu ảnh chỉ là dashboard FB Ads Manager không có chữ
  "quảng cáo"/"ads" trực tiếp (vd chỉ thấy account_id, spend) →
  Gemini có thể phân `other` và ghi nhận như chi phí thường.
- `_is_ad_account_context(caption)` + `_is_ad_account_context(note)`
  (`__init__.py:1489`) là guard duy nhất. Caption rỗng + note Gemini
  không nhắc "tài khoản quảng cáo" → bỏ lọt.
- Đề xuất: prompt Gemini thêm `is_ads_dashboard: bool` riêng + heuristic
  từ Gemini về layout (cột Spend/Impressions/Reach...).

### 5.3 [RESOLVED 2026-05-18 commit 6c43700f] NS gửi 1-1 (private) — không xử lý
**Fix**: Nhánh `is_private` (`web_app.py:935-952`) detect NS TRƯỚC mọi
bước expense bằng `parse_budget_message(body)` + `is_budget_intent`.
Nếu match → push hint "🌸 Lan thấy bạn báo NS qua chat riêng. NS phải
báo trong NHÓM TEAM tương ứng..." + cooldown mark + return
`skipped=ns_via_1to1`. NS **KHÔNG** persist (không có team_code), chỉ
hướng dẫn NV copy sang group team.

- `web_app.py:882-908`: private flow CHỈ chạy expense pipeline. Tin NS
  gửi 1-1 cho Lan (vd PM giao chỉ định NS ngoài giờ) → bị parse
  expense, KHÔNG insert vào `team_budget_*`.
- Caption ảnh trong private: branch ưu tiên budget chỉ chạy khi
  `not is_private` (`web_app.py:847`). Ảnh có caption NS gửi 1-1 sẽ
  bị OCR Gemini như chi phí.

### 5.4 [RESOLVED 2026-05-18 commit 6c43700f] Cooldown / rate-limit
**Fix**: Thêm `_lan_cooldown_check` + `_lan_cooldown_mark`
(`web_app.py:790-827`) — window mặc định 3.0s/sender per thread (env
`ZALO_LAN_REPLY_COOLDOWN_SEC`). Map in-memory mỗi worker, sweep
entries >60s khi >500 keys. Mọi nhánh push reply (NS hint, TK QC hint,
1-1 ask-amount, expense reply, lan_text NS) đều gọi cooldown check
trước, mark sau push. **Vẫn save data** khi cooldown — chỉ skip
reply, không skip persist.

- Dedup cấp tin: `zalo_msg_id` UNIQUE check (`web_app.py:824-839`,
  `__init__.py:1328, 1540`).
- Dup item: `_find_duplicate_item` 10 phút ±5% amount cùng sender.
- **KHÔNG có rate-limit per-sender / per-thread**. Nếu bridge replay
  lỗi mà thiếu `zalo_msg_id` → có thể spam ghi nhận hàng loạt.
- `_push_to_zalo` có 3 retry 1.5s/4.5s/6s nhưng không exponential cap
  → nếu bridge outbound 5051 down lâu, request webhook block tới
  ~12s × số call.

### 5.5 [RESOLVED 2026-05-18 commit 6c43700f] `_is_relevant_to_lan` false positive
**Fix**: `_RE_BIG_NUMBER` (`__init__.py:1054-1058`) thắt — chỉ match
nhóm hàng nghìn (`1.000.000` / `200,000`) hoặc số ≥4 chữ kèm hậu tố
tiền (`5000đ` / `200000 vnd`). Bỏ rule cũ `\b\d{4,}\b` trần. SĐT, mã
đơn, ID đơn POS, mã vận đơn không còn trigger.

Còn lại: `_MONEY_KEYWORDS` "mua"/"trả" vẫn match chat chung chung
("mua giúp em", "trả lời nha") — chấp nhận trade-off vì những từ này
đa số là context tiền.
- `_RE_BIG_NUMBER \b\d{4,}\b` match SĐT (0987654321), mã đơn (#12345),
  ID đơn POS, mã vận đơn → Lan sẽ trả lời "chưa hiểu số tiền" trong
  group NS gây nhiễu khi NV bàn nghiệp vụ.
- `_MONEY_KEYWORDS` chứa "mua"/"trả" — match tin chat chung chung
  ("trả lời nha", "mua giúp em").

### 5.6 [RESOLVED 2026-05-18 commit 6c43700f] `_is_ad_account_context` miss case
**Fix**:
- `_AD_ACCOUNT_PATTERNS` (`__init__.py:1005-1015`) thêm "ads acc",
  "acc ads", "acc qc", "tk qc".
- `_AD_ACCOUNT_REGEX` (`__init__.py:1017-1026`) thêm pattern 4 bắt
  "Linh 16.4" (tên 2-15 ký tự + space + DD[./]M) với negative
  lookahead loại "phòng/phong/version/ver/build/chương/chuong" để
  không nuốt false-positive.
- Pattern 1 mở rộng `(?:[a-zA-Z]+\s*){1,3}\d` → bắt "tk linh tk16",
  "tài khoản Long 15".

Còn lại (chưa fix): "tk #884" thuần số, "A16.4" tên 1 ký tự — accept
trade-off, rare in practice.
- "Linh 16.4 chạy 500k" (có space giữa tên và số) → `[a-zA-Z]+\d`
  miss vì có space.
- "tk #884" (account ID thuần số) → miss.
- Tên TK 1 ký tự + số ("A16.4") → regex `[a-zA-Z]{3,}\d+\.\d+` cần
  ≥3 chữ → miss.

### 5.7 [RESOLVED 2026-05-18 commit 6c43700f] Dup pending state ở `app_config`
**Fix**: Thêm `scripts/sweep_dup_pending.py` (script mới) — quét
`app_config` xoá key `dup_pending_<uid>` có `ts` cũ hơn 1 giờ. Cron
qua `scheduler.py` job mới, chạy mỗi 30 phút. State stale không còn
tích tụ khi user không reply.

- `_save_dup_pending` ghi key `dup_pending_<uid>` vào `app_config`
  (`__init__.py:725-731`). Mỗi tin có dup → 1 row config + commit.
- Có thể tích tụ nếu user không reply (TTL 300s nhưng row chỉ xoá
  khi `_load_dup_pending` cleanup hoặc `_clear_dup_pending` chạy
  qua `handle_dup_pending_reply`). Đề xuất sweep cron.

### 5.8 [RESOLVED 2026-05-18 commit 6c43700f] Webhook block trên flow `1-1` luôn reply
**Fix**: Nhánh 1-1 (`web_app.py:962-997`) áp `_is_relevant_to_lan(body)`
TRƯỚC khi parse expense. Tin không liên quan tiền/Lan/TK → IM LẶNG
return `skipped=1to1_irrelevant_silent`. Bypass relevance gate khi
sender đang có `dup_pending` (user trả lời "đúng/khác" rất ngắn, không
chứa keyword tiền). Nhánh ask-amount cuối cùng cũng có cooldown
check.

- `web_app.py:901-905`: tin 1-1 mọi tin không có số đều push reply
  "Lan chưa thấy số tiền". Nếu user chat dài dòng → spam mỗi câu 1
  reply. Group flow có `_is_relevant_to_lan` filter, private flow
  KHÔNG có.

### 5.9 Order check trong image branch
- `web_app.py:847-870`: caption-NS branch ưu tiên trước caption-
  expense, nhưng cả 2 cùng đọc `caption`. Nếu caption là
  "tk Long15 nạp 500k cho đo đạc 600k" → `parse_budget_message` có
  thể bắt 500k, `_parse_expense_text` cũng có thể bắt 600k. Trình tự
  hiện tại ưu tiên NS → xếp ảnh sang flow budget, bỏ qua 600k expense.
  Là quyết định nghiệp vụ đúng (theo commit `c3041374`) nhưng dev
  cần biết khi audit số.

### 5.10 [RESOLVED 2026-05-18 commit 6c43700f] Thread expense whitelist `is_expense_thread`
**Fix**: Xoá hàm `is_expense_thread` (dead code, không có call site).
Decision tree giữ nguyên cấu trúc "team_code present vs absent". Key
`zalo_expense_thread_<tid>` không còn dùng — có thể clean ở
`app_config` nếu cần (không bắt buộc, để mặc kệ).

---

## 6. Reference quick lookup

| Concept | File:line |
|---|---|
| Webhook entry | `web_app.py:830` |
| Cooldown helpers | `web_app.py:790-827` |
| Image dispatch | `web_app.py:885` |
| Private dispatch (NS-first + relevance gate) | `web_app.py:925` |
| Non-team thread dispatch | `web_app.py:1008` |
| NS pipeline | `web_app.py:1093-1234` |
| TK QC reroute (in NS pipe) | `web_app.py:1162` |
| `_is_ad_account_context` | `modules/expense_chat/__init__.py:1029` |
| `_is_relevant_to_lan` | `modules/expense_chat/__init__.py:1066` |
| `_is_non_ads_sender` | `modules/expense_chat/__init__.py:1100` |
| `_force_ads_if_owner_transfer` | `modules/expense_chat/__init__.py:1128` |
| `_parse_expense_text` (DeepSeek) | `modules/expense_chat/__init__.py:1202` |
| `_TEXT_PROMPT` | `modules/expense_chat/__init__.py:1155` |
| `handle_expense_text` (incl. TK QC amount=0 hint) | `modules/expense_chat/__init__.py:1272` |
| `handle_expense_image` (Gemini + dashboard reject) | `modules/expense_chat/__init__.py:1382` |
| `vprompt` (vision, with `is_ad_account_dashboard`) | `modules/expense_chat/__init__.py:1428` |
| `sweep_dup_pending.py` (cron 30m) | `posbottieuhiem/scripts/sweep_dup_pending.py` |
| `_find_duplicate_item` | `modules/expense_chat/__init__.py:665` |
| `_classify_dup_intent` | `modules/expense_chat/__init__.py:765` |
| `handle_dup_pending_reply` | `modules/expense_chat/__init__.py:828` |
| `handle_expense_follow_up` | `modules/expense_chat/__init__.py:955` |
| `_forward_to_inbox` | `modules/expense_chat/__init__.py:532` |
| `_get_inbox_thread_id` | `modules/expense_chat/__init__.py:498` |
| `_get_private_inbox_thread_id` | `modules/expense_chat/__init__.py:514` |
| `parse_budget_message` | `modules/budget_chat/ai_parser.py:144` |
| `_SYSTEM_PROMPT_TEMPLATE` | `modules/budget_chat/ai_parser.py:36` |
| `_regex_fallback_parse` | `modules/budget_chat/ai_parser.py:106` |
| `is_budget_intent` | `modules/budget_chat/lan_personality.py:112` |
| `extract_fill_date_intent` | `modules/budget_chat/lan_personality.py:79` |
| `_BUDGET_KEYWORDS` / `_TK_PATTERN` | `modules/budget_chat/lan_personality.py:64-71` |
