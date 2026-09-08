-- Module Chấm công & Phân công việc (prefix: cc_)
-- Migration: 030

-- Ca làm việc
CREATE TABLE IF NOT EXISTS cc_shifts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    color VARCHAR(20) DEFAULT '#6366f1'
);

INSERT INTO cc_shifts (name, start_time, end_time, color) VALUES
  ('Sáng', '07:00', '12:00', '#10b981'),
  ('Chiều', '13:00', '18:00', '#f59e0b'),
  ('Cả ngày', '07:00', '18:00', '#6366f1'),
  ('Tối', '18:00', '22:00', '#8b5cf6')
ON CONFLICT DO NOTHING;

-- Nhân viên (mở rộng từ users.json)
CREATE TABLE IF NOT EXISTS cc_employees (
    user_id VARCHAR(50) PRIMARY KEY,
    full_name VARCHAR(200),
    cc_role VARCHAR(50) NOT NULL DEFAULT 'sale',
    department VARCHAR(100),
    phone VARCHAR(20),
    position VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Chấm công
CREATE TABLE IF NOT EXISTS cc_attendance (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(50) NOT NULL,
    date DATE NOT NULL,
    check_in TIMESTAMP,
    check_out TIMESTAMP,
    shift_id INTEGER REFERENCES cc_shifts(id),
    status VARCHAR(20) DEFAULT 'present',
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_cc_attendance_date ON cc_attendance(date);
CREATE INDEX IF NOT EXISTS idx_cc_attendance_user ON cc_attendance(user_id);

-- Phân công việc
CREATE TABLE IF NOT EXISTS cc_tasks (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    task_type VARCHAR(30) DEFAULT 'one-time',
    role_type VARCHAR(50),
    assigned_to VARCHAR(50),
    assigned_by VARCHAR(50),
    priority VARCHAR(20) DEFAULT 'normal',
    status VARCHAR(20) DEFAULT 'todo',
    due_date DATE,
    completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cc_tasks_assigned ON cc_tasks(assigned_to);
CREATE INDEX IF NOT EXISTS idx_cc_tasks_status ON cc_tasks(status);

-- Bình luận task
CREATE TABLE IF NOT EXISTS cc_task_comments (
    id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES cc_tasks(id) ON DELETE CASCADE,
    user_id VARCHAR(50) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- KPI hàng ngày
CREATE TABLE IF NOT EXISTS cc_kpi_daily (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(50) NOT NULL,
    date DATE NOT NULL,
    cc_role VARCHAR(50),
    ads_budget NUMERIC(15,0) DEFAULT 0,
    ads_orders INTEGER DEFAULT 0,
    ads_revenue NUMERIC(15,0) DEFAULT 0,
    ads_roas NUMERIC(5,2) DEFAULT 0,
    ads_new_orders INTEGER DEFAULT 0,
    sale_contacts INTEGER DEFAULT 0,
    sale_closed INTEGER DEFAULT 0,
    sale_revenue NUMERIC(15,0) DEFAULT 0,
    sale_returned INTEGER DEFAULT 0,
    pack_orders INTEGER DEFAULT 0,
    pack_returned INTEGER DEFAULT 0,
    pack_errors INTEGER DEFAULT 0,
    purch_requests INTEGER DEFAULT 0,
    purch_completed INTEGER DEFAULT 0,
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, date)
);

-- Thông báo nội bộ
CREATE TABLE IF NOT EXISTS cc_announcements (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    content TEXT,
    author_id VARCHAR(50),
    target_role VARCHAR(50),
    is_pinned BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
