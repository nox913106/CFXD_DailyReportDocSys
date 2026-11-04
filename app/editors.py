"""日報各分頁的編輯畫面。"""

from itertools import groupby

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import contains_eager

from . import db, docno
from . import approval as perm
from .approval import approvers
from .helpers import SHEETS, checklist_stats, get_report, locked_redirect, parse_rows
from .models import (
    AlarmSummaryRow,
    ChecklistItem,
    ChecklistResult,
    IssueRecord,
    OnsiteRecord,
    ScreenshotSection,
    SystemUpdateRecord,
    Report,
    ReportImage,
    ReviewLog,
    TrueAlarmRecord,
    User,
)

bp = Blueprint("editors", __name__, url_prefix="/reports/<int:report_id>")

ISSUE_FIELDS = [
    ("received_by", "Received By"),
    ("received_from", "Received From"),
    ("received_time", "Received Time"),
    ("report_by", "Report By"),
    ("reporter_phone", "Reporter Phone No."),
    ("issue_type", "Issue Type"),
    ("description", "Issue Description"),
    ("status", "Status"),
    ("further_action", "Further Action"),
    ("jira", "Issue on Jira"),
]
ONSITE_FIELDS = [
    ("personnel", "On-site Personnel"),
    ("recorded_by", "Recorded By"),
    ("time", "Time"),
    ("title", "Work Title"),
    ("status", "Work Status"),
    ("further_action", "Further Action"),
    ("jira", "Work on Jira"),
]
SYSUPDATE_FIELDS = [
    ("system_name", "System Name"),
    ("update_personnel", "Update Personnel"),
    ("backup_support", "Backup Support"),
    ("time_period", "Time Period"),
    ("risk_info", "Risk Information"),
    ("logbook_updated", "Log Book Updated"),
]
TRUE_ALARM_FIELDS = [
    ("alarm_item", "Alarm Item"),
    ("start_time", "Alarm Start Time"),
    ("end_time", "Alarm End Time"),
    ("checked_by", "Issue Checked By"),
    ("recorded_by", "Recorded By"),
    ("status", "Status"),
    ("follow_up", "Follow Up"),
]
ALARM_ROW_FIELDS = [
    ("category", "Alarm Category"),
    ("item", "Item"),
    ("device_name", "Device Name"),
    ("description", "Description"),
    ("reason", "Reason"),
    ("false_alarm", "False Alarm (Y/N)"),
    ("note", "Note"),
]
ALARM_CATEGORIES = ["Virtual Machines", "Software", "Physical Devices", "PI"]
SHEET_TO_ALARM_CATEGORY = dict(zip(SHEETS, ALARM_CATEGORIES))


@bp.before_request
@login_required
def _require_login():
    pass


def _replace_rows(collection, model, rows):
    collection.clear()
    for row in rows:
        collection.append(model(**row))


def _touch(report):
    report.updated_by_id = current_user.id


def _int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@bp.route("/basic", methods=["GET", "POST"])
def basic(report_id):
    report = get_report(report_id)
    if request.method == "POST":
        if (r := locked_redirect(report)) is not None:
            return r
        if perm.can_override_doc_no(current_user, report):
            err = _doc_numbers(report)
            if err:
                flash(err, "error")
                return redirect(url_for("editors.basic", report_id=report.id))
        report.revision = _int(request.form.get("revision"))
        for f in ("issue_purpose", "inspection_time", "first_line_officer", "second_line_support"):
            setattr(report, f, request.form.get(f, "").strip())
        assign = perm.can_assign(current_user, report)
        if assign:
            _assign(report, assign.principal)
        _touch(report)
        db.session.commit()
        flash("已儲存。", "success")
        return redirect(url_for("editors.basic", report_id=report.id))
    choices = approvers()
    if report.approver and report.approver not in choices:
        choices.append(report.approver)
    authors = User.query.filter(User.role != "admin").order_by(User.display_name, User.username).all()
    return render_template("edit_basic.html", report=report, tab="basic", approvers=choices, authors=authors,
                           can_assign=perm.can_assign(current_user, report),
                           can_doc_no=perm.can_override_doc_no(current_user, report),
                           auto_doc_no=docno.doc_no(report.report_date),
                           auto_contractor_doc_no=docno.contractor_doc_no(report.report_date))


