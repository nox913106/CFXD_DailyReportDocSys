"""端到端流程測試：dev 登入 → 建立日報 → 各分頁編輯 → 檢查表 → 截圖 → 送審 → 審核。

CSRF 保持開啟，從頁面抓 token，順便驗證每個表單都有帶 token。
執行：pytest -q
"""

import io
import re
from datetime import date

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CHECK_MD_DIR", str(tmp_path / "check_reports"))
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.setenv("APPROVER_USERS", "boss,boss2")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(
        r'<meta name="csrf-token" content="([^"]+)"', html
    )
    assert m, "頁面找不到 CSRF token"
    return m.group(1)


class Browser:
    def __init__(self, app):
        self.c = app.test_client()
        self.tok = None

    def get(self, url, status=200):
        r = self.c.get(url, follow_redirects=True)
        assert r.status_code == status, (url, r.status_code)
        html = r.get_data(as_text=True)
        if status == 200 and "csrf" in html:
            self.tok = token(html)
        return html

    def post(self, url, data=None, status=200, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.tok)
        r = self.c.post(url, data=data, follow_redirects=True, **kw)
        assert r.status_code == status, (url, r.status_code, r.get_data(as_text=True)[:500])
        html = r.get_data(as_text=True)
        if status == 200 and "csrf-token" in html:
            self.tok = token(html)
        return html

    def login(self, username):
        self.get("/login")
        return self.post("/login", {"username": username, "password": "x"})


def report_id_from(html):
    m = re.search(r"/reports/(\d+)/", html)
    assert m
    return int(m.group(1))


def fill_checklist(b, rid, result="N", leave_blank=0):
    from app.helpers import SHEETS

    blanks = leave_blank
    for sheet in SHEETS:
        html = b.get(f"/reports/{rid}/checklist/{sheet}")
        ids = sorted(set(re.findall(r'name="r_(\d+)"', html)), key=int)
        assert ids, sheet
        data = {}
        for i in ids:
            if blanks:
                blanks -= 1
                continue
            data[f"r_{i}"] = result
        b.post(f"/reports/{rid}/checklist/{sheet}", data)


def test_requires_login(app):
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_login_formats_and_open_redirect(app):
    b = Browser(app)
    b.get("/login")
    r = b.c.post(
        "/login?next=//evil.com", data={"username": "CORP\\Alice", "password": "x", "csrf_token": b.tok}
    )
    assert r.status_code == 302 and r.headers["Location"] == "/"
    from app.models import User

    with app.app_context():
        assert User.query.filter_by(username="alice").one()


def test_csrf_enforced(app):
    c = app.test_client()
    r = c.post("/login", data={"username": "a", "password": "x"})
    assert r.status_code == 400


def test_seed_counts(app):
    from app.models import ChecklistItem, ScreenshotSection

    with app.app_context():
        counts = {s: ChecklistItem.query.filter_by(sheet=s).count() for s in ("vm", "software", "physical", "pi")}
        assert counts == {"vm": 89, "software": 10, "physical": 581, "pi": 5}
        assert ScreenshotSection.query.count() > 0


def test_docno():
    from app import docno

    assert docno.doc_no(date(2025, 6, 12)) == "CFXD-SCA-RLY-DPR-000387"
    assert docno.contractor_doc_no(date(2025, 6, 12)) == "RT230018-CFXD SLA-RP-000287"


