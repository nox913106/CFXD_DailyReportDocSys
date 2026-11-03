from datetime import datetime

from flask_login import UserMixin

from . import db, login_manager


class User(UserMixin, db.Model):
    """AD 帳號首次登入時建立，之後每次登入更新顯示名稱、Email 與審核權限。"""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(128), unique=True, nullable=False)
    display_name = db.Column(db.String(256))
    email = db.Column(db.String(256))
    is_approver = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)

    @property
    def name(self):
        return self.display_name or self.username


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------- 主檔（由 app/seed 匯入） ----------


class ChecklistItem(db.Model):
    """SCADA Daily Inspection CheckList 的一個檢查項目。"""

    id = db.Column(db.Integer, primary_key=True)
    sheet = db.Column(db.String(16), nullable=False, index=True)  # vm / software / physical / pi
    order = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(128), default="")  # 圓餅圖統計分類（如 CCTV、UTM）
    no = db.Column(db.String(16), default="")
    device_type = db.Column(db.String(128), default="")
    group_item = db.Column(db.String(16), default="")
    device_name = db.Column(db.String(128), default="")
    item_no = db.Column(db.String(16), nullable=False)
    location = db.Column(db.String(32), default="")
    status_type = db.Column(db.String(128), default="")
    description = db.Column(db.Text, default="")
    active = db.Column(db.Boolean, default=True, nullable=False)

    __table_args__ = (db.UniqueConstraint("sheet", "item_no"),)


class ScreenshotSection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, nullable=False)  # 例：4.1
    title = db.Column(db.String(128), nullable=False)
    order = db.Column(db.Integer, nullable=False)

    @property
    def level(self):
        return self.code.count(".") + 1


# ---------- 日報 ----------

STATUS_LABELS = {
    "draft": "草稿",
    "submitted": "待審核",
    "approved": "已核准",
    "rejected": "退回",
}

RESULT_LABELS = {"N": "Normal", "W": "Warning", "F": "Failed"}


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_date = db.Column(db.Date, unique=True, nullable=False)
    doc_no = db.Column(db.String(64), nullable=False)
    contractor_doc_no = db.Column(db.String(64), nullable=False)
    revision = db.Column(db.Integer, default=0, nullable=False)
    issue_purpose = db.Column(db.String(64), default="IFI - Issue for Information")
    inspection_time = db.Column(db.String(16), default="10:00 AM")

    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    author = db.relationship("User", foreign_keys=[author_id])
    approver_name = db.Column(db.String(128), default="")  # 封面 Approval 欄顯示的名字

    status = db.Column(db.String(16), default="draft", nullable=False, index=True)
    submitted_at = db.Column(db.DateTime)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])
    reviewed_at = db.Column(db.DateTime)
    review_comment = db.Column(db.Text, default="")

    # 2 Inspection Record / Shift Record
    inspection_summary = db.Column(db.Text, default="There was no issue record reported today.")
    first_line_officer = db.Column(db.String(128), default="")
    second_line_support = db.Column(db.String(256), default="")

    # 3 On-site Record / System Update Record
    onsite_summary = db.Column(
        db.Text, default="There was no on-site work or system update performed today."
    )

    # 4 Daily Alarm Inspection
    alarm_new = db.Column(db.Integer, default=0)
    alarm_known = db.Column(db.Integer, default=0)
    alarm_false = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])

    issues = db.relationship("IssueRecord", cascade="all, delete-orphan", order_by="IssueRecord.id")
    onsite_records = db.relationship("OnsiteRecord", cascade="all, delete-orphan", order_by="OnsiteRecord.id")
    system_updates = db.relationship(
        "SystemUpdateRecord", cascade="all, delete-orphan", order_by="SystemUpdateRecord.id"
    )
    true_alarms = db.relationship("TrueAlarmRecord", cascade="all, delete-orphan", order_by="TrueAlarmRecord.id")
    alarm_rows = db.relationship("AlarmSummaryRow", cascade="all, delete-orphan", order_by="AlarmSummaryRow.order")
    results = db.relationship("ChecklistResult", cascade="all, delete-orphan", lazy="dynamic")
    images = db.relationship("ReportImage", cascade="all, delete-orphan", order_by="ReportImage.order")

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def editable(self):
        return self.status in ("draft", "rejected")


class IssueRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    received_by = db.Column(db.String(128), default="")
    received_from = db.Column(db.String(128), default="")
    received_time = db.Column(db.String(64), default="")
    report_by = db.Column(db.String(128), default="")
    reporter_phone = db.Column(db.String(64), default="")
    issue_type = db.Column(db.String(128), default="")
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(128), default="")
    further_action = db.Column(db.Text, default="")
    jira = db.Column(db.String(128), default="")


class OnsiteRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    personnel = db.Column(db.String(256), default="")
    recorded_by = db.Column(db.String(128), default="")
    time = db.Column(db.String(64), default="")
    title = db.Column(db.String(256), default="")
    status = db.Column(db.String(128), default="")
    further_action = db.Column(db.Text, default="")
    jira = db.Column(db.String(128), default="")


class SystemUpdateRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    system_name = db.Column(db.String(256), default="")
    update_personnel = db.Column(db.String(256), default="")
    backup_support = db.Column(db.String(256), default="")
    time_period = db.Column(db.String(128), default="")
    risk_info = db.Column(db.Text, default="")
    logbook_updated = db.Column(db.String(32), default="")


class TrueAlarmRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    alarm_item = db.Column(db.String(256), default="")
    start_time = db.Column(db.String(64), default="")
    end_time = db.Column(db.String(64), default="")
    checked_by = db.Column(db.String(128), default="")
    recorded_by = db.Column(db.String(128), default="")
    status = db.Column(db.String(128), default="")
    follow_up = db.Column(db.Text, default="")


class AlarmSummaryRow(db.Model):
    """CheckList 的 Alarm Summary 頁：已知/新增告警清單。"""

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    order = db.Column(db.Integer, default=0)
    category = db.Column(db.String(64), default="")  # Virtual Machines / Software / Physical Devices / PI
    item = db.Column(db.String(32), default="")
    device_name = db.Column(db.String(128), default="")
    description = db.Column(db.Text, default="")
    reason = db.Column(db.Text, default="")
    false_alarm = db.Column(db.String(1), default="N")  # Y / N
    note = db.Column(db.Text, default="")


class ChecklistResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("checklist_item.id"), nullable=False)
    item = db.relationship("ChecklistItem")
    result = db.Column(db.String(1))  # N / W / F；None 表示尚未勾選
    note = db.Column(db.Text, default="")

    __table_args__ = (db.UniqueConstraint("report_id", "item_id"),)


class ReportImage(db.Model):
    """截圖與照片。kind: screenshot（section_id 指向截圖章節）/ onsite / alarm。"""

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey("screenshot_section.id"))
    filename = db.Column(db.String(128), nullable=False)  # 儲存在 instance/uploads/<report_id>/ 下的檔名
    original_name = db.Column(db.String(256), default="")
    order = db.Column(db.Integer, default=0)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.now)
