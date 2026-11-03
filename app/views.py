from datetime import date, datetime

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import db, docno
from .approval import active_principals, approvers, pending_for, review_role
from .helpers import get_report
from .models import AlarmSummaryRow, ApprovalDelegation, ChecklistItem, ChecklistResult, Report, ReviewLog, User

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
    return render_template(
        "index.html", reports=reports, month=month, pending=pending_for(current_user), today=date.today().isoformat()
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
    if not prev or (prev.approver_id is None and not prev.approver_name):
        only = approvers()
        if len(only) == 1:  # 只有一位核准人時預設指定給他
            report.approver_id, report.approver_name = only[0].id, only[0].name
    if prev:
        # 沿用前一份日報的固定資料、已知告警清單與檢查表備註；檢查結果每天重新勾選
        report.second_line_support = prev.second_line_support
        report.approver_id = prev.approver_id
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
    """完整日報檢視：嵌入與原始範本相同排版的 PDF，並提供送審/審核操作。"""
    report = get_report(report_id)
    can_review, principal, _ = review_role(current_user, report)
    return render_template("report_detail.html", report=report, can_review=can_review, principal=principal)


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
    allowed, principal, reason = review_role(current_user, report)
    if not allowed:
        # 沒有任何審核身分（非審核者、也不是任何人的代簽人），或日報不歸他審核：403
        no_role = not current_user.is_approver and not active_principals(current_user)
        if no_role or (report.status == "submitted" and report.author_id != current_user.id):
            abort(403)
        flash(reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))

    action = request.form.get("action")
    comment = request.form.get("comment", "").strip()
    if action not in ("approve", "reject"):
        abort(400)
    if action == "reject" and not comment:
        flash("退回時請填寫原因。", "error")
        return redirect(url_for("views.detail", report_id=report.id))

    signer = principal or current_user  # 文件上的簽核者：代簽時仍是核准人本人
    if report.approver_id is None:
        report.approver_id = current_user.id
    if action == "approve":
        report.status = "approved"
        report.approver_name = signer.name
        msg = "已核准" + (f"（代 {signer.name} 簽核，文件顯示為 {signer.name}）。" if principal else "。")
    else:
        report.status = "rejected"
        msg = "已退回給撰寫人" + (f"（代 {signer.name}）。" if principal else "。")
    report.reviewed_by_id = current_user.id
    report.reviewed_on_behalf_of_id = principal.id if principal else None
    report.reviewed_at = datetime.now()
    report.review_comment = comment
    db.session.add(ReviewLog(report_id=report.id, action=action, actor_id=current_user.id,
                             on_behalf_of_id=principal.id if principal else None, comment=comment))
    db.session.commit()
    flash(msg, "success")
    return redirect(url_for("views.detail", report_id=report.id))


# ---------------------------------------------------------------- 代簽設定


@bp.route("/delegations", methods=["GET", "POST"])
def delegations():
    """核准人設定代簽人；代簽人可看到自己被授權代誰簽核。"""
    if request.method == "POST":
        if not current_user.is_approver:
            abort(403)
        delegate = db.session.get(User, request.form.get("delegate_id", type=int) or 0)
        try:
            start = datetime.strptime(request.form.get("start_date", ""), "%Y-%m-%d").date()
            end_raw = request.form.get("end_date", "").strip()
            end = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else None
        except ValueError:
            flash("日期格式錯誤。", "error")
            return redirect(url_for("views.delegations"))
        if not delegate or delegate.id == current_user.id:
            flash("請選擇其他帳號作為代簽人。", "error")
        elif end and end < start:
            flash("結束日期不能早於開始日期。", "error")
        else:
            db.session.add(ApprovalDelegation(
                principal_id=current_user.id, delegate_id=delegate.id, start_date=start, end_date=end,
                note=request.form.get("note", "").strip()[:256], created_by_id=current_user.id,
            ))
            db.session.commit()
            flash(f"已授權 {delegate.name} 代簽（{start}～{end or '撤銷為止'}）。文件上仍顯示 {current_user.name}。",
                  "success")
        return redirect(url_for("views.delegations"))

    mine = (ApprovalDelegation.query.filter_by(principal_id=current_user.id)
            .order_by(ApprovalDelegation.id.desc()).all())
    granted = (ApprovalDelegation.query.filter_by(delegate_id=current_user.id)
               .order_by(ApprovalDelegation.id.desc()).all())
    users = User.query.filter(User.id != current_user.id).order_by(User.display_name, User.username).all()
    return render_template("delegations.html", mine=mine, granted=granted, users=users,
                           today=date.today().isoformat())


@bp.route("/delegations/<int:delegation_id>/revoke", methods=["POST"])
def revoke_delegation(delegation_id):
    d = db.session.get(ApprovalDelegation, delegation_id) or abort(404)
    if d.principal_id != current_user.id:
        abort(403)
    if d.revoked_at is None:
        d.revoked_at = datetime.now()
        db.session.commit()
        flash(f"已撤銷 {d.delegate.name} 的代簽權限。", "success")
    return redirect(url_for("views.delegations"))


@bp.route("/reports/<int:report_id>/pdf")
def pdf(report_id):
    """與原始範本相同排版的完整日報 PDF。?download=1 會以附件下載。"""
    from .pdf.report_pdf import TemplateMissing, build_pdf, pdf_filename

    report = get_report(report_id)
    try:
        data = build_pdf(report)
    except TemplateMissing as e:
        return Response(str(e), status=503, mimetype="text/plain; charset=utf-8")
    disposition = "attachment" if request.args.get("download") else "inline"
    return Response(
        data,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{pdf_filename(report)}"'},
    )
