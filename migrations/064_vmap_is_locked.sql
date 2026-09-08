-- 064: Thêm cờ is_locked cho wh_variation_map.
-- Phản ánh nút bật/tắt sản phẩm trên Pancake POS (variation.is_locked qua products API).
-- Khi shop TẮT 1 SP → mọi biến thể của SP đó ở shop = is_locked TRUE → phần mềm GỠ phần
-- shop đó (badge + tồn POS), giữ SP cho shop còn bán. Chỉ ẩn hẳn product khi MỌI shop locked.
-- Bật lại trên Pancake → vòng sync kế đặt is_locked=FALSE → tự khôi phục.
ALTER TABLE wh_variation_map ADD COLUMN IF NOT EXISTS is_locked BOOLEAN NOT NULL DEFAULT FALSE;
