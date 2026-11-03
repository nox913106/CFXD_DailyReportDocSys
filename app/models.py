from datetime import datetime

from flask_login import UserMixin

from . import db, login_manager


ROLE_LABELS = {
    "admin": "系統管理員（本機緊急帳號）",
    "file_manager": "檔案管理員",
    "second_line": "二線管理員",
    "first_line": "一線使用者",
}
ASSIGNABLE_ROLES = ("file_manager", "second_line", "first_line")  # 本機緊急帳號只能用指令建立
ADMIN_ELIGIBLE_ROLES = ("file_manager", "second_line")  # 可被指派為系統管理員的角色；一線使用者強制排除


class User(UserMixin, db.Model):
    """AD 帳號首次登入時建立（預設一線使用者），角色由系統管理員在「帳號管理」調整。

    系統管理員有兩種：
    - 指派：檔案管理員或二線管理員被勾選 sys_admin（一線使用者強制排除）。AD 帳號也可以。
    - 本機緊急帳號（role=admin）：密碼雜湊存在 password_hash，不經 AD，AD 無法驗證時仍可登入。
    """

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(128), unique=True, nullable=False)
    display_name = db.Column(db.String(256))
    email = db.Column(db.String(256))
    is_approver = db.Column(db.Boolean, default=False, nullable=False)  # 舊欄位，與 role 同步
    role = db.Column(db.String(16), default="first_line")
    password_hash = db.Column(db.String(256))  # 僅本機帳號（系統管理員緊急帳號）
    sys_admin = db.Column(db.Boolean, default=False)  # 指派的系統管理員權限（只對檔案 / 二線管理員有效）
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)

    @property
    def name(self):
        return self.display_name or self.username

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    def set_role(self, role):
        self.role = role
        self.is_approver = role in ("file_manager", "admin")
        if role not in ADMIN_ELIGIBLE_ROLES:
            self.sys_admin = False  # 改成一線使用者：系統管理員權限一併移除

    @property
    def is_local_admin(self):
        return self.role == "admin"

    @property
    def is_admin(self):
        """系統管理員：本機緊急帳號，或被指派的檔案 / 二線管理員（一線使用者即使旗標殘留也無效）。"""
        return self.role == "admin" or bool(self.sys_admin and self.role in ADMIN_ELIGIBLE_ROLES)

    @property
    def is_file_manager(self):
        return self.role == "file_manager"

    @property
    def is_second_line(self):
        return self.role == "second_line"


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
    "voided": "作廢",
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
    # 指定核准人（文件上的簽核者）。approver_name 是文件顯示的名字，代簽時仍顯示核准人本人
    approver_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    approver = db.relationship("User", foreign_keys=[approver_id])
    approver_name = db.Column(db.String(128), default="")  # 封面 Approval / Document Approved 欄

    status = db.Column(db.String(16), default="draft", nullable=False, index=True)
    submitted_at = db.Column(db.DateTime)
    submitted_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    submitted_by = db.relationship("User", foreign_keys=[submitted_by_id])
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])
    reviewed_at = db.Column(db.DateTime)
    review_comment = db.Column(db.Text, default="")
    # 作廢：保留日期與文件編號（流水號不會被後續日報沖掉），內容唯讀
    void_reason = db.Column(db.Text)
    voided_at = db.Column(db.DateTime)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])
    # 一線使用者的作廢申請，待檔案管理員（或其代理二線）核定
    void_request_reason = db.Column(db.Text)
    void_requested_at = db.Column(db.DateTime)
    void_requested_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    void_requested_by = db.relationship("User", foreign_keys=[void_requested_by_id])
    # 代簽時記錄被代簽的核准人（reviewed_by 為實際操作者）；本人簽核時為空
    reviewed_on_behalf_of_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    reviewed_on_behalf_of = db.relationship("User", foreign_keys=[reviewed_on_behalf_of_id])

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
    review_logs = db.relationship("ReviewLog", cascade="all, delete-orphan", order_by="ReviewLog.id")

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
    # 截圖系統時間 OCR（背景服務處理）：pending / running / done / error
    ocr_status = db.Column(db.String(12), index=True)
    ocr_result = db.Column(db.Text)  # JSON：status, dates, time, region, text, ocr
    ocr_error = db.Column(db.Text)
    ocr_started_at = db.Column(db.DateTime)
    ocr_at = db.Column(db.DateTime)


