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
    assert "Disk full" in html
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
    rid = report_id_from(boss.post("/reports/new", {"report_date": "2026-11-01"}))
    fill_checklist(boss, rid)
    boss.get(f"/reports/{rid}")
    boss.post(f"/reports/{rid}/submit")
    boss.get(f"/reports/{rid}")
    html = boss.post(f"/reports/{rid}/review", {"action": "approve"})
    assert "不能審核自己" in html


def db_status(rid):
    from app import db
    from app.models import Report

    return db.session.get(Report, rid).status
