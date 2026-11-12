"""自動儲存、告警列順序、照片位置、檢查項目維護、範本對應、歷史日報 Excel 匯入。

真正的 CheckList 範本不放在 git：這裡依資料庫的檢查項目產生一份格式相同的活頁簿
（勾選欄 TRUE/FALSE、Device Name 合併儲存格、Alarm Summary 的 I2~I5 與第 7 列起的告警）。
"""

import datetime as dt
import io
import json
import re
from html import unescape

import openpyxl

from test_flow import Browser, app, report_id_from, set_role  # noqa: F401  app 是 fixture

from app.checklist_template import CHECK_COLS


def build_checklist_xlsx(items_by_sheet, day=None, author="Simon Lin", alarms=(), results=None, doc_no=None):
    """results: {(sheet, item_no): ("N"/"W"/"F", note)}"""
    from openpyxl.utils import column_index_from_string as ci

    results = results or {}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Alarm Summary"
    ws["B1"] = "CFXD SLA - SCADA Daily Inspection CheckList Alarm Summary"
    if doc_no:
        ws["F2"] = doc_no
    ws["I2"] = dt.datetime.combine(day, dt.time()) if day else None
    ws["I3"] = dt.time(10, 0)
    ws["I4"] = 0
    ws["I5"] = author
    for k, a in enumerate(alarms):
        r = 7 + k
        ws[f"B{r}"] = k + 1
        for col, key in zip("CDEFGHI", ("category", "item", "device_name", "description", "reason", "false_alarm", "note")):
            ws[f"{col}{r}"] = a.get(key, "")
    wb.create_sheet("Summary")
    for sheet, (name, cn, cw, cf, cnote) in CHECK_COLS.items():
        s = wb.create_sheet(name)
        s["A1"] = "No."
        s["B1"] = "Device Name"
        r = 3
        group_start, group_dev = None, None
        for it in items_by_sheet.get(sheet, []):
            if it.device_name != group_dev:
                if group_start and r - 1 > group_start:
                    s.merge_cells(start_row=group_start, start_column=2, end_row=r - 1, end_column=2)
                group_start, group_dev = r, it.device_name
                s.cell(row=r, column=2, value=it.device_name)
            try:
                s.cell(row=r, column=3, value=float(it.item_no))
            except ValueError:
                s.cell(row=r, column=3, value=it.item_no)
            s.cell(row=r, column=4, value=it.status_type)
            s.cell(row=r, column=5, value=it.description)
            if ci(cn) > 6 and it.location:
                s.cell(row=r, column=6, value=it.location)
            res, note = results.get((sheet, it.item_no), (None, None))
            for code, col in (("N", cn), ("W", cw), ("F", cf)):
                s[f"{col}{r}"] = res == code
            s[f"{cnote}{r}"] = note
            r += 1
        if group_start and r - 1 > group_start:
            s.merge_cells(start_row=group_start, start_column=2, end_row=r - 1, end_column=2)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _items(app):
    from app.checklist_template import active_items

    with app.app_context():
        return active_items()


def _browser(app, username, role):
    b = Browser(app)
    b.login(username)
    set_role(app, username, role)
    return b


# ---------------------------------------------------------------- 自動儲存


