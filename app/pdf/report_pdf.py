"""把一份日報（資料庫）組成與原始範本相同排版的完整 PDF：
Coverpage（8 頁起）→ CheckList（Excel 範本，25 頁）→ Screenshot（52 頁起）。"""

import copy
import datetime as dt
import hashlib
import io
import os

from flask import current_app
from reportlab.pdfgen import canvas

from ..models import Report, ScreenshotSection
from . import fonts
from .cover import CoverDoc
from .screenshot import ScreenshotDoc
from .xlsx_print import Workbook, render_sheets

PAGE = (595.32, 841.92)
ASSETS = os.path.join(os.path.dirname(__file__), "assets")
SHEETS = ["Alarm Summary", "Summary", "Virtual Machines", "Software", "Physical devices", "PI"]
from ..checklist_template import CHECK_COLS  # noqa: E402  各檢查表工作表：(Normal, Warning, Failed, Note) 欄
ALARM_FIRST_ROW = 7
ALARM_LINE_H = 15.5
# 第二層章節在原範本中有手動換頁者
SCREENSHOT_PAGE_BREAKS = {"4.2", "4.3", "4.4", "5.2", "14.2", "14.3", "15.3", "17.3", "17.5", "18.3", "19.3", "19.5"}
SCREENSHOT_ZWSP = {"4.2", "4.3"}  # 原範本標題含零寬空白（目錄編號以 Times New Roman 呈現）


class TemplateMissing(RuntimeError):
    pass


def template_path():
    path = current_app.config["PDF_TEMPLATE_XLSX"]
    if not os.path.exists(path):
        raise TemplateMissing(
            f"找不到 CheckList 範本：{path}。請把「CFXD SLA - SCADA Daily Inspection CheckList.xlsx」複製到該位置。"
        )
    return path


def pdf_filename(report):
    purpose = (report.issue_purpose or "IFI").split(" ")[0]
    return f"{report.doc_no}_{report.revision}_{purpose}_{report.report_date.isoformat()}_CFXD_SLA_Daily_Work_Report.pdf"


def _image_path(report, img):
    return os.path.join(current_app.config["UPLOAD_DIR"], str(report.id), img.filename)


def cache_key(report):
    imgs = ",".join(f"{i.id}:{i.order}:{i.section_id}" for i in report.images)
    raw = f"{report.id}|{report.updated_at}|{report.status}|{report.approver_name}|{imgs}|preview-v2"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


PREVIEW_MARK = "PREVIEW - NOT THE FINAL DOCUMENT"


class _Canvas(canvas.Canvas):
    """預覽版每一頁加上浮水印；正式 PDF（結案後產生）不加。"""

    watermark = None

    def showPage(self):
        if self.watermark:
            w, h = self._pagesize
            self.saveState()
            self.setFillColorRGB(0.85, 0.1, 0.1)
            self.setFillAlpha(0.16)
            self.setFont("Helvetica-Bold", 34)
            self.translate(w / 2, h / 2)
            self.rotate(52)
            self.drawCentredString(0, 0, self.watermark)
            self.restoreState()
        super().showPage()


def build_pdf(report):
    """預覽用 PDF bytes（有快取，每頁有浮水印）。正式 PDF 只在結案後由發送流程產生一次並存檔。"""
    cache_dir = os.path.join(current_app.instance_path, "pdf_cache")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{report.id}_{cache_key(report)}.pdf")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    data = render_pdf(report, preview=True)
    for old in os.listdir(cache_dir):
        if old.startswith(f"{report.id}_"):
            os.remove(os.path.join(cache_dir, old))
    with open(path, "wb") as f:
        f.write(data)
    return data


def render_pdf(report, preview=False):
    fonts.configure(current_app.config.get("FONT_DIR"))
    buf = io.BytesIO()
    cv = _Canvas(buf, pagesize=PAGE)
    cv.watermark = PREVIEW_MARK if preview else None
    cv.setTitle(f"{report.doc_no} CFXD SLA Daily Work Report")
    cv.setAuthor(report.author_name)
    logo = os.path.join(ASSETS, "logo_word.png")

    CoverDoc(cover_data(report), logo).render(cv)
    if report.imported:
        # Excel 匯入的過去日報：CheckList 直接畫原始 Excel（儲存格值與版面都不經過系統範本，內容與原檔完全相同）
        book = original_workbook(report)
        render_sheets(book, [s for s in SHEETS if s in book.wb.sheetnames], cv, PAGE)
    else:
        book = fill_workbook(report)
        render_sheets(book, SHEETS, cv, PAGE)
    if not (report.imported and not report.images):  # 匯入的沒有截圖，不印空白的截圖章節
        ScreenshotDoc(screenshot_sections(report), logo).render(cv)
    cv.save()
    return buf.getvalue()