class ApprovalDelegation(db.Model):
    """核准人把簽核權委派給其他帳號（代簽人）。在有效期間內，代簽人可代為核准 / 退回
    指定給該核准人的日報；文件上仍顯示核准人本人的名字，系統另記錄實際操作者。"""

    id = db.Column(db.Integer, primary_key=True)
    principal_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    principal = db.relationship("User", foreign_keys=[principal_id])
    delegate_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    delegate = db.relationship("User", foreign_keys=[delegate_id])
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)  # 空白 = 直到撤銷
    note = db.Column(db.String(256), default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.now)
    revoked_at = db.Column(db.DateTime)

    def active_on(self, day):
        return self.revoked_at is None and self.start_date <= day and (self.end_date is None or day <= self.end_date)

    @property
    def state_label(self):
        from datetime import date

        today = date.today()
        if self.revoked_at:
            return "已撤銷"
        if self.end_date and self.end_date < today:
            return "已到期"
        if self.start_date > today:
            return "尚未生效"
        return "生效中"


REVIEW_ACTION_LABELS = {
    "approve": "核准",
    "reject": "退回",
    "assign": "指派",
    "void": "作廢",
    "void_request": "申請作廢",
    "void_request_reject": "駁回作廢申請",
}


class ReviewLog(db.Model):
    """審核歷程：核准 / 退回 / 指派 / 作廢 / 作廢申請，含代理資訊。"""

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    action = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    actor = db.relationship("User", foreign_keys=[actor_id])
    on_behalf_of_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    on_behalf_of = db.relationship("User", foreign_keys=[on_behalf_of_id])
    comment = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class AuditLog(db.Model):
    """不隨日報刪除的稽核紀錄：刪除日報、角色變更。"""

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(24), nullable=False)  # delete_report / set_role
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    actor = db.relationship("User", foreign_keys=[actor_id])
    target = db.Column(db.String(256), default="")
    detail = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


CHECK_STAGE_LABELS = {"submit": "送審", "approve": "核准", "reject": "退回", "manual": "手動重新產生",
                      "backfill": "補產生"}
AI_STATE_LABELS = {"pending": "等待產生", "running": "產生中", "done": "已產生", "error": "產生失敗"}


class CheckReport(db.Model):
    """檢查結果.md 快照：送審、核准、退回時各存一份（與 DDoc_compare 的 <DocNo>_比對報告.md 相同結構）。

    用途：審核時的 AI 建議依據、日後 RAG 的知識庫（同時匯出成檔案，見 CHECK_MD_DIR）。
    日報刪除時快照一併刪除，但已匯出的 .md 檔案保留。
    """

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False, index=True)
    report = db.relationship("Report", backref=db.backref("check_reports", cascade="all, delete-orphan",
                                                          order_by="CheckReport.id", lazy="dynamic"))
    stage = db.Column(db.String(12), nullable=False)  # submit / approve / reject / manual / backfill
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    markdown = db.Column(db.Text, nullable=False)
    errors = db.Column(db.Integer, default=0)
    warnings = db.Column(db.Integer, default=0)
    infos = db.Column(db.Integer, default=0)
    file_path = db.Column(db.String(512))  # 匯出的 .md（相對 CHECK_MD_DIR）
    # AI 建議與提醒（背景服務 ai-worker 產生）：None（不產生）/ pending / running / done / error
    ai_status = db.Column(db.String(12), index=True)
    ai_markdown = db.Column(db.Text)
    ai_model = db.Column(db.String(128))
    ai_error = db.Column(db.Text)
    ai_started_at = db.Column(db.DateTime)
    ai_at = db.Column(db.DateTime)

    @property
    def stage_label(self):
        return CHECK_STAGE_LABELS.get(self.stage, self.stage)

    @property
    def ai_state_label(self):
        return AI_STATE_LABELS.get(self.ai_status, "")


