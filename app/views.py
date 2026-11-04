from datetime import date, datetime, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import db, docno
import os

from flask import current_app

from . import approval as perm
from .approval import approvers, pending_for
from .ai_providers import ai_enabled
from .check_report import render as render_check_md
from .check_report import snapshot
from .checks import run_checks
from .helpers import get_report
from .models import (
    ADMIN_ELIGIBLE_ROLES,
    ASSIGNABLE_ROLES,
    ROLE_LABELS,
    AlarmSummaryRow,
    ApprovalDelegation,
    AuditLog,
    CheckReport,
    ChecklistItem,
    ChecklistResult,
    IssueRecord,
    OnsiteRecord,
    Report,
    ReportImage,
    ReviewLog,
    SystemUpdateRecord,
    TrueAlarmRecord,
    User,
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
    pending, void_requests = pending_for(current_user)
    pending_checks = {r.id: run_checks(r, history=False).counts() for r in pending}
    return render_template(
        "index.html", reports=reports, month=month, pending=pending, void_requests=void_requests,
        pending_checks=pending_checks,
        today=date.today().isoformat(), can_create=perm.can_create(current_user),
        official=last_official(date.today() + timedelta(days=1)),
    )


def last_official(d):
    """d 之前最近一份已核准（正式）的日報。"""
    return (Report.query.filter(Report.report_date < d, Report.status == "approved")
            .order_by(Report.report_date.desc()).first())


def _copy_cols(obj, model, skip=("id", "report_id")):
    return {c.name: getattr(obj, c.name) for c in model.__table__.columns if c.name not in skip}


COPY_REPORT_FIELDS = ("issue_purpose", "inspection_time", "inspection_summary", "second_line_support", "onsite_summary",
                      "alarm_false")


def _copy_official(report, src):
    """複製正式日報的內容：封面、紀錄、告警、檢查表（另外處理）。不複製：日期、文件編號、版次、撰寫人、
    值班人員（今天的建立者）、送審 / 核准 / 作廢狀態與截圖。"""
    for f in COPY_REPORT_FIELDS:
        setattr(report, f, getattr(src, f))
    report.alarm_new = 0  # 內容與前一份相同時沒有新增；有新告警再改
    for rel, model in (("issues", IssueRecord), ("onsite_records", OnsiteRecord),
                       ("system_updates", SystemUpdateRecord), ("true_alarms", TrueAlarmRecord)):
        for row in getattr(src, rel):
            getattr(report, rel).append(model(**_copy_cols(row, model)))


def _copy_photos(report, src):
    """現場照片、告警照片連同檔案一起複製（截圖不複製）。"""
    import shutil
    import uuid

    root = current_app.config["UPLOAD_DIR"]
    os.makedirs(os.path.join(root, str(report.id)), exist_ok=True)
    for img in src.images:
        if img.kind == "screenshot":
            continue
        path = os.path.join(root, str(src.id), img.filename)
        if not os.path.isfile(path):
            continue
        name = f"{uuid.uuid4().hex}{os.path.splitext(img.filename)[1]}"
        shutil.copyfile(path, os.path.join(root, str(report.id), name))
        db.session.add(ReportImage(report_id=report.id, kind=img.kind, filename=name, original_name=img.original_name,
                                   order=img.order, uploaded_by_id=current_user.id))


@bp.route("/reports/new", methods=["POST"])
def create():
    if not perm.can_create(current_user):
        abort(403)
    try:
        d = datetime.strptime(request.form.get("report_date", ""), "%Y-%m-%d").date()
    except ValueError:
        flash("日期格式錯誤。", "error")
        return redirect(url_for("views.index"))

    existing = Report.query.filter_by(report_date=d).first()
    if existing:
        flash(f"{d} 的日報已經存在。", "error")
        return redirect(url_for("views.detail", report_id=existing.id))

    # 預設複製上一份已核准（正式）日報的全部內容，截圖除外；取消勾選時只沿用值班、核准人與已知告警
    shown = request.form.get("copy_shown") == "1"
    copy = request.form.get("copy_prev") == "1" if shown else True
    official = last_official(d) if copy else None
    prev = official or Report.query.filter(Report.report_date < d).order_by(Report.report_date.desc()).first()
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
        # 固定資料與已知告警清單；告警計數滾動：昨天的新增今天變成已知
        report.second_line_support = prev.second_line_support
        report.approver_id = prev.approver_id
        report.approver_name = prev.approver_name
        report.inspection_time = prev.inspection_time
        report.alarm_known = (prev.alarm_known or 0) + (prev.alarm_new or 0)
        for row in prev.alarm_rows:
            report.alarm_rows.append(AlarmSummaryRow(**_copy_cols(row, AlarmSummaryRow)))
    if official:
        _copy_official(report, official)
    db.session.add(report)
    db.session.flush()

    prev_results = {}
    if prev:
        q = db.session.query(ChecklistResult.item_id, ChecklistResult.result, ChecklistResult.note).filter(
            ChecklistResult.report_id == prev.id)
        prev_results = {i: (res if official else None, note or "") for i, res, note in q}
    for (item_id,) in db.session.query(ChecklistItem.id).filter_by(active=True):
        res, note = prev_results.get(item_id, (None, ""))
        db.session.add(ChecklistResult(report_id=report.id, item_id=item_id, result=res, note=note))
    if official:
        _copy_photos(report, official)
    db.session.commit()
    flash(f"已建立 {d} 日報 {report.doc_no}"
          + (f"，已複製 {official.report_date} 已核准日報的內容（截圖除外），請逐項確認今天的狀況。" if official else "。"),
          "success")
    return redirect(url_for("editors.basic", report_id=report.id))


@bp.route("/reports/<int:report_id>")
def detail(report_id):
    """完整日報檢視：嵌入與原始範本相同排版的 PDF，並提供送審/審核操作。"""
    report = get_report(report_id)
    u = current_user
    review = perm.can_review(u, report)
    snaps = report.check_reports.order_by(None).order_by(CheckReport.id.desc()).all()
    return render_template(
        "report_detail.html", report=report, can_review=review.allowed, principal=review.principal,
        checks=run_checks(report) if report.status != "voided" else None,
        snaps=snaps, ai_snap=next((s for s in snaps if s.ai_status), None),
        ai_enabled=ai_enabled(),
        can={
            "ai": _can_request_ai(u, report),
            "edit": perm.can_edit(u, report), "submit": perm.can_submit(u, report),
            "delete": perm.can_delete(u, report), "void": perm.can_void(u, report),
            "request_void": perm.can_request_void(u, report),
            "decide_void": perm.can_decide_void_request(u, report),
        },
    )


@bp.route("/reports/<int:report_id>/submit", methods=["POST"])
def submit(report_id):
    report = get_report(report_id)
    d = perm.can_submit(current_user, report)
    if not d:
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))

    missing = report.results.filter(ChecklistResult.result.is_(None)).count()
    if missing:
        flash(f"檢查表還有 {missing} 個項目未勾選，請全部勾選後再送審。", "error")
        return redirect(url_for("editors.checklist_overview", report_id=report.id))

    report.status = "submitted"
    report.submitted_at = datetime.now()
    report.submitted_by_id = current_user.id
    report.updated_by_id = current_user.id
    snapshot(report, "submit", current_user, ai=True)  # 檢查結果.md，並排入 AI 建議
    db.session.commit()
    flash("已送出審核。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/review", methods=["POST"])
