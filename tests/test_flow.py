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
    monkeypatch.setenv("DR_BACKUP_DIR", str(tmp_path / "backups"))
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
    # 截圖還在辨識：不能送審（審核者要看到辨識結果）
    html = alice.post(f"/reports/{rid}/submit")
    assert "正在辨識系統時間" in html and "已送出審核" not in html
    finish_ocr(app, rid)
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
        # 預設複製上一份已核准日報的內容（檢查表勾選、紀錄、照片），截圖除外
        r1 = db.session.get(Report, rid)
        assert {(x.item_id, x.result, x.note) for x in r2.results} == {(x.item_id, x.result, x.note) for x in r1.results}
        assert any(x.result for x in r2.results)
        assert r2.results.filter_by(note="slow").count() == 1
        assert [i.kind for i in r1.images if i.kind == "screenshot"] and not [i for i in r2.images if i.kind == "screenshot"]
        assert len(r2.issues) == len(r1.issues) and r2.inspection_summary == r1.inspection_summary
        assert (r2.alarm_new, r2.alarm_false) == (0, r1.alarm_false)
        assert r2.doc_no == "CFXD-SCA-RLY-DPR-000896" and r2.status == "draft" and r2.revision == 0

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


def finish_ocr(app, rid):
    """測試沒有 OCR 背景服務：把截圖標成已辨識。"""
    from app import db
    from app.models import ReportImage

    with app.app_context():
        for img in ReportImage.query.filter_by(report_id=rid, kind="screenshot"):
            img.ocr_status, img.ocr_result = "done", '{"status": "no_clock", "text": "", "ocr": {}}'
        db.session.commit()


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

    # 一線：可新增 / 編輯 / 送審，不能指派、刪除、管理帳號或代理
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-10"}))
    alice.get(f"/reports/{rid}/basic")
    alice.post(f"/reports/{rid}/basic", {"author_id": str(dave_id), "approver_id": "", "revision": "0"})
    with app.app_context():
        r = db.session.get(Report, rid)
        assert r.author_id == alice_id and r.approver_id == boss_id  # 指派被忽略
    alice.get(f"/reports/{rid}")
    assert "刪除日報" not in alice.get(f"/reports/{rid}")
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

    # 作廢功能已取消
    for url in (f"/reports/{rid}/void", f"/reports/{rid}/void-request", f"/reports/{rid}/void-request/decide"):
        assert boss.c.post(url, data={"csrf_token": boss.tok, "reason": "x"}).status_code == 404
    assert "作廢" not in boss.get(f"/reports/{rid}").split("處理紀錄")[0]

    # 刪除：需輸入文件編號；刪除後文件編號釋放（同一天重新建立得到同一個編號，也可覆寫給別份），稽核紀錄保留
    rid2 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-11"}))
    # 二線刪除：需經檔案管理員授權代理
    html = sl.get(f"/reports/{rid2}")
    assert "刪除日報" not in html
    assert "需要檔案管理員授權代理" in sl.post(f"/reports/{rid2}/delete", {"confirm_doc_no": "x"})
    with app.app_context():
        from datetime import date

        from app.models import ApprovalDelegation

        db.session.add(ApprovalDelegation(principal_id=boss_id, delegate_id=dave_id, start_date=date.today()))
        db.session.commit()
    assert "刪除日報" in sl.get(f"/reports/{rid2}")
    assert "請輸入完整文件編號" in sl.post(f"/reports/{rid2}/delete", {"confirm_doc_no": "wrong"})
    with app.app_context():
        doc = db.session.get(Report, rid2).doc_no
    assert f"文件編號 {doc} 已釋放" in sl.post(f"/reports/{rid2}/delete", {"confirm_doc_no": doc})
    with app.app_context():
        from app.models import AuditLog

        assert db.session.get(Report, rid2) is None
        assert AuditLog.query.filter_by(action="delete_report", target=doc).one()
    sl.get(f"/reports/{rid}/basic")
    base = {"revision": "0", "issue_purpose": "IFI - Issue for Information", "inspection_time": "10:00 AM",
            "first_line_officer": "Alice", "second_line_support": ""}
    assert "已儲存" in sl.post(f"/reports/{rid}/basic", {**base, "doc_no": doc, "contractor_doc_no": ""})  # 釋放的編號可再用
    sl.post(f"/reports/{rid}/basic", {**base, "doc_no": "", "contractor_doc_no": ""})
    rid3 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-11"}))
    with app.app_context():
        assert db.session.get(Report, rid3).doc_no == doc


def test_local_admin(app):
    from app import db
    from app.models import Report, User

    runner = app.test_cli_runner()
    res = runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "short"])
    assert res.exit_code != 0
    res = runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "a-very-long-pass"])
    assert res.exit_code == 0, res.output

    b = Browser(app)
    b.get("/login")
    assert "帳號或密碼錯誤" in b.post("/login", {"username": "sysadmin", "password": "wrong"})  # dev 模式也要驗證本機密碼
    html = b.post("/login", {"username": "sysadmin", "password": "a-very-long-pass"})
    assert "系統管理員" in html and "系統設定" in html

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

    # 系統管理員可刪除任何未結案的日報
    rid = report_id_from(other.post("/reports/new", {"report_date": "2026-11-12"}))
    b.get(f"/reports/{rid}")
    with app.app_context():
        doc = db.session.get(Report, rid).doc_no
    assert "已釋放" in b.post(f"/reports/{rid}/delete", {"confirm_doc_no": doc})


def test_create_admin_rename_from(app):
    """安裝後引導：把剛建立、還沒登入過的 sysadmin 改成操作者自己的帳號與密碼。"""
    from werkzeug.security import check_password_hash

    from app.models import User

    runner = app.test_cli_runner()
    assert runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "random-initial-pw"]).exit_code == 0
    res = runner.invoke(args=["create-admin", "--username", "Ops.Admin", "--rename-from", "sysadmin",
                              "--password", "my-own-password"])
    assert res.exit_code == 0, res.output
    with app.app_context():
        assert User.query.filter_by(username="sysadmin").first() is None
        u = User.query.filter_by(username="ops.admin").one()
        assert u.role == "admin" and check_password_hash(u.password_hash, "my-own-password")
    assert runner.invoke(args=["create-admin", "--username", "bad name!", "--password", "my-own-password"]).exit_code != 0
    # 已登入過的帳號不能被改名
    b = Browser(app)
    b.get("/login")
    b.post("/login", {"username": "ops.admin", "password": "my-own-password"})
    res = runner.invoke(args=["create-admin", "--username", "third", "--rename-from", "ops.admin", "--password", "my-own-password"])
    assert res.exit_code != 0 and "不能改名" in res.output


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

    # 直接複製前一天的已核准日報：計數滾動後與內容一致，不會有錯誤
    rid_copy = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-02"}))
    with app.app_context():
        rc = db.session.get(Report, rid_copy)
        assert run_checks(rc).counts()["錯誤"] == 0, [(i.severity, i.message) for i in run_checks(rc).issues]
        db.session.delete(rc)
        db.session.commit()

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
    from app import ai, db
    from app import ai_providers as prov
    from app.models import AIProvider, AIUsage, CheckReport, ReportImage

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

    # 啟用 AI：兩個服務，第一個失敗時改用第二個
    with app.app_context():
        prov.set_setting("ai_enabled", True)
        db.session.add_all([AIProvider(name="Local", preset="ollama", kind="openai", base_url="http://x/v1",
                                       model="qwen3", billing="self_hosted", external=False, priority=10),
                            AIProvider(name="Backup", preset="ollama", kind="openai", base_url="http://y/v1",
                                       model="llama", billing="self_hosted", external=False, priority=20)])
        db.session.commit()
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

        def fake(p, key, system, user, opts):
            if p.name == "Local":
                raise RuntimeError("connection refused")
            seen["user"] = user
            return prov.Reply("## 建議\n- **建議退回修正** <script>alert(1)</script>\n- [一-2] 告警計數不符",
                              "llama-3", 1200, 80)

        assert ai.process_one(db, caller=fake)
        snap = db.session.get(CheckReport, snap.id)
        assert snap.ai_status == "done" and snap.ai_model == "Backup / llama-3"
        assert "檢查結果.md" in seen["user"] and "## 總覽" in seen["user"] and "<details>" not in seen["user"]
        assert "先前被退回時的檢查結果" in seen["user"] and "Alarm 計數錯誤" in seen["user"]  # RAG
        assert "1 張截圖辨識失敗" in snap.markdown  # AI 產生前依最新 OCR 狀態更新 .md
        uses = AIUsage.query.order_by(AIUsage.id).all()
        assert [(u.provider_name, u.ok) for u in uses] == [("Local", False), ("Backup", True)]
        assert uses[1].input_tokens == 1200 and uses[1].check_report_id == snap.id
        assert not ai.process_one(db, caller=fake)

    html = boss.get(f"/reports/{rid}")
    assert "<strong>建議退回修正</strong>" in html and "&lt;script&gt;" in html and "<script>alert" not in html
    assert "Backup / llama-3" in html and "歷次檢查結果.md（3 份）" in html

    # 手動重新產生、下載 .md
    boss.post(f"/reports/{rid}/ai")
    with app.app_context():
        assert CheckReport.query.filter_by(report_id=rid, stage="manual", ai_status="pending").count() == 1
    r = boss.c.get(f"/reports/{rid}/checks.md")
    assert r.status_code == 200 and r.mimetype == "text/markdown" and "日報檢查結果" in r.get_data(as_text=True)
    alice.post(f"/reports/{rid}/ai", status=403)  # 撰寫人：送審中不能編輯也不能審核


