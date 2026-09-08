-- 057: Vùng lương HCM/ĐN cho team + hồ sơ HR (sếp chốt 11/06/2026)
-- ĐN: lương cứng 4tr, phụ cấp 700K sau 3 tháng. HCM: 5,2tr, phụ cấp 800K sau 3 tháng.
-- Thử việc 1 tháng đầu: 85% lương cứng.
-- Mapping theo team (sếp chỉ định): Nam/Thành/Công Minh = ĐN · Nhật/Minh/Ken = HCM.

ALTER TABLE teams ADD COLUMN IF NOT EXISTS region TEXT
    CHECK (region IN ('HCM', 'DN'));
ALTER TABLE hr_profiles ADD COLUMN IF NOT EXISTS region TEXT
    CHECK (region IN ('HCM', 'DN'));

UPDATE teams SET region = 'DN'
WHERE team_code IN ('team-nam', 'team-thanh', 'team-congminh') AND region IS NULL;
UPDATE teams SET region = 'HCM'
WHERE team_code IN ('team-nhat', 'team-minh', 'team-ken') AND region IS NULL;

-- Backfill hồ sơ HR theo team của NV (chỉ chỗ chưa set tay)
UPDATE hr_profiles p
SET region = t.region
FROM users u
JOIN teams t ON t.id = u.team_id
WHERE u.id = p.user_id AND p.region IS NULL AND t.region IS NOT NULL;