def test_autosave_json_and_csrf(app):
    alice = _browser(app, "alice", "first_line")
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-02"}))
    html = alice.get(f"/reports/{rid}/records")
    assert "data-autosave" in html and "每分鐘自動儲存" in html
    r = alice.c.post(f"/reports/{rid}/records", headers={"X-Autosave": "1"},
                     data={"csrf_token": alice.tok, "inspection_summary": "Autosaved text", "onsite_summary": "x"})
    assert r.status_code == 200 and r.is_json and r.json["ok"] and r.json["csrf"]
    from app import db
    from app.models import Report

    with app.app_context():
        assert db.session.get(Report, rid).inspection_summary == "Autosaved text"
    # 自動儲存不留下 flash 訊息
    assert "已儲存。" not in alice.get(f"/reports/{rid}/records")
    # 新 token 可以用
    r = alice.c.post(f"/reports/{rid}/checklist/vm", headers={"X-Autosave": "1"}, data={"csrf_token": r.json["csrf"]})
    assert r.is_json and r.json["ok"]
    # token 錯誤：自動儲存回 JSON（app.js 會換新 token 重送），一般送出顯示說明頁
    r = alice.c.post(f"/reports/{rid}/records", headers={"X-Autosave": "1"}, data={"csrf_token": "bad"})
    assert r.status_code == 400 and r.json["error"] == "csrf"
    r = alice.c.post(f"/reports/{rid}/records", data={"csrf_token": "bad"})
    assert r.status_code == 400 and "還原" in r.get_data(as_text=True)
    t = alice.c.get("/csrf-token")
    assert t.is_json and t.json["csrf"]
    # 文件編號覆寫錯誤：JSON 錯誤訊息
    # 送審後不能再自動儲存
    from test_flow import fill_checklist

    fill_checklist(alice, rid)
    alice.get(f"/reports/{rid}")
    alice.post(f"/reports/{rid}/submit")
    r = alice.c.post(f"/reports/{rid}/records", headers={"X-Autosave": "1"},
                     data={"csrf_token": t.json["csrf"], "inspection_summary": "late"})
    assert r.status_code == 409 and not r.json["ok"] and "不能修改" in r.json["error"]
    # 未登入：/csrf-token 回 401（不轉到登入頁）
    assert app.test_client().get("/csrf-token").status_code == 401


def test_alarm_rows_follow_screen_order_and_photos_inside_blocks(app):
    alice = _browser(app, "alice", "first_line")
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-02"}))
    html = alice.get(f"/reports/{rid}/alarms")
    # 告警照片在 True Alarm Record 區塊內、表單內；現場照片在 On-site Record 區塊內
    ta = html.index("True Alarm Record（真實告警）")
    assert ta < html.index("True Alarm Photo") < html.index("CheckList Alarm Summary")
    assert html.index("True Alarm Photo") < html.index("</form>", ta) < html.index('id="import-form"')
    assert "data-sortable" in html and "drag-handle" in html
    rec = alice.get(f"/reports/{rid}/records")
    assert rec.index("On-site Record（現場作業）") < rec.index("Work Photo") < rec.index("System Update Record（系統更新）")
    # 畫面上拖曳後的順序（表單欄位先後），不是 n 的大小
    data = {"alarm_new": "0", "alarm_known": "3", "alarm_false": "0"}
    for n, dev in ((2, "C-dev"), (0, "A-dev"), (1, "B-dev")):
        data[f"alarmrow-{n}-category"] = "Physical Devices"
        data[f"alarmrow-{n}-device_name"] = dev
        data[f"alarmrow-{n}-description"] = "x"
    alice.post(f"/reports/{rid}/alarms", data)
    from app import db
    from app.models import Report

    with app.app_context():
        rows = db.session.get(Report, rid).alarm_rows
        assert [(r.order, r.device_name) for r in rows] == [(1, "C-dev"), (2, "A-dev"), (3, "B-dev")]


# ---------------------------------------------------------------- 檢查項目與範本


def test_template_alignment_by_text(app):
    from app.checklist_template import map_workbook

    items = _items(app)
    wb = openpyxl.load_workbook(io.BytesIO(build_checklist_xlsx(items)))
    with app.app_context():
        maps = map_workbook(wb, items)
        for sheet, m in maps.items():
            assert m.ok and m.mode == "text", sheet
            assert [it.id for _, it in sorted(m.pairs.items())] == [it.id for it in items[sheet]]
        # 範本少一列、多一列、兩項對調、一項文字不同
        vm = list(items["vm"])
        tpl = vm[:2] + [vm[3], vm[2]] + vm[5:]  # 移除 vm[4]（系統有、範本沒有）、對調 2/3
        wb2 = openpyxl.load_workbook(io.BytesIO(build_checklist_xlsx({**items, "vm": tpl})))
        m = map_workbook(wb2, items)["vm"]
        assert [it.item_no for it, _ in m.missing] == [vm[4].item_no]
        assert len(m.moved) == 1 and not m.extra
        # 已存在的項目仍對到正確的列
        row_of = {it.id: r for r, it in m.pairs.items()}
        assert row_of[vm[3].id] < row_of[vm[2].id]