def test_ai_billing_and_external(app):
    """按量預算、包月額度用完就跳過；外部服務需明確允許；金鑰加密保存。"""
    from app import db
    from app import ai_providers as prov
    from app.models import AIProvider, AIUsage

    with app.app_context():
        tok = AIProvider(name="Cloud", preset="openai", kind="openai", base_url="https://api.example/v1", model="m",
                         billing="token", price_in=2.0, price_out=10.0, monthly_budget=0.05, external=True, priority=1)
        mon = AIProvider(name="PTU", preset="azure_ptu", kind="azure_openai", base_url="https://r.openai.azure.com",
                         deployment="d", billing="monthly", monthly_fee=1000, monthly_token_quota=5000,
                         external=True, priority=2)
        db.session.add_all([tok, mon])
        tok.api_key_enc, tok.api_key_hint = prov.encrypt_key("sk-secret-1234"), "1234"
        db.session.commit()
        assert "sk-secret" not in tok.api_key_enc and prov.decrypt_key(tok) == "sk-secret-1234"

        assert all("外部服務" in prov.blocked_reason(p, False) for p in (tok, mon))
        prov.set_setting("ai_allow_external", True)
        db.session.commit()
        assert [p.name for p in prov.available_providers()] == ["Cloud", "PTU"]

        def fake(p, key, system, user, opts):
            assert key == ("sk-secret-1234" if p.name == "Cloud" else "")
            return prov.Reply("OK", "m", 10_000, 3_000)

        prov.call(tok, "s", "u", caller=fake)  # 10k*2 + 3k*10 = 0.05 USD
        assert AIUsage.query.one().cost == 0.05
        assert "預算上限" in prov.blocked_reason(tok, True)
        prov.call(mon, "s", "u", caller=fake)
        assert "額度" in prov.blocked_reason(mon, True)
        assert prov.available_providers() == []


def test_ai_adapters(app):
    """五種呼叫協定的實際 HTTP 請求格式與 token 計算（本機假伺服器）。"""
    import http.server
    import json
    import threading

    from app import db
    from app import ai_providers as prov
    from app.models import AIProvider

    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            if "/v1/messages" in self.path:
                out = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                       "content": [{"type": "text", "text": "claude ok"}], "stop_reason": "end_turn",
                       "stop_sequence": None, "usage": {"input_tokens": 11, "output_tokens": 3}}
            elif "generateContent" in self.path:
                out = {"candidates": [{"content": {"parts": [{"text": "gemini ok"}]}}],
                       "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4}}
            elif "/converse" in self.path:
                out = {"output": {"message": {"content": [{"text": "bedrock ok"}]}},
                       "usage": {"inputTokens": 13, "outputTokens": 5}}
            else:
                out = {"model": body.get("model", "dep"), "usage": {"prompt_tokens": 14, "completion_tokens": 6},
                       "choices": [{"message": {"content": "<think>x</think>openai ok"}}]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    cases = [
        (dict(kind="openai", base_url=base + "/v1", model="qwen3", token_param="max_completion_tokens"), "openai ok", 14),
        (dict(kind="azure_openai", base_url=base, deployment="gpt-dep", api_version="2024-10-21"), "openai ok", 14),
        (dict(kind="anthropic", base_url=base, model="claude-opus-5-5", send_temperature=False), "claude ok", 11),
        (dict(kind="gemini", base_url=base, model="gemini-x"), "gemini ok", 12),
        (dict(kind="bedrock", base_url=base, region="ap-northeast-1", model="anthropic.claude-opus-5-5"), "bedrock ok", 13),
    ]
    try:
        with app.app_context():
            for fields, want, tin in cases:
                p = AIProvider(name=fields["kind"], preset="custom_local", billing="self_hosted", external=False,
                               **fields)
                p.api_key_enc = prov.encrypt_key("key-" + fields["kind"])
                db.session.add(p)
                db.session.commit()
                r = prov.call(p, "SYS", "USER", purpose="test")
                assert (r.text, r.input_tokens) == (want, tin), fields["kind"]
    finally:
        srv.shutdown()
    by = {path.split("?")[0]: (h, b) for path, h, b in seen}
    h, b = by["/v1/chat/completions"]
    assert h["authorization"] == "Bearer key-openai" and b["max_completion_tokens"] == 16000
    assert b["messages"][0] == {"role": "system", "content": "SYS"}
    h, b = by["/openai/deployments/gpt-dep/chat/completions"]
    assert h["api-key"] == "key-azure_openai" and "model" not in b
    h, b = by["/v1/messages"]
    assert h["authorization"] == "Bearer key-anthropic" and b["system"] == "SYS" and "temperature" not in b
    h, b = by["/v1beta/models/gemini-x:generateContent"]
    assert h["x-goog-api-key"] == "key-gemini" and b["systemInstruction"]["parts"][0]["text"] == "SYS"
    h, b = by["/model/anthropic.claude-opus-5-5/converse"]
    assert h["authorization"] == "Bearer key-bedrock" and b["system"] == [{"text": "SYS"}]


def test_settings_page_and_admin_assignment(app):
    """系統設定只有系統管理員看得到；系統管理員可指派（AD 帳號可、一線使用者強制排除）；本機緊急帳號保留。"""
    from app import db
    from app.models import AIProvider, AuditLog, User

    runner = app.test_cli_runner()
    assert runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "a-very-long-pass"]).exit_code == 0
    root, bob, carl = Browser(app), Browser(app), Browser(app)
    root.get("/login")
    root.post("/login", {"username": "sysadmin", "password": "a-very-long-pass"})
    bob.login("bob")
    carl.login("carl")
    bob.get("/admin/settings/", status=403)
    with app.app_context():
        bid = User.query.filter_by(username="bob").one().id
        cid = User.query.filter_by(username="carl").one().id

    # 一線使用者不能被指派
    html = root.post("/admin/users", {"user_id": str(bid), "sys_admin": "1"})
    assert "一線使用者不能成為系統管理員" in html
    with app.app_context():
        assert not db.session.get(User, bid).is_admin
        u = db.session.get(User, bid)
        u.sys_admin = True  # 旗標殘留也無效
        db.session.commit()
        assert not db.session.get(User, bid).is_admin
    root.post("/admin/users", {"user_id": str(bid), "role": "second_line"})
    with app.app_context():
        assert db.session.get(User, bid).is_admin  # 二線 + 旗標 -> 系統管理員
        db.session.get(User, bid).sys_admin = False
        db.session.commit()
    root.post("/admin/users", {"user_id": str(bid), "sys_admin": "1"})
    html = bob.get("/admin/settings/ai")
    assert "系統設定" in html and "AI 服務" in html and "系統管理員" in bob.get("/")

    # 指派的系統管理員可以再指派別人、不能移除自己；改成一線使用者時自動移除
    root.post("/admin/users", {"user_id": str(cid), "role": "file_manager"})
    bob.get("/admin/users")
    bob.post("/admin/users", {"user_id": str(cid), "sys_admin": "1"})
    assert "不能移除自己" in bob.post("/admin/users", {"user_id": str(bid), "sys_admin": "0"})
    assert "不能把自己改成一線使用者" in bob.post("/admin/users", {"user_id": str(bid), "role": "first_line"})
    bob.post("/admin/users", {"user_id": str(cid), "role": "first_line"})
    with app.app_context():
        assert not db.session.get(User, cid).is_admin and not db.session.get(User, cid).sys_admin
        assert AuditLog.query.filter_by(action="set_admin").count() >= 3

    # 指令指派（初次設定）
    assert runner.invoke(args=["grant-admin", "--username", "carl"]).exit_code != 0  # 一線使用者

    # 新增服務：從範本帶入、金鑰加密、畫面不顯示金鑰
    bob.get("/admin/settings/providers/new?preset=anthropic")
    html = bob.post("/admin/settings/providers/new", {
        "preset": "anthropic", "name": "Claude", "kind": "anthropic", "model": "claude-opus-5-5", "base_url": "",
        "api_key": "sk-ant-secret-9876", "billing": "token", "currency": "usd", "price_in": "4", "price_out": "20",
        "monthly_budget": "50", "reset_day": "1", "enabled": "1", "external": "1", "token_param": "max_tokens"})
    assert "已儲存 Claude" in html and "sk-ant-secret" not in html
    with app.app_context():
        p = AIProvider.query.filter_by(name="Claude").one()
        assert p.api_key_hint == "9876" and p.currency == "USD" and "sk-ant" not in p.api_key_enc
        pid = p.id
    assert "末 4 碼 9876" in bob.get(f"/admin/settings/providers/{pid}")
    assert "外部服務，尚未允許" in bob.get("/admin/settings/ai")
    assert "請先在一般設定允許" in bob.post(f"/admin/settings/providers/{pid}/test")

    # 允許外部服務必須勾選確認
    form = {"ai_enabled": "1", "ai_auto_on_submit": "1", "ai_allow_external": "1", "ai_context_reports": "3",
            "ai_max_chars": "24000", "ai_timeout": "180", "ai_max_output_tokens": "16000", "ai_temperature": "0.2",
            "check_md_dir": ""}
    assert "請勾選確認" in bob.post("/admin/settings/general", form)
    assert "已儲存設定" in bob.post("/admin/settings/general", {**form, "external_ack": "1"})
    assert "運作中：1 個服務可用" in bob.get("/admin/settings/ai")
    assert "已儲存提示詞" in bob.post("/admin/settings/prompt", {"ai_system_prompt": "自訂規則"})
    assert "自訂規則" in bob.get("/admin/settings/ai")


