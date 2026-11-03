from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import db, docno
from .helpers import SHEETS, checklist_stats, get_report
from .models import (
    AlarmSummaryRow,
    ChecklistItem,
    ChecklistResult,
    Report,
    ScreenshotSection,
)

bp = Blueprint("views", __name__)


@bp.before_request
@login_required
def _require_login():
    pass


@bp.route("/")
def index():
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    try:
        first = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        abort(400)
    next_first = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    reports = (
        Report.query.filter(Report.report_date >= first, Report.report_date < next_first)
        .order_by(Report.report_date.desc())
        .all()
    )
    pending = []
    if current_user.is_approver:
        pending = Report.query.filter_by(status="submitted").order_by(Report.report_date).all()
    return render_template(
        "index.html", reports=reports, month=month, pending=pending, today=date.today().isoformat()
    )


@bp.route("/reports/new", methods=["POST"])
def create():
    try:
        d = datetime.strptime(request.form.get("report_date", ""), "%Y-%m-%d").date()
    except ValueError:
        flash("日期格式錯誤。", "error")
        return redirect(url_for("views.index"))

    existing = Report.query.filter_by(report_date=d).first()
    if existing:
        flash(f"{d} 的日報已經存在。", "error")
        return redirect(url_for("views.detail", report_id=existing.id))

    prev = Report.query.filter(Report.report_date < d).order_by(Report.report_date.desc()).first()
    report = Report(
        report_date=d,
        doc_no=docno.doc_no(d),
        contractor_doc_no=docno.contractor_doc_no(d),
        author_id=current_user.id,
        updated_by_id=current_user.id,
        first_line_officer=current_user.name,
    )
    if prev:
        # 沿用前一份日報的固定資料、已知告警清單與檢查表備註；檢查結果每天重新勾選
        report.second_line_support = prev.second_line_support
        report.approver_name = prev.approver_name
        report.inspection_time = prev.inspection_time
        report.alarm_known = (prev.alarm_known or 0) + (prev.alarm_new or 0)
        for row in prev.alarm_rows:
            report.alarm_rows.append(
                AlarmSummaryRow(
                    order=row.order,
                    category=row.category,
                    item=row.item,
                    device_name=row.device_name,
                    description=row.description,
                    reason=row.reason,
                    false_alarm=row.false_alarm,
                    note=row.note,
                )
            )
    db.session.add(report)
    db.session.flush()

    prev_notes = {}
    if prev:
        prev_notes = dict(
            db.session.query(ChecklistResult.item_id, ChecklistResult.note)
            .filter(ChecklistResult.report_id == prev.id, ChecklistResult.note != "")
            .all()
        )
    for (item_id,) in db.session.query(ChecklistItem.id).filter_by(active=True):
        db.session.add(ChecklistResult(report_id=report.id, item_id=item_id, note=prev_notes.get(item_id, "")))
    db.session.commit()
    flash(f"已建立 {d} 日報 {report.doc_no}。", "success")
    return redirect(url_for("editors.basic", report_id=report.id))


@bp.route("/reports/<int:report_id>")
def detail(report_id):
    """完整日報檢視（審核用，也是之後 PDF 排版的基礎）。"""
    report = get_report(report_id)
    month_start = report.report_date.replace(day=1)
    control = (
        Report.query.filter(Report.report_date >= month_start, Report.report_date <= report.report_date)
        .order_by(Report.report_date)
        .all()
    )
    abnormal = (
        ChecklistResult.query.filter(
            ChecklistResult.report_id == report.id, ChecklistResult.result.in_(["W", "F"])
        )
        .join(ChecklistItem)
        .order_by(ChecklistItem.sheet, ChecklistItem.order)
        .all()
    )
    sections = ScreenshotSection.query.order_by(ScreenshotSection.order).all()
    images_by_section = {}
    for img in report.images:
        if img.kind == "screenshot":
            images_by_section.setdefault(img.section_id, []).append(img)
    return render_template(
        "report_detail.html",
        report=report,
        control=control,
        stats=checklist_stats(report),
        sheets=SHEETS,
        abnormal=abnormal,
        sections=sections,
        images_by_section=images_by_section,
        onsite_images=[i for i in report.images if i.kind == "onsite"],
        alarm_images=[i for i in report.images if i.kind == "alarm"],
    )


@bp.route("/reports/<int:report_id>/submit", methods=["POST"])
def submit(report_id):
    report = get_report(report_id)
    if not report.editable:
        flash("此日報目前不能送審。", "error")
        return redirect(url_for("views.detail", report_id=report.id))

    missing = report.results.filter(ChecklistResult.result.is_(None)).count()
    if missing:
        flash(f"檢查表還有 {missing} 個項目未勾選，請全部勾選後再送審。", "error")
        return redirect(url_for("editors.checklist_overview", report_id=report.id))

    report.status = "submitted"
    report.submitted_at = datetime.now()
    report.updated_by_id = current_user.id
    db.session.commit()
    flash("已送出審核。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/review", methods=["POST"])
def review(report_id):
    report = get_report(report_id)
    if not current_user.is_approver:
        abort(403)
    if report.status != "submitted":
        flash("此日報不是待審核狀態。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    if report.author_id == current_user.id:
        flash("不能審核自己撰寫的日報。", "error")
        return redirect(url_for("views.detail", report_id=report.id))

    action = request.form.get("action")
    comment = request.form.get("comment", "").strip()
    if action == "approve":
        report.status = "approved"
        report.approver_name = current_user.name
        flash("已核准。", "success")
    elif action == "reject":
        if not comment:
            flash("退回時請填寫原因。", "error")
            return redirect(url_for("views.detail", report_id=report.id))
        report.status = "rejected"
        flash("已退回給撰寫人。", "success")
    else:
        abort(400)
    report.reviewed_by_id = current_user.id
    report.reviewed_at = datetime.now()
    report.review_comment = comment
    db.session.commit()
    return redirect(url_for("views.detail", report_id=report.id))