class AppSetting(db.Model):
    """系統設定（前端「系統設定」頁維護）：key -> JSON 值。"""

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))


class AIProvider(db.Model):
    """一個可呼叫的 AI 服務。依 priority 由小到大嘗試，失敗或額度用完就換下一個。"""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    preset = db.Column(db.String(32), nullable=False)  # ai_providers.PRESETS 的 key
    kind = db.Column(db.String(24), nullable=False)  # 呼叫協定：openai / azure_openai / anthropic / gemini / bedrock
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    priority = db.Column(db.Integer, default=100, nullable=False)
    base_url = db.Column(db.String(512), default="")
    model = db.Column(db.String(256), default="")
    deployment = db.Column(db.String(256), default="")  # Azure OpenAI
    api_version = db.Column(db.String(32), default="")  # Azure OpenAI
    region = db.Column(db.String(32), default="")  # AWS Bedrock
    api_key_enc = db.Column(db.Text)  # 加密後的金鑰，畫面上只顯示末 4 碼
    api_key_hint = db.Column(db.String(8), default="")
    external = db.Column(db.Boolean, default=True, nullable=False)  # 資料會送到公司外部
    send_temperature = db.Column(db.Boolean, default=True, nullable=False)
    token_param = db.Column(db.String(32), default="max_tokens")  # max_tokens / max_completion_tokens / none
    extra_body = db.Column(db.Text, default="")  # 進階：併入請求內容的 JSON
    # 計費：token（按量）/ monthly（包月）/ self_hosted（自建，固定成本）
    billing = db.Column(db.String(16), default="token", nullable=False)
    currency = db.Column(db.String(8), default="USD")
    price_in = db.Column(db.Float, default=0.0)  # 每 100 萬 input token
    price_out = db.Column(db.Float, default=0.0)  # 每 100 萬 output token
    monthly_budget = db.Column(db.Float, default=0.0)  # 按量：每期費用上限，0 = 不限
    monthly_fee = db.Column(db.Float, default=0.0)  # 包月 / 自建：每月固定費用
    monthly_token_quota = db.Column(db.Integer, default=0)  # 包月：每期 token 額度，0 = 不限
    reset_day = db.Column(db.Integer, default=1)  # 每期起算日（1–28）
    note = db.Column(db.Text, default="")
    last_test_at = db.Column(db.DateTime)
    last_test_ok = db.Column(db.Boolean)
    last_test_msg = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class AIUsage(db.Model):
    """每一次 AI 呼叫（含失敗與連線測試），用於費用與額度計算。"""

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("ai_provider.id", ondelete="SET NULL"), index=True)
    provider = db.relationship("AIProvider")
    provider_name = db.Column(db.String(128), default="")  # 服務刪除後仍看得到
    check_report_id = db.Column(db.Integer, db.ForeignKey("check_report.id", ondelete="SET NULL"))
    purpose = db.Column(db.String(16), default="suggest")  # suggest / test
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    ok = db.Column(db.Boolean, default=False, nullable=False)
    model = db.Column(db.String(256), default="")
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)  # 按量計費才有；幣別同服務設定
    currency = db.Column(db.String(8), default="")
    latency_ms = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)


class MaintJob(db.Model):
    """維護工作（備份、還原）：系統設定頁排入，由維護背景服務（maint-worker）執行。"""

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(16), nullable=False)  # backup / restore
    trigger = db.Column(db.String(16), default="manual")  # manual / scheduled / pre_restore
    param = db.Column(db.String(256), default="")  # restore：備份名稱
    status = db.Column(db.String(16), default="pending")  # pending / running / done / error
    message = db.Column(db.Text, default="")
    requested_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    created_at = db.Column(db.DateTime, default=datetime.now)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)


MAINT_KIND_LABELS = {"backup": "備份", "restore": "還原"}
MAINT_STATUS_LABELS = {"pending": "排隊中", "running": "執行中", "done": "完成", "error": "失敗"}
MAINT_TRIGGER_LABELS = {"manual": "手動", "scheduled": "排程", "pre_restore": "還原前自動"}
