-- 056: Leader Brain LB-2 — Nhật ký điều phối (thẻ việc cảnh báo + hành động leader)
-- Mỗi cảnh báo máy phát = 1 thẻ; leader bấm Đã xử lý / Giữ lại / Chuyển NV → log lại.
-- Đây là bằng chứng "leader có làm việc" (chỉ báo kỷ luật 2.1) + data dạy GĐ4 Agent.

CREATE TABLE IF NOT EXISTS lb_alert_log (
    id            BIGSERIAL PRIMARY KEY,
    alert_date    DATE NOT NULL,
    alert_type    VARCHAR(20) NOT NULL,            -- dot_0don | hoan_cao
    ad_id         VARCHAR(64)  NOT NULL DEFAULT '',
    campaign_name VARCHAR(512) NOT NULL DEFAULT '',
    page_name     VARCHAR(255) NOT NULL DEFAULT '',
    spend         NUMERIC(18,2) NOT NULL DEFAULT 0, -- VND chưa VAT trong cửa sổ cảnh báo
    detail        TEXT NOT NULL DEFAULT '',          -- mô tả người đọc: "đốt 1,16tr/2 ngày · 0 đơn"
    team_id       INTEGER,                           -- soft key teams.id (snapshot lúc phát)
    nv_user_id    INTEGER,                           -- soft key users.id
    nv_name       VARCHAR(255) NOT NULL DEFAULT '',
    status        VARCHAR(20) NOT NULL DEFAULT 'open', -- open | resolved | kept | transferred
    acted_by      INTEGER,
    acted_by_name VARCHAR(255) NOT NULL DEFAULT '',
    acted_at      TIMESTAMPTZ,
    note          TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT lb_alert_uniq UNIQUE (alert_date, alert_type, ad_id)
);

CREATE INDEX IF NOT EXISTS idx_lb_alert_team_status ON lb_alert_log (team_id, status);
CREATE INDEX IF NOT EXISTS idx_lb_alert_nv ON lb_alert_log (nv_user_id, status);
CREATE INDEX IF NOT EXISTS idx_lb_alert_date ON lb_alert_log (alert_date);
