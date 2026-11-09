"""CheckList Excel（範本或過去的日報）各工作表的檢查項目列 ↔ 資料庫檢查項目的對應。

以前 PDF 用「第 N 個勾選列 = 第 N 個項目」對應，項目新增、刪除或調整順序後就會錯位。
現在依每一列的文字（Device Name、Status Type、Description）找出是哪一個項目，
再依先後順序對齊（difflib），所以：

- 項目內容與順序都和範本一致：與以前的結果完全相同。
- 在系統裡新增、刪除、修改、拖曳調整項目後，範本還沒改版：已存在的項目照樣填到範本中正確的那一列，
  `diff()` 列出範本需要怎麼改（新增哪些列、刪除哪些列、哪些列的文字或順序要改）。
- 匯入過去的日報 Excel（舊版範本）：同一套對應方式，對不到的列列出警告。
"""

import difflib
from dataclasses import dataclass, field

# 各檢查表工作表：(工作表名稱, Normal, Warning, Failed, Note 欄)
CHECK_COLS = {
    "vm": ("Virtual Machines", "F", "H", "J", "L"),
    "software": ("Software", "I", "K", "M", "O"),
    "physical": ("Physical devices", "I", "K", "M", "O"),
    "pi": ("PI", "F", "H", "J", "L"),
}


def norm(v):
    return " ".join(str(v).split()).casefold() if v is not None else ""


def num(v):
    """Item 編號比對用：Excel 會把 11.10 存成 11.1，兩邊都轉成相同寫法。"""
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return norm(v)


def item_key(item):
    return (norm(item.device_name), norm(item.status_type), norm(item.description))


def item_label(item):
    return " / ".join(x for x in (item.item_no, item.device_name, item.status_type, item.description) if x)


@dataclass
class Row:
    row: int  # Excel 列號
    texts: set  # 勾選欄左邊各欄的文字（合併儲存格取左上角的值）
    label: str  # 給人看的內容
    has_box: bool  # 有勾選框（TRUE / FALSE）
    key: tuple = None
    nums: set = field(default_factory=set)  # 這列的 Item 編號候選


@dataclass
class SheetMap:
    sheet: str
    pairs: dict = field(default_factory=dict)  # Excel 列號 -> item
    changed: list = field(default_factory=list)  # (Row, item)：同一個位置但文字不同（範本文字要更新）
    moved: list = field(default_factory=list)  # (Row, item)：文字相同但順序不同（範本順序要調整）
    missing: list = field(default_factory=list)  # (item, 前一個項目或 None)：範本沒有（範本要新增）
    extra: list = field(default_factory=list)  # Row：範本有、系統沒有（範本要刪除）
    mode: str = "text"  # text：依文字對應 / position：文字完全對不到，依順序對應（相容舊範本）

    @property
    def ok(self):
        return not (self.changed or self.moved or self.missing or self.extra)


def _col_index(letter):
    from openpyxl.utils import column_index_from_string

    return column_index_from_string(letter)