DOC_FIELDS = (("doc_no", "Document No.", docno.doc_no), ("contractor_doc_no", "Contractor Doc No.", docno.contractor_doc_no))


def _doc_numbers(report):
    """二線以上覆寫文件編號；空白 = 恢復依日期自動產生。回傳錯誤訊息或 None。"""
    new = {}
    for field, label, auto in DOC_FIELDS:
        v = " ".join(request.form.get(field, "").split()) or auto(report.report_date)
        if len(v) > 64 or not v.isprintable():
            return f"{label} 最多 64 個字元，不能有特殊控制字元。"
        dup = Report.query.filter(Report.id != report.id, db.func.lower(getattr(Report, field)) == v.lower()).first()
        if dup:
            return f"{label} {v} 已用在 {dup.report_date} 的日報（作廢的日報編號也保留不再使用）。"
        new[field] = v
    changes = [f"{label} {getattr(report, f)} → {new[f]}" for f, label, _ in DOC_FIELDS if new[f] != getattr(report, f)]
    if changes:
        for f in new:
            setattr(report, f, new[f])
        db.session.add(ReviewLog(report_id=report.id, action="doc_no", actor_id=current_user.id, comment="；".join(changes)))
    return None


def _assign(report, principal):
    """指派撰寫人（Author）與核准人（Approval），並記錄於審核紀錄。"""
    changes = []
    author = db.session.get(User, request.form.get("author_id", type=int) or 0)
    if author and author.role != "admin" and author.id != report.author_id:
        changes.append(f"撰寫人 {report.author.name} → {author.name}")
        report.author_id = author.id
    raw = request.form.get("approver_id")
    approver = db.session.get(User, int(raw)) if raw and raw.isdigit() else None
    if approver and approver.role == "file_manager" and approver.id != report.approver_id:
        changes.append(f"核准人 {report.approver_name or '未指定'} → {approver.name}")
        report.approver_id, report.approver_name = approver.id, approver.name
    elif raw == "" and report.approver_id is not None:
        changes.append(f"核准人 {report.approver_name} → 未指定")
        report.approver_id, report.approver_name = None, ""
    if changes:
        db.session.add(ReviewLog(report_id=report.id, action="assign", actor_id=current_user.id,
                                 on_behalf_of_id=principal.id if principal else None, comment="；".join(changes)))


@bp.route("/records", methods=["GET", "POST"])
def records(report_id):
    report = get_report(report_id)
    if request.method == "POST":
        if (r := locked_redirect(report)) is not None:
            return r
        report.inspection_summary = request.form.get("inspection_summary", "").strip()
        report.onsite_summary = request.form.get("onsite_summary", "").strip()
        _replace_rows(report.issues, IssueRecord, parse_rows(request.form, "issue", [f for f, _ in ISSUE_FIELDS]))
        _replace_rows(
            report.onsite_records, OnsiteRecord, parse_rows(request.form, "onsite", [f for f, _ in ONSITE_FIELDS])
        )
        _replace_rows(
            report.system_updates,
            SystemUpdateRecord,
            parse_rows(request.form, "sysupdate", [f for f, _ in SYSUPDATE_FIELDS]),
        )
        _touch(report)
        db.session.commit()
        flash("已儲存。", "success")
        return redirect(url_for("editors.records", report_id=report.id))
    return render_template(
        "edit_records.html",
        report=report,
        tab="records",
        groups=[
            ("issue", "Issue Record（問題記錄）", ISSUE_FIELDS, report.issues),
            ("onsite", "On-site Record（現場作業）", ONSITE_FIELDS, report.onsite_records),
            ("sysupdate", "System Update Record（系統更新）", SYSUPDATE_FIELDS, report.system_updates),
        ],
        onsite_images=[i for i in report.images if i.kind == "onsite"],
    )


