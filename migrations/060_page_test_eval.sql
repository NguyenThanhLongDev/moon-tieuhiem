-- Đánh giá Đạt / Không đạt cho từng page test (chỉ là nhãn — leader tự đánh).
-- Lưu THEO PAGE (1 page 1 đánh giá, dùng chung mọi ngày).
CREATE TABLE IF NOT EXISTS page_test_eval (
    page_id    VARCHAR(64) PRIMARY KEY,
    danh_gia   VARCHAR(12),            -- 'dat' | 'khong_dat' | NULL (chưa đánh)
    updated_by VARCHAR(64),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
