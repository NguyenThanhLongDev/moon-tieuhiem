-- 073: vá SĐT khách cho bảng orders (Pancake trả số trong shipping_address,
-- không phải customer_phone) — cần cho đối soát đơn LadiPage ↔ POS.
UPDATE orders
   SET customer_phone = COALESCE(
        NULLIF(raw_payload_json->'shipping_address'->>'phone_number',''),
        NULLIF(raw_payload_json->>'bill_phone_number',''))
 WHERE COALESCE(customer_phone,'') = ''
   AND raw_payload_json IS NOT NULL;

-- Index theo SĐT đã chuẩn hoá (bỏ ký tự lạ, 84xx → 0xx) để dò nhanh
CREATE INDEX IF NOT EXISTS ix_orders_phone_norm ON orders (
  (CASE WHEN regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') LIKE '84%'
         AND length(regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g')) >= 11
        THEN '0' || substring(regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') from 3)
        ELSE regexp_replace(COALESCE(customer_phone,''),'[^0-9]','','g') END)
);
