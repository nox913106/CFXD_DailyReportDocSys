"""把一份日報（資料庫）組成與原始範本相同排版的完整 PDF：
Coverpage（8 頁起）→ CheckList（Excel 範本，25 頁）→ Screenshot（52 頁起）。"""

import copy
import datetime as dt
import hashlib
import io
import json
import os

from flask import current_app
from reportlab.pdfgen import canvas

from .. import db
from ..models import OFFICIAL_STATUSES, Report, ScreenshotSection
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


def month_reports_of(report):
    """封面 Reports Control：當月 1 日到這一天的正式日報（已核准、已結案，含 Excel 匯入），加上這一份本身。"""
    d = report.report_date
    q = Report.query.filter(Report.report_date >= d.replace(day=1), Report.report_date <= d,
                            db.or_(Report.status.in_(OFFICIAL_STATUSES), Report.id == report.id))
    return q.order_by(Report.report_date).all()


def cache_key(report):
    imgs = ",".join(f"{i.id}:{i.order}:{i.section_id}" for i in report.images)
    month = ",".join(f"{r.id}:{r.doc_no}:{r.author_name}" for r in month_reports_of(report))  # 補匯入前幾天時預覽要更新
    raw = f"{report.id}|{report.updated_at}|{report.status}|{report.approver_name}|{imgs}|{month}|{report.alarm_new}|{report.alarm_known}|preview-v3"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


PREVIEW_MARK = "PREVIEW - NOT THE FINAL DOCUMENT"


IMPORTED_MARK = "IMPORTED FROM ORIGINAL EXCEL"


def imported_mark_lines(report, today=None):
    """Excel 匯入日報的浮水印（正式 PDF 與預覽都有）：說明這份 PDF 是由原始 Excel 重新產生。"""
    m = report.import_info
    issued = (today or dt.date.today()).isoformat()
    return (IMPORTED_MARK,
            f"Re-issued {issued} by CFXD Daily Report System - not the originally issued PDF",
            f"Source: {m.get('file', '')}",
            f"SHA-256 {m.get('sha256', '')}")


COMPANY_ZH = "雷力科技股份有限公司"
COMPANY_EN = "Rally Technology Co., Ltd."
CLASSIFICATION_ZH = "機密，僅限內部使用"
CLASSIFICATION_EN = "CONFIDENTIAL - INTERNAL USE ONLY"


def content_fingerprint(report):
    """正式 PDF 內容的 SHA-256：日報所有欄位、各記錄表、告警列、檢查結果、每張圖片檔案的位元組與範本檔。
    PDF 檔案本身的 SHA-256 無法寫進自己（寫入就會改變），所以浮水印印的是這個內容雜湊；
    正式 PDF 檔案的 MD5 / SHA-256 另外記錄在系統（正式文件頁）。同樣的內容永遠得到同樣的值。"""

    def rows(objs, skip=("id", "report_id")):
        return [{c.name: getattr(o, c.name) for c in o.__table__.columns if c.name not in skip} for o in objs]

    def file_sha(path):
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return None
        return h.hexdigest()

    images = sorted(report.images, key=lambda i: (i.order, i.id))
    data = {
        "report": rows([report], skip=("updated_at", "updated_by_id"))[0],
        "issues": rows(report.issues), "onsite": rows(report.onsite_records),
        "updates": rows(report.system_updates), "true_alarms": rows(report.true_alarms),
        "alarm_rows": rows(report.alarm_rows),
        "results": sorted(rows(report.results), key=lambda r: r["item_id"]),
        "images": [{"kind": i.kind, "section_id": i.section_id, "order": i.order,
                    "sha256": file_sha(_image_path(report, i))} for i in images],
        "template": file_sha(current_app.config.get("PDF_TEMPLATE_XLSX") or "") if not report.imported else None,
    }
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def official_stamp(report, now=None):
    """系統建立日報的正式 PDF 浮水印內容。"""
    now = (now or dt.datetime.now()).astimezone()
    off = now.strftime("%z")
    return {"filename": pdf_filename(report), "generated": now.strftime("%Y-%m-%d %H:%M:%S") + f" UTC{off[:3]}:{off[3:]}",
            "sha256": content_fingerprint(report)}


def _faded_logo(alpha=0.13):
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    im = Image.open(os.path.join(ASSETS, "logo_word.png")).convert("RGBA")
    a = im.getchannel("A").point(lambda v: int(v * alpha))
    im.putalpha(a)
    return ImageReader(im)