def test_checklist_items_manage(app, tmp_path):
    from app import db
    from app.models import AuditLog, ChecklistItem, ChecklistResult, Report

    alice = _browser(app, "alice", "first_line")
    assert alice.c.get("/checklist-items/vm").status_code == 403
    rid = report_id_from(alice.post("/reports/new", {"report_date": "2026-11-02"}))
    bob = _browser(app, "bob", "second_line")
    # 範本：先放一份與目前項目一致的
    path = tmp_path / "checklist.xlsx"
    path.write_bytes(build_checklist_xlsx(_items(app)))
    app.config["PDF_TEMPLATE_XLSX"] = str(path)
    html = bob.get("/checklist-items/vm")
    assert "範本與系統的檢查項目一致" in html
    ids = re.findall(r'name="item-(\d+)-id" value="(\d+)"', html)
    assert len(ids) == 89

    def form_from(html):
        data = {}
        for k, v in re.findall(r'name="(item-\d+-[a-z_]+)" value="([^"]*)"', html):
            data[k] = unescape(v)
        for k, v in re.findall(r'name="(item-\d+-description)"[^>]*>([^<]*)</textarea>', html):
            data[k] = unescape(v)
        return data

    data = form_from(html)
    # 依畫面順序：把第 2 項移到最前面、刪除第 3 項、修改第 4 項、最後新增一項
    keys = sorted({k.split("-")[1] for k in data}, key=int)
    order = [keys[1], keys[0]] + keys[3:]
    removed_id = int(data[f"item-{keys[2]}-id"])
    edited_id = int(data[f"item-{keys[3]}-id"])
    data[f"item-{keys[3]}-description"] = "Check the protection status is ON (edited)"
    post = {"csrf_token": bob.tok}
    for k in order:
        for f in ("id", "no", "device_type", "device_name", "group_item", "item_no", "location", "category", "status_type",
                  "description"):
            post[f"item-{k}-{f}"] = data.get(f"item-{k}-{f}", "")
    new = {"id": "", "no": "1", "device_name": "CFXD_Cluster", "item_no": "1.9", "status_type": "Backup",
           "description": "Check the backup job"}
    for f, v in new.items():
        post[f"item-999-{f}"] = v
    r = bob.c.post("/checklist-items/vm", data=post, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "新增 1 項" in html and "刪除 1 項" in html and "修改 1 項" in html and "調整順序" in html
    assert "範本需要改版" in html or "需要改版" in html
    with app.app_context():
        vm = ChecklistItem.query.filter_by(sheet="vm", active=True).order_by(ChecklistItem.order).all()
        assert len(vm) == 89 and vm[-1].item_no == "1.9"
        assert db.session.get(ChecklistItem, removed_id).active is False
        assert "(edited)" in db.session.get(ChecklistItem, edited_id).description
        # 可編輯的日報：新項目加入、刪除的項目移除
        have = {i for (i,) in db.session.query(ChecklistResult.item_id).filter_by(report_id=rid)}
        assert vm[-1].id in have and removed_id not in have
        assert AuditLog.query.filter_by(action="checklist_items").count() == 1
        from app.checklist_template import diff_rows, template_status

        acts = {d[1] for d in diff_rows(template_status())}
        assert {"新增一列", "刪除這一列", "修改文字"} <= acts
    csv = bob.c.get("/checklist-items/template-diff.csv")
    assert csv.status_code == 200 and "新增一列" in csv.get_data(as_text=True)
    # PDF 填值：每個結果填在範本中那個項目的列（不是第 N 列 = 第 N 項）
    with app.app_context():
        rep = db.session.get(Report, rid)
        for res in rep.results:
            res.result = "W" if res.item_id == edited_id else "N"
        db.session.commit()
        from app.pdf.report_pdf import _fill_checklist

        class Book:
            def __init__(self):
                self.wb = openpyxl.load_workbook(path)
                self.values = {}

            def set(self, sheet, coord, value):
                self.values[(sheet, coord)] = value

        book = Book()
        _fill_checklist(book, rep)
        ws = book.wb["Virtual Machines"]
        edited = db.session.get(ChecklistItem, edited_id)
        row = next(r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=4).value == edited.status_type
                   and ws.cell(row=r, column=5).value != edited.description
                   and ws.cell(row=r, column=3).value == float(edited.item_no))
        assert book.values[("Virtual Machines", f"H{row}")] is True  # Warning
        removed_row = next(r for r in range(1, ws.max_row + 1)
                           if ws.cell(row=r, column=3).value == float(db.session.get(ChecklistItem, removed_id).item_no))
        assert book.values[("Virtual Machines", f"F{removed_row}")] is False  # 範本多出的列留空
    # 編號重複、空白
    post2 = {"csrf_token": bob.tok, "item-0-id": str(vm[0].id), "item-0-item_no": vm[1].item_no, "item-0-status_type": "x",
             "item-1-id": str(vm[1].id), "item-1-item_no": vm[1].item_no, "item-1-status_type": "y"}
    assert "重複" in bob.c.post("/checklist-items/vm", data=post2, follow_redirects=True).get_data(as_text=True)
    # 還原：新增已刪除項目的編號
    with app.app_context():
        gone = db.session.get(ChecklistItem, removed_id)
        gone_no = gone.item_no
    html = bob.get("/checklist-items/vm")
    data = form_from(html)
    post = {"csrf_token": bob.tok, **data, "item-998-id": "", "item-998-item_no": gone_no, "item-998-status_type": "Back",
            "item-998-description": "restored"}
    html = bob.c.post("/checklist-items/vm", data=post, follow_redirects=True).get_data(as_text=True)
    assert "還原 1 項" in html
    with app.app_context():
        assert db.session.get(ChecklistItem, removed_id).active is True