def test_full_flow(app):
    alice = Browser(app)
    alice.login("alice")
    html = alice.post("/reports/new", {"report_date": "2026-11-02"})
    rid = report_id_from(html)
    assert "CFXD-SCA-RLY-DPR-" in html

    # 重複日期
    html = alice.post("/reports/new", {"report_date": "2026-11-02"})
    assert "已經存在" in html

    # 基本資料
    alice.post(
        f"/reports/{rid}/basic",
        {
            "revision": "1",
            "issue_purpose": "IFI - Issue for Information",
            "inspection_time": "10:00 AM",
            "approver_name": "Boss",
            "first_line_officer": "Alice",
            "second_line_support": "Bob",
        },
    )
    # 記錄頁
    html = alice.get(f"/reports/{rid}/records")
    html = alice.post(
        f"/reports/{rid}/records",
        {
            "inspection_summary": "One issue.",
            "onsite_summary": "None.",
            "issue-0-received_by": "Alice",
            "issue-0-description": "Disk full",
            "issue-1-received_by": "",  # 空白列應略過
            "onsite-0-personnel": "Carl",
            "sysupdate-0-system_name": "PI",
        },
    )
    assert "Disk full" in html
    # 告警頁
    alice.get(f"/reports/{rid}/alarms")
    html = alice.post(
        f"/reports/{rid}/alarms",
        {
            "alarm_new": "2",
            "alarm_known": "3",
            "alarm_false": "-1",
            "truealarm-0-alarm_item": "UPS",
            "alarmrow-0-category": "Software",
            "alarmrow-0-item": "1.1",
            "alarmrow-0-device_name": "Known-Dev",
            "alarmrow-0-false_alarm": "y",
        },
    )
    assert "Known-Dev" in html

    # 截圖上傳 / 拒收非圖片 / 排序 / 刪除
    from app.models import Report, ReportImage, ScreenshotSection

    with app.app_context():
        sec_id = ScreenshotSection.query.order_by(ScreenshotSection.order).first().id
    alice.get(f"/reports/{rid}/screenshots")
    hdr = {"X-CSRFToken": alice.tok}
    r = alice.c.post(
        f"/reports/{rid}/images",
        data={
            "kind": "screenshot",
            "section_id": str(sec_id),
            "files": [(io.BytesIO(PNG), "a.png"), (io.BytesIO(PNG), "b.png"), (io.BytesIO(b"hello"), "x.png")],
        },
        headers=hdr,
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert len(j["images"]) == 2 and len(j["errors"]) == 1
    first, second = j["images"]
    assert alice.c.get(first["url"]).status_code == 200
    assert alice.c.post(f"/reports/{rid}/images/{second['id']}/move", data={"direction": "up"}, headers=hdr).status_code == 200
    with app.app_context():
        order = [i.id for i in ReportImage.query.filter_by(report_id=rid).order_by(ReportImage.order)]
        assert order == [second["id"], first["id"]]
    assert alice.c.delete(f"/reports/{rid}/images/{first['id']}", headers=hdr).status_code == 200

    # 未勾完不能送審
    fill_checklist(alice, rid, leave_blank=3)
    alice.get(f"/reports/{rid}")
    html = alice.post(f"/reports/{rid}/submit")
    assert "3 個項目未勾選" in html

    # 勾一個 W，帶入告警
    fill_checklist(alice, rid)
    html = alice.get(f"/reports/{rid}/checklist/vm")
    first_r = re.search(r'name="r_(\d+)"', html).group(1)
    alice.post(f"/reports/{rid}/checklist/vm", {
        **{f"r_{i}": "N" for i in set(re.findall(r'name="r_(\d+)"', html))},
        f"r_{first_r}": "W",
        f"n_{first_r}": "slow",
    })
    alice.get(f"/reports/{rid}/alarms")
    html = alice.post(f"/reports/{rid}/alarms/import")
    assert "帶入 1 筆" in html
    html = alice.post(f"/reports/{rid}/alarms/import")
    assert "沒有新的異常" in html

    alice.get(f"/reports/{rid}/checklist")
    html = alice.get(f"/reports/{rid}")
    assert f"/reports/{rid}/pdf" in html
    html = alice.post(f"/reports/{rid}/submit")
    assert "已送出審核" in html
    with app.app_context():
        assert db_status(rid) == "submitted"

    # 送審後鎖定
    html = alice.post(f"/reports/{rid}/basic", {"revision": "9"})
    assert "不能修改" in html
    r = alice.c.post(f"/reports/{rid}/images", data={"kind": "onsite"}, headers={"X-CSRFToken": alice.tok})
    assert r.status_code == 409

    # 非審核者不能審核
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/review", {"action": "approve"}, status=403)

    # 審核者退回（需理由）
    boss = Browser(app)
    html = boss.login("boss")
    assert "待審核" in html
    boss.get(f"/reports/{rid}")
    html = boss.post(f"/reports/{rid}/review", {"action": "reject", "comment": ""})
    assert "請填寫原因" in html
    html = boss.post(f"/reports/{rid}/review", {"action": "reject", "comment": "fix typo"})
    assert "已退回" in html

    # 撰寫人修改後重送，審核者核准
    alice.get(f"/reports/{rid}/basic")
    alice.post(
        f"/reports/{rid}/basic",
        {"revision": "2", "approver_name": "Boss", "inspection_time": "10:00 AM",
         "issue_purpose": "IFI - Issue for Information", "first_line_officer": "Alice", "second_line_support": "Bob"},
    )
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/submit")
    boss.get(f"/reports/{rid}")
    html = boss.post(f"/reports/{rid}/review", {"action": "approve"})
    assert "已核准" in html
    with app.app_context():
        assert db_status(rid) == "approved"

    # 隔天日報沿用前日資料
    html = alice.post("/reports/new", {"report_date": "2026-11-03"})
    rid2 = report_id_from(html)
    with app.app_context():
        from app import db

        r2 = db.session.get(Report, rid2)
        assert r2.second_line_support == "Bob"
        assert r2.alarm_known == 5
        assert len(r2.alarm_rows) == 2
        assert r2.results.filter_by(result=None).count() == 685
        assert r2.results.filter_by(note="slow").count() == 1
        assert r2.doc_no == "CFXD-SCA-RLY-DPR-000896"

    # 首頁、月份切換、所有頁面都能開
    for url in ["/", "/?month=2026-11", f"/reports/{rid2}", f"/reports/{rid2}/basic", f"/reports/{rid2}/records",
                f"/reports/{rid2}/alarms", f"/reports/{rid2}/checklist", f"/reports/{rid2}/screenshots"]:
        alice.get(url)
    alice.get("/?month=bad", status=400)
    alice.get("/reports/9999", status=404)
    alice.get(f"/reports/{rid2}/checklist/nope", status=404)


def test_self_review_blocked(app):
    boss = Browser(app)
    boss.login("boss")
    rid = report_id_from(boss.post("/reports/new", {"report_date": "2026-11-01"}))  # 檔案管理員建立，撰寫人是自己
    alice = Browser(app)
    alice.login("alice")
    fill_checklist(alice, rid)
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/submit")  # 一線使用者送審
    boss.get(f"/reports/{rid}")
    html = boss.post(f"/reports/{rid}/review", {"action": "approve"})
    assert "不能審核自己" in html


def db_status(rid):
    from app import db
    from app.models import Report

    return db.session.get(Report, rid).status


def set_role(app, username, role):
    from app import db
    from app.models import User

    with app.app_context():
        u = User.query.filter_by(username=username).one()
        u.set_role(role)
        db.session.commit()
        return u.id


def submitted_report(app, alice, day):
    rid = report_id_from(alice.post("/reports/new", {"report_date": day}))
    fill_checklist(alice, rid)
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/submit")
    return rid


def test_delegated_approval(app):
    from app.models import Report, ReviewLog, User

    boss = Browser(app)
    boss.login("boss")  # 唯一的檔案管理員 -> 新日報預設指定給 boss
    carol = Browser(app)
    carol.login("carol")
    boss2 = Browser(app)
    boss2.login("boss2")
    boss_id = set_role(app, "boss", "file_manager")
    carol_id = set_role(app, "carol", "second_line")
    set_role(app, "boss2", "first_line")

    alice = Browser(app)
    alice.login("alice")
    rid = submitted_report(app, alice, "2026-11-05")
    with app.app_context():
        from app import db

        r = db.session.get(Report, rid)
        assert r.approver_id == boss_id and r.approver_name == "boss"
    set_role(app, "boss2", "file_manager")

    # 未授權前：二線看不到、不能審；另一位檔案管理員也不能審指定給 boss 的日報
    assert "待我審核" not in carol.get("/")
    carol.get(f"/reports/{rid}")
    carol.post(f"/reports/{rid}/review", {"action": "approve"}, status=403)
    boss2.get(f"/reports/{rid}")
    boss2.post(f"/reports/{rid}/review", {"action": "approve"}, status=403)

    # 二線不能建立授權；只能授權給二線管理員；過期的授權無效
    carol.get("/delegations")
    carol.post("/delegations", {"delegate_id": str(boss_id), "start_date": "2026-01-01"}, status=403)
    boss.get("/delegations")
    assert "請選擇一位二線管理員" in boss.post("/delegations", {"delegate_id": str(boss_id), "start_date": "2026-01-01"})
    boss.post("/delegations", {"delegate_id": str(carol_id), "start_date": "2020-01-01", "end_date": "2020-01-31"})
    carol.get(f"/reports/{rid}")
    carol.post(f"/reports/{rid}/review", {"action": "approve"}, status=403)

    # 有效授權：carol 代簽核准，文件仍顯示 boss
    assert "已授權" in boss.post("/delegations", {"delegate_id": str(carol_id), "start_date": "2020-01-01", "note": "出差"})
    html = carol.get("/")
    assert "待我審核" in html and "代簽" in html
    assert "代 boss 簽核" in carol.get(f"/reports/{rid}")
    html = carol.post(f"/reports/{rid}/review", {"action": "approve"})
    assert "代 boss 簽核" in html and "處理紀錄" in html and "carol 代簽" in html
    with app.app_context():
        from app import db
        from app.pdf.report_pdf import cover_data

        r = db.session.get(Report, rid)
        assert r.status == "approved" and r.approver_name == "boss"
        assert r.reviewed_by.username == "carol" and r.reviewed_on_behalf_of.username == "boss"
        log = ReviewLog.query.filter_by(report_id=rid, action="approve").one()
        assert log.actor_id == carol_id and log.on_behalf_of_id == boss_id
        d = cover_data(r)
        assert d["approver"] == "boss" and d["revisions"][0]["approval"] == "boss"
        assert User.query.filter_by(username="carol").one().role == "second_line"

    # 撤銷後不能再代簽；檔案管理員本人仍可簽核
    rid2 = submitted_report(app, alice, "2026-11-06")
    did = re.findall(r"/delegations/(\d+)/revoke", boss.get("/delegations"))[0]
    assert "已撤銷" in boss.post(f"/delegations/{did}/revoke")
    carol.get(f"/reports/{rid2}")
    carol.post(f"/reports/{rid2}/review", {"action": "approve"}, status=403)
    boss.get(f"/reports/{rid2}")
    assert "已核准。" in boss.post(f"/reports/{rid2}/review", {"action": "approve"})


def test_role_permissions(app):
    from app import db
    from app.models import Report

    boss, sl, alice = Browser(app), Browser(app), Browser(app)
    boss.login("boss")
    sl.login("dave")
    alice.login("alice")
    boss_id = set_role(app, "boss", "file_manager")
    dave_id = set_role(app, "dave", "second_line")
    with app.app_context():
        from app.models import User

        alice_id = User.query.filter_by(username="alice").one().id

    # 一線：可新增 / 編輯 / 送審，不能指派、作廢、刪除、管理帳號或代理
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-10"}))
    alice.get(f"/reports/{rid}/basic")
    alice.post(f"/reports/{rid}/basic", {"author_id": str(dave_id), "approver_id": "", "revision": "0"})
    with app.app_context():
        r = db.session.get(Report, rid)
        assert r.author_id == alice_id and r.approver_id == boss_id  # 指派被忽略
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/void", {"reason": "x"}, status=403)
    alice.post(f"/reports/{rid}/delete", {"confirm_doc_no": "x"}, status=403)
    alice.get("/admin/users", status=403)
    alice.get("/delegations", status=403)

    # 二線：可編輯 / 刪除；未授權不能指派
    sl.get(f"/reports/{rid}/basic")
    sl.post(f"/reports/{rid}/basic", {"author_id": str(dave_id), "approver_id": str(boss_id), "revision": "0"})
    with app.app_context():
        assert db.session.get(Report, rid).author_id == alice_id

    # 檔案管理員：指派撰寫人
    boss.get(f"/reports/{rid}/basic")
    boss.post(f"/reports/{rid}/basic", {"author_id": str(dave_id), "approver_id": str(boss_id), "revision": "0",
                                        "issue_purpose": "IFI - Issue for Information", "inspection_time": "10:00 AM"})
    with app.app_context():
        r = db.session.get(Report, rid)
        assert r.author_id == dave_id
        assert any(log.action == "assign" for log in r.review_logs)

    # 一線申請作廢 -> 檔案管理員駁回 -> 再申請 -> 核定作廢；作廢後唯讀、編號保留
    alice.get(f"/reports/{rid}")
    assert "已送出作廢申請" in alice.post(f"/reports/{rid}/void-request", {"reason": "日期建錯"})
    assert "已有待處理" in alice.post(f"/reports/{rid}/void-request", {"reason": "again"})
    assert "待核定的作廢申請" in boss.get("/")
    boss.get(f"/reports/{rid}")
    assert "已駁回作廢申請" in boss.post(f"/reports/{rid}/void-request/decide", {"action": "reject"})
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/void-request", {"reason": "日期建錯"})
    boss.get(f"/reports/{rid}")
    html = boss.post(f"/reports/{rid}/void-request/decide", {"action": "approve"})
    assert "已核定作廢" in html and "保留" in html
    with app.app_context():
        r = db.session.get(Report, rid)
        assert r.status == "voided" and r.void_reason.startswith("日期建錯")
    assert "不能修改" in alice.post(f"/reports/{rid}/basic", {"revision": "3"})
    assert "已經存在" in alice.post("/reports/new", {"report_date": "2026-11-10"})  # 日期 / 流水號仍被保留

    # 刪除：需輸入文件編號；刪除後日期可重新建立，稽核紀錄保留
    rid2 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-11"}))
    sl.get(f"/reports/{rid2}")
    assert "請輸入完整文件編號" in sl.post(f"/reports/{rid2}/delete", {"confirm_doc_no": "wrong"})
    with app.app_context():
        doc = db.session.get(Report, rid2).doc_no
    assert "已刪除" in sl.post(f"/reports/{rid2}/delete", {"confirm_doc_no": doc})
    with app.app_context():
        from app.models import AuditLog

        assert db.session.get(Report, rid2) is None
        assert AuditLog.query.filter_by(action="delete_report", target=doc).one()


def test_local_admin(app):
    from app import db
    from app.models import User

    runner = app.test_cli_runner()
    res = runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "short"])
    assert res.exit_code != 0
    res = runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "a-very-long-pass"])
    assert res.exit_code == 0, res.output

    b = Browser(app)
    b.get("/login")
    assert "帳號或密碼錯誤" in b.post("/login", {"username": "sysadmin", "password": "wrong"})  # dev 模式也要驗證本機密碼
    html = b.post("/login", {"username": "sysadmin", "password": "a-very-long-pass"})
    assert "系統管理員" in html and "帳號管理" in html

    other = Browser(app)
    other.login("erin")
    html = b.get("/admin/users")
    assert "erin" in html
    with app.app_context():
        erin = User.query.filter_by(username="erin").one()
        assert erin.role == "first_line"
        eid = erin.id
    b.post("/admin/users", {"user_id": str(eid), "role": "file_manager"})
    b.post("/admin/users", {"user_id": str(eid), "role": "admin"}, status=400)  # 系統管理員只能用指令建立
    with app.app_context():
        assert db.session.get(User, eid).role == "file_manager"
    assert "變更角色" in b.get("/admin/users")

    # 系統管理員可直接作廢任何日報
    rid = report_id_from(other.post("/reports/new", {"report_date": "2026-11-12"}))
    b.get(f"/reports/{rid}")
    assert "已作廢" in b.post(f"/reports/{rid}/void", {"reason": "測試"})


