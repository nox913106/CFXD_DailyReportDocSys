"""PDF 輸出測試。需要 CheckList 範本（預設 instance/templates/checklist.xlsx，
或以環境變數 PDF_TEMPLATE_XLSX 指定）；找不到時略過整合測試。"""

import datetime as dt
import io
import os

import pytest

from app.pdf.formula import Evaluator
from app.pdf.xlsx_print import format_value

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.environ.get("PDF_TEMPLATE_XLSX", os.path.join(ROOT, "instance", "templates", "checklist.xlsx"))


def test_format_value_excel_rounding():
    assert format_value(0.125, "0%") == "13%"  # Excel 四捨五入，不是銀行家捨入
    assert format_value(0.875, "0.0%") == "87.5%"
    assert format_value(dt.datetime(2026, 11, 3), "yyyy\\-mm\\-dd;@") == "2026-11-03"
    assert format_value(dt.time(10, 0), "[$-409]h:mm\\ AM/PM;@") == "10:00 AM"
    assert format_value(5.0, "General") == "5"


def test_formula_evaluator():
    cells = {("S", "A1"): True, ("S", "A2"): False, ("S", "A3"): True, ("S", "B1"): 3.0,
             ("T", "I2"): dt.datetime(2026, 11, 3)}
    ev = Evaluator(lambda sh, c: cells.get((sh, c)))
    assert ev.eval("=COUNTIF(A1:A3, TRUE)", "S") == 2
    assert ev.eval("=B1/(B1+1)", "S") == 0.75
    assert ev.eval('="CFXD-SCA-RLY-DPR-" & TEXT(387 + (T!I2 - DATE(2025,6,12)), "000000")', "S") == "CFXD-SCA-RLY-DPR-000896"


@pytest.mark.skipif(not os.path.exists(TEMPLATE), reason="沒有 CheckList 範本")
def test_pdf_route(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("SECRET_KEY", "t")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "up"))
    monkeypatch.setenv("PDF_TEMPLATE_XLSX", TEMPLATE)
    from app import create_app, db, docno
    from app.models import ChecklistItem, ChecklistResult, Report, ReportImage, ScreenshotSection, User

    app = create_app()
    with app.app_context():
        u = User(username="simon", display_name="Simon Lin")
        db.session.add(u)
        db.session.flush()
        d = dt.date(2026, 11, 3)
        r = Report(report_date=d, doc_no=docno.doc_no(d), contractor_doc_no=docno.contractor_doc_no(d),
                   author_id=u.id, approver_name="Alan Lin")
        db.session.add(r)
        db.session.flush()
        for item in ChecklistItem.query:
            db.session.add(ChecklistResult(report_id=r.id, item_id=item.id, result="N"))
        os.makedirs(tmp_path / "up" / str(r.id))
        Image.new("RGB", (1920, 1080), "navy").save(tmp_path / "up" / str(r.id) / "a.png")
        sec = ScreenshotSection.query.filter_by(code="1").one()
        db.session.add(ReportImage(report_id=r.id, kind="screenshot", section_id=sec.id, filename="a.png", order=1))
        db.session.commit()
        rid = r.id

    c = app.test_client()
    c.get("/login")
    import re

    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    c.post("/login", data={"username": "simon", "password": "x", "csrf_token": tok})
    resp = c.get(f"/reports/{rid}/pdf?download=1")
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert "CFXD-SCA-RLY-DPR-000896_0_IFI_2026-11-03_CFXD_SLA_Daily_Work_Report.pdf" in resp.headers["Content-Disposition"]
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(resp.data))
    # Coverpage 8 + CheckList 25（Alarm Summary 沒有資料時仍 1 頁）+ Screenshot（封面 + 目錄 2 + 19 個大章節以上）
    assert len(reader.pages) >= 8 + 25 + 3 + 19
    text = reader.pages[0].extract_text()
    assert "CFXD-SCA-RLY-DPR-000896" in text and "Alan Lin" in text