# ---------------------------------------------------------------- 歷史日報匯入


def _alarm(dev, desc, false="N"):
    return {"category": "Physical Devices", "item": "6.1", "device_name": dev, "description": desc,
            "reason": "Down", "false_alarm": false, "note": ""}


def test_import_history_xlsx(app):
    from app import db
    from app.models import ChecklistResult, Report

    items = _items(app)
    first = {it.item_no: it for it in items["physical"]}
    some = next(iter(first))
    d1, d2 = dt.date(2026, 10, 1), dt.date(2026, 10, 2)
    x1 = build_checklist_xlsx(items, d1, alarms=[_alarm("FW-01", "IPSec Tunnel"), _alarm("SW-02", "Fan", "Y")],
                              results={("physical", some): ("F", "Fan failed")}, doc_no="Document ID : CFXD-SCA-RLY-DPR-000863")
    # 舊版範本：VM 少最後一項
    old = {**items, "vm": items["vm"][:-1]}
    all_n = {(sh, it.item_no): ("N", None) for sh, its in items.items() for it in its}
    x2 = build_checklist_xlsx(old, d2, author="Nobody Here", results=all_n,
                              alarms=[_alarm("FW-01", "IPSec Tunnel"), _alarm("UTM-01", "Syslog")])

    alice = _browser(app, "alice", "first_line")
    assert alice.c.get("/import/").status_code == 403
    fm = _browser(app, "fm", "file_manager")
    assert "匯入歷史日報" in fm.get("/import/")

    def up(name, data, dry=False):
        payload = {"csrf_token": fm.tok, "files": (io.BytesIO(data), name)}
        if dry:
            payload["dry_run"] = "1"
        r = fm.c.post("/import/upload", data=payload, content_type="multipart/form-data",
                      headers={"X-CSRFToken": fm.tok})
        assert r.status_code == 200, r.get_data(as_text=True)[:300]
        return r.json["results"][0]

    r = up("CheckList_2026-10-02.xlsx", x2, dry=True)
    assert r["status"] == "would_import" and any("系統有 1 個項目" in w for w in r["warnings"])
    with app.app_context():
        assert Report.query.count() == 0
    # 匯入順序不照日期：先 10/02 再 10/01，New / Known 會重算
    r2 = up("CheckList_2026-10-02.xlsx", x2)
    assert r2["status"] == "imported", r2
    r1 = up("CFXD SLA - SCADA Daily Inspection CheckList_20261001.xlsx", x1)
    assert r1["status"] == "imported" and r1["doc_no"] == "CFXD-SCA-RLY-DPR-000863"
    assert up("again.xlsx", x1)["status"] == "skipped"
    assert up("bad.xlsx", b"PK\x03\x04garbage")["status"] == "error"
    assert up("notes.txt", b"hello")["status"] == "skipped"
    wb = openpyxl.Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    assert "缺少工作表" in up("other.xlsx", buf.getvalue())["message"]

    with app.app_context():
        a = Report.query.filter_by(report_date=d1).one()
        b = Report.query.filter_by(report_date=d2).one()
        assert a.status == "closed" and a.imported and a.status_label == "已結案（Excel 匯入）"
        assert a.inspection_time == "10:00 AM" and a.first_line_officer == "Simon Lin"
        assert (a.alarm_new, a.alarm_known, a.alarm_false) == (0, 1, 1)
        assert (b.alarm_new, b.alarm_known, b.alarm_false) == (1, 1, 0)  # UTM-01 是新的
        res = {r.item.item_no: (r.result, r.note) for r in a.results if r.item.sheet == "physical"}
        assert res[some] == ("F", "Fan failed")
        assert ChecklistResult.query.filter_by(report_id=b.id, result=None).count() == 1  # 舊範本沒有的 VM 項目
        assert "Nobody Here" in b.import_info["author"]
        bid = b.id
    html = fm.get(f"/reports/{bid}")
    assert "從過去的 CheckList Excel 匯入" in html and "下載原始 Excel" in html and "正式 PDF 產生中" in html
    assert "撰寫人為 Excel 的 Recorded By「Nobody Here」" in html and "不是系統帳號" not in html  # 撰寫人照 Excel 原文，不換成匯入者
    # 原始 Excel 逐位元保存
    r = fm.c.get(f"/reports/{bid}/original.xlsx")
    assert r.status_code == 200 and r.data == x2
    # 正式 PDF：維護服務產生（封面 + 原始 Excel），不上傳、不寄信、不通知發送完成
    from app import delivery
    from app.models import Delivery

    with app.app_context():
        dl = Delivery.query.filter_by(report_id=bid).one()
        assert dl.state == "queued" and [dl.step_map()[k]["status"] for k in ("upload", "link", "mail")] == ["skipped"] * 3
    preview = fm.c.get(f"/reports/{bid}/pdf")
    assert preview.status_code == 200 and preview.data.startswith(b"%PDF")
    with app.app_context():
        assert delivery.tick()
        dl = Delivery.query.filter_by(report_id=bid).one()
        assert dl.state == "done" and dl.pdf_md5, dl.step_map()
        import pypdf

        text = "".join(pg.extract_text() for pg in pypdf.PdfReader(io.BytesIO(open(delivery.final_path(dl), "rb").read())).pages)
        assert "UTM-01" in text and "Syslog" in text and "Nobody Here" in text
        assert "IMPORTED FROM ORIGINAL EXCEL" in text and "CONFIDENTIAL" not in text  # 匯入的用匯入浮水印
        rd = pypdf.PdfReader(delivery.final_path(dl))
        tops = [o.title for o in rd.outline if not isinstance(o, list)]
        assert tops == ["Daily Work Report", "CheckList"]  # PDF 書籤（沒有截圖章節）
        assert rd.pages[1].get("/Annots")  # 封面目錄可點
        from app.models import TeamsMessage

        assert TeamsMessage.query.filter_by(event="delivery_done").count() == 0
    html = fm.get(f"/reports/{bid}")
    assert "下載正式 PDF" in html and "封面 + 原始 Excel" in html
    assert fm.c.get(f"/reports/{bid}/final.pdf").status_code == 200
    assert r2["doc_no"] in fm.get("/archive")
    # 匯入的日報是新日報的比對基準與複製來源
    html = alice.post("/reports/new", {"report_date": "2026-10-03"})
    nid = report_id_from(html)
    with app.app_context():
        n = db.session.get(Report, nid)
        assert [r.device_name for r in n.alarm_rows] == ["FW-01", "UTM-01"]
    # 檔案管理員可以刪除匯入的日報（重匯用）；一般已結案日報仍不能刪
    html = fm.post(f"/reports/{bid}/delete", {"confirm_doc_no": r2["doc_no"]})
    assert "已刪除" in html
    with app.app_context():
        assert Report.query.filter_by(report_date=d2).count() == 0


