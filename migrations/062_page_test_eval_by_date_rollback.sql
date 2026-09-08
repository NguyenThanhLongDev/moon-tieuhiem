-- Rollback 062: quay về PK = page_id (giữ đánh giá ngày mới nhất mỗi page).
DELETE FROM page_test_eval a
 USING page_test_eval b
 WHERE a.page_id = b.page_id AND a.entry_date < b.entry_date;
ALTER TABLE page_test_eval DROP CONSTRAINT IF EXISTS page_test_eval_pkey;
ALTER TABLE page_test_eval ADD PRIMARY KEY (page_id);
ALTER TABLE page_test_eval DROP COLUMN IF EXISTS entry_date;
