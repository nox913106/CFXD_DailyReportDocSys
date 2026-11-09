"""匯入歷史日報（過去已結案的 CheckList Excel，可一次選很多檔或整個資料夾）。系統管理員、檔案管理員。"""

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

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
    return render_template("import.html", recent=recent)


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