def test_import_cli(app, tmp_path):
    items = _items(app)
    folder = tmp_path / "nas"
    (folder / "2026" / "10").mkdir(parents=True)
    for day in (5, 6):
        (folder / "2026" / "10" / f"CheckList_2026-10-0{day}.xlsx").write_bytes(
            build_checklist_xlsx(items, dt.date(2026, 10, day)))
    (folder / "~$open.xlsx").write_bytes(b"x")
    _browser(app, "fm", "file_manager")
    runner = app.test_cli_runner()
    out = runner.invoke(args=["import-xlsx", str(folder), "--user", "fm", "--dry-run"])
    assert out.exit_code == 0 and "would_import 2" in out.output, out.output
    out = runner.invoke(args=["import-xlsx", str(folder), "--user", "fm"])
    assert "imported 2" in out.output
    from app.models import Report

    with app.app_context():
        assert Report.query.filter(Report.import_meta.isnot(None)).count() == 2
        meta = json.loads(Report.query.first().import_meta)
        assert meta["sha256"] and meta["by"]


def test_interrupted_delivery_becomes_retryable(app):
    """維護服務在產生 PDF 時被終止（記憶體不足）：不能永遠停在「執行中」，要變成中止、可以重試。"""
    from datetime import datetime, timedelta

    from app import db, delivery, docno
    from app.approval import can_retry_delivery
    from app.models import Delivery, Report, User

    with app.app_context():
        u = User(username="well", display_name="Well Lai", role="second_line")
        db.session.add(u)
        db.session.flush()
        d = dt.date(2026, 11, 6)
        r = Report(report_date=d, doc_no=docno.doc_no(d), contractor_doc_no=docno.contractor_doc_no(d),
                   author_id=u.id, status="closed")
        db.session.add(r)
        db.session.flush()
        dl = Delivery(report=r, state="running", closed_by_id=u.id, run_after=datetime.now())
        dl.set_step("pdf", "running")
        db.session.add(dl)
        db.session.commit()
        assert not can_retry_delivery(u, r)
        # 剛開始執行：平常的輪詢不動它
        assert delivery.recover_interrupted() == 0
        # 超過 30 分鐘：改成中止
        later = datetime.now() + timedelta(minutes=31)
        assert delivery.recover_interrupted(later) == 1
        dl = db.session.get(Delivery, dl.id)
        assert dl.state == "error" and "中斷" in dl.step_map()["pdf"]["message"]
        assert can_retry_delivery(u, db.session.get(Report, r.id))
        # 服務重新啟動時：所有執行中的都改成中止
        dl.state = "running"
        dl.set_step("pdf", "running")
        db.session.commit()
        assert delivery.recover_interrupted(all_running=True) == 1
        assert db.session.get(Delivery, dl.id).state == "error"


