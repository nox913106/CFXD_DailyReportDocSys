"""匯入歷史日報（過去已結案的 CheckList Excel，可一次選很多檔或整個資料夾）。系統管理員、檔案管理員。"""

from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from . import db
from .models import Report

bp = Blueprint("imports", __name__, url_prefix="/import")


def can_import(user):
    return user.is_authenticated and (user.is_admin or user.role in ("admin", "file_manager"))


@bp.before_request
@login_required
def _guard():
    if not can_import(current_user):
        abort(403)


@bp.route("/")
def index():
    recent = Report.query.filter(Report.import_meta.isnot(None)).order_by(Report.report_date.desc()).limit(500).all()
    return render_template("import.html", recent=recent, pdf=pdf_status())


PDF_WINDOW = timedelta(hours=3)  # 進度計算：還沒完成的，加上最近 3 小時內完成的（同一批）


def pdf_status():
    """匯入日報的正式 PDF 產生進度（維護服務依序產生）。"""
    from .models import Delivery

    now = datetime.now()
    rows = (Delivery.query.join(Report).filter(Report.import_meta.isnot(None))
            .filter(db.or_(Delivery.state != "done", Delivery.finished_at >= now - PDF_WINDOW))
            .order_by(Delivery.closed_at, Delivery.id).all())
    items, finished = [], []
    for d in rows:
        st = d.step_map()["pdf"]
        state = d.state if d.state in ("queued", "running", "error", "done") else "queued"
        it = {"report_id": d.report_id, "date": d.report.report_date.isoformat(), "doc_no": d.report.doc_no,
              "state": state, "message": st.get("message", "") if state == "error" else "",
              "pdf_name": d.pdf_name or "", "finished_at": d.finished_at.strftime("%H:%M:%S") if d.finished_at else ""}
        if state == "running" and st.get("at"):
            try:
                it["elapsed"] = int((now - datetime.fromisoformat(st["at"])).total_seconds())
            except ValueError:
                pass
        items.append(it)
        if state == "done" and d.finished_at:
            finished.append(d.finished_at)
    count = {k: sum(1 for i in items if i["state"] == k) for k in ("queued", "running", "done", "error")}
    finished.sort()
    per = (finished[-1] - finished[0]).total_seconds() / (len(finished) - 1) if len(finished) >= 2 else 40.0
    left = count["queued"] + count["running"]
    pos = 0
    for i in items:  # 排隊順序
        if i["state"] == "queued":
            pos += 1
            i["position"] = pos
    return {"items": items, "count": count, "total": len(items), "seconds_per_pdf": round(per),
            "eta_seconds": round(per * left), "now": now.strftime("%H:%M:%S")}


@bp.route("/pdf-status")
def pdf_status_api():
    return jsonify(pdf_status())


@bp.route("/upload", methods=["POST"])
def upload():
    """一次一個檔案（瀏覽器逐檔送出，不受單次上傳上限影響）。"""
    from .xlsx_import import import_one

    dry = request.form.get("dry_run") == "1"
    out = []
    for f in request.files.getlist("files"):
        name = (f.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not name.lower().endswith((".xlsx", ".xlsm")):
            out.append({"file": name, "status": "skipped", "message": "不是 .xlsx / .xlsm（舊版 .xls 請先用 Excel 另存新檔）"})
            continue
        if name.startswith("~$"):
            out.append({"file": name, "status": "skipped", "message": "Excel 開啟中的暫存檔"})
            continue
        out.append(import_one(name, f.read(), current_user, dry_run=dry))
    return jsonify(results=out)
