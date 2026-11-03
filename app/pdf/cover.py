"""Daily Inspection Coverpage（原 Word 範本）的重現。

所有座標取自原始 Word 輸出的 PDF（A4，單位 pt，自頁面頂端起算的 top）。
Calibri 基線 = top + 0.75 × 字級。
記錄筆數變多時表格會自動換頁，目錄頁碼與「Page x of N」跟著更新。
"""

from dataclasses import dataclass, field

from . import fonts

PAGE_W, PAGE_H = 595.32, 841.92
BOTTOM_LIMIT = PAGE_H - 72  # 下邊界 1440 twips
HEAD_COLOR = (0.216, 0.298, 0.502)
GRAY_TEXT = (0.349, 0.349, 0.349)
SHADE = (0.922, 0.91, 0.925)
LINE_W = 0.48
ROW_H = 15.12
TEXT_DY = 2.64  # 列上框線到文字 top
CELL_PAD = 5.4


def F(bold=False):
    return fonts.font("Calibri", bold=bold)


def text(cv, s, x, top, size=12, bold=False, color=(0, 0, 0), font=None):
    fn = font or F(bold)
    cv.setFillColorRGB(*color)
    cv.setFont(fn, size)
    asc = 0.75 if fn.startswith("Calibri") else 0.7896
    cv.drawString(x, PAGE_H - (top + asc * size), s)
    return fonts.width(s, fn, size)


def ctext(cv, s, cx, top, size=12, bold=False, color=(0, 0, 0)):
    w = fonts.width(s, F(bold), size)
    text(cv, s, cx - w / 2, top, size, bold, color)


def hline(cv, y, x0, x1, w=LINE_W):
    cv.setLineWidth(w)
    cv.setStrokeColorRGB(0, 0, 0)
    cv.line(x0, PAGE_H - y, x1, PAGE_H - y)


def vline(cv, x, y0, y1, w=LINE_W):
    cv.setLineWidth(w)
    cv.setStrokeColorRGB(0, 0, 0)
    cv.line(x, PAGE_H - y0, x, PAGE_H - y1)


def shade(cv, x0, y0, x1, y1):
    cv.setFillColorRGB(*SHADE)
    cv.rect(x0, PAGE_H - y1, x1 - x0, y1 - y0, stroke=0, fill=1)


def wrap(s, width, size=12, bold=False):
    import re

    out = []
    for para in (s or "").split("\n"):
        words = re.split(r"(\s+)", para)
        cur = ""
        for w in words:
            trial = cur + w
            if cur and fonts.width(trial.rstrip(), F(bold), size) > width + 1.0:
                out.append(cur.rstrip())
                cur = w.lstrip()
            else:
                cur = trial
        out.append(cur.rstrip())
    return out or [""]


DOT_EM = {False: 0.25, True: 0.26502}  # Calibri「.」字寬（em），點狀定位點以此為間距


