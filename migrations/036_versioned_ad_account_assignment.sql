-- 036_versioned_ad_account_assignment.sql
-- Versioned NV↔TK QC assignment + history cho shop↔TK mapping.
-- Mục đích: 1 TK QC do 1 NV phụ trách trong 1 khoảng thời gian. Khi đổi NV,
-- close khoảng cũ + mở khoảng mới. Báo cáo lùi quá khứ vẫn truy được đúng NV
-- đã phụ trách trong từng ngày.

-- ─────────────────────────────────────────────────────────────────────
-- (1) Bảng mới: NV ↔ TK QC, versioned
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_ad_account_assignments (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ad_account_id   VARCHAR(64) NOT NULL,
    ad_account_name VARCHAR(255),
    assigned_from   DATE        NOT NULL,
    assigned_to     DATE,                                                       -- NULL = đang phụ trách
    assigned_by     BIGINT      REFERENCES users(id) ON DELETE SET NULL,
    reason          TEXT,
    created_at      TIMESTAMP   NOT NULL DEFAULT now(),
    CONSTRAINT chk_uaa_window CHECK (assigned_to IS NULL OR assigned_to >= assigned_from)
);

CREATE INDEX IF NOT EXISTS idx_uaa_account ON user_ad_account_assignments (ad_account_id);
CREATE INDEX IF NOT EXISTS idx_uaa_user    ON user_ad_account_assignments (user_id);
CREATE INDEX IF NOT EXISTS idx_uaa_window  ON user_ad_account_assignments (ad_account_id, assigned_from, assigned_to);
-- Mỗi TK chỉ 1 NV active cùng lúc
CREATE UNIQUE INDEX IF NOT EXISTS uq_uaa_active ON user_ad_account_assignments (ad_account_id) WHERE assigned_to IS NULL;

-- ─────────────────────────────────────────────────────────────────────
-- (2) Versioning cho fb_ad_account_mappings (shop ↔ TK)
-- ─────────────────────────────────────────────────────────────────────
ALTER TABLE fb_ad_account_mappings
    ADD COLUMN IF NOT EXISTS assigned_from        DATE,
    ADD COLUMN IF NOT EXISTS assigned_to          DATE,
    ADD COLUMN IF NOT EXISTS derived_from_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL;

-- Drop UNIQUE cũ (shop_id, fb_ad_account_id), replace bằng partial unique cho rows đang active
ALTER TABLE fb_ad_account_mappings DROP CONSTRAINT IF EXISTS uq_fb_ad_account_mappings_shop_account;
CREATE UNIQUE INDEX IF NOT EXISTS uq_fbam_active ON fb_ad_account_mappings (shop_id, fb_ad_account_id) WHERE assigned_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_fbam_window ON fb_ad_account_mappings (fb_ad_account_id, assigned_from, assigned_to);

-- ─────────────────────────────────────────────────────────────────────
-- (3) Backfill assigned_from cho rows hiện tại
-- ─────────────────────────────────────────────────────────────────────
UPDATE fb_ad_account_mappings
   SET assigned_from = DATE '2026-01-01'
 WHERE assigned_from IS NULL;

ALTER TABLE fb_ad_account_mappings ALTER COLUMN assigned_from SET NOT NULL;
ALTER TABLE fb_ad_account_mappings ALTER COLUMN assigned_from SET DEFAULT CURRENT_DATE;

-- ─────────────────────────────────────────────────────────────────────
-- (4) Backfill user_ad_account_assignments từ user_ad_account_map
-- ─────────────────────────────────────────────────────────────────────
INSERT INTO user_ad_account_assignments
    (user_id, ad_account_id, ad_account_name, assigned_from, assigned_to, reason, created_at)
SELECT
    uam.user_id::bigint,
    uam.ad_account_id,
    uam.ad_account_name,
    DATE '2026-01-01',
    NULL::date,
    'Backfill từ user_ad_account_map (migration 036)',
    uam.created_at::timestamp
FROM user_ad_account_map uam
WHERE uam.user_id ~ '^\d+$'
  AND EXISTS (SELECT 1 FROM users u WHERE u.id = uam.user_id::bigint)
  AND NOT EXISTS (
        SELECT 1 FROM user_ad_account_assignments a
         WHERE a.ad_account_id = uam.ad_account_id AND a.assigned_to IS NULL
  );

-- ─────────────────────────────────────────────────────────────────────
-- (5) Backfill derived_from_user_id cho fb_ad_account_mappings hiện active
-- ─────────────────────────────────────────────────────────────────────
UPDATE fb_ad_account_mappings m
   SET derived_from_user_id = a.user_id
  FROM user_ad_account_assignments a
 WHERE a.ad_account_id  = m.fb_ad_account_id
   AND a.assigned_to    IS NULL
   AND m.assigned_to    IS NULL
   AND m.derived_from_user_id IS NULL;