def test_add_missing_columns(tmp_path, monkeypatch):
    """舊版資料庫（沒有代簽欄位）啟動時自動補欄位。"""
    import sqlite3

    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.execute('CREATE TABLE report (id INTEGER PRIMARY KEY, report_date DATE NOT NULL UNIQUE, doc_no VARCHAR(64) NOT NULL, '
                'contractor_doc_no VARCHAR(64) NOT NULL, revision INTEGER NOT NULL, author_id INTEGER NOT NULL, '
                'status VARCHAR(16) NOT NULL)')
    con.commit()
    con.close()
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "up"))
    from app import create_app

    create_app()
    cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(report)")}
    assert {"approver_id", "reviewed_on_behalf_of_id", "approver_name"} <= cols


def test_managers_can_submit_but_not_approve_own_submission(app):
    boss, dave, alice = Browser(app), Browser(app), Browser(app)
    boss.login("boss")
    dave.login("dave")
    alice.login("alice")
    set_role(app, "boss", "file_manager")
    dave_id = set_role(app, "dave", "second_line")

    # 二線管理員送審（日報由一線建立）；即使有代理授權，也不能核准自己送審的日報
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-20"}))
    fill_checklist(dave, rid)
    dave.get(f"/reports/{rid}")
    assert "已送出審核" in dave.post(f"/reports/{rid}/submit")
    boss.get("/delegations")
    boss.post("/delegations", {"delegate_id": str(dave_id), "start_date": "2020-01-01"})
    dave.get(f"/reports/{rid}")
    assert "不能審核自己送審" in dave.post(f"/reports/{rid}/review", {"action": "approve"})
    boss.get(f"/reports/{rid}")
    assert "已核准" in boss.post(f"/reports/{rid}/review", {"action": "approve"})

    # 檔案管理員送審別人的日報後，也不能自己核准；交由代理人核准，文件仍顯示 boss
    rid2 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-21"}))
    fill_checklist(boss, rid2)
    boss.get(f"/reports/{rid2}")
    assert "已送出審核" in boss.post(f"/reports/{rid2}/submit")
    boss.get(f"/reports/{rid2}")
    assert "不能審核自己送審" in boss.post(f"/reports/{rid2}/review", {"action": "approve"})
    dave.get(f"/reports/{rid2}")
    assert "代 boss 簽核" in dave.post(f"/reports/{rid2}/review", {"action": "approve"})