def review(report_id):
    report = get_report(report_id)
    d = perm.can_review(current_user, report)
    if not d:
        own = d.reason.startswith("不能審核自己")  # 職責分離：撰寫人 / 送審人
        if current_user.role == "first_line" or (report.status == "submitted" and not own):
            abort(403)
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    principal = d.principal

    action = request.form.get("action")
    comment = request.form.get("comment", "").strip()
    if action not in ("approve", "reject"):
        abort(400)
    if action == "reject" and not comment:
        flash("退回時請填寫原因。", "error")
        return redirect(url_for("views.detail", report_id=report.id))

    signer = principal or current_user  # 文件上的簽核者：代簽時仍是核准人本人
    if report.approver_id is None:
        report.approver_id = signer.id
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
    # 審核當下的檢查結果與決定，留給日後 RAG（哪些問題會被退回）
    snapshot(report, action, current_user, review=(action, current_user, principal, comment))
    db.session.commit()
    flash(msg, "success")
    return redirect(url_for("views.detail", report_id=report.id))


def _can_request_ai(u, report):
    """AI 建議：撰寫中（送審前自我檢查）或送審中，可編輯或可審核的人才能要求重新產生。"""
    if not ai_enabled() or report.status not in ("draft", "rejected", "submitted"):
        return False
    return bool(perm.can_edit(u, report) or perm.can_review(u, report) or u.is_admin or u.role == "file_manager")


@bp.route("/reports/<int:report_id>/ai", methods=["POST"])
def request_ai(report_id):
    report = get_report(report_id)
    if not _can_request_ai(current_user, report):
        abort(403)
    snapshot(report, "manual", current_user, ai=True)
    db.session.commit()
    flash("已依目前的檢查結果重新產生檢查結果.md，AI 建議產生中。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/checks.md")
