"""角色權限與代理（代簽）判斷。所有畫面與路由都透過這裡決定能做什麼。

角色：
  系統管理員 admin         所有動作（本機帳號，平時不使用）
  檔案管理員 file_manager  指派、審核、退件、送審、新增、編輯、刪除
  二線管理員 second_line   送審、新增、編輯；經檔案管理員授權後可「代」審核 / 指派 / 退件 / 刪除
  一線使用者 first_line    送審、新增、編輯
  檢視者     viewer        只能下載已結案日報的正式 PDF

流程：草稿 → 送審 → 核准（待結案，線上預覽確認排版）→ 結案（鎖檔，1 分鐘後產生正式 PDF、上傳 SharePoint、寄給客戶）。
二線以上可以結案、在結案前退回修正、在發送失敗時重試；結案後只有檔案管理員能在發送開始前取消結案。

代理（二線代檔案管理員）時，文件上的 Approval / Document Approved 仍顯示檔案管理員本人。
"""

from dataclasses import dataclass
from datetime import date

from . import db
from .models import REVIEWER_ROLES, ApprovalDelegation, Report, User


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
    """檔案管理員類動作（審核 / 退件 / 指派 / 刪除）。
    assigned_only=True：指定了核准人時，只有該核准人或其代理人可以做（審核、退件）。"""
    if user.is_admin:
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


def _reviewer(user):
    """二線以上（二線、檔案管理員）或系統管理員。"""
    return user.is_admin or user.role in REVIEWER_ROLES


def can_create(user):
    return Decision(user.is_admin or user.role in ("admin", "file_manager", "second_line", "first_line"))


def can_edit(user, report):
    if not report.editable:
        return Decision(False, None, f"此日報目前為「{report.status_label}」，不能修改。")
    return can_create(user)


def can_delete(user, report):
    """刪除（釋放文件編號）：系統管理員、檔案管理員；二線管理員需經檔案管理員授權代理。已結案的不能刪除。"""
    if report.status == "closed":
        return Decision(False, None, "已結案的日報不能刪除。")
    return _manager_action(user, report, False, "刪除")


def can_submit(user, report):
    if not report.editable:
        return Decision(False, None, "此日報目前不能送審。")
    # 送審不涉及核准權限，所有可編輯日報的角色都可以送審；送審者不能核准自己送審的日報（見 can_review）
    return Decision(user.is_admin or user.role in ("admin", "file_manager", "second_line", "first_line"))


def can_review(user, report):
    """核准 / 退件。"""
    if report.status != "submitted":
        return Decision(False, None, "此日報不是待審核狀態。")
    # 職責分離：撰寫人、送審人不能審核自己的日報；系統管理員擁有所有權限，不受此限制
    if report.author_id == user.id and not user.is_admin:
        return Decision(False, None, "不能審核自己撰寫的日報。")
    if report.submitted_by_id == user.id and not user.is_admin:
        return Decision(False, None, "不能審核自己送審的日報。")
    return _manager_action(user, report, True, "核准")


def can_assign(user, report):
    """指派撰寫人與核准人（草稿 / 退回時）。"""
    if not report.editable:
        return Decision(False, None, "只能在草稿或退回狀態指派。")
    return _manager_action(user, report, False, "指派")


def can_override_doc_no(user, report):
    """Document No. / Contractor Doc No. 覆寫：二線管理員以上（二線、檔案管理員、本機系統管理員），草稿或退回時。"""
    if not report.editable:
        return Decision(False, None, "只能在草稿或退回狀態變更文件編號。")
    return Decision(_reviewer(user), None, "只有二線管理員以上可以變更文件編號。")


def can_close(user, report):
    """結案：核准後、線上確認排版沒有問題，二線以上執行。"""
    if report.status != "approved":
        return Decision(False, None, "只有已核准的日報可以結案。")
    return Decision(_reviewer(user), None, "只有二線管理員以上可以結案。")


