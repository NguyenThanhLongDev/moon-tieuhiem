-- 075: Bổ sung đủ giá trị enum user_role cho khớp tieuhiem (thiếu → save_users rollback cả mẻ)
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'manager';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'accountant';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'kho';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'it';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'it_staff';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'kho_leader';