@bp.route("/alarms", methods=["GET", "POST"])
def alarms(report_id):
    report = get_report(report_id)
    if request.method == "POST":
        if (r := locked_redirect(report)) is not None:
            return r
        report.alarm_new = _int(request.form.get("alarm_new"))
        report.alarm_known = _int(request.form.get("alarm_known"))
        report.alarm_false = _int(request.form.get("alarm_false"))
        _replace_rows(
            report.true_alarms,
            TrueAlarmRecord,
            parse_rows(request.form, "truealarm", [f for f, _ in TRUE_ALARM_FIELDS]),
        )
        rows = parse_rows(request.form, "alarmrow", [f for f, _ in ALARM_ROW_FIELDS])
        for i, row in enumerate(rows, 1):
            row["order"] = i
            row["false_alarm"] = "Y" if row["false_alarm"].upper() == "Y" else "N"
        _replace_rows(report.alarm_rows, AlarmSummaryRow, rows)
        _touch(report)
        db.session.commit()
        flash("已儲存。", "success")
        return redirect(url_for("editors.alarms", report_id=report.id))
    return render_template(
        "edit_alarms.html",
        report=report,
        tab="alarms",
        true_alarm_fields=TRUE_ALARM_FIELDS,
        alarm_row_fields=ALARM_ROW_FIELDS,
        alarm_categories=ALARM_CATEGORIES,
        alarm_images=[i for i in report.images if i.kind == "alarm"],
    )


@bp.route("/alarms/import", methods=["POST"])
def import_alarms(report_id):
    """把檢查表中 Warning/Failed 且尚未列入的項目加入 Alarm Summary。"""
    report = get_report(report_id)
    if (r := locked_redirect(report)) is not None:
        return r
    existing = {(row.category, row.device_name, row.item) for row in report.alarm_rows}
    abnormal = (
        report.results.filter(ChecklistResult.result.in_(["W", "F"]))
        .join(ChecklistItem)
        .order_by(ChecklistItem.sheet, ChecklistItem.order)
        .all()
    )
    added = 0
    order = len(report.alarm_rows)
    for res in abnormal:
        item = res.item
        key = (SHEET_TO_ALARM_CATEGORY[item.sheet], item.device_name, item.item_no)
        if key in existing:
            continue
        order += 1
        report.alarm_rows.append(
            AlarmSummaryRow(
                order=order,
                category=key[0],
                item=item.item_no,
                device_name=item.device_name,
                description=item.status_type,
                reason=res.note,
                false_alarm="N",
            )
        )
        added += 1
    _touch(report)
    db.session.commit()
    flash(f"已從檢查表帶入 {added} 筆異常項目。" if added else "沒有新的異常項目需要帶入。", "success")
    return redirect(url_for("editors.alarms", report_id=report.id))


@bp.route("/checklist")
def checklist_overview(report_id):
    report = get_report(report_id)
    return render_template(
        "checklist_overview.html", report=report, tab="checklist", stats=checklist_stats(report), sheets=SHEETS
    )


@bp.route("/checklist/<sheet>", methods=["GET", "POST"])
def checklist(report_id, sheet):
    report = get_report(report_id)
    if sheet not in SHEETS:
        abort(404)
    results = (
        report.results.join(ChecklistItem)
        .options(contains_eager(ChecklistResult.item))
        .filter(ChecklistItem.sheet == sheet)
        .order_by(ChecklistItem.order)
        .all()
    )
    if request.method == "POST":
        if (r := locked_redirect(report)) is not None:
            return r
        for res in results:
            value = request.form.get(f"r_{res.id}")
            res.result = value if value in ("N", "W", "F") else None
            res.note = request.form.get(f"n_{res.id}", "").strip()
        _touch(report)
        db.session.commit()
        missing = sum(1 for res in results if res.result is None)
        flash(f"已儲存。{'還有 %d 項未勾選。' % missing if missing else '本頁已全部勾選。'}", "success")
        return redirect(url_for("editors.checklist", report_id=report.id, sheet=sheet))

    # 依編號 (No.) 分組顯示，對應 Excel 的合併儲存格
    groups = [(no, list(rows)) for no, rows in groupby(results, key=lambda r: r.item.no)]
    return render_template(
        "edit_checklist.html",
        report=report,
        tab="checklist",
        sheet=sheet,
        sheet_title=SHEETS[sheet],
        sheets=SHEETS,
        groups=groups,
        show_location=sheet == "physical",
    )