# ---------------------------------------------------------------- Coverpage


def cover_data(report):
    d = report.report_date
    author = report.author_name
    purpose = report.issue_purpose or "IFI - Issue for Information"
    status, _, changes = purpose.partition(" - ")
    month_start = d.replace(day=1)
    month_reports = (
        Report.query.filter(Report.report_date >= month_start, Report.report_date <= d)
        .order_by(Report.report_date)
        .all()
    )
    images = sorted(report.images, key=lambda i: (i.order, i.id))
    return {
        "doc_no": report.doc_no,
        "contractor_doc_no": report.contractor_doc_no,
        "issue_purpose": purpose,
        "revision": report.revision,
        "date_dot": d.strftime("%Y.%m.%d"),
        "author": author,
        "approver": report.approver_name or "",
        "revisions": [{
            "rev": report.revision, "date": d.isoformat(), "status": status, "changes": changes or purpose,
            "author": author, "approval": report.approver_name or "",
        }],
        "month_label": d.strftime("%B %Y"),
        "reports": [(r.report_date.isoformat(), r.author_name, r.doc_no) for r in month_reports],
        "inspection_summary": report.inspection_summary or "",
        "first_line_officer": report.first_line_officer or "",
        "second_line_support": report.second_line_support or "",
        "issues": [_row(i) for i in report.issues],
        "onsite_summary": report.onsite_summary or "",
        "onsite": [_row(i) for i in report.onsite_records],
        "system_updates": [_row(i) for i in report.system_updates],
        "onsite_images": [_image_path(report, i) for i in images if i.kind == "onsite"],
        "alarm_new": report.alarm_new or 0,
        "alarm_known": report.alarm_known or 0,
        "alarm_false": report.alarm_false or 0,
        "true_alarms": [_row(i) for i in report.true_alarms],
        "alarm_images": [_image_path(report, i) for i in images if i.kind == "alarm"],
    }