def test_admin_deletes_closed_report(app, tmp_path):
    """已結案的日報：只有系統管理員能刪除；發送進行中不行；正式 PDF 移到 _deleted 保留。"""
    import os
    from datetime import datetime

    from test_flow import _admin_browser

    from app import db, docno
    from app.models import AuditLog, Delivery, Report, User

    app.instance_path = str(tmp_path / "instance")
    admin = _admin_browser(app)
    fm = _browser(app, "fm", "file_manager")
    with app.app_context():
        u = User.query.filter_by(username="fm").one()
        d = dt.date(2026, 11, 6)
        r = Report(report_date=d, doc_no=docno.doc_no(d), contractor_doc_no=docno.contractor_doc_no(d),
                   author_id=u.id, status="closed")
        db.session.add(r)
        db.session.flush()
        rel = "final_pdf/2026/11/x.pdf"
        full = os.path.join(app.instance_path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "wb").write(b"%PDF-1.4")
        dl = Delivery(report=r, state="running", closed_by_id=u.id, run_after=datetime.now(), pdf_path=rel,
                      pdf_name="x.pdf", pdf_size=8, pdf_md5="abc123", pdf_sha256="f" * 64)
        db.session.add(dl)
        db.session.commit()
        rid, doc, did = r.id, r.doc_no, dl.id
    assert fm.c.post(f"/reports/{rid}/delete", data={"csrf_token": fm.tok, "confirm_doc_no": doc}).status_code == 403
    assert "刪除日報" not in fm.get(f"/reports/{rid}")
    html = admin.get(f"/reports/{rid}")
    assert "刪除日報" not in html  # 發送進行中
    with app.app_context():
        db.session.get(Delivery, did).state = "error"
        db.session.commit()
    html = admin.get(f"/reports/{rid}")
    assert "刪除日報" in html and "已結案" in html and "_deleted" in html
    html = admin.post(f"/reports/{rid}/delete", {"confirm_doc_no": doc})
    assert "已刪除" in html
    with app.app_context():
        assert db.session.get(Report, rid) is None
        log = AuditLog.query.filter_by(action="delete_report").one()
        assert "_deleted" in log.detail and "abc123" in log.detail
        assert not os.path.exists(full)
        assert os.listdir(os.path.join(app.instance_path, "final_pdf", "_deleted"))