@bp.route("/screenshots")
def screenshots(report_id):
    report = get_report(report_id)
    sections = ScreenshotSection.query.order_by(ScreenshotSection.order).all()
    by_section = {}
    for img in report.images:
        if img.kind == "screenshot":
            by_section.setdefault(img.section_id, []).append(img)
    return render_template(
        "edit_screenshots.html", report=report, tab="screenshots", sections=sections, by_section=by_section,
        all_sections=sections,
    )


# ---------------------------------------------------------------- 截圖批次上傳（暫存區 → 確認 → 一次放入）


def _batch_report(report_id):
    report = get_report(report_id)
    d = perm.can_edit(current_user, report)
    if not d:
        abort(403 if report.editable else 409, d.reason)
    return report


@bp.route("/screenshots/batch")
def shot_batch(report_id):
    from . import shot_batch as sb

    report = get_report(report_id)
    return render_template("edit_batch.html", report=report, tab="screenshots", r=sb.review(report),
                           names=sb.suggested_names(), can_edit=perm.can_edit(current_user, report))


@bp.route("/screenshots/batch/upload", methods=["POST"])
def shot_batch_upload(report_id):
    from flask import jsonify

    from . import shot_batch as sb

    report = _batch_report(report_id)
    files = [(f.filename or "", f.read()) for f in request.files.getlist("files")]
    return jsonify(results=sb.stage(report, files, current_user))


def _apply_assignments(report, form):
    """表單上每張暫存截圖的章節選擇；選了建議的章節記為「採用建議」。回傳變更張數。"""
    from . import shot_batch as sb

    n = 0
    for k, v in form.items():
        if not (k.startswith("sec_") and k[4:].isdigit()):
            continue
        cur = ReportImage.query.filter_by(id=int(k[4:]), report_id=report.id, kind="staged").first()
        if cur and str(cur.section_id or "") != v:
            src = "suggest" if v and form.get(f"sug_{k[4:]}") == v else "manual"
            n += sb.assign(report, cur.id, int(v) if v.isdigit() else None, src)
    return n


@bp.route("/screenshots/batch/assign", methods=["POST"])
def shot_batch_assign(report_id):
    from . import shot_batch as sb

    report = _batch_report(report_id)
    f = request.form
    n = _apply_assignments(report, f)
    if f.get("remove"):
        sb.remove(report, int(f["remove"]))
        msg = "已從暫存區移除 1 張。"
    elif f.get("discard") == "1":
        for img in ReportImage.query.filter_by(report_id=report.id, kind="staged").all():
            sb.remove(report, img.id)
        msg = "已清空暫存區。"
    else:
        msg = f"已更新 {n} 張的章節。" if n else "沒有變更。"
    db.session.commit()
    flash(msg, "success")
    return redirect(url_for("editors.shot_batch", report_id=report.id))


@bp.route("/screenshots/batch/commit", methods=["POST"])
def shot_batch_commit(report_id):
    from . import shot_batch as sb

    report = _batch_report(report_id)
    _apply_assignments(report, request.form)
    db.session.commit()
    ok, msg = sb.commit(report, current_user, confirm_counts=request.form.get("confirm_counts") == "1")
    flash(msg, "success" if ok else "error")
    return redirect(url_for("editors.screenshots" if ok else "editors.shot_batch", report_id=report.id))


@bp.route("/screenshots/batch/names.csv")
def shot_batch_names(report_id):
    """建議檔名清單（Excel 可直接開啟）。"""
    from flask import Response

    from . import shot_batch as sb

    get_report(report_id)
    rows = ["章節,名稱,建議檔名,同章節第 2 張"] + [
        f'{s.code},"{s.title}",{n},{n[:-4]}_2.png' for s, n in sb.suggested_names()]
    return Response("\ufeff" + "\r\n".join(rows) + "\r\n", mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="screenshot_filenames.csv"'})

