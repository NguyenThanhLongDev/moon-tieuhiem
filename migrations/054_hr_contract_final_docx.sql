-- 054: Cột lưu path file DOCX "final" có chèn PNG chữ ký NV (re-generated).
ALTER TABLE hr_contracts
  ADD COLUMN IF NOT EXISTS final_docx_path TEXT;
