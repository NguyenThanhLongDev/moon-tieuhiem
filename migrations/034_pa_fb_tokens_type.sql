-- Thêm cột token_type vào pa_fb_tokens
-- Giá trị: 'user' (token người dùng, ~60 ngày) hoặc 'system_user' (không hết hạn)
ALTER TABLE pa_fb_tokens
    ADD COLUMN IF NOT EXISTS token_type TEXT NOT NULL DEFAULT 'user'
        CHECK (token_type IN ('user', 'system_user'));