def test_report_checks(app):
    """資料庫檢查與跨日比對：送審中的日報與最近一份已核准日報比對，結果顯示在日報頁與待審清單。"""
    from app import db
    from app.checks import run_checks
    from app.models import AlarmSummaryRow, ChecklistItem, ChecklistResult, Report, TrueAlarmRecord

    boss, alice = Browser(app), Browser(app)
    boss.login("boss")
    alice.login("alice")
    set_role(app, "boss", "file_manager")

    # 前一天：一筆告警（VM 第 1 項 Warning），已核准
    rid1 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-01"}))
    with app.app_context():
        r1 = db.session.get(Report, rid1)
        first = ChecklistItem.query.filter_by(sheet="vm").order_by(ChecklistItem.order).first()
        for res in r1.results:
            res.result = "W" if res.item_id == first.id else "N"
            res.note = "disk latency" if res.item_id == first.id else ""
        r1.alarm_rows.append(AlarmSummaryRow(order=1, category="Virtual Machines", item=first.item_no,
                                             device_name=first.device_name, description=first.status_type,
                                             reason="disk latency", false_alarm="N"))
        r1.alarm_known = 1
        r1.inspection_summary = "There was no issue record reported today."
        r1.second_line_support = "Bob"
        r1.status = "approved"
        db.session.commit()
        assert run_checks(r1).counts()["錯誤"] == 0

    # 今日：延續 + 新增一筆 Failed 但沒有寫 Note、沒有列入告警表，New/Known 填錯
    rid2 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-02"}))
    with app.app_context():
        r2 = db.session.get(Report, rid2)
        second = ChecklistItem.query.filter_by(sheet="vm").order_by(ChecklistItem.order).offset(1).first()
        for res in r2.results:
            res.result = {first.id: "W", second.id: "F"}.get(res.item_id, "N")
        r2.alarm_new, r2.alarm_known = 0, 1  # 沿用前一天的告警列，New 應為 1（若有列入）
        db.session.commit()
        chk = run_checks(r2)
        msgs = [(i.severity, i.message) for i in chk.issues]
        assert chk.baseline.id == rid1 and chk.continued == 1 and not chk.new_alarms
        assert ("警告", "勾選 Failed 但 Note 空白") in msgs
        assert ("錯誤", "檢查表勾選 Warning/Failed，但 Alarm Summary 沒有列出") in msgs
        assert any(i.ref == second.item_no for i in chk.issues)
        assert len(chk.status_changes) == 1 and chk.status_changes[0][2] == "F"

        # 修正：列入告警表、寫 Note，但 New Alarm 仍填 0、沒有 True Alarm Record
        res2 = ChecklistResult.query.filter_by(report_id=rid2, item_id=second.id).one()
        res2.note = "host down"
        r2.alarm_rows.append(AlarmSummaryRow(order=2, category="Virtual Machines", item=second.item_no,
                                             device_name=second.device_name, description=second.status_type,
                                             reason="host down", false_alarm="N"))
        db.session.commit()
        msgs = [i.message for i in run_checks(r2).issues if i.severity == "錯誤"]
        assert "告警計數與 Alarm Summary 項數不符" in msgs
        assert "New Alarm 數與實際新增的告警數不同" in msgs

        r2.alarm_new = 1
        r2.true_alarms.append(TrueAlarmRecord(alarm_item=second.device_name, status="Open"))
        db.session.commit()
        chk = run_checks(r2)
        assert chk.counts()["錯誤"] == 0, [i.message for i in chk.issues if i.severity == "錯誤"]
        assert len(chk.new_alarms) == 1

    # 畫面：送審後審核者在日報頁與待審清單看到檢查結果
    alice.get(f"/reports/{rid2}")
    alice.post(f"/reports/{rid2}/submit")
    html = boss.get("/")
    assert "檢查結果" in html and ("無誤" in html or "警告" in html)
    html = boss.get(f"/reports/{rid2}")
    assert "檢查結果" in html and "告警差異：新增 1" in html and "2026-11-01" in html


