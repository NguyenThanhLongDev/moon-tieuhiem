-- Thêm 2 role mới: sale (nhân viên chốt đơn) và sale_leader (trưởng nhóm sale)
-- Hai role này không cần gán shop — xử lý đơn hàng marketing toàn hệ thống.
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'sale';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'sale_leader';
