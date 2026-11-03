"""角色權限與代理（代簽）判斷。所有畫面與路由都透過這裡決定能做什麼。

角色：
  系統管理員 admin         所有動作（本機帳號，平時不使用）
  檔案管理員 file_manager  指派、審核、退件、作廢、新增、編輯、刪除
  二線管理員 second_line   新增、編輯、刪除；經檔案管理員授權後可「代」審核 / 指派 / 退件 / 作廢
  一線使用者 first_line    送審、新增、編輯、申請作廢

代理（二線代檔案管理員）時，文件上的 Approval / Document Approved 仍顯示檔案管理員本人。
"""

from dataclasses import dataclass
from datetime import date

from . import db
from .models import ApprovalDelegation, Report, User


@dataclass
class Decision:
    allowed: bool
    principal: User | None = None  # 代理時：被代理的檔案管理員
    reason: str = ""

    def __bool__(self):
        return self.allowed


DENY = Decision(False, None, "您沒有權限執行此動作。")


def approvers():
    """可被指定為核准人的帳號（檔案管理員）。"""
    return User.query.filter_by(role="file_manager").order_by(User.display_name, User.username).all()


def active_principals(user, day=None):
    """二線管理員目前可代理的檔案管理員 id。"""
    if user.role != "second_line":
        return set()
    day = day or date.today()
    rows = ApprovalDelegation.query.filter_by(delegate_id=user.id, revoked_at=None).all()
    return {d.principal_id for d in rows if d.active_on(day) and d.principal and d.principal.role == "file_manager"}


def _principal_for(user, report, assigned_only):
    """二線管理員代理時要代表哪位檔案管理員；沒有適用的授權回傳 None。"""
    principals = active_principals(user)
    if not principals:
        return None
    if report.approver_id in principals:
        return report.approver
    if report.approver_id is None or not assigned_only:
        return db.session.get(User, sorted(principals)[0])
    return None


def _manager_action(user, report, assigned_only, what):
    """檔案管理員類動作（審核 / 退件 / 作廢 / 指派 / 核定作廢申請）。
    assigned_only=True：指定了核准人時，只有該核准人或其代理人可以做（審核、退件）。"""
    if user.role == "admin":
        # 系統管理員可做任何事；文件上的簽核者仍是指定的核准人
        principal = report.approver if report.approver_id and report.approver_id != user.id else None
        return Decision(True, principal)
    if user.role == "file_manager":
        if assigned_only and report.approver_id not in (None, user.id):
            name = report.approver_name or report.approver.name
            return Decision(False, None, f"此日報指定由 {name} {what}。")
        return Decision(True, None)
    if user.role == "second_line":
        principal = _principal_for(user, report, assigned_only)
        if principal:
            return Decision(True, principal)
        return Decision(False, None, "需要檔案管理員授權代理才能執行此動作。")
    return DENY


# ---------------------------------------------------------------- 各動作


def can_create(user):
    return Decision(user.role in ("admin", "file_manager", "second_line", "first_line"))


def can_edit(user, report):
    if not report.editable:
        return Decision(False, None, f"此日報目前為「{report.status_label}」，不能修改。")
    return can_create(user)


def can_delete(user, report):
    return Decision(user.role in ("admin", "file_manager", "second_line"))


def can_submit(user, report):
    if not report.editable:
        return Decision(False, None, "此日報目前不能送審。")
    return Decision(user.role in ("admin", "first_line"), None, "只有一線使用者可以送審。")


def can_review(user, report):
    """核准 / 退件。"""
    if report.status != "submitted":
        return Decision(False, None, "此日報不是待審核狀態。")
    if report.author_id == user.id and user.role != "admin":
        return Decision(False, None, "不能審核自己撰寫的日報。")
    return _manager_action(user, report, True, "核准")


def can_assign(user, report):
    """指派撰寫人與核准人（草稿 / 退回時）。"""
    if not report.editable:
        return Decision(False, None, "只能在草稿或退回狀態指派。")
    return _manager_action(user, report, False, "指派")


def can_void(user, report):
    if report.status == "voided":
        return Decision(False, None, "此日報已作廢。")
    return _manager_action(user, report, False, "作廢")


def can_request_void(user, report):
    if report.status == "voided":
        return Decision(False, None, "此日報已作廢。")
    if report.void_requested_at:
        return Decision(False, None, "已有待處理的作廢申請。")
    return Decision(user.role in ("admin", "first_line"), None, "只有一線使用者需要申請作廢。")


def can_decide_void_request(user, report):
    if not report.void_requested_at or report.status == "voided":
        return Decision(False, None, "沒有待處理的作廢申請。")
    return can_void(user, report)


def can_manage_delegations(user):
    return user.role in ("admin", "file_manager")


def can_manage_users(user):
    return user.role == "admin"


# ---------------------------------------------------------------- 清單


def pending_for(user):
    """待 user 處理：可審核的日報與可核定的作廢申請。"""
    if user.role not in ("admin", "file_manager", "second_line"):
        return [], []
    reviews = [r for r in Report.query.filter_by(status="submitted").order_by(Report.report_date)
               if can_review(user, r)]
    voids = [r for r in Report.query.filter(Report.void_requested_at.isnot(None), Report.status != "voided")
             .order_by(Report.report_date) if can_decide_void_request(user, r)]
    return reviews, voids


def review_role(user, report):
    """相容舊介面：(可否審核, 代簽的核准人, 原因)。"""
    d = can_review(user, report)
    return d.allowed, d.principal, d.reason