def checks_md(report_id):
    """下載檢查結果.md：指定快照（?snap=），或沒有指定時即時產生目前的檢查結果（不存檔）。"""
    report = get_report(report_id)
    sid = request.args.get("snap", type=int)
    if sid:
        snap = CheckReport.query.filter_by(id=sid, report_id=report.id).first_or_404()
        md, stamp = snap.markdown, f"{snap.stage}_{snap.created_at:%Y%m%d%H%M%S}"
    else:
        md, stamp = render_check_md(report, stage="manual"), "目前"
    from urllib.parse import quote

    name = f"{report.doc_no}_{stamp}_檢查結果.md"
    return Response(md, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---------------------------------------------------------------- 代簽設定


@bp.route("/delegations", methods=["GET", "POST"])
def delegations():
    """核准人設定代簽人；代簽人可看到自己被授權代誰簽核。"""
    if current_user.role == "first_line":
        abort(403)
    if request.method == "POST":
        if current_user.role != "file_manager":
            abort(403)
        delegate = db.session.get(User, request.form.get("delegate_id", type=int) or 0)
        try:
            start = datetime.strptime(request.form.get("start_date", ""), "%Y-%m-%d").date()
            end_raw = request.form.get("end_date", "").strip()
            end = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else None
        except ValueError:
            flash("日期格式錯誤。", "error")
            return redirect(url_for("views.delegations"))
        if not delegate or delegate.id == current_user.id or delegate.role != "second_line":
            flash("請選擇一位二線管理員作為代理人。", "error")
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

    q = ApprovalDelegation.query.order_by(ApprovalDelegation.id.desc())
    mine = q.all() if current_user.is_admin else q.filter_by(principal_id=current_user.id).all()
    granted = (ApprovalDelegation.query.filter_by(delegate_id=current_user.id)
               .order_by(ApprovalDelegation.id.desc()).all())
    users = User.query.filter_by(role="second_line").order_by(User.display_name, User.username).all()
    return render_template("delegations.html", mine=mine, granted=granted, users=users,
                           today=date.today().isoformat())


@bp.route("/delegations/<int:delegation_id>/revoke", methods=["POST"])
def revoke_delegation(delegation_id):
    d = db.session.get(ApprovalDelegation, delegation_id) or abort(404)
    if d.principal_id != current_user.id and not current_user.is_admin:
        abort(403)
    if d.revoked_at is None:
        d.revoked_at = datetime.now()
        db.session.commit()
        flash(f"已撤銷 {d.delegate.name} 的代簽權限。", "success")
    return redirect(url_for("views.delegations"))


# ---------------------------------------------------------------- 作廢 / 刪除


def _log(report, action, principal=None, comment=""):
    db.session.add(ReviewLog(report_id=report.id, action=action, actor_id=current_user.id,
                             on_behalf_of_id=principal.id if principal else None, comment=comment))


@bp.route("/reports/<int:report_id>/void", methods=["POST"])
def void(report_id):
    """作廢：保留日期與文件編號，內容唯讀。"""
    report = get_report(report_id)
    d = perm.can_void(current_user, report)
    if not d:
        abort(403) if current_user.role == "first_line" else flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    reason = request.form.get("reason", "").strip() or (report.void_request_reason or "")
    if not reason:
        flash("作廢時請填寫原因。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    _void(report, reason, d.principal)
    db.session.commit()
    flash(f"已作廢 {report.doc_no}，文件編號保留不再使用。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


def _void(report, reason, principal):
    report.status = "voided"
    report.void_reason = reason
    report.voided_at = datetime.now()
    report.voided_by_id = current_user.id
    report.updated_by_id = current_user.id
    _log(report, "void", principal, reason)


@bp.route("/reports/<int:report_id>/void-request", methods=["POST"])
def request_void(report_id):
    report = get_report(report_id)
    d = perm.can_request_void(current_user, report)
    if not d:
        flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("申請作廢請填寫原因。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    report.void_request_reason = reason
    report.void_requested_at = datetime.now()
    report.void_requested_by_id = current_user.id
    _log(report, "void_request", None, reason)
    db.session.commit()
    flash("已送出作廢申請，待檔案管理員核定。", "success")
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/void-request/decide", methods=["POST"])
def decide_void(report_id):
    report = get_report(report_id)
    d = perm.can_decide_void_request(current_user, report)
    if not d:
        abort(403) if current_user.role == "first_line" else flash(d.reason, "error")
        return redirect(url_for("views.detail", report_id=report.id))
    comment = request.form.get("comment", "").strip()
    if request.form.get("action") == "approve":
        _void(report, report.void_request_reason + (f"（{comment}）" if comment else ""), d.principal)
        flash(f"已核定作廢 {report.doc_no}。", "success")
    else:
        _log(report, "void_request_reject", d.principal, comment)
        flash("已駁回作廢申請。", "success")
    report.void_requested_at = None
    db.session.commit()
    return redirect(url_for("views.detail", report_id=report.id))


@bp.route("/reports/<int:report_id>/delete", methods=["POST"])
def delete(report_id):
    """刪除日報（含截圖檔）。刪除後該日期可重新建立；要保留編號請改用作廢。"""
    report = get_report(report_id)
    if not perm.can_delete(current_user, report):
        abort(403)
    if request.form.get("confirm_doc_no", "").strip() != report.doc_no:
        flash("請輸入完整文件編號以確認刪除。", "error")
        return redirect(url_for("views.detail", report_id=report.id))
    folder = os.path.join(current_app.config["UPLOAD_DIR"], str(report.id))
    db.session.add(AuditLog(action="delete_report", actor_id=current_user.id, target=report.doc_no,
                            detail=f"{report.report_date} {report.status_label}"))
    for log in report.review_logs:
        db.session.delete(log)
    db.session.delete(report)
    db.session.commit()
    if os.path.isdir(folder):
        import shutil

        shutil.rmtree(folder, ignore_errors=True)
    flash(f"已刪除 {report.doc_no}。", "success")
    return redirect(url_for("views.index"))


# ---------------------------------------------------------------- 帳號管理（系統管理員）


@bp.route("/admin/users", methods=["GET", "POST"])
def users():
    if not perm.can_manage_users(current_user):
        abort(403)
    if request.method == "POST" and request.form.get("sys_admin") is not None:
        return _set_sys_admin()
    if request.method == "POST":
        user = db.session.get(User, request.form.get("user_id", type=int) or 0) or abort(404)
        role = request.form.get("role")
        if user.id == current_user.id and user.is_admin and role not in ADMIN_ELIGIBLE_ROLES:
            flash("不能把自己改成一線使用者（會失去系統管理員權限）。請由其他系統管理員操作。", "error")
            return redirect(url_for("views.users"))
        if user.role == "admin" or role not in ASSIGNABLE_ROLES:
            abort(400)
        if user.role != role:
            db.session.add(AuditLog(action="set_role", actor_id=current_user.id, target=user.username,
                                    detail=f"{ROLE_LABELS.get(user.role)} → {ROLE_LABELS[role]}"))
            was_admin = user.is_admin
            user.set_role(role)
            if was_admin and not user.is_admin:
                db.session.add(AuditLog(action="set_admin", actor_id=current_user.id, target=user.username,
                                        detail="改為一線使用者，系統管理員權限自動移除"))
            if role != "second_line":
                # 不再是二線管理員：撤銷其所有代理授權
                for dl in ApprovalDelegation.query.filter_by(delegate_id=user.id, revoked_at=None):
                    dl.revoked_at = datetime.now()
            db.session.commit()
            flash(f"{user.name} 已設為{ROLE_LABELS[role]}。", "success")
        return redirect(url_for("views.users"))
    from . import admin_views as adm

    all_users = User.query.order_by(User.role, User.display_name, User.username).all()
    return render_template("users.html", tab="users", users=all_users, roles=ASSIGNABLE_ROLES, role_labels=ROLE_LABELS,
                           logs=adm.recent_audit(), audit_labels=adm.AUDIT_LABELS, admin_roles=ADMIN_ELIGIBLE_ROLES,
                           local_accounts=adm.local_accounts(), min_pw=adm.MIN_PASSWORD)


def _set_sys_admin():
    """指派 / 移除系統管理員。一線使用者強制排除；不能移除自己的權限（避免把自己鎖在外面）。"""
    user = db.session.get(User, request.form.get("user_id", type=int) or 0) or abort(404)
    on = request.form.get("sys_admin") == "1"
    if user.is_local_admin:
        abort(400)
    if on and user.role not in ADMIN_ELIGIBLE_ROLES:
        flash("一線使用者不能成為系統管理員，請先改為檔案管理員或二線管理員。", "error")
        return redirect(url_for("views.users"))
    if not on and user.id == current_user.id:
        flash("不能移除自己的系統管理員權限，請由其他系統管理員操作。", "error")
        return redirect(url_for("views.users"))
    if bool(user.sys_admin) != on:
        user.sys_admin = on
        db.session.add(AuditLog(action="set_admin", actor_id=current_user.id, target=user.username,
                                detail="指派為系統管理員" if on else "移除系統管理員"))
        db.session.commit()
        flash(f"{user.name} {'已指派為' if on else '已移除'}系統管理員。", "success")
    return redirect(url_for("views.users"))


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