def can_return(user, report):
    """結案前發現內容或排版有問題：退回給撰寫人修正，修正後重新送審。"""
    if report.status != "approved":
        return Decision(False, None, "只有已核准、尚未結案的日報可以退回修正。")
    return Decision(_reviewer(user), None, "只有二線管理員以上可以退回修正。")


def can_cancel_close(user, report):
    """結案後只有檔案管理員能變動，而且只能在發送開始前（鎖檔的 1 分鐘內）取消。"""
    d = report.delivery
    if report.status != "closed" or not d:
        return Decision(False, None, "此日報尚未結案。")
    if d.state != "scheduled":
        return Decision(False, None, "發送流程已經開始，不能取消結案。")
    return Decision(user.is_admin or user.role in ("admin", "file_manager"), None, "結案後只有檔案管理員可以取消。")


def can_retry_delivery(user, report):
    d = report.delivery
    if report.status != "closed" or not d or d.state != "error":
        return Decision(False, None, "發送流程沒有中止，不需要重試。")
    return Decision(_reviewer(user), None, "只有二線管理員以上可以重試。")


def can_manage_delegations(user):
    return user.is_admin or user.role == "file_manager"


def can_manage_users(user):
    """調整角色、指派系統管理員、本機帳號：只有系統管理員。"""
    return user.is_admin


def can_manage_settings(user):
    """後端系統設定：只有系統管理員。"""
    return user.is_admin


# ---------------------------------------------------------------- 清單


def pending_for(user):
    """待 user 審核的日報。"""
    if not _reviewer(user):
        return []
    return [r for r in Report.query.filter_by(status="submitted").order_by(Report.report_date)
            if can_review(user, r)]


def review_role(user, report):
    """相容舊介面：(可否審核, 代簽的核准人, 原因)。"""
    d = can_review(user, report)
    return d.allowed, d.principal, d.reason


# ---------------------------------------------------------------- 權限表（帳號管理頁顯示；tests 逐項比對實際判斷）

MATRIX_ROLES = (("sysadmin", "系統管理員"), ("file_manager", "檔案管理員"), ("second_line", "二線管理員"),
                ("first_line", "一線使用者"), ("viewer", "檢視者"))
# y = 可以（✔）；n = 不可以（✘）；d = 經檔案管理員授權代理時可以（✔*）
PERMISSION_MATRIX = (
    ("日報", (
        ("新增、編輯（草稿 / 退回）、送審", "yyyyn"),
        ("截圖上傳（含批次上傳）", "yyyyn"),
        ("線上預覽 PDF", "yyyyn"),
        ("指派撰寫人、核准人", "yydnn"),
        ("覆寫文件編號", "yyynn"),
        ("審核：核准 / 退回", "yydnn"),
        ("審核自己撰寫或送審的日報", "ynnnn"),
        ("結案、結案前退回修正", "yyynn"),
        ("取消結案（發送開始前）", "yynnn"),
        ("發送中止時重試", "yyynn"),
        ("刪除日報（未結案；刪除後文件編號可再使用）", "yydnn"),
        ("授權二線管理員代理", "yynnn"),
        ("下載已結案的正式 PDF", "yyyyy"),
    )),
    ("系統", (
        ("調整角色、指派系統管理員", "ynnnn"),
        ("本機緊急帳號（新增、改密碼、停用）", "ynnnn"),
        ("系統設定（AD、備份還原、健康檢查、PDF 範本、HTTPS、發送與提醒、AI）", "ynnnn"),
        ("稽核紀錄", "ynnnn"),
    )),
)
MATRIX_NOTES = (
    "系統管理員：本機緊急帳號，或在「帳號管理」被指派的檔案管理員 / 二線管理員。擁有所有權限，不受職責分離限制。",
    "✔* = 經檔案管理員授權代理時可以（右上角「代理設定」），文件上仍顯示核准人本人。",
    "檔案管理員、二線管理員不能審核自己撰寫或送審的日報（職責分離）。",
    "只有系統管理員能調整角色與後端系統設定。已結案的日報任何人都不能修改或刪除。",
)