def test_healthz_and_doctor(app):
    """部署健檢：/healthz 不需登入且不含資料；doctor-json 輸出可解析、會指出預設 SECRET_KEY 與缺少的範本。"""
    import json

    r = app.test_client().get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}
    app.config["PDF_TEMPLATE_XLSX"] = "/nonexistent/checklist.xlsx"
    res = app.test_cli_runner().invoke(args=["doctor-json"])
    assert res.exit_code == 0, res.output
    items = {(x["group"], x["name"]): x for x in json.loads(res.output.strip().splitlines()[-1])}
    assert items[("設定", "SECRET_KEY")]["status"] == "fail"  # 測試用 SECRET_KEY 太短
    assert items[("PDF 輸出", "CheckList 範本")]["status"] == "fail" and "PDF 與上傳" in items[("PDF 輸出", "CheckList 範本")]["fix"]
    assert items[("資料庫", "完整性")]["status"] == "ok"
    assert all(x["status"] in ("ok", "warn", "fail") for x in items.values())


def _admin_browser(app):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "a-very-long-pass"]).exit_code == 0
    b = Browser(app)
    b.get("/login")
    b.post("/login", {"username": "sysadmin", "password": "a-very-long-pass"})
    return b


def test_ldap_multiple_groups(app, monkeypatch):
    """允許登入群組與檔案管理員群組都可以設定多個（屬於任一個即可）。"""
    import re

    from app import auth, sysconf

    assert sysconf.group_list("CN=A,DC=x\r\n\n CN=B,DC=x ;CN=a,DC=x;CN=C\\;D,DC=x") == ["CN=A,DC=x", "CN=B,DC=x", "CN=C\\;D,DC=x"]
    f = auth._in_group_filter("bob", ["CN=A,DC=x", "CN=B (x),DC=x"])
    assert f == ("(&(sAMAccountName=bob)(|(memberOf:1.2.840.113556.1.4.1941:=CN=A,DC=x)"
                 "(memberOf:1.2.840.113556.1.4.1941:=CN=B \\28x\\29,DC=x)))")
    assert auth._in_group_filter("bob", "CN=A,DC=x") == "(&(sAMAccountName=bob)(memberOf:1.2.840.113556.1.4.1941:=CN=A,DC=x))"

    # 假的 AD：bob 屬於 ITI，carl 屬於 hsadmin，dave 都不屬於
    member = {"bob": {"cn=iti,dc=ad,dc=tw", "cn=staff,dc=ad,dc=tw"}, "carl": {"cn=hsadmin,dc=ad,dc=tw", "cn=staff,dc=ad,dc=tw"},
              "dave": {"cn=staff,dc=ad,dc=tw"}}

    class Entry:
        displayName = mail = None

    class FakeConn:
        def __init__(self, server, user=None, password=None, auto_bind=False):
            self.user, self.entries = (user or "").split("@")[0], []

        def search(self, base, flt, scope, attributes=None):
            groups = {g.lower() for g in re.findall(r"1941:=([^)]*)\)", flt)}
            ok = not groups or groups & member.get(self.user, set())
            self.entries = [Entry()] if ok else []

        def unbind(self):
            pass

    monkeypatch.setattr(auth, "Connection", FakeConn)
    import socket

    class FakeSock:
        def close(self):
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: FakeSock())
    with app.app_context():
        for k, v in {"ldap_url": "ldaps://dc.ad.tw:636", "ad_domain": "ad.tw", "ldap_base_dn": "DC=ad,DC=tw",
                     "ldap_required_group": "", "ldap_approver_group": "CN=MIS,DC=ad,DC=tw\nCN=hsadmin,DC=ad,DC=tw\nCN=ITI,DC=ad,DC=tw"}.items():
            sysconf.put(k, v)
        assert auth.ad_authenticate("bob", "pw")[2] is True  # 第三個群組
        assert auth.ad_authenticate("carl", "pw")[2] is True  # 第二個群組
        assert auth.ad_authenticate("dave", "pw")[2] is False
        sysconf.put("ldap_required_group", "CN=ITI,DC=ad,DC=tw\nCN=hsadmin,DC=ad,DC=tw")
        assert auth.ad_authenticate("bob", "pw") and auth.ad_authenticate("carl", "pw")
        assert auth.ad_authenticate("dave", "pw") is None  # 兩個允許群組都不屬於
        c = {**sysconf.ldap_config()}
        steps = auth.ldap_test(c, "carl", "pw")
        assert any("hsadmin" in d and "檔案管理員" in d for _, s, d in steps if s == "檔案管理員群組"), steps


def test_ldap_failover(app, monkeypatch):
    """多台網域控制站：連不上（含 TLS 錯誤）換下一台；帳號密碼錯誤不換台。"""
    from ldap3.core.exceptions import LDAPBindError, LDAPSocketOpenError

    from app import auth, sysconf

    assert sysconf.url_list(" ldaps://a:636\r\nldaps://b:636 ; ldaps://A:636,ldap://c") == ["ldaps://a:636", "ldaps://b:636", "ldap://c"]
    tried = []

    class Entry:
        displayName = mail = None

    class FakeConn:
        def __init__(self, server, user=None, password=None, auto_bind=False):
            tried.append(server.host)
            if server.host in ("dc01", "dc02"):
                raise LDAPSocketOpenError("socket ssl wrapping error: certificate verify failed")
            if password == "wrong":
                raise LDAPBindError("invalidCredentials 52e")
            self.entries = []

        def search(self, *a, **k):
            self.entries = [Entry()]

        def unbind(self):
            pass

    monkeypatch.setattr(auth, "Connection", FakeConn)
    with app.app_context():
        for k, v in {"ldap_url": "ldaps://dc01:636\nldaps://dc02:636\nldaps://dc03:636", "ad_domain": "ad.tw",
                     "ldap_base_dn": "DC=ad,DC=tw", "ldap_required_group": "", "ldap_approver_group": ""}.items():
            sysconf.put(k, v)
        assert auth.ad_authenticate("bob", "pw") == ("bob", None, False)
        assert tried == ["dc01", "dc02", "dc03"]
        tried.clear()
        assert auth.ad_authenticate("bob", "wrong") is None
        assert tried == ["dc01", "dc02", "dc03"]  # 前兩台連不上才換；第三台回答密碼錯誤就停
        sysconf.put("ldap_url", "ldaps://dc03:636\nldaps://dc01:636")
        tried.clear()
        assert auth.ad_authenticate("bob", "wrong") is None and tried == ["dc03"]  # 密碼錯誤不會再試其他台
        sysconf.put("ldap_url", "ldaps://dc01:636\nldaps://dc02:636")
        assert auth.ad_authenticate("bob", "pw") is None  # 全部連不上