def test_alarm_history(app):
    """告警歷程：連續天數（沒有已核准日報的日子不中斷）、上次發生、超過 365 天截斷、提醒。"""
    import datetime as dt

    from app import db
    from app.checks import alarm_history, run_checks
    from app.models import AlarmSummaryRow, Report, User

    def alarm(dev, desc="Status Check", order=1):
        return AlarmSummaryRow(order=order, category="Physical Devices", item="1.1", device_name=dev,
                               description=desc, reason="r", false_alarm="N")

    with app.app_context():
        u = User(username="hist", display_name="Hist")
        u.set_role("first_line")
        db.session.add(u)
        db.session.flush()
        today = dt.date(2026, 11, 3)

        def report(day, status, devs):
            r = Report(report_date=day, doc_no=f"D{day:%Y%m%d}", contractor_doc_no="x", revision=0,
                       author_id=u.id, status=status)
            for n, d in enumerate(devs, 1):
                r.alarm_rows.append(alarm(d, order=n))
            db.session.add(r)
            return r

        # LONG：一年多每天都有 -> 截斷；RUN：10/20 起連續（10/25 沒有日報、10/28 退回不中斷）
        # BACK：10/01–10/05 出現、解除、今天再發生；OLD：只有 400 天前出現過 -> 超出回溯期間
        for n in range(1, 400):
            day = today - dt.timedelta(days=n)
            if day == dt.date(2026, 10, 25):
                continue
            devs = ["LONG"]
            if day >= dt.date(2026, 10, 20):
                devs.append("RUN")
            if dt.date(2026, 10, 1) <= day <= dt.date(2026, 10, 5):
                devs.append("BACK")
            if n == 399:
                devs.append("OLD")
            report(day, "rejected" if day == dt.date(2026, 10, 28) else "approved", devs)
        cur = report(today, "submitted", ["LONG", "RUN", "BACK", "OLD", "NEW"])
        db.session.commit()

        h = {x.alarm.device_name: x for x in alarm_history(cur, list(cur.alarm_rows))}
        assert h["LONG"].truncated and h["LONG"].last_seen is None
        assert not h["RUN"].truncated and h["RUN"].since == dt.date(2026, 10, 20) and h["RUN"].days == 15
        assert h["BACK"].is_new and h["BACK"].last_seen == dt.date(2026, 10, 5)
        assert h["BACK"].last_doc_no == "D20261005"
        assert h["OLD"].is_new and h["OLD"].last_seen is None  # 400 天前 -> 超出 365 天，不查
        assert h["NEW"].is_new and h["NEW"].last_seen is None
        assert [x.alarm.device_name for x in alarm_history(cur, list(cur.alarm_rows))][:2] == ["LONG", "RUN"]

        chk = run_checks(cur)
        notes = {i.message: i.obj for i in chk.issues if i.area == "告警歷程"}
        assert any("LONG" in o and "超過 365" in o and "RUN" not in o for o in notes.values())
        assert any("BACK" in o and "上次 2026-10-05" in o for o in notes.values())
        assert not run_checks(cur, history=False).history
        cur_id = cur.id

    page = Browser(app)
    page.login("hist")
    html = page.get(f"/reports/{cur_id}")
    assert "告警歷程" in html and "超過 365 天" in html and "第 15 天" in html