def test_official_watermark_and_fingerprint(app):
    """系統建立日報的正式 PDF：整頁平鋪公司、機密等級、檔名、時間、內容 SHA-256；內容雜湊固定，內容一改就變。"""
    import pypdf

    from app import db, docno
    from app.models import Report, User
    from app.pdf import fonts
    from app.pdf.report_pdf import _Canvas, content_fingerprint, official_stamp

    with app.app_context():
        u = User(username="w1", display_name="W One", role="first_line")
        db.session.add(u)
        db.session.flush()
        d = dt.date(2026, 11, 3)
        r = Report(report_date=d, doc_no=docno.doc_no(d), contractor_doc_no=docno.contractor_doc_no(d),
                   author_id=u.id, updated_by_id=u.id, status="closed")
        db.session.add(r)
        db.session.commit()
        h1 = content_fingerprint(r)
        assert h1 == content_fingerprint(r) and len(h1) == 64
        r.inspection_summary = "changed"
        db.session.commit()
        assert content_fingerprint(r) != h1
        st = official_stamp(r)
        assert st["filename"].startswith(r.doc_no) and st["sha256"] == content_fingerprint(r)
        fonts.configure(app.config.get("FONT_DIR"))
        buf = io.BytesIO()
        cv = _Canvas(buf, pagesize=(595.32, 841.92))
        cv.stamp = dict(st)
        cv.drawString(72, 700, "page one")
        cv.showPage()
        cv.drawString(72, 700, "page two")
        cv.showPage()
        cv.save()
        rd = pypdf.PdfReader(io.BytesIO(buf.getvalue()))
        for pg in rd.pages:
            t = pg.extract_text()
            assert "CONFIDENTIAL - INTERNAL USE ONLY" in t and st["sha256"] in t and st["filename"] in t
            assert t.count("CONFIDENTIAL") > 10  # 平鋪整頁


