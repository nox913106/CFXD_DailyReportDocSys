"""核准與代簽的權限判斷。"""

from datetime import date

from .models import ApprovalDelegation, Report, User


def approvers():
    return User.query.filter_by(is_approver=True).order_by(User.display_name, User.username).all()


def active_principals(user, day=None):
    """user 目前可代簽的核准人（id 集合）。"""
    day = day or date.today()
    rows = ApprovalDelegation.query.filter_by(delegate_id=user.id, revoked_at=None).all()
    return {d.principal_id for d in rows if d.active_on(day)}


def review_role(user, report):
    """回傳 (可否審核, 代簽的核准人 User 或 None, 不可審核的原因)。"""
    if report.status != "submitted":
        return False, None, "此日報不是待審核狀態。"
    if report.author_id == user.id:
        return False, None, "不能審核自己撰寫的日報。"
    if report.approver_id is None:
        # 未指定核准人：任何審核者都可以核准，並成為核准人
        if user.is_approver:
            return True, None, ""
        return False, None, "您沒有審核權限。"
    if report.approver_id == user.id:
        return True, None, ""
    if report.approver_id in active_principals(user):
        return True, report.approver, ""
    return False, None, f"此日報指定由 {report.approver_name or report.approver.name} 核准，您沒有代簽權限。"


def pending_for(user):
    """待 user 審核（含代簽）的日報。"""
    principals = active_principals(user) | {user.id}
    q = Report.query.filter(Report.status == "submitted", Report.author_id != user.id)
    out = []
    for r in q.order_by(Report.report_date).all():
        if r.approver_id in principals or (r.approver_id is None and user.is_approver):
            out.append(r)
    return out