def dot_leader(cv, text_end, num_start, top, size, bold, anchor):
    """Word 點狀定位點：點落在以 anchor 為基準的固定格線上，逐點繪製。"""
    d = DOT_EM[bold] * size
    first = anchor - ((anchor - (text_end + 1.3)) // d) * d
    while first < text_end + 1.3:
        first += d
    n = int((num_start - 0.5 - first) // d)
    fn = F(bold)
    cv.setFont(fn, size)
    cv.setFillColorRGB(0, 0, 0)
    base = PAGE_H - (top + 0.75 * size)
    for k in range(max(n, 0)):
        cv.drawString(first + k * d, base, ".")


@dataclass
class Page:
    ops: list = field(default_factory=list)  # callables(cv, page_no, total)
    kind: str = "body"  # cover / body


class CoverDoc:
    """data 欄位見 build_cover_data()。"""

    def __init__(self, data, logo_path):
        self.d = data
        self.logo = logo_path
        self.pages = []
        self.toc = {}  # key -> page number

    # ------------------------------------------------------------ 共用
    def _header_footer(self, cv, n, total, first):
        d = self.d
        if first:
            cv.drawImage(self.logo, 384.75, PAGE_H - 25.65 - 45.05, 149.32, 45.05, mask="auto")
            return
        cv.drawImage(self.logo, 383.25, PAGE_H - 24.85 - 45.05, 149.32, 45.05, mask="auto")
        text(cv, "CFXD SLA Project", 54.0, 37.53, 9.96, color=GRAY_TEXT)
        text(cv, "Daily Work Report" + d["doc_no"], 54.0, 49.77, 9.96, color=GRAY_TEXT)
        top = 782.02
        text(cv, d["doc_no"], 59.42, top)
        text(cv, f"Rev. {d['revision']}", 247.01, top)
        text(cv, d["date_dot"], 346.87, top)
        x = 470.86
        x += text(cv, "Page ", x, top)
        x += text(cv, str(n), x, top, bold=True)
        x += text(cv, " of ", x, top)
        text(cv, str(total), x, top, bold=True)

    def _heading(self, page, num, title, top):
        def op(cv, n, total):
            text(cv, str(num), 54.0, top, 18, True, HEAD_COLOR)
            text(cv, title, 68.18, top, 18, True, HEAD_COLOR)

        page.ops.append(op)

    def new_page(self, kind="body"):
        p = Page(kind=kind)
        self.pages.append(p)
        return p

    # ------------------------------------------------------------ 版面
    def layout(self):
        self.pages = []
        self._cover()
        self._contents()
        self._list_of_tables()
        self._revision_control()
        self._reports_control()
        self._inspection_record()
        self._onsite_record()
        self._true_alarm()

    def _cover(self):
        d = self.d
        p = self.new_page("cover")

        def op(cv, n, total):
            ctext(cv, "CFXD SLA Project", PAGE_W / 2, 223.5, 27.96)
            ctext(cv, "Daily Work Report", PAGE_W / 2, 274.8, 27.96)
            rows = [
                ("Document No.", d["doc_no"]),
                ("Contractor", "Rally Technology Co., Ltd."),
                ("Contractor Doc No.", d["contractor_doc_no"]),
                ("Issue Purpose", d["issue_purpose"]),
                ("Revision", f"Rev. {d['revision']}"),
                ("Date", d["date_dot"]),
                ("Author", d["author"]),
                ("Approval", d["approver"]),
            ]
            y = 397.49
            for label, value in rows:
                text(cv, label, 79.94, y + 3.84)
                text(cv, value, 193.13, y + 3.84)
                y += 17.52
            for i in range(len(rows) + 1):
                hline(cv, 397.49 + 17.52 * i, 74.78 - 0.24, 520.66 + 0.24)
            for x in (74.54, 187.73, 520.9):
                vline(cv, x, 397.49, 397.49 + 17.52 * len(rows))

        p.ops.append(op)

    def _toc_line(self, cv, top, prefix, title, key, size, bold, end_x, num_end):
        x = 54.0
        if prefix:
            text(cv, prefix, x, top, size, bold)
            x = 64.19 if size > 13 else x
        x += text(cv, title, x, top, size, bold)
        num = str(self.toc.get(key, ""))
        nw = fonts.width(num, F(bold), size)
        anchor = end_x - DOT_EM[bold] * size
        dot_leader(cv, x, min(num_end - nw, end_x + 0.5), top, size, bold, anchor)
        text(cv, num, num_end - nw, top, size, bold)

    def _contents(self):
        p = self.new_page()
        entries = [
            (None, "LIST OF TABLES", "lot"),
            (None, "REVISION CONTROL", "rev"),
            ("1", f"REPORTS CONTROL ({self.d['month_label'].upper()})", "s1"),
            ("2", "INSPECTION RECORD", "s2"),
            ("3", "ON-SITE RECORD/SYSTEM UPDATE RECORD", "s3"),
            ("4", "DAILY ALARM INSPECTION (TRUE ALARM)", "s4"),
        ]
        tops = [107.57, 130.61, 153.77, 176.81, 199.85, 223.01]

        def op(cv, n, total):
            text(cv, "Contents", 54.0, 77.93, 15.96, True)
            for (prefix, title, key), top in zip(entries, tops):
                self._toc_line(cv, top, prefix, title, key, 14.04, True, 494.86, 504.14)

        p.ops.append(op)

    def _list_of_tables(self):
        p = self.toc_page_lot = self.new_page()
        self.toc["lot"] = len(self.pages)
        items = [
            (f"Table 1 Reports Control ({self.d['month_label']})", "t1"),
            ("Table 2 Shift Record and Issue Record", "t2"),
            ("Table 3 On-site Record and System Update Record", "t3"),
            ("Table 4 Daily Alarm Inspection (True Alarm)", "t4"),
        ]

        def op(cv, n, total):
            text(cv, "List of Tables", 54.0, 94.49, 15.96, True)
            for i, (title, key) in enumerate(items):
                self._toc_line(cv, 119.06 + 14.68 * i, None, title, key, 12, False, 498.12, 505.14)

        p.ops.append(op)

    def _revision_control(self):
        p = self.new_page()
        self.toc["rev"] = len(self.pages)
        d = self.d
        xs = [54.24, 107.18, 194.21, 236.09, 383.83, 462.1, 541.18]
        rows = d["revisions"]

        def op(cv, n, total):
            text(cv, "Revision Control", 54.0, 94.49, 15.96, True)
            heads = ["Revision", "Date", "Status", "Changes", "Author", "Approval"]
            y = 138.14
            for i, h in enumerate(heads):
                ctext(cv, h, (xs[i] + xs[i + 1]) / 2, y + 2.64, bold=True)
            for r in rows:
                y += 16.32
                vals = [str(r["rev"]), r["date"], r["status"], None, r["author"], r["approval"]]
                for i, v in enumerate(vals):
                    if v is not None:
                        ctext(cv, v, (xs[i] + xs[i + 1]) / 2, y + 2.96)
                text(cv, "•", 242.2, y + 2.1, 12)
                text(cv, r["changes"], 255.65, y + 2.96)
            bottom = 138.14 + 16.32 * (len(rows) + 1)
            for i in range(len(rows) + 2):
                hline(cv, 138.14 + 16.32 * i, 54.48 - 0.24, 540.94 + 0.24)
            for x in xs:
                vline(cv, x, 138.14, bottom)

        p.ops.append(op)

    def _reports_control(self):
        p = self.new_page()
        self.toc["s1"] = self.toc["t1"] = len(self.pages)
        d = self.d
        xs = [74.54, 123.98, 216.05, 322.51, 520.9]
        n_rows = max(30, len(d["reports"]))
        # 原始列高：15.24 / 15.12 交錯
        base = [15.24, 15.12, 15.12, 15.12, 15.12, 15.24, 15.15, 15.12, 15.12, 15.24, 15.12, 15.12, 15.12, 15.24,
                15.12, 15.12, 15.12, 15.24, 15.14, 15.12, 15.12, 15.24, 15.12, 15.12, 15.12, 15.12, 15.24, 15.12,
                15.12, 15.24]
        pitches = [base[i] if i < len(base) else 15.15 for i in range(n_rows)]

        def op(cv, n, total):
            self._heading_draw(cv, 1, f"Reports Control ({d['month_label']})", 98.48)
            top = 128.06
            shade(cv, xs[0] + 0.24, top + 0.24, xs[-1] - 0.24, top + 29.16 - 0.24)
            for i, h in enumerate(["No.", "Date", "Author", "Report ID"]):
                ctext(cv, h, (xs[i] + xs[i + 1]) / 2, 137.78)
            y = top + 29.16
            lines = [top, y]
            for i in range(n_rows):
                vals = [str(i + 1)] + (list(d["reports"][i]) if i < len(d["reports"]) else ["", "", ""])
                for k, v in enumerate(vals):
                    if v:
                        ctext(cv, v, (xs[k] + xs[k + 1]) / 2, y + 2.64)
                y += pitches[i]
                lines.append(y)
            for ly in lines:
                hline(cv, ly, xs[0] + 0.24 - 0.24, xs[-1] + 0.24 - 0.24)
            for x in xs:
                vline(cv, x, top, y)
            ctext(cv, f"Table 1 Reports Control ({d['month_label']})", PAGE_W / 2, y + 2.91)

        p.ops.append(op)

    def _heading_draw(self, cv, num, title, top):
        text(cv, str(num), 54.0, top, 18, True, HEAD_COLOR)
        text(cv, title, 68.18, top, 18, True, HEAD_COLOR)

    # ---- 表格流（可跨頁）
    def _flow_table(self, page, start_top, x0, split, x1, rows, caption, caption_key, label_pad=5.4):
        """rows: [("section", 標題) | ("row", 標籤, 值, 最小列高) | ("image", 標籤, [路徑], 高度)]"""
        y = start_top
        cur = page
        segs = []  # (page, [(kind, y0, y1, payload)])
        seg = []
        for row in rows:
            kind = row[0]
            if kind == "section":
                h = row[2] if len(row) > 2 else ROW_H
            elif kind == "row":
                lines = wrap(row[2], x1 - split - 2 * CELL_PAD)
                llines = wrap(row[1], split - x0 - 2 * CELL_PAD)
                h = max(row[3], 14.64 * max(len(lines), len(llines)) + 0.48)
            else:
                h = row[3]
            if y + h > BOTTOM_LIMIT and seg:
                segs.append((cur, seg, y))
                cur = self.new_page()
                seg = []
                y = 76.1
            seg.append((kind, y, y + h, row))
            y += h
        segs.append((cur, seg, y))
        last_page = cur
        self.toc[caption_key] = self.pages.index(last_page) + 1

        for pg, seg, bottom in segs:
            def op(cv, n, total, seg=seg, bottom=bottom, is_last=(pg is last_page)):
                if not seg:
                    return
                top = seg[0][1]
                for kind, y0, y1, row in seg:
                    if kind == "section":
                        shade(cv, x0 + 0.24, y0 + 0.24, x1 - 0.24, y1 - 0.24)
                        text(cv, row[1], x0 + label_pad, y0 + TEXT_DY, bold=True)
                    elif kind == "row":
                        shade(cv, x0 + 0.24, y0 + 0.24, split - 0.24, y1 - 0.24)
                        llines = wrap(row[1], split - x0 - 2 * CELL_PAD)
                        vlines_ = wrap(row[2], x1 - split - 2 * CELL_PAD)
                        if len(llines) < len(vlines_):
                            ltop = y0 + (y1 - y0 - 14.64 * len(llines)) / 2
                        else:
                            ltop = y0 + TEXT_DY + max(0.0, (y1 - y0 - 15.12 - 14.64 * (len(vlines_) - 1)) / 2)
                        for i, s in enumerate(llines):
                            text(cv, s, x0 + label_pad, ltop + 14.64 * i)
                        for i, s in enumerate(vlines_):
                            text(cv, s, split + CELL_PAD, y0 + TEXT_DY + 14.64 * i)
                    else:
                        shade(cv, x0 + 0.24, y0 + 0.24, split - 0.24, y1 - 0.24)
                        text(cv, row[1], x0 + label_pad, (y0 + y1) / 2 - 4.91)
                        imgs = row[2]
                        if not imgs:
                            text(cv, "N/A", split + CELL_PAD, y0 + TEXT_DY)
                        else:
                            _draw_images(cv, imgs, split + CELL_PAD, y0 + 3, x1 - split - 2 * CELL_PAD, y1 - y0 - 6)
                    hline(cv, y0, x0 + 0.24 - 0.24, x1)
                hline(cv, bottom, x0, x1)
                vline(cv, x0, top, bottom)
                vline(cv, x1, top, bottom)
                for kind, y0, y1, row in seg:
                    if kind != "section":
                        vline(cv, split, y0, y1)
                if is_last:
                    ctext(cv, caption, PAGE_W / 2, bottom + 2.91)

            pg.ops.append(op)
        return last_page

    def _inspection_record(self):
        d = self.d
        p = self.new_page()
        self.toc["s2"] = len(self.pages)

        def op(cv, n, total):
            self._heading_draw(cv, 2, "Inspection Record", 99.32)
            for i, s in enumerate(wrap(d["inspection_summary"], 487.18)):
                text(cv, s, 54.0, 131.06 + 14.64 * i)

        p.ops.append(op)
        extra = 14.64 * (len(wrap(d["inspection_summary"], 487.18)) - 1)
        rows = [
            ("section", "Shift Record", 15.12),
            ("row", "First Line Duty officer", d["first_line_officer"], 15.12),
            ("row", "Second Line Backup Support", d["second_line_support"], 15.12),
            ("row", "Document Approved", d["approver"], 15.24),
            ("row", d["author"], d["author"], 15.12),
        ]
        issue_h = [15.15, 15.24, 15.12, 15.12, 15.12, 15.24, 15.84, 15.12, 15.12, 15.24]
        issues = d["issues"] or [None]
        for issue in issues:
            rows.append(("section", "Issue Record ", 15.12))
            for (key, label), h in zip(ISSUE_FIELDS, issue_h):
                rows.append(("row", label, (issue or {}).get(key) or "N/A", h))
        self._flow_table(p, 158.18 + extra, 67.46, 216.05, 527.98, rows,
                         "Table 2 Shift Record and Issue Record", "t2")

    def _onsite_record(self):
        d = self.d
        p = self.new_page()
        self.toc["s3"] = len(self.pages)

        def op(cv, n, total):
            self._heading_draw(cv, 3, "On-site Record/System Update Record", 99.32)
            for i, s in enumerate(wrap(d["onsite_summary"], 487.18)):
                text(cv, s, 54.0, 131.06 + 14.64 * i)

        p.ops.append(op)
        extra = 14.64 * (len(wrap(d["onsite_summary"], 487.18)) - 1)
        rows = []
        onsite_h = [15.12, 15.12, 15.24, 15.12, 15.12, 15.15, 15.24, 15.12]
        for i, rec in enumerate(d["onsite"] or [None]):
            rows.append(("section", "On-site Record", 15.12))
            for (key, label), h in zip(ONSITE_FIELDS, onsite_h):
                if key == "photo":
                    imgs = d["onsite_images"] if i == 0 else []
                    if imgs:
                        rows.append(("image", label, imgs, 260))
                    else:
                        rows.append(("row", label, "N/A", h))
                    continue
                rows.append(("row", label, (rec or {}).get(key) or "N/A", h))
        sys_h = [15.12, 15.12, 15.24, 15.12, 15.12, 15.12, 15.24]
        for rec in d["system_updates"] or [None]:
            rows.append(("section", "System Update Record", sys_h[0]))
            for (key, label), h in zip(SYSUPDATE_FIELDS, sys_h[1:]):
                rows.append(("row", label, (rec or {}).get(key) or "N/A", h))
        self._flow_table(p, 158.18 + extra, 70.94, 184.1, 524.38, rows,
                         "Table 3 On-site Record and System Update Record", "t3")

    def _true_alarm(self):
        d = self.d
        records = d["true_alarms"] or [None]
        first = True
        for idx, rec in enumerate(records):
            p = self.new_page()
            if first:
                self.toc["s4"] = len(self.pages)

            def op(cv, n, total, first=first):
                if first:
                    self._heading_draw(cv, 4, "Daily Alarm Inspection (True Alarm)", 101.12)
                    text(cv, f"New Alarm: {d['alarm_new']}", 54.0, 132.86)
                    text(cv, f"Known Alarm: {d['alarm_known']}", 54.0, 147.5)
                    text(cv, f"False Alarm: {d['alarm_false']}", 54.0, 162.14)

            p.ops.append(op)
            r = rec or {}
            imgs = d["alarm_images"] if idx == 0 else []
            rows = [("section", "True Alarm Record", 15.12),
                    ("row", "Alarm Item", r.get("alarm_item") or "N/A", 15.24),
                    ("row", "Alarm Start Time", r.get("start_time") or "N/A", 17.28),
                    ("row", "Alarm End Time", r.get("end_time") or "N/A", 17.28),
                    ("row", "Issue Checked By", r.get("checked_by") or "N/A", 16.92),
                    ("row", "Recorded By", r.get("recorded_by") or "N/A", 17.31),
                    ("row", "Status", r.get("status") or "N/A", 15.24),
                    ("image", "Screenshot", imgs, 395.29),
                    ("row", "Follow Up", r.get("follow_up") or "N/A", 14.92)]
            caption = "Table 4 Daily Alarm Inspection (True Alarm)" if idx == len(records) - 1 else ""
            self._flow_table(p, 174.62 if first else 76.1, 54.24, 153.26, 541.18, rows, caption, "t4")
            first = False

    # ------------------------------------------------------------ 輸出
    def render(self, cv, page_offset=0, total_override=None):
        self.layout()
        self.layout()  # 第二次：目錄頁碼已知
        total = len(self.pages)
        for i, p in enumerate(self.pages, 1):
            self._header_footer(cv, i, total, p.kind == "cover")
            for op in p.ops:
                op(cv, i, total)
            cv.showPage()
        return total


def _draw_images(cv, paths, x, top, w, h):
    from .screenshot import jpeg_reader

    n = len(paths)
    each_h = (h - 4 * (n - 1)) / n
    y = top
    for p in paths:
        img = jpeg_reader(p)
        iw, ih = img.getSize()
        s = min(w / iw, each_h / ih)
        dw, dh = iw * s, ih * s
        cv.drawImage(img, x, PAGE_H - y - dh, dw, dh)
        y += dh + 4


ISSUE_FIELDS = [
    ("received_by", "Received By"),
    ("received_from", "Received From"),
    ("received_time", "Received Time"),
    ("report_by", "Report By"),
    ("reporter_phone", "Reporter Phone No."),
    ("issue_type", "Issue Type"),
    ("description", "Issue Description"),
    ("status", "Status"),
    ("further_action", "Further Action"),
    ("jira", "Issue on Jira"),
]
ONSITE_FIELDS = [
    ("personnel", "On-site Personnel"),
    ("recorded_by", "Recorded By"),
    ("time", "Time"),
    ("title", "Work Title"),
    ("status", "Work Status"),
    ("photo", "Work Photo"),
    ("further_action", "Further Action"),
    ("jira", "Work on Jira"),
]
SYSUPDATE_FIELDS = [
    ("system_name", "System Name"),
    ("update_personnel", "Update Personnel"),
    ("backup_support", "Backup Support"),
    ("time_period", "Time Period"),
    ("risk_info", "Risk Information"),
    ("logbook_updated", "Log Book Updated"),
]
