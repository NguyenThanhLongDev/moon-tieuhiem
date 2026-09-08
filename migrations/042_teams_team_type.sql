-- 042_teams_team_type.sql — Phân loại team kinh doanh / sale / khác
--
-- Mục đích: budget_chat chỉ áp cho team kinh doanh. Team sale (Trọng Nam) +
-- team khác (kho, ...) không phải báo ngân sách FB Ads.

ALTER TABLE teams ADD COLUMN IF NOT EXISTS team_type VARCHAR(20) NOT NULL DEFAULT 'kinh_doanh';

-- Constraint: chỉ chấp nhận 3 giá trị
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.constraint_column_usage
         WHERE table_name='teams' AND constraint_name='teams_team_type_chk'
    ) THEN
        ALTER TABLE teams ADD CONSTRAINT teams_team_type_chk
            CHECK (team_type IN ('kinh_doanh', 'sale', 'khac'));
    END IF;
END $$;

-- Backfill: team-trong-nam = sale, team-kho = khac, các team khác = kinh_doanh (default)
UPDATE teams SET team_type = 'sale' WHERE team_code = 'team-trong-nam';
UPDATE teams SET team_type = 'khac' WHERE team_code IN ('team-kho');

CREATE INDEX IF NOT EXISTS idx_teams_team_type ON teams (team_type);