def _merged_values(ws):
    out = {}
    for rng in ws.merged_cells.ranges:
        v = ws.cell(row=rng.min_row, column=rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                out[(r, c)] = v
    return out


def sheet_rows(ws, sheet, keyset=None):
    """工作表中的檢查項目列。範本的勾選欄是 TRUE / FALSE；過去的日報可能有沒勾的空白格，
    所以文字對得到資料庫項目（keyset）的列也算。"""
    _, cn, cw, cf, _ = CHECK_COLS[sheet]
    first = _col_index(cn)
    boxes = [_col_index(c) for c in (cn, cw, cf)]
    merged = _merged_values(ws)
    by_desc = {}
    for k in keyset or ():
        by_desc.setdefault(k[2], []).append(k)
    rows = []
    for r in range(1, ws.max_row + 1):
        has_box = any(isinstance(ws.cell(row=r, column=c).value, bool) for c in boxes)
        texts, shown, nums = {""}, [], set()
        for c in range(1, first):
            v = merged.get((r, c), ws.cell(row=r, column=c).value)
            if isinstance(v, str) and v.strip():
                texts.add(norm(v))
                shown.append(" ".join(v.split()))
                nums.add(num(v))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                shown.append(f"{v:g}")
                nums.add(num(v))
        # 符合的項目中取最具體的（空白欄位最少）：避免 Status Type 空白的項目搶走有 Status Type 的列
        key = max((k for t in texts for k in by_desc.get(t, ()) if k[0] in texts and k[1] in texts),
                  key=lambda k: sum(1 for x in k if x), default=None)
        if has_box or key:
            rows.append(Row(r, texts, " / ".join(dict.fromkeys(shown)), has_box, key, nums))
    return rows


def align(sheet, rows, items):
    """rows：sheet_rows()；items：資料庫項目（依系統順序）。"""
    m = SheetMap(sheet)
    keys = [item_key(i) for i in items]
    if rows and not any(r.key for r in rows) and len(rows) == len(items):
        # 文字完全對不到（範本格式與預期不同）：沿用舊的依順序對應
        m.mode = "position"
        m.pairs = {r.row: it for r, it in zip(rows, items)}
        return m
    a = [r.key if r.key else ("__row__", r.row) for r in rows]
    sm = difflib.SequenceMatcher(None, a, keys, autojunk=False)
    free_rows, free_items = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for r, it in zip(rows[i1:i2], items[j1:j2]):
                m.pairs[r.row] = it
        elif op == "replace" and i2 - i1 == j2 - j1:
            for r, it in zip(rows[i1:i2], items[j1:j2]):
                if r.key and r.key in keys[j2:] + keys[:j1]:  # 這列其實是別處的項目（順序不同），稍後處理
                    free_rows.append(r)
                    free_items.append(it)
                    continue
                m.pairs[r.row] = it
                m.changed.append((r, it))
        else:
            free_rows.extend(rows[i1:i2])
            free_items.extend(items[j1:j2])
    # 文字相同、位置不同：拖曳調整過順序
    left = {}
    for it in free_items:
        left.setdefault(item_key(it), []).append(it)
    rest = []
    for r in free_rows:
        cands = left.get(r.key) if r.key else None
        if cands:
            it = cands.pop(0)
            m.pairs[r.row] = it
            m.moved.append((r, it))
        else:
            rest.append(r)
    # 文字改過的項目：同一個 Item 編號、同一個 Device 的列（範本文字要更新）
    paired = {id(it) for it in m.pairs.values()}
    rest2 = []
    for r in rest:
        it = next((it for it in free_items if id(it) not in paired and num(it.item_no) in r.nums
                   and norm(it.device_name) in r.texts), None)
        if it is None:
            rest2.append(r)
            continue
        paired.add(id(it))
        m.pairs[r.row] = it
        m.changed.append((r, it))
    rest = rest2
    prev = None
    for it in items:
        if id(it) not in paired:
            m.missing.append((it, prev))
        prev = it
    m.extra = [r for r in rest if r.has_box]
    return m


def map_workbook(wb, items_by_sheet):
    """{sheet: SheetMap}。wb：openpyxl 活頁簿；items_by_sheet：{sheet: [item...]}（依系統順序）。"""
    out = {}
    for sheet, (name, *_rest) in CHECK_COLS.items():
        items = items_by_sheet.get(sheet, [])
        if name not in wb.sheetnames:
            m = SheetMap(sheet)
            m.missing = [(it, None) for it in items]
            out[sheet] = m
            continue
        keyset = {item_key(i) for i in items}
        out[sheet] = align(sheet, sheet_rows(wb[name], sheet, keyset), items)
    return out


def active_items():
    from .models import ChecklistItem

    out = {s: [] for s in CHECK_COLS}
    for it in ChecklistItem.query.filter_by(active=True).order_by(ChecklistItem.sheet, ChecklistItem.order,
                                                                ChecklistItem.id):
        out.setdefault(it.sheet, []).append(it)
    return out


# ---------------------------------------------------------------- 範本是否需要改版

_status_cache = {}


def template_status():
    """{"ok", "error", "sheets": {sheet: SheetMap}}；範本檔或檢查項目沒變時用快取。"""
    import os

    import openpyxl
    from flask import current_app

    from . import db
    from .models import ChecklistItem

    path = current_app.config["PDF_TEMPLATE_XLSX"]
    if not os.path.exists(path):
        return {"ok": False, "error": "尚未上傳 CheckList 範本", "sheets": {}}
    stamp = db.session.query(db.func.count(ChecklistItem.id), db.func.max(ChecklistItem.updated_at)).filter(
        ChecklistItem.active.is_(True)).one()
    sig = (path, os.path.getmtime(path), os.path.getsize(path), tuple(stamp))
    hit = _status_cache.get("v")
    if hit and hit[0] == sig:
        return hit[1]
    try:
        wb = openpyxl.load_workbook(path)
        sheets = map_workbook(wb, active_items())
        res = {"ok": all(m.ok for m in sheets.values()), "error": "", "sheets": sheets}
    except Exception as e:  # noqa: BLE001 - 範本壞掉時顯示原因
        res = {"ok": False, "error": f"範本讀取失敗：{e}", "sheets": {}}
    _status_cache["v"] = (sig, res)
    return res


def diff_rows(status):
    """範本修改清單（給人看 / 下載 CSV）：[(工作表, 動作, Excel 列, 內容, 說明)]。"""
    out = []
    for sheet, m in status.get("sheets", {}).items():
        name = CHECK_COLS[sheet][0]
        for it, prev in m.missing:
            where = f"放在 {prev.item_no} 之後" if prev else "放在最前面"
            out.append((name, "新增一列", "", item_label(it), where))
        for r in m.extra:
            out.append((name, "刪除這一列", r.row, r.label, "系統中已沒有這個項目"))
        for r, it in m.changed:
            out.append((name, "修改文字", r.row, r.label, "改成：" + item_label(it)))
        for r, it in m.moved:
            out.append((name, "調整順序", r.row, r.label, f"系統中排在第 {it.order} 項"))
    return out