def test_import_earlier_day_regenerates_later_pdf(app):
    """先匯入 11/11 並產生正式 PDF，之後補匯入 11/10：11/11 的 Reports Control 要包含 11/10，所以重新產生。"""
    import os

    import pypdf

    from app import delivery
    from app.models import Delivery, Report

    items = _items(app)
    fm = _browser(app, "fm", "file_manager")

    def up(day, author):
        x = build_checklist_xlsx(items, day, author=author)
        r = fm.c.post("/import/upload", data={"csrf_token": fm.tok, "files": (io.BytesIO(x), f"CheckList_{day}.xlsx")},
                      content_type="multipart/form-data", headers={"X-CSRFToken": fm.tok})
        return r.json["results"][0]

    assert up(dt.date(2026, 11, 11), "Simon Lin")["status"] == "imported"
    with app.app_context():
        delivery.tick()
        d11 = Report.query.filter_by(report_date=dt.date(2026, 11, 11)).one().delivery
        old_path, old_md5 = delivery.final_path(d11), d11.pdf_md5
        assert d11.state == "done" and os.path.isfile(old_path)
    r = up(dt.date(2026, 11, 10), "Willy Liang")
    assert r["status"] == "imported" and any("2026-11-11" in w and "重新產生" in w for w in r["warnings"])
    with app.app_context():
        d11 = Report.query.filter_by(report_date=dt.date(2026, 11, 11)).one().delivery
        assert d11.state == "queued" and d11.pdf_md5 is None and not os.path.isfile(old_path)
        delivery.tick()
        d11 = db_get(Delivery, d11.id)
        assert d11.state == "done" and d11.pdf_md5 != old_md5
        cover = pypdf.PdfReader(delivery.final_path(d11)).pages[4].extract_text()  # 1 Reports Control
        assert "2026-11-10" in cover and "Willy Liang" in cover and "2026-11-11" in cover


def db_get(model, pk):
    from app import db

    return db.session.get(model, pk)


def test_ai_context_previous_days_continuity(app):
    """AI 參考前 N 份（預設 7）正式日報的檢查結果判斷異常連續性；N 份全部都出現就回答「異常持續 >N 天」。"""
    from app import ai, db, docno
    from app.models import CheckReport, Report, User

    with app.app_context():
        u = User(username="ctx", display_name="Ctx", role="first_line")
        db.session.add(u)
        db.session.flush()

        def rep(day, status, md=None):
            r = Report(report_date=day, doc_no=docno.doc_no(day), contractor_doc_no=docno.contractor_doc_no(day),
                       author_id=u.id, updated_by_id=u.id, status=status)
            db.session.add(r)
            db.session.flush()
            if md:
                db.session.add(CheckReport(report_id=r.id, stage="approve", markdown=md))
            return r

        base = dt.date(2026, 11, 20)
        for k in range(1, 11):  # 11/10–11/19：10 份正式日報
            rep(base - dt.timedelta(days=k), "closed", f"# day {k} ALARM-X")
        rep(base - dt.timedelta(days=11), "draft", "# draft only")  # 草稿不算
        today = rep(base, "submitted")
        snap = CheckReport(report_id=today.id, stage="submit", markdown="# today ALARM-X")
        db.session.add(snap)
        db.session.commit()

        assert ai.prov.get_setting("ai_context_reports") == 7
        days, rejected = ai.retrieve_context(snap)
        assert [d for _, d, _ in days] == [base - dt.timedelta(days=k) for k in range(1, 8)] and rejected == []
        user = ai.build_messages(snap)[1]["content"]
        assert "前 7 份正式日報" in user and "異常持續 >7 天" in user
        assert user.index("day 1 ALARM") < user.index("day 7 ALARM") and "day 8" not in user and "draft only" not in user
        # 過去資料不足 7 份
        days, _ = ai.retrieve_context(snap, limit=20)
        assert len(days) == 10
        assert "至少持續 4 天" in ai.continuity_rule(3, 7)
