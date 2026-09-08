"""HR Contract — Generator file DOCX hợp đồng lao động.

Format MATCH với mẫu kế toán đã duyệt (xem PDF "HĐ LĐ ĐÀO THỊ NỤ").

Public API:
    generate_contract_docx(contract: dict, company: dict, employee: dict, out_path: Path) -> Path
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Cm, Pt


CONTRACT_TYPE_LABEL = {
    "xac_dinh": "XÁC ĐỊNH THỜI HẠN",
    "khong_xac_dinh": "KHÔNG XÁC ĐỊNH THỜI HẠN",
    "thu_viec": "THỬ VIỆC",
    "thoi_vu": "THỜI VỤ",
}

CONTRACT_TYPE_LINE1 = {
    "xac_dinh": "Có xác định thời hạn.",
    "khong_xac_dinh": "Không xác định thời hạn.",
    "thu_viec": "Thử việc.",
    "thoi_vu": "Thời vụ.",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_date_long(d) -> str:
    """30/07/1975 → '30 tháng 07 năm 1975'."""
    if not d:
        return "…… tháng …… năm ……"
    if isinstance(d, str):
        try:
            d = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return d
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%d tháng %m năm %Y")


def _fmt_date_short(d) -> str:
    """30/07/1975 → '30/07/1975'."""
    if not d:
        return "……/……/………"
    if isinstance(d, str):
        try:
            d = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return d
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%d/%m/%Y")


def _fmt_money_vn(n) -> str:
    """4500000 → '4,500,000' (comma format, theo template kế toán)."""
    if n is None or n == "":
        return "…………"
    try:
        v = int(n)
        return f"{v:,}"
    except Exception:
        return str(n)


def _set_font(run, size: int = 10, bold: bool = False, italic: bool = False):
    run.font.name = "Times New Roman"
    # Đảm bảo font áp cho cả cs (East Asian)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), "Times New Roman")
    rFonts.set(qn("w:hAnsi"), "Times New Roman")
    rFonts.set(qn("w:cs"), "Times New Roman")
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic


def _p(doc, text: str = "", bold: bool = False, italic: bool = False,
       align=None, size: int = 10, space_after: int = 2):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    if text:
        run = p.add_run(text)
        _set_font(run, size=size, bold=bold, italic=italic)
    return p


def _label_value(doc, label: str, value: str, value_bold: bool = True,
                 size: int = 10, sep: str = " ", trailing: str = ""):
    """Tạo dòng: 'Label:  VALUE  trailing'."""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    r1 = p.add_run(label + sep)
    _set_font(r1, size=size)
    r2 = p.add_run(value or "………………")
    _set_font(r2, size=size, bold=value_bold)
    if trailing:
        r3 = p.add_run(trailing)
        _set_font(r3, size=size)
    return p


def _two_columns(doc, left: tuple, right: tuple, size: int = 10):
    """1 dòng 2 đoạn: trái 'label: VALUE', phải 'label: VALUE'."""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    # Trái
    r = p.add_run(left[0] + " "); _set_font(r, size=size)
    r = p.add_run(left[1] or "………"); _set_font(r, size=size, bold=True)
    # Tab giữa
    r = p.add_run("\t"); _set_font(r, size=size)
    # Phải
    r = p.add_run(right[0] + " "); _set_font(r, size=size)
    r = p.add_run(right[1] or "………"); _set_font(r, size=size, bold=True)
    return p


# ── Main generator ───────────────────────────────────────────────────────────

def generate_contract_docx(
    contract: Dict[str, Any],
    company: Dict[str, Any],
    employee: Dict[str, Any],
    out_path: Path,
    employee_signature_path: Optional[Path] = None,
    company_signature_path: Optional[Path] = None,
) -> Path:
    """Sinh file .docx HĐLĐ và lưu vào out_path. Return out_path.

    Nếu cung cấp employee_signature_path / company_signature_path (file PNG),
    chèn ảnh vào ô ký tương ứng — file final sẽ hiện đầy đủ chữ ký 2 bên.
    """
    doc = Document()

    # Page margin (chuẩn A4 doc hành chính)
    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2)

    # ── HEADER ──────────────────────────────────────────────────────────
    # Quốc hiệu (phải)
    _p(doc, "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM",
       bold=True, align=WD_ALIGN_PARAGRAPH.RIGHT)
    _p(doc, "Độc lập - Tự do - Hạnh phúc",
       bold=True, italic=True, align=WD_ALIGN_PARAGRAPH.RIGHT)

    # "Số: 001-26/HĐTH/TIỂU HIỀM" + "city, ngày dd tháng mm năm yyyy" cùng hàng
    today = date.today()
    signing_city = (contract.get("signing_city")
                    or company.get("signing_city")
                    or "Hưng Yên")
    signed_today_long = today.strftime("ngày %d tháng %m năm %Y")
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run(f"Số: ")
    _set_font(r)
    r = p.add_run(contract.get("contract_number", "..."))
    _set_font(r, bold=True)
    r = p.add_run("\t" * 3 + f"{signing_city}, {signed_today_long}")
    _set_font(r, italic=True)

    _p(doc, "")

    # Title
    _p(doc, "HỢP ĐỒNG LAO ĐỘNG",
       bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size=18)
    sub = CONTRACT_TYPE_LABEL.get(contract.get("contract_type"), "")
    if sub:
        _p(doc, sub, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size=16)
    _p(doc, "")

    # ── BÊN A (giám đốc đại diện cty) ───────────────────────────────────
    nationality_a = "Việt Nam"
    _label_value(doc, "Chúng tôi, một bên là Ông, bà:",
                 (company.get("legal_rep_name") or "").upper(),
                 trailing=f"\tQuốc tịch: {nationality_a}")
    _label_value(doc, "Ngày tháng năm sinh:",
                 _fmt_date_long(company.get("legal_rep_dob")),
                 value_bold=False)
    _label_value(doc, "Số CMND/CCCD hoặc hộ chiếu:",
                 company.get("legal_rep_id_card") or "")
    _label_value(doc, "Địa chỉ cư trú:",
                 company.get("legal_rep_address") or company.get("address") or "",
                 value_bold=False)
    _label_value(doc, "Chức vụ:",
                 company.get("legal_rep_title") or "Giám đốc",
                 value_bold=False)
    _label_value(doc, "Đại diện cho:",
                 (company.get("company_name") or "").upper())
    _label_value(doc, "Địa chỉ:",
                 company.get("address") or "",
                 value_bold=False)

    # ── BÊN B (NV) ──────────────────────────────────────────────────────
    gender_map = {"male": "Nam", "female": "Nữ", "other": "Khác"}
    emp_gender = gender_map.get(employee.get("gender") or "", "")
    emp_nat = employee.get("nationality") or "Việt Nam"

    # "Và một bên là Ông, Bà: TÊN  Giới tính: X  Quốc tịch: Y"
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run("Và một bên là Ông, Bà: "); _set_font(r)
    r = p.add_run((employee.get("full_name") or "").upper()); _set_font(r, bold=True)
    r = p.add_run("\tGiới tính: "); _set_font(r)
    r = p.add_run(emp_gender or "………"); _set_font(r, bold=True)
    r = p.add_run("\tQuốc tịch: "); _set_font(r)
    r = p.add_run(emp_nat); _set_font(r, bold=True)

    # "Sinh ngày: dd/mm/yyyy  Tại: <place>"
    place_of_birth = employee.get("place_of_birth") or employee.get("hometown_address") or ""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run("Sinh ngày: "); _set_font(r)
    r = p.add_run(_fmt_date_short(employee.get("dob"))); _set_font(r, bold=True)
    r = p.add_run("\tTại: "); _set_font(r)
    r = p.add_run(place_of_birth); _set_font(r)

    _label_value(doc, "Địa chỉ thường trú:",
                 employee.get("hometown_address") or "",
                 value_bold=False)
    _label_value(doc, "Chỗ ở hiện tại:",
                 employee.get("current_address") or "",
                 value_bold=False)

    # "Số CMND/CCCD: ...  Cấp ngày: ...  Tại: ..."
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run("Số CMND/CCCD: "); _set_font(r)
    r = p.add_run(employee.get("id_card_number") or "………"); _set_font(r)
    r = p.add_run("\tCấp ngày: "); _set_font(r)
    r = p.add_run(_fmt_date_short(employee.get("id_card_issued_date"))); _set_font(r)
    r = p.add_run("\tTại: "); _set_font(r)
    r = p.add_run(employee.get("id_card_issued_place") or "………"); _set_font(r)

    # Số giấy phép lao động (nếu có)
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run("Số giấy phép lao động (nếu có): "); _set_font(r)
    r = p.add_run(employee.get("work_permit_no") or ""); _set_font(r)
    r = p.add_run("\tCấp ngày: "); _set_font(r)
    r = p.add_run(_fmt_date_short(employee.get("work_permit_date"))); _set_font(r)
    r = p.add_run("\tTại: "); _set_font(r)
    r = p.add_run(employee.get("work_permit_place") or ""); _set_font(r)

    _p(doc, "Thỏa thuận ký kết hợp đồng lao động và cam kết làm đúng những điều khoản sau đây:")

    # ── ĐIỀU 1 ──────────────────────────────────────────────────────────
    _p(doc, "Điều 1. Thời hạn và công việc hợp đồng:", bold=True)
    type1 = CONTRACT_TYPE_LINE1.get(contract.get("contract_type"), "")
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(2)
    r = p.add_run("- Loại hợp đồng lao động: "); _set_font(r)
    r = p.add_run(type1); _set_font(r, bold=True)

    if contract.get("contract_type") != "khong_xac_dinh":
        p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(2)
        r = p.add_run("- Từ ngày: "); _set_font(r)
        r = p.add_run(_fmt_date_long(contract.get("start_date"))); _set_font(r, bold=True)
        r = p.add_run("\tđến ngày: "); _set_font(r)
        r = p.add_run(_fmt_date_long(contract.get("end_date"))); _set_font(r, bold=True)
    else:
        p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(2)
        r = p.add_run("- Có hiệu lực từ ngày: "); _set_font(r)
        r = p.add_run(_fmt_date_long(contract.get("start_date"))); _set_font(r, bold=True)

    workplace = contract.get("workplace") or company.get("address") or ""
    _p(doc, f"- Địa điểm làm việc: {workplace}")
    _p(doc, f"- Chức danh chuyên môn:   {contract.get('position') or '………'}")
    _p(doc, f"- Chức vụ (nếu có):   {contract.get('position') or '………'}")
    _p(doc, f"- Công việc phải làm:   {contract.get('job_duties') or contract.get('position') or '………'}")

    # ── ĐIỀU 2 ──────────────────────────────────────────────────────────
    _p(doc, "Điều 2. Chế độ làm việc:", bold=True)
    _p(doc, f"- Thời giờ làm việc: {contract.get('work_hours') or '08 giờ/ngày'}")
    equipment = (contract.get("equipment_provided")
                 or "Người lao động được cấp phát tất cả các vật dụng cần thiết để thực hiện công việc hàng ngày")
    _p(doc, f"- Được cấp phát những dụng cụ làm việc gồm: {equipment}")

    # ── ĐIỀU 3 ──────────────────────────────────────────────────────────
    _p(doc, "Điều 3. Nghĩa vụ và quyền lợi của người lao động:", bold=True)
    _p(doc, "1. Quyền lợi:", bold=True)
    _p(doc, f"- Phương tiện đi lại làm việc: {contract.get('transport_mode') or 'Tự túc'}.")

    # Lương
    salary = contract.get("salary_base") or 0
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(2)
    r = p.add_run("- Mức lương chính tại thời điểm ký hợp đồng: "); _set_font(r)
    r = p.add_run(f"\t{_fmt_money_vn(salary)}"); _set_font(r, bold=True)
    r = p.add_run("\tđồng/tháng"); _set_font(r)

    _p(doc, f"- Hình thức trả lương: {contract.get('pay_method') or 'Chuyển khoản'}")
    _p(doc, "- Phụ cấp và các khoản bổ sung khác: Theo nội quy, quy định của Công ty")
    _p(doc, "- Các khoản chế độ và phúc lợi khác: Theo nội quy, quy định của Công ty")
    pay_day = contract.get("pay_day") or 10
    _p(doc, f"- Được trả lương vào ngày {pay_day} tháng sau")
    _p(doc, "- Tiền thưởng: Theo quy định của Công ty")
    _p(doc, "- Chế độ nâng lương: Theo quy định của Công ty")
    _p(doc, "- Chế độ nghỉ ngơi : Theo quy định của luật lao động và quy chế của công ty.")
    _p(doc, "- Các khoản bảo hiểm bắt buộc: Theo quy định của pháp luật hiện hành")
    _p(doc, "- Chế độ đào tạo: Theo quy định của công ty")
    _p(doc, "- Bảo hộ lao động: Được cấp phát theo quy định Cấp phát của công ty")
    _p(doc, f"- Những thoả thuận khác: {contract.get('notes') or ''}")

    _p(doc, "2. Nghĩa vụ:", bold=True)
    _p(doc, "- Hoàn thành những công việc đã cam kết trong hợp đồng lao động.")
    _p(doc, "- Chấp hành lệnh điều hành sản xuất - kinh doanh, nội quy kỷ luật lao động, an toàn lao động.")
    _p(doc, "- Bồi thường vi phạm và vật chất: Theo quy định của công ty")

    # ── ĐIỀU 4 ──────────────────────────────────────────────────────────
    _p(doc, "Điều 4. Nghĩa vụ và quyền hạn của người sử dụng lao động", bold=True)
    _p(doc, "1. Nghĩa vụ:", bold=True)
    _p(doc, "- Bảo đảm việc làm và thực hiện đầy đủ những điều đã cam kết trong hợp đồng lao động.")
    _p(doc, "- Thanh toán đầy đủ, đúng thời hạn các chế độ và quyền lợi cho người lao động theo hợp đồng lao động, thoả ước lao động tập thể (nếu có).")
    _p(doc, "2. Quyền hạn:", bold=True)
    _p(doc, "- Điều hành người lao động hoàn thành công việc theo hợp đồng (bố trí, điều chuyển, tạm ngừng việc...).")
    _p(doc, "- Tạm hoãn, chấm dứt hợp đồng lao động, kỷ luật người lao động theo quy định của pháp luật, thoả ước lao động tập thể (nếu có) và nội quy lao động của doanh nghiệp")

    # ── ĐIỀU 5 ──────────────────────────────────────────────────────────
    _p(doc, "Điều 5. Điều khoản thi hành", bold=True)
    _p(doc, "- Những vấn đề về lao động không ghi trong hợp đồng lao động này thì áp dụng quy định của thoả ước tập thể, trường hợp chưa có thoả ước tập thể thì áp dụng quy định của pháp luật lao động.")
    company_name = (company.get("company_name") or "").upper()
    _p(doc, f"- Hợp đồng lao động được làm tại {company_name} và làm thành 02 bản có giá trị ngang nhau, mỗi bên giữ một bản và có hiệu lực từ ngày ký. Khi hai bên ký kết phụ lục hợp đồng lao động thì nội dung của phụ lục hợp đồng lao động cũng có giá trị như các nội dung của bản hợp đồng lao động này.")

    _p(doc, "")

    # ── KÝ TÊN ──────────────────────────────────────────────────────────
    # Table 2 cột, không border. Cột trái = NLĐ, cột phải = NSDLĐ
    table = doc.add_table(rows=3, cols=2)
    table.autofit = True

    # Hàng 1: Title
    c_left = table.cell(0, 0); c_right = table.cell(0, 1)
    pl = c_left.paragraphs[0]; pl.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = pl.add_run("NGƯỜI LAO ĐỘNG"); _set_font(r, bold=True)
    pr = c_right.paragraphs[0]; pr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = pr.add_run("NGƯỜI SỬ DỤNG LAO ĐỘNG"); _set_font(r, bold=True)

    # Hàng 2: chú thích (Ký, ...)
    p = c_left.cell(1, 0).paragraphs[0] if False else table.cell(1, 0).paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("(Ký, ghi rõ họ tên)"); _set_font(r, italic=True)
    p = table.cell(1, 1).paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("(Ký, đóng dấu)"); _set_font(r, italic=True)

    # Hàng 3: vùng ký + tên dưới
    sig_l = table.cell(2, 0); sig_r = table.cell(2, 1)

    # Bên trái: chữ ký NV (nếu có) hoặc 5 dòng trống
    if employee_signature_path and Path(employee_signature_path).is_file():
        p = sig_l.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run()
        try:
            run.add_picture(str(employee_signature_path), width=Cm(4.5))
        except Exception:
            for _ in range(5):
                sig_l.add_paragraph("")
    else:
        for _ in range(5):
            sig_l.add_paragraph("")

    # Bên phải: chữ ký cty (nếu có) hoặc 5 dòng trống
    if company_signature_path and Path(company_signature_path).is_file():
        p = sig_r.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run()
        try:
            run.add_picture(str(company_signature_path), width=Cm(4.5))
        except Exception:
            for _ in range(5):
                sig_r.add_paragraph("")
    else:
        for _ in range(5):
            sig_r.add_paragraph("")

    # Hàng tên (in hoa)
    pl = sig_l.add_paragraph(); pl.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = pl.add_run((employee.get("full_name") or "").upper()); _set_font(r, bold=True)
    pr = sig_r.add_paragraph(); pr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = pr.add_run((company.get("legal_rep_name") or "").upper()); _set_font(r, bold=True)

    # Save
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path