def _row(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ---------------------------------------------------------------- CheckList


class OriginalMissing(TemplateMissing):
    pass


def original_workbook(report):
    """匯入時保存的原始 Excel，用 Excel 存檔時的值（不重算公式）。"""
    path = report.original_xlsx
    if not path:
        raise OriginalMissing("這份日報匯入時沒有保存原始 Excel（舊版匯入），無法產生 PDF。請刪除後重新匯入。")
    return Workbook(path, recalc=False)


def fill_workbook(report):
    book = Workbook(template_path(), recalc=True)
    author = report.author_name
    t = _parse_time(report.inspection_time)
    d = dt.datetime.combine(report.report_date, dt.time())

    book.set("Alarm Summary", "I2", d)
    book.set("Alarm Summary", "I3", t)
    book.set("Alarm Summary", "I4", report.revision)
    book.set("Alarm Summary", "I5", author)
    for name in SHEETS:
        ws = book.wb[name]
        for row in ws.iter_rows():
            for c in row:
                label = c.value if isinstance(c.value, str) else ""
                if not label.startswith(("Revision Number", "Inspection Time")):
                    continue
                target = _value_cell_right_of(ws, c)
                if target is None:
                    continue
                raw = ws[target].value
                if isinstance(raw, str) and raw.startswith("="):
                    continue
                book.set(name, target, report.revision if label.startswith("Revision") else t)

    _fill_alarm_rows(book, report)
    _fill_checklist(book, report)
    return book


def _value_cell_right_of(ws, cell):
    for col in range(cell.column + 1, min(cell.column + 6, ws.max_column + 1)):
        target = ws.cell(row=cell.row, column=col)
        if target.coordinate in ws.merged_cells:
            continue
        if target.value is not None:
            return target.coordinate
    return None


def _parse_time(s):
    for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p"):
        try:
            return dt.datetime.strptime((s or "").strip(), fmt).time()
        except ValueError:
            continue
    return dt.time(10, 0)


def _fill_alarm_rows(book, report):
    ws = book.wb["Alarm Summary"]
    rows = list(report.alarm_rows)
    template_last = ws.max_row
    style_row = ALARM_FIRST_ROW + 1
    # 範本的列印範圍固定到最後一列：資料較少時其餘列留白但保留框線
    n = max(len(rows), template_last - ALARM_FIRST_ROW + 1)
    for k in range(n):
        r = ALARM_FIRST_ROW + k
        if r > template_last:
            for col in range(1, ws.max_column + 1):
                src = ws.cell(row=style_row, column=col)
                dst = ws.cell(row=r, column=col)
                dst._style = copy.copy(src._style)
        row = rows[k] if k < len(rows) else None
        values = {
            "B": k + 1 if row else None,
            "C": row.category if row else None,
            "D": row.item if row else None,
            "E": row.device_name if row else None,
            "F": row.description if row else None,
            "G": row.reason if row else None,
            "H": row.false_alarm if row else None,
            "I": row.note if row else None,
        }
        lines = 1
        for col, v in values.items():
            book.set("Alarm Summary", f"{col}{r}", v or None)
            if col in ("F", "G", "I") and v:
                lines = max(lines, _line_count(ws, col, r, str(v)))
        tpl = book.cached["Alarm Summary"]
        same = row is not None and r <= template_last and all(
            (tpl[f"{c}{r}"].value or "") == (values[c] or "") for c in ("E", "F", "G"))
        if not (same and ws.row_dimensions[r].height):
            # 範本中同一列內容相同時沿用其（可能手動調整過的）列高，否則依換行行數計算
            ws.row_dimensions[r].height = lines * ALARM_LINE_H
    last = ALARM_FIRST_ROW + n - 1
    for r in range(last + 1, template_last + 1):
        for col in "BCDEFGHI":
            book.set("Alarm Summary", f"{col}{r}", None)
    ws.print_area = f"A2:I{last}"


def _line_count(ws, col, row, text):
    from .xlsx_print import CHAR_PT, PAD_X, _wrap

    cell = ws[f"{col}{row}"]
    width = (ws.column_dimensions[col].width or 9) * CHAR_PT - 2 * PAD_X
    f = fonts.font(cell.font.name, bool(cell.font.b))
    return sum(len(_wrap(p, f, cell.font.sz or 11, width)) for p in text.split("\n"))


def _fill_checklist(book, report):
    """依每一列的文字找出是哪個檢查項目（見 checklist_template），不是第 N 列 = 第 N 項；
    範本還沒依「檢查項目」頁的修改改版時，已存在的項目照樣填在正確的列，範本沒有的項目不會出現在 PDF。"""
    from ..checklist_template import active_items, map_workbook

    results = {res.item_id: res for res in report.results}
    items = active_items()
    for res in report.results:  # 舊日報：已刪除的項目也要對應得到
        if not res.item.active:
            items.setdefault(res.item.sheet, []).append(res.item)
    for sheet, items_ in items.items():
        items_.sort(key=lambda i: (i.order, i.id))
    maps = map_workbook(book.wb, items)
    for sheet, (name, cn, cw, cf, cnote) in CHECK_COLS.items():
        ws = book.wb[name]
        pairs = maps[sheet].pairs
        for r in range(1, ws.max_row + 1):
            if not isinstance(ws[f"{cn}{r}"].value, bool):
                continue
            it = pairs.get(r)
            res = results.get(it.id) if it else None
            val = res.result if res else None
            book.set(name, f"{cn}{r}", val == "N")
            book.set(name, f"{cw}{r}", val == "W")
            book.set(name, f"{cf}{r}", val == "F")
            note = (res.note or None) if res else None
            book.set(name, f"{cnote}{r}", note)
            if note:
                # 像 Excel 一樣，備註換行後放不下時自動加高列
                cell = ws[f"{cnote}{r}"]
                size = cell.font.sz or 11
                need = _line_count(ws, cnote, r, note) * size * 1.2917
                if need > (ws.row_dimensions[r].height or 15):
                    ws.row_dimensions[r].height = need


# ---------------------------------------------------------------- Screenshot


def screenshot_sections(report):
    by_section = {}
    for img in sorted(report.images, key=lambda i: (i.order, i.id)):
        if img.kind == "screenshot":
            by_section.setdefault(img.section_id, []).append(_image_path(report, img))
    out = []
    for sec in ScreenshotSection.query.order_by(ScreenshotSection.order):
        title = sec.title + (" ​" if sec.code in SCREENSHOT_ZWSP else "")
        out.append({
            "code": sec.code,
            "title": title,
            "level": sec.level,
            "page_break": sec.code in SCREENSHOT_PAGE_BREAKS,
            "images": by_section.get(sec.id, []),
        })
    return out