def test_check_report_and_ai(app, tmp_path):
    """檢查結果.md：送審 / 退回 / 核准各存一份並匯出；AI 依據檢查結果.md 產生建議（背景服務、輸出跳脫）。"""
    import http.server
    import json
    import threading

    from app import ai, db
    from app.models import CheckReport, ReportImage

    boss, alice = Browser(app), Browser(app)
    boss.login("boss")
    alice.login("alice")

    # 未啟用 AI：區塊保留、顯示說明；送審仍會產生檢查結果.md，但不排 AI
    rid = submitted_report(app, alice, "2026-11-02")
    html = alice.get(f"/reports/{rid}")
    assert "AI 建議與提醒" in html and "尚未啟用" in html
    with app.app_context():
        s = CheckReport.query.filter_by(report_id=rid).one()
        assert s.stage == "submit" and s.ai_status is None
        text = (tmp_path / "check_reports" / s.file_path).read_text(encoding="utf-8")
        assert text.startswith("---\ntype: \"cfxd-daily-report-check\"") and "## 一、今日文件內部一致性" in text
        assert "### 二-1 跨日一致性" in text and 'stage: "submit"' in text

    # 退回：快照含審核結果與意見（RAG 用）
    boss.get(f"/reports/{rid}")
    boss.post(f"/reports/{rid}/review", {"action": "reject", "comment": "Alarm 計數錯誤"})
    with app.app_context():
        rej = CheckReport.query.filter_by(report_id=rid, stage="reject").one()
        assert "## 三、審核結果" in rej.markdown and "Alarm 計數錯誤" in rej.markdown

    # 啟用 AI，再送審 -> 排入 AI；截圖還在辨識時先等
    app.config["AI_BASE_URL"] = "http://ai.invalid/v1"
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/submit")
    with app.app_context():
        snap = CheckReport.query.filter_by(report_id=rid).order_by(CheckReport.id.desc()).first()
        assert snap.stage == "submit" and snap.ai_status == "pending"
        img = ReportImage(report_id=rid, kind="screenshot", filename="x.png", ocr_status="pending")
        db.session.add(img)
        db.session.commit()
        assert ai.claim_next(db) is None  # 等截圖辨識
        img.ocr_status = "error"
        db.session.commit()

        seen = {}

        def fake_llm(messages):
            seen["messages"] = messages
            return "## 建議\n- **建議退回修正** <script>alert(1)</script>\n- [一-2] 告警計數不符", "test-model"

        assert ai.process_one(db, llm=fake_llm)
        snap = db.session.get(CheckReport, snap.id)
        assert snap.ai_status == "done" and snap.ai_model == "test-model"
        user_msg = seen["messages"][1]["content"]
        assert "檢查結果.md" in user_msg and "## 總覽" in user_msg
        assert "<details>" not in user_msg  # 逐張明細不送
        assert "先前被退回時的檢查結果" in user_msg and "Alarm 計數錯誤" in user_msg  # RAG：先前退回的版本
        assert "1 張截圖辨識失敗" in snap.markdown  # AI 產生前依最新 OCR 狀態更新 .md
        assert not ai.process_one(db, llm=fake_llm)

    html = boss.get(f"/reports/{rid}")
    assert "<strong>建議退回修正</strong>" in html and "&lt;script&gt;" in html and "<script>alert" not in html
    assert "模型 test-model" in html and "歷次檢查結果.md（3 份）" in html

    # 手動重新產生、下載 .md
    boss.post(f"/reports/{rid}/ai")
    with app.app_context():
        assert CheckReport.query.filter_by(report_id=rid, stage="manual", ai_status="pending").count() == 1
    r = boss.c.get(f"/reports/{rid}/checks.md")
    assert r.status_code == 200 and r.mimetype == "text/markdown" and "日報檢查結果" in r.get_data(as_text=True)

    # 一線使用者不能替送審中、別人審的日報要求 AI（不能編輯也不能審核）
    alice.post(f"/reports/{rid}/ai", status=403)

    # 真正的 HTTP 呼叫：OpenAI 相容 API，推理模型的 <think> 會被移除
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["auth"] = self.headers.get("Authorization")
            seen["path"] = self.path
            out = json.dumps({"model": body["model"], "choices": [
                {"message": {"role": "assistant", "content": "<think>...</think>\n## 建議\n可核准"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    app.config.update(AI_BASE_URL=f"http://127.0.0.1:{srv.server_port}/v1", AI_MODEL="qwen3", AI_API_KEY="k")
    with app.app_context():
        text, model = ai.chat([{"role": "user", "content": "hi"}])
    srv.server_close()
    assert text == "## 建議\n可核准" and model == "qwen3"
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer k"
