-- 072: LadiPage đổi vai trò — KHÔNG đẩy đơn lên POS nữa, chuyển sang ĐỐI SOÁT.
-- Sếp Phong 12/08: chỉ 1 shop POS nên phân biệt đơn/SP rất khó; thay vì đẩy,
-- phần mềm kéo đơn Ladi về rồi kiểm tra đơn đó ĐÃ có trên POS chưa —
-- đơn nào chưa có thì báo sale kiểm tra lại (bắt sót đơn / bán ra ngoài).
ALTER TABLE ladipage_inbound_orders
    ADD COLUMN IF NOT EXISTS match_status   TEXT DEFAULT 'cho_kiem_tra',  -- cho_kiem_tra|co_pos|chua_co_pos|bo_qua
    ADD COLUMN IF NOT EXISTS matched_order_code TEXT,
    ADD COLUMN IF NOT EXISTS matched_order_id   BIGINT,
    ADD COLUMN IF NOT EXISTS matched_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS checked_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS phone_norm     TEXT;

-- Chuẩn hoá SĐT 1 lần cho dữ liệu cũ: bỏ ký tự không phải số, +84 → 0
UPDATE ladipage_inbound_orders
   SET phone_norm = CASE
        WHEN regexp_replace(COALESCE(so_dien_thoai,''), '[^0-9]', '', 'g') LIKE '84%'
         AND length(regexp_replace(COALESCE(so_dien_thoai,''), '[^0-9]', '', 'g')) >= 11
        THEN '0' || substring(regexp_replace(COALESCE(so_dien_thoai,''), '[^0-9]', '', 'g') from 3)
        ELSE regexp_replace(COALESCE(so_dien_thoai,''), '[^0-9]', '', 'g')
       END
 WHERE phone_norm IS NULL;

-- Đơn đã đẩy POS thành công trước đây = đã có trên POS, khỏi báo động giả
UPDATE ladipage_inbound_orders
   SET match_status='co_pos', matched_order_code=pos_order_id, matched_at=pushed_at
 WHERE pos_status='ok' AND COALESCE(match_status,'') <> 'co_pos';

CREATE INDEX IF NOT EXISTS ix_ladi_match  ON ladipage_inbound_orders (match_status, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_ladi_phone  ON ladipage_inbound_orders (phone_norm);
CREATE INDEX IF NOT EXISTS ix_orders_phone_time ON orders (customer_phone, created_at_pos DESC);