def test_admin_ldap_and_local_accounts(app):
    """AD 設定在設定頁（.env 只是舊版沿用的預設值）、測試不會儲存；本機緊急帳號在帳號管理頁新增 / 重設 / 停用。"""
    from app import db, sysconf
    from app.auth import ad_authenticate
    from app.models import AuditLog, User

    app.config["LDAP_URL"] = "ldaps://old.example.test:636"
    b = _admin_browser(app)
    for tab in ("/admin/settings/", "/admin/settings/ldap", "/admin/users", "/admin/settings/backup",
                "/admin/settings/pdf", "/admin/settings/tls", "/admin/settings/ai"):
        html = b.get(tab)
        assert "admin-tabs" in html, tab
    assert "沿用安裝設定檔" in b.get("/admin/settings/ldap")

    form = {"ldap_url": "ldaps://127.0.0.1:1", "ad_domain": "Corp.Local", "ldap_base_dn": "DC=corp,DC=local",
            "ldap_required_group": "", "ldap_approver_group": "", "approver_users": "Boss, alice",
            "ldap_timeout": "2", "ldap_ca_pem": ""}
    html = b.post("/admin/settings/ldap/test", form)
    assert "無法連線" in html and "還沒有儲存" in html
    with app.app_context():
        assert sysconf.get("ldap_url") == "ldaps://old.example.test:636"  # 測試不儲存
    assert "格式" in b.post("/admin/settings/ldap", {**form, "ldap_url": "http://x"})
    assert "「http://y」格式" in b.post("/admin/settings/ldap", {**form, "ldap_url": "ldaps://a:636\nhttp://y"})
    assert "CA 憑證不是 PEM" in b.post("/admin/settings/ldap", {**form, "ldap_ca_pem": "garbage"})
    assert "不是群組 DN" in b.post("/admin/settings/ldap", {**form, "ldap_approver_group": "CN=MIS,DC=corp,DC=local\nMIS"})
    groups = "CN=MIS,OU=MIS,DC=corp,DC=local\r\n\r\nCN=ITI,OU=ITI,DC=corp,DC=local\r\nCN=MIS,OU=MIS,DC=corp,DC=local"
    assert "已儲存 AD 設定" in b.post("/admin/settings/ldap", {**form, "ldap_approver_group": groups})
    with app.app_context():
        assert sysconf.ldap_config()["approver_groups"] == ["CN=MIS,OU=MIS,DC=corp,DC=local", "CN=ITI,OU=ITI,DC=corp,DC=local"]
    assert "CN=ITI,OU=ITI,DC=corp,DC=local</textarea>" in b.get("/admin/settings/ldap")
    assert "已儲存 AD 設定" in b.post("/admin/settings/ldap", form)
    with app.app_context():
        c = sysconf.ldap_config()
        assert c["ldap_url"] == "ldaps://127.0.0.1:1" and c["ad_domain"] == "corp.local"
        assert c["approver_users_set"] == {"alice", "boss"} and sysconf.source("ldap_url") == "db"
        with app.test_request_context():
            assert ad_authenticate("alice", "") is None and ad_authenticate("alice", "x") is None  # 連不上 -> 失敗，不例外

    # 本機緊急帳號
    assert "至少需要 12" in b.post("/admin/settings/local/create", {"username": "breakglass", "password": "short", "password2": "short"})
    assert "不一致" in b.post("/admin/settings/local/create", {"username": "breakglass", "password": "x" * 12, "password2": "y" * 12})
    assert "已新增本機緊急帳號 breakglass" in b.post("/admin/settings/local/create",
                                                 {"username": "breakglass", "password": "p" * 14, "password2": "p" * 14})
    erin = Browser(app)
    erin.login("erin")
    assert "已存在" in b.post("/admin/settings/local/create", {"username": "erin", "password": "p" * 14, "password2": "p" * 14})
    with app.app_context():
        me = User.query.filter_by(username="sysadmin").one().id
        bg = User.query.filter_by(username="breakglass").one().id
        assert db.session.get(User, bg).is_admin
    assert "目前的密碼不正確" in b.post(f"/admin/settings/local/{me}/password",
                                         {"current_password": "nope", "password": "n" * 13, "password2": "n" * 13})
    assert "已更新 sysadmin" in b.post(f"/admin/settings/local/{me}/password",
                                       {"current_password": "a-very-long-pass", "password": "n" * 13, "password2": "n" * 13})
    assert "已更新 breakglass" in b.post(f"/admin/settings/local/{bg}/password", {"password": "q" * 13, "password2": "q" * 13})
    nb = Browser(app)
    nb.get("/login")
    assert "系統設定" in nb.post("/login", {"username": "breakglass", "password": "q" * 13})
    assert "不能停用自己" in b.post(f"/admin/settings/local/{me}/disable")
    assert "已停用 breakglass" in b.post(f"/admin/settings/local/{bg}/disable")
    with app.app_context():
        u = db.session.get(User, bg)
        assert not u.is_admin and u.password_hash is None and u.username.startswith("breakglass#")
        assert AuditLog.query.filter_by(action="local_account").count() == 4
    assert "<td>breakglass" not in b.get("/admin/users").split("稽核紀錄")[0]
    nb.get("/admin/settings/", status=403)  # 停用後原本的登入階段也失去權限
    erin.get("/admin/settings/ldap", status=403)
    erin.post("/admin/settings/local/create", {"username": "evil", "password": "p" * 14, "password2": "p" * 14}, status=403)


def test_admin_backup_restore_and_health(app, tmp_path):
    """備份 / 還原由維護背景服務執行，格式與 dailyreport.sh 相同；健康檢查結果顯示在設定頁、失敗時頁首提醒。"""
    import os
    import subprocess

    from app import backup, db, maint, sysconf
    from app.models import MaintJob, Report

    app.instance_path = str(tmp_path / "instance")
    os.makedirs(os.path.join(app.instance_path, "pdf_cache"))
    os.makedirs(os.path.join(app.instance_path, "fonts"))
    with open(os.path.join(app.instance_path, "fonts", "a.ttf"), "wb") as fh:
        fh.write(b"\x00\x01\x00\x00font")
    b = _admin_browser(app)
    rid = report_id_from(b.post("/reports/new", {"report_date": "2026-10-01"}))

    assert "必須是絕對路徑" in b.post("/admin/settings/backup/config", {"backup_dir": "rel", "backup_time": "02:30", "backup_keep": "3"})
    assert "HH:MM" in b.post("/admin/settings/backup/config", {"backup_time": "25:00", "backup_keep": "3"})
    assert "不能放在資料夾 instance" in b.post("/admin/settings/backup/config", {
        "backup_dir": os.path.join(app.instance_path, "bk"), "backup_time": "02:30", "backup_keep": "3"})
    assert "已儲存備份設定" in b.post("/admin/settings/backup/config", {
        "backup_enabled": "1", "backup_time": "02:30", "backup_keep": "2", "backup_dir": "", "backup_include_env": "1"})
    assert "已排入備份" in b.post("/admin/settings/backup/now")
    assert "已有備份或還原工作" in b.post("/admin/settings/backup/now")
    with app.app_context():
        maint.tick()
        job = MaintJob.query.order_by(MaintJob.id.desc()).first()
        assert job.status == "done", job.message
        items = backup.list_backups()
        assert len(items) == 1 and items[0]["meta"]["trigger"] == "manual" and items[0]["meta"]["reports"] == 1
        name = items[0]["name"]
        path = backup.path_of(name)
        # dailyreport.sh restore 用 sha256sum -c 驗證：格式必須相容
        assert subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=path).returncode == 0
        assert {"dailyreport.db", "instance.tar.gz", "VERSION", "meta.json", "SHA256SUMS"} <= set(os.listdir(path))
        import tarfile

        with tarfile.open(os.path.join(path, "instance.tar.gz")) as t:
            names = t.getnames()
        assert "instance/fonts/a.ttf" in names and not any("pdf_cache" in n for n in names)

    html = b.get("/admin/settings/backup")
    assert name in html and "資料庫" in html
    r = b.c.get(f"/admin/settings/backup/{name}/dailyreport.db")
    assert r.status_code == 200 and r.data[:15] == b"SQLite format 3"
    assert b.c.get(f"/admin/settings/backup/{name}/env").status_code == 404
    assert b.c.get("/admin/settings/backup/../../etc/dailyreport.db").status_code == 404

    # 備份之後的變更，還原後消失；還原前會自動備份
    b.post(f"/reports/{rid}/delete", {"reason": "x"})
    rid2 = report_id_from(b.post("/reports/new", {"report_date": "2026-10-02"}))
    os.remove(os.path.join(app.instance_path, "fonts", "a.ttf"))
    assert "請輸入備份日期" in b.post(f"/admin/settings/backup/{name}/restore", {"confirm": "x"})
    assert "已排入還原" in b.post(f"/admin/settings/backup/{name}/restore", {"confirm": name[:8]})
    with app.app_context():
        maint.tick()
        job = MaintJob.query.order_by(MaintJob.id.desc()).first()
        assert job.kind == "restore" and job.status == "done", job.message
        assert MaintJob.query.filter(MaintJob.status.in_(("pending", "running"))).count() == 0  # 不會卡住
        assert db.session.get(Report, rid) is not None and db.session.get(Report, rid2) is None
        assert os.path.exists(os.path.join(app.instance_path, "fonts", "a.ttf"))
        triggers = sorted(i["meta"]["trigger"] for i in backup.list_backups())
        assert triggers == ["manual", "pre_restore"]
    assert "已還原" in b.get("/admin/settings/backup")
    assert "還原" in b.get("/admin/users")  # 稽核紀錄

    # 損壞的備份不能還原
    with app.app_context():
        with open(os.path.join(backup.path_of(name), "dailyreport.db"), "ab") as fh:
            fh.write(b"x")
    assert "檢查碼不符" in b.post(f"/admin/settings/backup/{name}/restore", {"confirm": name[:8]})

    # 排程：到時間才做、同一天只做一次、保留份數
    from datetime import datetime

    with app.app_context():
        sysconf.put("backup_last_scheduled", "")
        db.session.commit()
        assert maint.backup_due(datetime(2026, 11, 3, 3, 0))
        sysconf.put("backup_last_scheduled", "2026-11-03T02:31:00")
        db.session.commit()
        assert not maint.backup_due(datetime(2026, 11, 3, 23, 0))
        assert maint.backup_due(datetime(2026, 11, 4, 2, 31))
        assert not maint.backup_due(datetime(2026, 11, 4, 2, 29))

    # 健康檢查
    html = b.post("/admin/settings/health/run")
    assert "健康檢查完成" in html and "備份位置" in html and "服務" in html
    with app.app_context():
        last = sysconf.get("health_last")
        assert last["fail"] >= 1  # 測試環境：SECRET_KEY 太短、沒有範本
    assert "健康檢查有" in b.get("/")
    assert "已儲存健康檢查設定" in b.post("/admin/settings/health/config", {"health_enabled": "1", "health_interval_hours": "12"})


