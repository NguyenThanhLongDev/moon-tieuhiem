-- Migration 063: Văn phòng MẶC ĐỊNH theo team (chấm công)
-- Mục đích: nhớ "team X dùng văn phòng Y" thành mapping cứng, để NV mới / NV
-- đổi team tự nhận đúng văn phòng chấm công (không phải suy từ teammate + restart).

CREATE TABLE IF NOT EXISTS cc_team_office (
    team_code  VARCHAR(64) PRIMARY KEY,
    office_id  INTEGER REFERENCES cc_offices(id) ON DELETE CASCADE,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Backfill: lấy "văn phòng chính" hiện tại của mỗi team (office mà nhiều NV cùng
-- team đang dùng nhất; loại WFH bán kính lớn) — đồng bộ với logic seed cũ.
INSERT INTO cc_team_office (team_code, office_id)
SELECT t.team_code, x.office_id
FROM teams t
JOIN LATERAL (
    SELECT e2.office_id
    FROM cc_employees e2
    JOIN users u2 ON u2.id::text = e2.user_id
    JOIN cc_offices o2 ON o2.id = e2.office_id
    WHERE u2.team_id = t.id
      AND COALESCE(o2.radius_m, 0) < 1000000
    GROUP BY e2.office_id
    ORDER BY COUNT(*) DESC
    LIMIT 1
) x ON TRUE
WHERE t.team_code IS NOT NULL
ON CONFLICT (team_code) DO NOTHING;
