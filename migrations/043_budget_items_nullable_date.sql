-- 043: cho phép budget_items.for_date NULL — khi Lan parse được TK+tiền
-- nhưng NV chưa ghi ngày, item vẫn được lưu để NV bổ sung ngày sau.
-- Summary queries dùng `WHERE for_date = %s` → items NULL tự loại
-- khỏi tổng hợp đến khi NV sửa ngày.

ALTER TABLE budget_items ALTER COLUMN for_date DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bi_missing_date
    ON budget_items (team_id, created_at)
    WHERE for_date IS NULL AND deleted_at IS NULL;