def test_admin_pdf_upload_and_tls(app, tmp_path):
    """範本與字型從設定頁上傳（會檢查內容）；上傳上限可調；HTTPS 憑證檢查配對後交給主機套用。"""
    import datetime as dt
    import io
    import json
    import os

    import openpyxl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    app.instance_path = str(tmp_path / "instance")
    app.config["PDF_TEMPLATE_XLSX"] = str(tmp_path / "instance" / "templates" / "checklist.xlsx")
    app.config["FONT_DIR"] = str(tmp_path / "instance" / "fonts")
    b = _admin_browser(app)
    assert "尚未上傳範本" in b.get("/admin/settings/pdf")

    def xlsx(sheets):
        wb = openpyxl.Workbook()
        wb.active.title = sheets[0]
        for s in sheets[1:]:
            wb.create_sheet(s)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    html = b.post("/admin/settings/pdf/template", {"template": (xlsx(["Sheet"]), "x.xlsx")}, content_type="multipart/form-data")
    assert "缺少工作表" in html
    html = b.post("/admin/settings/pdf/template", {"template": (io.BytesIO(b"nope"), "x.xlsx")}, content_type="multipart/form-data")
    assert "無法讀取" in html
    html = b.post("/admin/settings/pdf/template", {"template": (xlsx(["Alarm Summary", "Summary", "Virtual Machines", "Software", "Physical devices", "PI"]), "CheckList.xlsx")},
                  content_type="multipart/form-data")
    assert "已更新 CheckList 範本" in html and "下載目前的範本" in html
    # 上傳後系統狀態頁不再顯示變更前的檢查結果：維護服務下一輪重新檢查
    from app import maint, sysconf

    with app.app_context():
        assert sysconf.get("health_refresh") and maint.health_due()
        maint.tick()
        last = sysconf.get("health_last")
        assert not sysconf.get("health_refresh")
        assert any(i["name"] == "CheckList 範本" and i["status"] == "ok" for i in last["items"])
    assert b.c.get("/admin/settings/pdf/template").status_code == 200

    html = b.post("/admin/settings/pdf/font", {"fonts": [(io.BytesIO(b"\x00\x01\x00\x00abc"), "calibri.ttf"),
                                                          (io.BytesIO(b"MZ"), "evil.ttf"), (io.BytesIO(b"x"), "a.exe")]},
                  content_type="multipart/form-data")
    assert "已上傳 1 個字型" in html and "evil.ttf 不是有效" in html
    assert os.listdir(app.config["FONT_DIR"]) == ["calibri.ttf"]
    assert "已刪除 calibri.ttf" in b.post("/admin/settings/pdf/font/delete", {"name": "calibri.ttf"})

    # 上傳上限：1 MB 時 2 MB 的上傳被拒絕
    assert "1 MB" in b.post("/admin/settings/pdf/upload", {"max_upload_mb": "1"})
    r = b.c.post("/admin/settings/pdf/font", data={"csrf_token": b.tok, "fonts": (io.BytesIO(b"\0" * (2 << 20)), "big.ttf")},
                 content_type="multipart/form-data")
    assert r.status_code == 413
    b.post("/admin/settings/pdf/upload", {"max_upload_mb": "20"})

    # HTTPS 憑證
    def make(cn, days=365):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=days))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False).sign(key, hashes.SHA256()))
        return (cert.public_bytes(serialization.Encoding.PEM),
                key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    c1, k1 = make("report.corp.local")
    c2, k2 = make("other")
    assert "不配對" in b.post("/admin/settings/tls/upload", {"cert_pem": c1.decode(), "key_pem": k2.decode()})
    assert "不是 PEM" in b.post("/admin/settings/tls/upload", {"cert_pem": "x", "key_pem": k1.decode()})
    html = b.post("/admin/settings/tls/upload", {"cert_pem": c1.decode(), "key_pem": k1.decode()})
    assert "憑證檢查通過（report.corp.local" in html and "正在套用" in html
    d = os.path.join(app.instance_path, "tls_request")
    assert json.load(open(os.path.join(d, "request.json")))["action"] == "custom"
    assert oct(os.stat(os.path.join(d, "key.pem")).st_mode & 0o777) == "0o600"
    # 主機的 apply-tls 完成後寫回 result.json
    os.remove(os.path.join(d, "request.json"))
    with open(os.path.join(d, "result.json"), "w") as fh:
        json.dump({"ok": True, "at": "2026-11-03 10:00", "message": "已套用 report.corp.local"}, fh)
    assert "已套用 report.corp.local" in b.get("/admin/settings/tls")


def test_doc_no_override(app):
    """二線管理員以上可以覆寫 Document No. / Contractor Doc No.；一線使用者不行；不能與其他日報重複。"""
    from app import db
    from app.models import Report, ReviewLog

    alice = Browser(app)
    alice.login("alice")
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-04"}))
    rid2 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-05"}))
    base = {"revision": "0", "issue_purpose": "IFI - Issue for Information", "inspection_time": "10:00 AM",
            "first_line_officer": "Alice", "second_line_support": ""}
    with app.app_context():
        assert db.session.get(Report, rid).doc_no == "CFXD-SCA-RLY-DPR-000897"

    # 一線使用者：欄位唯讀，送出也不會改
    html = alice.get(f"/reports/{rid}/basic")
    assert 'name="doc_no"' not in html and "二線管理員需經授權代理" in html
    alice.post(f"/reports/{rid}/basic", {**base, "doc_no": "HACK-1", "contractor_doc_no": "HACK-2"})
    with app.app_context():
        assert db.session.get(Report, rid).doc_no == "CFXD-SCA-RLY-DPR-000897"

    carol, boss = Browser(app), Browser(app)
    carol.login("carol")
    boss.login("boss")
    carol_id = set_role(app, "carol", "second_line")
    boss_id = set_role(app, "boss", "file_manager")
    # 二線未經授權：欄位唯讀，送出也不會改
    assert 'name="doc_no"' not in carol.get(f"/reports/{rid}/basic")
    carol.post(f"/reports/{rid}/basic", {**base, "doc_no": "NO-DELEGATION", "contractor_doc_no": "X"})
    with app.app_context():
        assert db.session.get(Report, rid).doc_no == "CFXD-SCA-RLY-DPR-000897"
        from datetime import date

        from app.models import ApprovalDelegation

        db.session.add(ApprovalDelegation(principal_id=boss_id, delegate_id=carol_id, start_date=date.today()))
        db.session.commit()
    assert 'name="doc_no"' in carol.get(f"/reports/{rid}/basic")
    html = carol.post(f"/reports/{rid}/basic", {**base, "doc_no": " CFXD-SCA-RLY-DPR-000900 ",
                                                "contractor_doc_no": "RT230018-CFXD SLA-RP-000800"})
    assert "已儲存" in html and "已覆寫，自動編號 CFXD-SCA-RLY-DPR-000897" in html
    with app.app_context():
        r = db.session.get(Report, rid)
        assert (r.doc_no, r.contractor_doc_no) == ("CFXD-SCA-RLY-DPR-000900", "RT230018-CFXD SLA-RP-000800")
        log = ReviewLog.query.filter_by(report_id=rid, action="doc_no").one()
        assert "000897 → CFXD-SCA-RLY-DPR-000900" in log.comment
    # 與另一份日報重複：拒絕，整筆不儲存
    html = carol.post(f"/reports/{rid2}/basic", {**base, "revision": "3", "doc_no": "cfxd-sca-rly-dpr-000900",
                                                 "contractor_doc_no": ""})
    assert "已用在 2026-11-04 的日報" in html
    with app.app_context():
        assert db.session.get(Report, rid2).revision == 0
    # 清空 = 恢復自動編號
    carol.post(f"/reports/{rid}/basic", {**base, "doc_no": "", "contractor_doc_no": ""})
    with app.app_context():
        r = db.session.get(Report, rid)
        assert (r.doc_no, r.contractor_doc_no) == ("CFXD-SCA-RLY-DPR-000897", "RT230018-CFXD SLA-RP-000797")


def test_new_report_copy_option(app):
    """新增日報：預設複製上一份已核准日報（截圖除外）；可取消；沒有已核准日報時只沿用固定資料。"""
    import os

    from app import db
    from app.models import ChecklistResult, Report, ReportImage, ReviewLog

    alice = Browser(app)
    alice.login("alice")
    assert 'name="copy_prev" value="1" checked' in alice.get("/")
    r1 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-01"}))
    alice.post(f"/reports/{r1}/basic", {"revision": "2", "issue_purpose": "IFI - Issue for Information", "inspection_time": "09:30 AM",
                                         "first_line_officer": "Alice", "second_line_support": "Bob"})
    up = app.config["UPLOAD_DIR"]
    with app.app_context():
        rep = db.session.get(Report, r1)
        rep.inspection_summary = "Router reboot"
        first = rep.results.first()
        first.result, first.note = "W", "fan noise"
        os.makedirs(os.path.join(up, str(r1)), exist_ok=True)
        for kind, name in (("screenshot", "s.png"), ("onsite", "o.png"), ("alarm", "a.png")):
            with open(os.path.join(up, str(r1), name), "wb") as fh:
                fh.write(kind.encode())
            db.session.add(ReportImage(report_id=r1, kind=kind, filename=name, original_name=name))
        db.session.commit()
        item_id = first.item_id

    # 上一份還不是正式（已核准）日報：只沿用固定資料，不複製檢查表與照片
    r2 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-02"}))
    with app.app_context():
        rep = db.session.get(Report, r2)
        assert rep.second_line_support == "Bob" and rep.inspection_summary != "Router reboot" and not rep.images
        assert rep.results.filter_by(item_id=item_id).one().result is None
        db.session.delete(rep)
        db.session.get(Report, r1).status = "approved"
        db.session.commit()

    html = alice.post("/reports/new", {"report_date": "2026-11-02", "copy_shown": "1", "copy_prev": "1"})
    assert "已複製 2026-11-01 已核准日報的內容（截圖除外）" in html
    r2 = report_id_from(html)
    with app.app_context():
        rep = db.session.get(Report, r2)
        assert rep.inspection_summary == "Router reboot" and rep.inspection_time == "09:30 AM"
        assert rep.revision == 0 and rep.status == "draft" and rep.doc_no.endswith("000895")
        res = rep.results.filter_by(item_id=item_id).one()
        assert (res.result, res.note) == ("W", "fan noise")
        kinds = sorted(i.kind for i in rep.images)
        assert kinds == ["alarm", "onsite"]  # 截圖不複製
        for i in rep.images:  # 照片檔案是複本：刪除舊日報的檔案不影響新日報
            assert os.path.isfile(os.path.join(up, str(r2), i.filename))
        assert not ReviewLog.query.filter_by(report_id=r2).count()

    # 取消勾選：只沿用固定資料
    r3 = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-03", "copy_shown": "1"}))
    with app.app_context():
        rep = db.session.get(Report, r3)
        assert rep.inspection_summary != "Router reboot" and not rep.images
        assert db.session.query(ChecklistResult).filter_by(report_id=r3, result="W").count() == 0


class FakeGraph:
    """本機假的 Microsoft Graph：權杖、網站、文件庫、分段上傳、分享連結、寄信；可指定某個步驟失敗一次。"""

    def __init__(self):
        import http.server
        import json
        import threading

        g = self
        self.uploads, self.mails, self.links, self.fail = {}, [], [], set()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                body = self._body()
                p = self.path
                if p.endswith("/oauth2/v2.0/token"):
                    assert b"client_secret=s3cret" in body
                    return self._send(200, {"access_token": "tok", "expires_in": 3600})
                assert self.headers["Authorization"] == "Bearer tok"
                if p.endswith(":/createUploadSession"):
                    g.last_path = p
                    return self._send(200, {"uploadUrl": f"http://127.0.0.1:{g.port}/upload/1"})
                if p.endswith("/createLink"):
                    if "link" in g.fail:
                        g.fail.discard("link")
                        return self._send(403, {"error": {"message": "Sharing is disabled for this site"}})
                    g.links.append(json.loads(body))
                    return self._send(200, {"link": {"webUrl": "https://contoso.sharepoint.com/:b:/s/CFXD/share123"}})
                if p.endswith("/sendMail"):
                    if "mail" in g.fail:
                        g.fail.discard("mail")
                        return self._send(500, {"error": {"message": "mailbox unavailable"}})
                    g.mails.append((p, json.loads(body)))
                    return self._send(202)
                self._send(404, {"error": {"message": "no route " + p}})

            def do_GET(self):
                p = self.path
                if p.startswith("/v1.0/sites/contoso.sharepoint.com:/sites/CFXD"):
                    return self._send(200, {"id": "site1"})
                if p == "/v1.0/sites/site1/drive":
                    return self._send(200, {"id": "drive1", "name": "Documents"})
                self._send(404, {"error": {"message": "no route " + p}})

            def do_PUT(self):
                assert "Authorization" not in self.headers  # uploadUrl 已含授權
                data = self._body()
                start, rest = self.headers["Content-Range"].split(" ")[1].split("-")
                end, total = rest.split("/")
                buf = g.uploads.setdefault(g.last_path, bytearray())
                assert int(start) == len(buf)
                buf.extend(data)
                if int(end) + 1 < int(total):
                    return self._send(202, {"nextExpectedRanges": [f"{int(end) + 1}-"]})
                self._send(201, {"id": "item1", "size": len(buf), "webUrl": "https://contoso.sharepoint.com/x.pdf"})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _approved_report(app, alice, boss, day):
    rid = submitted_report(app, alice, day)
    boss.get(f"/reports/{rid}")
    assert "已核准" in boss.post(f"/reports/{rid}/review", {"action": "approve"})
    return rid


def test_close_and_delivery(app, monkeypatch):
    """結案 → 鎖檔 1 分鐘 → 正式 PDF（MD5）→ 上傳 SharePoint → 連結 → 寄信；失敗中止、重試從失敗步驟繼續。"""
    import hashlib
    from datetime import datetime, timedelta

    from app import db, delivery, graph, maint, sysconf
    from app.models import Delivery, Report

    fake = FakeGraph()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    app.config["GRAPH_BASE"] = f"http://127.0.0.1:{fake.port}/v1.0"
    app.config["GRAPH_LOGIN_BASE"] = f"http://127.0.0.1:{fake.port}"
    graph._tokens.clear()
    pdf_bytes = b"%PDF-1.7 final " + b"x" * (graph.CHUNK + 1234)  # 超過一個分段
    calls = []

    def fake_render(report, preview=False):
        calls.append(preview)
        return pdf_bytes

    monkeypatch.setattr("app.pdf.report_pdf.render_pdf", fake_render)

    alice, boss, carol, vic = Browser(app), Browser(app), Browser(app), Browser(app)
    alice.login("alice")
    boss.login("boss")
    carol.login("carol")
    vic.login("vic")
    set_role(app, "boss", "file_manager")
    set_role(app, "carol", "second_line")
    set_role(app, "vic", "viewer")
    rid = _approved_report(app, alice, boss, "2026-11-04")

    # 一線不能結案；二線可以；只能結案一次
    html = alice.get(f"/reports/{rid}")
    assert "結案</button>" not in html and "流程" in html and "審核通過" in html
    assert "只有二線管理員以上可以結案" in alice.post(f"/reports/{rid}/close")
    html = carol.post(f"/reports/{rid}/close")
    assert "已結案並鎖檔" in html and "鎖檔中" in html
    assert "只有已核准的日報可以結案" in boss.post(f"/reports/{rid}/close")
    # 結案後不能再改、不能刪除；二線不能取消結案，檔案管理員可以（發送開始前）
    assert "取消結案" not in carol.get(f"/reports/{rid}")
    assert boss.c.post(f"/reports/{rid}/delete", data={"csrf_token": boss.tok, "confirm_doc_no": "x"}).status_code == 403
    html = boss.post(f"/reports/{rid}/close/cancel", {"reason": "排版要再確認"})
    assert "已取消結案" in html
    with app.app_context():
        assert db.session.get(Report, rid).status == "approved" and not Delivery.query.count()
    carol.post(f"/reports/{rid}/close")

    with app.app_context():
        for k, v in {"delivery_enabled": True, "graph_tenant_id": "tenant", "graph_client_id": "client",
                     "sp_site_url": "https://contoso.sharepoint.com/sites/CFXD", "sp_folder": "Daily/{yyyy}/{mm}",
                     "mail_sender": "dr@contoso.com", "mail_to": "customer@client.com, pm@client.com",
                     "mail_cc": "boss@contoso.com"}.items():
            sysconf.put(k, v)
        graph.set_secret("s3cret")
        db.session.commit()
        fake.fail = {"mail"}
        # 鎖檔 1 分鐘內不執行
        assert not delivery.tick(datetime.now() + timedelta(seconds=30))
        maint.tick(datetime.now() + timedelta(seconds=61))
        d = Delivery.query.one()
        st = d.step_map()
        assert d.state == "error" and [st[k]["status"] for k in ("pdf", "upload", "link", "mail")] == ["done"] * 3 + ["error"]
        assert "mailbox unavailable" in st["mail"]["message"]
        assert d.pdf_md5 == hashlib.md5(pdf_bytes).hexdigest() and calls == [False]
        assert bytes(fake.uploads[fake.last_path]) == pdf_bytes
        assert "/root:/Daily/2026/11/CFXD-SCA-RLY-DPR-000897_0_IFI_2026-11-04_CFXD_SLA_Daily_Work_Report.pdf:" in fake.last_path
        assert fake.links == [{"type": "view", "scope": "organization"}]

    # 中止時首頁與日報頁都看得到；一線不能重試，二線可以，從寄信那一步繼續
    assert "發送中止" in carol.get("/") and "mailbox unavailable" in carol.get(f"/reports/{rid}")
    assert "只有二線管理員以上可以重試" in alice.post(f"/reports/{rid}/delivery/retry")
    assert "已排入重試" in carol.post(f"/reports/{rid}/delivery/retry")
    with app.app_context():
        maint.tick()
        d = Delivery.query.one()
        assert d.state == "done" and calls == [False]  # 正式 PDF 沒有重新產生
        assert len(fake.uploads) == 1 and len(fake.mails) == 1
        path, mail = fake.mails[0]
        msg = mail["message"]
        assert path.endswith("/users/dr%40contoso.com/sendMail")
        assert [r["emailAddress"]["address"] for r in msg["toRecipients"]] == ["customer@client.com", "pm@client.com"]
        assert msg["subject"] == "CFXD-SCA-RLY-DPR-000897 CFXD SLA Daily Work Report 2026-11-04"
        assert "share123" in msg["body"]["content"] and d.pdf_md5 in msg["body"]["content"]

    html = carol.get(f"/reports/{rid}")
    assert html.count("☑") == 8 and "尚未上傳截圖" in html and "share123" in html and d.pdf_md5 in html
    # 正式 PDF 下載與預覽是同一份檔案
    r = carol.c.get(f"/reports/{rid}/final.pdf?download=1")
    assert r.data == pdf_bytes and "attachment" in r.headers["Content-Disposition"]
    assert carol.c.get(f"/reports/{rid}/pdf").data == pdf_bytes

    # 檢視者：只能看正式文件頁、下載已結案 PDF
    html = vic.get("/")
    assert "正式文件" in html and d.pdf_md5 in html
    assert vic.c.get(f"/reports/{rid}/final.pdf").data == pdf_bytes
    for url in (f"/reports/{rid}", f"/reports/{rid}/pdf", f"/reports/{rid}/basic", "/delegations", "/admin/users"):
        assert vic.c.get(url).status_code == 403, url
    vic.post("/reports/new", {"report_date": "2026-11-09"}, status=403)
    fake.close()


def test_preview_watermark_and_reminder(app, monkeypatch):
    """預覽版每頁有浮水印、正式版沒有；每日提醒在期限前寄給二線人員，一天一次。"""
    from datetime import datetime

    from app import db, delivery, sysconf
    from app.models import User
    from app.pdf import report_pdf

    pages = []

    class Probe(report_pdf._Canvas):
        def drawCentredString(self, x, y, text, *a, **k):
            pages.append(text)
            return super().drawCentredString(x, y, text, *a, **k)

    import io

    for wm in (report_pdf.PREVIEW_MARK, None):
        pages.clear()
        cv = Probe(io.BytesIO(), pagesize=report_pdf.PAGE)
        cv.watermark = wm
        cv.showPage()
        cv.showPage()
        assert pages == ([wm, wm] if wm else [])

    sent = []
    monkeypatch.setattr("app.graph.send_mail", lambda c, to, subject, html, cc=(): sent.append((to, subject)))
    carol = Browser(app)
    carol.login("carol")
    set_role(app, "carol", "second_line")
    with app.app_context():
        User.query.filter_by(username="carol").one().email = "carol@contoso.com"
        for k, v in {"graph_tenant_id": "t", "graph_client_id": "c", "mail_sender": "dr@contoso.com",
                     "reminder_extra": "duty@contoso.com"}.items():
            sysconf.put(k, v)
        from app import graph

        graph.set_secret("s")
        db.session.commit()
        assert not delivery.reminder_due(datetime(2026, 11, 7, 12, 29))
        assert delivery.reminder_due(datetime(2026, 11, 7, 12, 30))  # 週六也提醒
        res = delivery.send_reminder(datetime(2026, 11, 7, 12, 30))
        assert "已提醒 carol@contoso.com, duty@contoso.com（尚未建立）" in res
        assert sent and "期限 13:00" in sent[0][1]
        assert not delivery.reminder_due(datetime(2026, 11, 7, 12, 45))  # 一天一次


def _png(seed, size=(320, 200), tweak=0):
    """測試用截圖：不同 seed 的畫面差很多；tweak 只改一小塊（同一個系統畫面，不同天）。"""
    import io
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        d.rectangle([x, y, x + rnd.randrange(30, 140), y + rnd.randrange(20, 90)], fill=tuple(rnd.randrange(256) for _ in range(3)))
    if tweak:
        d.text((5, size[1] - 12), f"2026/11/0{tweak} 10:0{tweak}", fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_screenshot_filename_rules():
    from app.shot_batch import code_of, parse_path

    codes = {"1", "4", "4.1", "4.3", "14.1", "5"}
    assert [code_of(n) for n in ("4.3_DI.png", "4.3-2.png", "04.03 DI.png", "4.3.png", "14.1(2).jpg", "DI.png", "43_x.png")] == \
        ["4.3", "4.3", "4.3", "4.3", "14.1", None, "43"]
    assert parse_path("4.3_DI.png", codes) == ("4.3", "filename", "")
    assert parse_path("today/4.3 DI/IMG_001.png", codes) == ("4.3", "folder", "")
    assert parse_path("4.3 DI/4.3_2.png", codes) == ("4.3", "filename", "")
    assert parse_path("5 Veeam/4.3_x.png", codes)[0] is None  # 檔名與資料夾不一致：待分類
    assert parse_path("4.9_x.png", codes) == (None, None, "章節編號 4.9 不存在")
    assert parse_path("Screenshot 2026-11-04 100512.png", codes)[0] is None
    assert parse_path("4.10_x.png", codes)[0] is None  # 4.10 ≠ 4.1


def test_screenshot_batch_upload(app):
    import io
    import json
    import zipfile

    from app import db
    from app.models import Report, ReportImage, ReviewLog, ScreenshotSection

    alice, boss = Browser(app), Browser(app)
    alice.login("alice")
    boss.login("boss")
    set_role(app, "boss", "file_manager")
    with app.app_context():
        sec = {s.code: s.id for s in ScreenshotSection.query.all()}

    # 前一份正式日報：4.1、4.3 各一張
    prev = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-03"}))
    hdr = {"X-CSRFToken": alice.tok}
    for code, seed in (("4.1", 41), ("4.3", 43)):
        r = alice.c.post(f"/reports/{prev}/images", headers=hdr, content_type="multipart/form-data",
                         data={"kind": "screenshot", "section_id": str(sec[code]), "files": (io.BytesIO(_png(seed, tweak=3)), "x.png")})
        assert r.status_code == 200
    with app.app_context():
        db.session.get(Report, prev).status = "approved"
        db.session.commit()

    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-04"}))
    html = alice.get(f"/reports/{rid}/screenshots/batch")
    assert "4.3_DI.png" in html and "19.5_System Log.png" in html and "17.1_HA Status-Syslog.png" in html
    csv = alice.c.get(f"/reports/{rid}/screenshots/batch/names.csv").get_data(as_text=True)
    assert csv.count("\r\n") == 47 and "4.3,\"DI\",4.3_DI.png,4.3_DI_2.png" in csv

    def up(path, data):
        r = alice.c.post(f"/reports/{rid}/screenshots/batch/upload", headers=hdr, content_type="multipart/form-data",
                         data={"files": (io.BytesIO(data), path)})
        assert r.status_code == 200
        return r.get_json()["results"]

    assert up("4.1_ACC.png", _png(41, tweak=4))[0]["section"] == "4.1"  # 檔名
    assert up("tdy/4.3 DI/IMG_1.png", _png(43, tweak=4))[0]["section"] == "4.3"  # 資料夾
    assert up("dup/4.1_again.png", _png(41, tweak=4))[0]["status"] == "skipped"  # 同一張傳兩次
    old = up("4.3_old.png", _png(43, tweak=3))  # 昨天那一張原封不動
    assert old[0]["status"] == "staged"
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("day/5 Veeam/4.3_wrong.png", _png(5))  # 檔名與資料夾不一致
        zf.writestr("day/4.9_nope.png", _png(49))  # 章節不存在
        zf.writestr("day/Screenshot 1.png", _png(41, tweak=5))  # 沒有編號，但畫面像 4.1
        zf.writestr("day/readme.txt", b"hello")
    res = {r["path"]: r for r in up("batch.zip", z.getvalue())}
    assert res["batch.zip/day/readme.txt"]["status"] == "skipped"
    assert "不一致" in res["batch.zip/day/5 Veeam/4.3_wrong.png"]["note"]
    with app.app_context():
        assert db.session.get(Report, rid).images and not [i for i in db.session.get(Report, rid).images if i.kind == "screenshot"]

    html = alice.get(f"/reports/{rid}/screenshots/batch")
    assert "待分類（3）" in html and "與 2026-11-03 日報" in html and "截圖時間辨識中" in html
    assert "建議：4.1 ACC" in html and 'disabled' in html.split("放入報告（")[0][-200:]
    # 不能送審：暫存區還有截圖
    fill_checklist(alice, rid)
    assert "暫存區還有" in alice.post(f"/reports/{rid}/submit")

    with app.app_context():
        imgs = {i.source_path: i for i in ReportImage.query.filter_by(report_id=rid, kind="staged")}
        for i in imgs.values():  # 模擬 OCR 完成：日期正確，只有 IMG_1 是別天
            dates = ["2026-11-02"] if i.source_path.endswith("IMG_1.png") else ["2026-11-04"]
            i.ocr_status, i.ocr_result = "done", json.dumps({"status": "ok", "dates": dates, "time": "10:00", "text": "", "ocr": {}})
        db.session.commit()
        ids = {p.split("/")[-1]: i.id for p, i in imgs.items()}

    html = alice.get(f"/reports/{rid}/screenshots/batch")
    assert "截圖日期是 2026-11-02，不是 2026-11-04" in html
    # 移除舊圖與日期錯的；採用建議；把其餘待分類指定章節；全部放入前不能有問題
    alice.post(f"/reports/{rid}/screenshots/batch/assign", {"remove": str(ids["4.3_old.png"])})
    alice.post(f"/reports/{rid}/screenshots/batch/assign", {"remove": str(ids["IMG_1.png"])})
    html = alice.post(f"/reports/{rid}/screenshots/batch/commit", {
        f"sec_{ids['Screenshot 1.png']}": str(sec["4.1"]), f"sug_{ids['Screenshot 1.png']}": str(sec["4.1"]),
        f"sec_{ids['4.3_wrong.png']}": str(sec["4.3"])})
    assert "待處理" in html or "還有 1 個問題" in html  # 4.9_nope 還沒指定
    html = alice.post(f"/reports/{rid}/screenshots/batch/commit", {f"sec_{ids['4.9_nope.png']}": str(sec["5"])})
    assert "張數與前一份正式日報不同" in html  # 4.1 變 2 張、4.3 變 1 張（同）、5 新增
    html = alice.post(f"/reports/{rid}/screenshots/batch/commit", {"confirm_counts": "1"})
    assert "已放入 4 張截圖" in html
    with app.app_context():
        shots = ReportImage.query.filter_by(report_id=rid, kind="screenshot").order_by(ReportImage.section_id, ReportImage.order).all()
        got = sorted((db.session.get(ScreenshotSection, i.section_id).code, i.place_source) for i in shots)
        assert got == [("4.1", "filename"), ("4.1", "suggest"), ("4.3", "manual"), ("5", "manual")]
        assert all(i.placed_by_id and i.placed_at for i in shots)
        assert not ReportImage.query.filter_by(report_id=rid, kind="staged").count()
        log = ReviewLog.query.filter_by(report_id=rid, action="screenshots").one()
        assert "放入 4 張" in log.comment and "已確認張數差異" in log.comment


def test_review_marks_misplaced_screenshot(app):
    """審核者在截圖旁勾選「放錯章節」退回；撰寫人看到標記，移到正確章節後標記清除並留紀錄。"""
    import io

    from app import db
    from app.models import Report, ReportImage, ReviewLog, ScreenshotSection

    alice, boss = Browser(app), Browser(app)
    alice.login("alice")
    boss.login("boss")
    set_role(app, "boss", "file_manager")
    with app.app_context():
        sec = {s.code: s.id for s in ScreenshotSection.query.all()}
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-04"}))
    fill_checklist(alice, rid)
    r = alice.c.post(f"/reports/{rid}/images", headers={"X-CSRFToken": alice.tok}, content_type="multipart/form-data",
                     data={"kind": "screenshot", "section_id": str(sec["4.1"]), "files": (io.BytesIO(_png(7)), "veeam.png")})
    img_id = r.get_json()["images"][0]["id"]
    finish_ocr(app, rid)
    alice.get(f"/reports/{rid}")
    assert "已送出審核" in alice.post(f"/reports/{rid}/submit")

    html = boss.get(f"/reports/{rid}")
    assert 'name="misplaced"' in html and 'form="review-form"' in html and "4.1 ACC" in html
    boss.post(f"/reports/{rid}/review", {"action": "reject", "comment": "截圖位置錯", "misplaced": str(img_id)})
    with app.app_context():
        assert db.session.get(ReportImage, img_id).misplaced
        assert "標記放錯章節：4.1 veeam.png" in db.session.get(Report, rid).review_comment

    html = alice.get(f"/reports/{rid}/screenshots")
    assert "審核者標記：放錯章節" in html and "移到這個章節" in html
    html = alice.post(f"/reports/{rid}/images/{img_id}/section", {"section_id": str(sec["5.1"])})
    assert "已把 veeam.png 移到 5.1 Veeam Backup" in html and "審核者標記：放錯章節" not in html
    with app.app_context():
        img = db.session.get(ReportImage, img_id)
        assert img.section_id == sec["5.1"] and not img.misplaced and img.place_source == "manual"
        assert "4.1 → 5.1" in ReviewLog.query.filter_by(report_id=rid, action="move_image").one().comment


def test_permission_matrix_matches_code(app):
    """帳號管理頁的權限表逐格與實際的權限判斷一致（系統管理員：本機帳號與被指派的檔案管理員都檢查）。"""
    from datetime import date, datetime, timedelta

    from app import approval as perm
    from app import db
    from app.models import ApprovalDelegation, Delivery, Report, User

    with app.app_context():
        def mk(name, role, sys_admin=False):
            u = User(username=name, display_name=name)
            u.set_role(role)
            u.sys_admin = sys_admin
            db.session.add(u)
            return u

        other = mk("writer", "first_line")
        fm = mk("fmx", "file_manager")
        users = {"sysadmin": [mk("root", "admin"), mk("fmadmin", "file_manager", True)],
                 "file_manager": [mk("fm", "file_manager")], "second_line": [mk("sl", "second_line")],
                 "first_line": [mk("fl", "first_line")], "viewer": [mk("vw", "viewer")], "guest": [mk("gst", "guest")]}
        db.session.flush()

        def rep(status, author=None, delivery_state=None, day=[0]):
            day[0] += 1
            r = Report(report_date=date(2030, 1, 1) + timedelta(days=day[0]), doc_no=f"D{day[0]}", contractor_doc_no=f"C{day[0]}",
                       author_id=(author or other).id, status=status, submitted_by_id=(author or other).id)
            db.session.add(r)
            db.session.flush()
            if delivery_state:
                db.session.add(Delivery(report_id=r.id, state=delivery_state, run_after=datetime.now()))
                db.session.flush()
            return r

        draft, submitted, approved = rep("draft"), rep("submitted"), rep("approved")
        locked, failed = rep("closed", delivery_state="scheduled"), rep("closed", delivery_state="error")

        def check(label, u):
            own = rep("submitted", author=u) if label == "審核自己撰寫或送審的日報" else None
            return {
                "新增、編輯（草稿 / 退回）、送審": lambda: bool(perm.can_create(u) and perm.can_edit(u, draft) and perm.can_submit(u, draft)),
                "截圖上傳（含批次上傳）": lambda: bool(perm.can_edit(u, draft)),
                "線上預覽 PDF": lambda: u.role not in ("viewer", "guest"),
                "指派撰寫人、核准人": lambda: bool(perm.can_assign(u, draft)),
                "覆寫文件編號": lambda: bool(perm.can_override_doc_no(u, draft)),
                "審核：核准 / 退回": lambda: bool(perm.can_review(u, submitted)),
                "審核自己撰寫或送審的日報": lambda: bool(perm.can_review(u, own)),
                "結案、結案前退回修正": lambda: bool(perm.can_close(u, approved) and perm.can_return(u, approved)),
                "取消結案（發送開始前）": lambda: bool(perm.can_cancel_close(u, locked)),
                "發送中止時重試": lambda: bool(perm.can_retry_delivery(u, failed)),
                "刪除日報（未結案；刪除後文件編號可再使用）": lambda: bool(perm.can_delete(u, draft) and not perm.can_delete(u, locked)),
                "授權二線管理員代理": lambda: bool(perm.can_manage_delegations(u)),
                "下載已結案的正式 PDF": lambda: u.role != "guest",
                "調整角色、指派系統管理員": lambda: perm.can_manage_users(u),
                "本機緊急帳號（新增、改密碼、停用）": lambda: perm.can_manage_users(u),
                "系統設定（AD、備份還原、健康檢查、PDF 範本、HTTPS、發送與提醒、AI）": lambda: perm.can_manage_settings(u),
                "稽核紀錄": lambda: perm.can_manage_users(u),
            }[label]()

        cols = [k for k, _ in perm.MATRIX_ROLES]
        for _, rows in perm.PERMISSION_MATRIX:
            for label, marks in rows:
                for col, mark in zip(cols, marks):
                    for u in users[col]:
                        got = check(label, u)
                        assert got == (mark == "y"), (label, col, u.username, got)
                        if mark == "d":  # 經授權代理時可以
                            dl = ApprovalDelegation(principal_id=fm.id, delegate_id=u.id, start_date=date.today())
                            db.session.add(dl)
                            db.session.flush()
                            assert check(label, u), (label, col, "with delegation")
                            db.session.delete(dl)
                            db.session.flush()
        db.session.rollback()

    root = Browser(app)
    runner = app.test_cli_runner()
    runner.invoke(args=["create-admin", "--username", "sysadmin", "--password", "a-very-long-pass"])
    root.get("/login")
    root.post("/login", {"username": "sysadmin", "password": "a-very-long-pass"})
    html = root.get("/admin/users")
    assert "perm-matrix" in html and html.count("✔") > 20 and "✔*" in html and "不受職責分離限制" in html


def test_ad_first_login_is_guest(app, monkeypatch):
    """AD 首次登入一律是訪客：只能看歡迎頁（列出系統管理員），其他內容與操作都禁止；開通後才能使用。"""
    from app import auth, db
    from app.models import User

    app.config["AUTH_MODE"] = "ldap"
    monkeypatch.setattr(auth, "ad_authenticate", lambda u, p, c=None: (u.title(), f"{u}@corp.local", False))
    # 系統管理員（被指派的檔案管理員，有 email）
    adm = Browser(app)
    adm.get("/login")
    adm.post("/login", {"username": "anna", "password": "pw"})
    with app.app_context():
        a = User.query.filter_by(username="anna").one()
        assert a.role == "guest"
        a.set_role("file_manager")
        a.sys_admin = True
        db.session.commit()

    g = Browser(app)
    g.get("/login")
    html = g.post("/login", {"username": "gary", "password": "pw"})
    assert "帳號尚未開通權限" in html and "anna@corp.local" in html and "請聯絡系統管理員" in html
    assert "正式文件" not in html and "日報清單" not in html
    for url in ("/", "/archive", "/reports/1", "/delegations", "/admin/users", "/admin/settings/"):
        r = g.c.get(url)
        assert r.status_code == 302 and r.headers["Location"].endswith("/welcome"), url
    assert g.c.post("/reports/new", data={"csrf_token": g.tok, "report_date": "2026-11-20"}).status_code == 403

    # 系統管理員看到等待開通，設定角色後訪客即可使用
    assert "等待開通" in adm.get("/") and "gary" in adm.get("/admin/users")
    with app.app_context():
        gid = User.query.filter_by(username="gary").one().id
    adm.post("/admin/users", {"user_id": str(gid), "role": "first_line"})
    assert "日報清單" in g.get("/") and g.c.get("/welcome").status_code == 302