class _Canvas(canvas.Canvas):
    """浮水印：預覽版每頁 PREVIEW；Excel 匯入的日報（正式與預覽）每頁「由原始 Excel 重新產生」；
    系統建立日報的正式 PDF 每頁平鋪公司 logo、名稱、機密等級、檔名、產出時間、內容 SHA-256（stamp）。"""

    watermark = None
    imported = None  # (主標, 說明…)
    stamp = None  # official_stamp()：系統建立日報的正式 PDF，整頁平鋪公司 logo、名稱、機密等級、檔名、時間、雜湊

    def _tiled_mark(self):
        """整頁平鋪（旋轉 30°）：logo 圖樣只畫一次做成 Form XObject 每頁引用（圖片本身帶透明度）；
        文字每頁直接畫（半透明，Form 裡不能用透明度設定）。"""
        w, h = self._pagesize
        st = self.stamp
        if "_logo" not in st:
            st["_logo"] = _faded_logo()
            cjk = fonts.cjk()
            st["_cjk"] = cjk if fonts.has_glyphs(cjk, COMPANY_ZH + CLASSIFICATION_ZH) else None
            st["_forms"] = set()
        cjk = st["_cjk"]
        lines = [  # (文字, 字型, 字級, 透明度, 紅字)
            ((COMPANY_ZH + "  " if cjk else "") + COMPANY_EN, cjk or "Helvetica-Bold", 8, 0.16, False),
            ((CLASSIFICATION_ZH + "  " if cjk else "") + CLASSIFICATION_EN, cjk or "Helvetica-Bold", 8, 0.20, True),
            (st["filename"], "Helvetica", 5.2, 0.17, False),
            ("Generated " + st["generated"], "Helvetica", 5.2, 0.17, False),
            ("Content SHA-256 " + st["sha256"], "Helvetica", 5.2, 0.17, False),
        ]
        dx, dy = 340, 175
        diag = (w * w + h * h) ** 0.5
        ny, nx = int(diag / dy / 2) + 2, int(diag / dx / 2) + 2
        spots = [(i * dx + (dx / 2 if j % 2 else 0), j * dy) for j in range(-ny, ny + 1) for i in range(-nx, nx + 1)]
        name = f"wmlogo_{int(w)}x{int(h)}"
        if name not in st["_forms"]:
            self.beginForm(name, lowerx=0, lowery=0, upperx=w, uppery=h)
            self.translate(w / 2, h / 2)
            self.rotate(30)
            for x, y in spots:
                self.drawImage(st["_logo"], x - 40, y + 20, 80, 24, mask="auto")
            self.endForm()
            st["_forms"].add(name)
        self.doForm(name)
        self.saveState()
        self.translate(w / 2, h / 2)
        self.rotate(30)
        for k, (txt, font, size, alpha, red) in enumerate(lines):
            self.setFillColorRGB(*((0.55, 0.08, 0.08) if red else (0.25, 0.25, 0.25)))
            self.setFillAlpha(alpha)
            self.setFont(font, size)
            for x, y in spots:
                self.drawCentredString(x, y + 8 - k * 9.2 + (0 if k < 2 else 2), txt)
        self.restoreState()

    def _imported_mark(self):
        w, h = self._pagesize
        title, *notes = self.imported
        self.saveState()
        self.setFillColorRGB(0.12, 0.30, 0.55)
        self.translate(w / 2, h / 2)
        self.rotate(52 if h > w else 30)
        self.setFillAlpha(0.08)
        self.setFont("Helvetica-Bold", 30 if h > w else 40)
        self.drawCentredString(0, 18, title)
        self.setFillAlpha(0.18)
        size = 7.5 if h > w else 9
        self.setFont("Helvetica", size)
        for i, line in enumerate(notes):
            self.drawCentredString(0, -2 - i * (size + 3), line)
        self.restoreState()

    def showPage(self):
        if self.stamp:
            self._tiled_mark()
        if self.imported:
            self._imported_mark()
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


def render_official(report):
    """正式 PDF：回傳 (bytes, stamp)。系統建立的日報整頁平鋪浮水印；Excel 匯入的用匯入浮水印（stamp 為 None）。"""
    fonts.configure(current_app.config.get("FONT_DIR"))
    stamp = None if report.imported else official_stamp(report)
    data = render_pdf(report, preview=False, stamp=stamp)
    return data, ({k: v for k, v in stamp.items() if not k.startswith("_")} if stamp else None)


def render_pdf(report, preview=False, stamp=None):
    fonts.configure(current_app.config.get("FONT_DIR"))
    buf = io.BytesIO()
    cv = _Canvas(buf, pagesize=PAGE)
    cv.watermark = PREVIEW_MARK if preview else None
    cv.imported = imported_mark_lines(report) if report.imported else None
    cv.stamp = stamp
    if stamp:
        cv.setSubject(f"{CLASSIFICATION_EN}; generated {stamp['generated']}; content SHA-256 {stamp['sha256']}")
    cv.showOutline()
    cv.setTitle(f"{report.doc_no} CFXD SLA Daily Work Report")
    cv.setAuthor(report.author_name)
    logo = os.path.join(ASSETS, "logo_word.png")

    CoverDoc(cover_data(report), logo).render(cv)
    if report.imported:
        # Excel 匯入的過去日報：CheckList 直接畫原始 Excel（儲存格值與版面都不經過系統範本，內容與原檔完全相同）
        book = original_workbook(report)
        render_sheets(book, [s for s in SHEETS if s in book.wb.sheetnames], cv, PAGE, outline="CheckList")
    else:
        book = fill_workbook(report)
        render_sheets(book, SHEETS, cv, PAGE, outline="CheckList")
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
    month_reports = month_reports_of(report)
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
