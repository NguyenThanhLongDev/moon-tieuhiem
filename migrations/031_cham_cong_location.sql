-- Migration 031: GPS check-in cho Marketing + bảng cài đặt chấm công

-- Thêm cột GPS vào cc_attendance
ALTER TABLE cc_attendance ADD COLUMN IF NOT EXISTS check_in_lat  NUMERIC(10,7);
ALTER TABLE cc_attendance ADD COLUMN IF NOT EXISTS check_in_lng  NUMERIC(10,7);
ALTER TABLE cc_attendance ADD COLUMN IF NOT EXISTS check_in_distance_m INTEGER;

-- Bảng cấu hình module chấm công
CREATE TABLE IF NOT EXISTS cc_settings (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Giá trị mặc định (admin cập nhật lat/lng thực tế của công ty)
INSERT INTO cc_settings (key, value) VALUES
  ('office_lat',     '10.7769'),
  ('office_lng',     '106.7009'),
  ('office_radius_m','300'),
  ('work_start',     '08:00'),
  ('work_end',       '17:30'),
  ('min_hours_cong', '9')
ON CONFLICT DO NOTHING;
