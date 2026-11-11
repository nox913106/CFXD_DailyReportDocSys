"""依 Excel 檔本身的版面設定（欄寬、列高、合併儲存格、框線、字型、列印範圍、
縮放、邊界、分頁、頁首圖片、圖表）把工作表畫成 PDF 頁面，重現 Excel 的列印結果。

座標單位：工作表座標為 pt（未縮放），畫到頁面時乘上 geometry factor。
校正值（以原始 Excel 輸出的 PDF 量測）：
  * 欄寬 pt = 字元寬 × 5.25，整體再乘上 縮放比 × 0.9848
  * 字型大小只乘縮放比
  * 頁首圖片 = VML 尺寸 × (1.0318, 0.9837) × 縮放比，靠右對齊右邊界
"""

import datetime as dt
import re
import zipfile
from dataclasses import dataclass, field
from posixpath import dirname, join, normpath
from xml.etree import ElementTree as ET

import openpyxl
from openpyxl.utils import get_column_letter, range_boundaries
from reportlab.lib.utils import ImageReader

from . import fonts
from .formula import Evaluator, FormulaError
from .xlsx_chart import draw_chart, load_chart

K_GEOM = 0.9828  # 水平
K_GEOM_Y = 0.9826  # 垂直（預設）
# Excel 依縮放比把列高換算成印表機點數時的實測係數（以原始輸出校正）
K_GEOM_Y_BY_SCALE = {45: 0.9820, 47: 0.9842, 48: 0.9842, 52: 0.9826, 53: 0.9803, 62: 0.9800}
TOP_OFFSET = 0.45  # pt，原始輸出表格頂端比上邊界略低
OPTIMIZE_CHARTS = True  # 圖表排列優化：對齊統計表、統一尺寸、標籤避免重疊（表格與框線不變）
LEFT_OFFSET = -0.42  # pt，置中後的實測偏移
TEXT_DY = 1.0  # 工作表 pt，文字基線下移量
CHAR_PT = 5.25
HDR_IMG_W = 1.0318
HDR_IMG_H = 0.9837
PAD_X = 2.6  # 儲存格左右內距（工作表 pt）
LINE_H = 1.2207  # Calibri 行高 / 字級

BORDER_W = {"hair": 0.24, "thin": 0.48, "dotted": 0.48, "dashed": 0.48, "dashDot": 0.48,
            "dashDotDot": 0.48, "medium": 0.96, "mediumDashed": 0.96, "mediumDashDot": 0.96,
            "mediumDashDotDot": 0.96, "slantDashDot": 0.96, "thick": 1.44, "double": 0.48}
DASH = {"dotted": [0.5, 1], "dashed": [2, 1], "mediumDashed": [3, 1.5], "dashDot": [2, 1, 0.5, 1],
        "mediumDashDot": [3, 1.5, 1, 1.5]}

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}

INDEXED = {8: "000000", 9: "FFFFFF", 10: "FF0000", 12: "0000FF", 64: "000000"}


# ---------------------------------------------------------------- 色彩


class Theme:
    def __init__(self, xml_bytes):
        self.colors = {}
        if not xml_bytes:
            return
        root = ET.fromstring(xml_bytes)
        scheme = root.find(".//a:clrScheme", NS)
        for child in scheme:
            tag = child.tag.split("}")[1]
            el = child[0]
            val = el.get("lastClr") or el.get("val")
            self.colors[tag] = val
        # Excel theme index 順序：lt1, dk1, lt2, dk2, accent1..6
        self.order = ["lt1", "dk1", "lt2", "dk2"] + [f"accent{i}" for i in range(1, 7)] + ["hlink", "folHlink"]

    def by_index(self, i):
        return self.colors.get(self.order[i], "000000") if i < len(self.order) else "000000"

    def by_name(self, name):
        alias = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}
        return self.colors.get(alias.get(name, name), "000000")


def _tint(hex6, tint):
    r, g, b = (int(hex6[i : i + 2], 16) for i in (0, 2, 4))
    if tint < 0:
        r, g, b = (c * (1 + tint) for c in (r, g, b))
    else:
        r, g, b = (c + (255 - c) * tint for c in (r, g, b))
    return "%02X%02X%02X" % tuple(max(0, min(255, round(c))) for c in (r, g, b))


def resolve_color(color, theme, default=None):
    if color is None:
        return default
    try:
        if color.type == "rgb" and color.rgb and isinstance(color.rgb, str):
            rgb = color.rgb[-6:]
        elif color.type == "theme":
            rgb = theme.by_index(color.theme)
        elif color.type == "indexed":
            if color.indexed == 64:
                return default
            from openpyxl.styles.colors import COLOR_INDEX

            rgb = COLOR_INDEX[color.indexed][-6:] if color.indexed < len(COLOR_INDEX) else "000000"
        else:
            return default
    except (TypeError, AttributeError):
        return default
    if getattr(color, "tint", 0):
        rgb = _tint(rgb, color.tint)
    return rgb


def hex_to_rgb(h):
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


# ---------------------------------------------------------------- 數字格式


def format_value(v, fmt):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    fmt = (fmt or "General").split(";")[0]
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return _format_datetime(v, fmt)
    if isinstance(v, (int, float)):
        if "%" in fmt:
            m = re.search(r"0(?:\.(0+))?%", fmt)
            dec = len(m.group(1)) if m and m.group(1) else 0
            return _half_up(v * 100, dec) + "%"
        m = re.fullmatch(r"#?,?#*0(?:\.(0+))?", fmt.replace("\\", ""))
        if m:
            dec = len(m.group(1)) if m.group(1) else 0
            s = _half_up(v, dec)
            if "," in fmt:
                ip, _, fp = s.partition(".")
                s = f"{int(ip):,}" + ("." + fp if fp else "")
            return s
        if float(v).is_integer():
            return str(int(v))
        return ("%.10g" % v)
    return str(v)


def _half_up(x, dec):
    from decimal import ROUND_HALF_UP, Decimal

    return str(Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_UP))


def _format_datetime(v, fmt):
    f = re.sub(r"\[\$-[0-9A-Fa-f]+\]", "", fmt).replace("\\", "").replace('"', "").replace("@", "")
    if isinstance(v, dt.time):
        v = dt.datetime.combine(dt.date(1900, 1, 1), v)
    elif isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        v = dt.datetime.combine(v, dt.time())
    if f.strip().lower() == "general":
        f = "yyyy/m/d"
    ampm = "AM/PM" in f.upper()
    out = []
    i = 0
    tokens = [("yyyy", "%Y"), ("yy", "%y"), ("mmmm", "%B"), ("mmm", "%b"), ("dd", "%d"), ("hh", "HH"), ("h", "H"),
              ("ss", "%S"), ("am/pm", "AMPM"), ("mm", "MM"), ("m", "M"), ("d", "D")]
    prev_time = False
    while i < len(f):
        for tok, rep in tokens:
            if f[i : i + len(tok)].lower() == tok:
                if tok in ("mm", "m"):
                    # m 在 h 之後或 s 之前代表分鐘
                    rest = f[i + len(tok):].lower().lstrip(":")
                    is_min = prev_time or rest.startswith("s")
                    out.append(f"{v.minute:02d}" if is_min and tok == "mm" else
                               (f"{v.minute}" if is_min else (f"{v.month:02d}" if tok == "mm" else str(v.month))))
                elif rep == "D":
                    out.append(str(v.day))
                elif rep in ("HH", "H"):
                    hour = v.hour % 12 or 12 if ampm else v.hour
                    out.append(f"{hour:02d}" if rep == "HH" else str(hour))
                    prev_time = True
                elif rep == "AMPM":
                    out.append("AM" if v.hour < 12 else "PM")
                else:
                    out.append(v.strftime(rep))
                i += len(tok)
                break
        else:
            out.append(f[i])
            i += 1
    return "".join(out).strip()


# ---------------------------------------------------------------- 工作表模型


@dataclass
class Sheet:
    ws: object
    name: str
    col_x: dict  # col -> x 起點（工作表 pt）
    col_w: dict
    rows: list  # 列印範圍內可見列
    row_h: dict
    c1: int
    c2: int
    merged: dict  # (r,c) -> (r1,c1,r2,c2)  左上角
    covered: set
    scale: float
    margins: object
    h_centered: bool
    breaks: set
    header_image: tuple | None = None  # (ImageReader, w_pt, h_pt)
    charts: list = field(default_factory=list)


class Workbook:
    def __init__(self, path, values=None, recalc=False):
        """values: {(sheet_name, 'A1'): value}，覆寫/提供儲存格的顯示值。"""
        self.path = path
        self.wb = openpyxl.load_workbook(path)
        self.cached = openpyxl.load_workbook(path, data_only=True)
        self.values = values or {}
        self.recalc = recalc
        self._memo = {}
        self._evaluator = Evaluator(self.value)
        self.zip = zipfile.ZipFile(path)
        self.theme = Theme(self._read("xl/theme/theme1.xml"))
        self._sheet_paths = self._map_sheet_paths()

    def _read(self, name):
        try:
            return self.zip.read(name)
        except KeyError:
            return None

    def _rels(self, part):
        p = join(dirname(part), "_rels", part.split("/")[-1] + ".rels")
        data = self._read(p)
        if not data:
            return {}
        root = ET.fromstring(data)
        # Target 可能是相對路徑（Excel）或以 / 開頭的套件絕對路徑（openpyxl、LibreOffice 等存的檔案）
        return {
            r.get("Id"): (r.get("Target").lstrip("/") if r.get("Target", "").startswith("/")
                          else normpath(join(dirname(part), r.get("Target"))))
            for r in root.findall("rel:Relationship", NS)
        }

    def _map_sheet_paths(self):
        root = ET.fromstring(self._read("xl/workbook.xml"))
        rels = self._rels("xl/workbook.xml")
        out = {}
        for s in root.find("m:sheets", NS):
            out[s.get("name")] = rels[s.get(f"{{{NS['r']}}}id")]
        return out

    def value(self, sheet, coord):
        key = (sheet, coord)
        if key in self.values:
            return self.values[key]
        if not self.recalc:
            return self.cached[sheet][coord].value
        if key in self._memo:
            return self._memo[key]
        raw = self.wb[sheet][coord].value
        if isinstance(raw, str) and raw.startswith("="):
            self._memo[key] = None  # 防止循環參照
            try:
                v = self._evaluator.eval(raw, sheet)
                if isinstance(v, list):
                    v = v[0][0] if v and v[0] else None
            except (FormulaError, ZeroDivisionError, ValueError, TypeError, KeyError, IndexError):
                v = self.cached[sheet][coord].value
        else:
            v = raw
        self._memo[key] = v
        return v

    def set(self, sheet, coord, value):
        self.values[(sheet, coord)] = value
        self._memo.clear()

    # -- 版面
    def sheet(self, name):
        ws = self.wb[name]
        pa = ws.print_area
        if isinstance(pa, (list, tuple)):
            pa = pa[0]
        if pa:
            c1, r1, c2, r2 = range_boundaries(pa.split("!")[-1].replace("$", ""))
        else:
            c1, r1, c2, r2 = 1, 1, ws.max_column, ws.max_row

        default_w = ws.sheet_format.defaultColWidth or 8.43
        widths = {}
        hidden_cols = set()
        for dim in ws.column_dimensions.values():
            if dim.min is None:
                continue
            for c in range(dim.min, min(dim.max, 300) + 1):
                if dim.width:
                    widths[c] = dim.width
                if dim.hidden:
                    hidden_cols.add(c)
        col_x, col_w, x = {}, {}, 0.0
        for c in range(1, c2 + 2):
            w = 0 if c in hidden_cols else widths.get(c, default_w) * CHAR_PT
            col_x[c] = x
            col_w[c] = w
            if c >= c1:
                x += w
        # col_x 以列印範圍第一欄為 0
        base = col_x[c1]
        col_x = {c: col_x[c] - base for c in col_x}
        for c in range(1, c1):
            col_x[c] = -sum(col_w[k] for k in range(c, c1))

        default_h = ws.sheet_format.defaultRowHeight or 15
        rows, row_h = [], {}
        for r in range(1, max(r2, ws.max_row) + 2):
            dim = ws.row_dimensions.get(r)
            hidden = dim is not None and dim.hidden
            h = (dim.height if dim is not None and dim.height is not None else default_h)
            row_h[r] = 0 if hidden else h
            if r1 <= r <= r2 and not hidden:
                rows.append(r)

        merged, covered = {}, set()
        for mr in ws.merged_cells.ranges:
            merged[(mr.min_row, mr.min_col)] = (mr.min_row, mr.min_col, mr.max_row, mr.max_col)
            for r in range(mr.min_row, mr.max_row + 1):
                for c in range(mr.min_col, mr.max_col + 1):
                    if (r, c) != (mr.min_row, mr.min_col):
                        covered.add((r, c))

        breaks = {b.id for b in ws.row_breaks.brk}
        sh = Sheet(
            ws=ws, name=name, col_x=col_x, col_w=col_w, rows=rows, row_h=row_h, c1=c1, c2=c2,
            merged=merged, covered=covered, scale=(ws.page_setup.scale or 100) / 100,
            margins=ws.page_margins, h_centered=bool(ws.print_options.horizontalCentered), breaks=breaks,
        )
        self._load_drawings(sh)
        return sh

    def _load_drawings(self, sh):
        part = self._sheet_paths[sh.name]
        rels = self._rels(part)
        root = ET.fromstring(self._read(part))
        hf = root.find("m:legacyDrawingHF", NS)
        if hf is not None:
            vml_part = rels[hf.get(f"{{{NS['r']}}}id")]
            vml = self._read(vml_part).decode("utf-8", "ignore")
            vrels = self._rels(vml_part)
            m = re.search(r"width:([\d.]+)pt;height:([\d.]+)pt", vml)
            rid = re.search(r'o:relid="([^"]+)"', vml)
            if m and rid:
                img = ImageReader(self.zip.open(vrels[rid.group(1)]))
                sh.header_image = (img, float(m.group(1)), float(m.group(2)))
        dr = root.find("m:drawing", NS)
        if dr is not None:
            dpart = rels[dr.get(f"{{{NS['r']}}}id")]
            drels = self._rels(dpart)
            droot = ET.fromstring(self._read(dpart))
            for anchor in droot:
                frm, to = anchor.find("xdr:from", NS), anchor.find("xdr:to", NS)
                chart = anchor.find(".//c:chart", NS)
                if frm is None or to is None or chart is None:
                    continue
                cpart = drels[chart.get(f"{{{NS['r']}}}id")]

                def pos(el):
                    col = int(el.find("xdr:col", NS).text) + 1
                    row = int(el.find("xdr:row", NS).text) + 1
                    return (
                        self._x(sh, col) + int(el.find("xdr:colOff", NS).text) / 12700,
                        row,
                        int(el.find("xdr:rowOff", NS).text) / 12700,
                    )

                sh.charts.append((pos(frm), pos(to), load_chart(self._read(cpart), self.theme)))

    @staticmethod
    def _x(sh, col):
        return sh.col_x.get(col, 0)


# ---------------------------------------------------------------- 繪製


class SheetPainter:
    def __init__(self, book, sheet, canvas, page_w, page_h):
        self.b, self.s, self.cv = book, sheet, canvas
        self.pw, self.ph = page_w, page_h
        self.theme = book.theme
        self.f = sheet.scale * K_GEOM  # 水平幾何縮放
        self.fy = sheet.scale * K_GEOM_Y_BY_SCALE.get(round(sheet.scale * 100), K_GEOM_Y)  # 垂直幾何縮放
        self.fs = sheet.scale  # 字型縮放

    # 頁面分割
    def pages(self):
        s = self.s
        avail = (self.ph - (s.margins.top + s.margins.bottom) * 72) / self.fy
        pages, cur, h = [], [], 0.0
        prev = None
        for r in s.rows:
            # 手動分頁點可能落在隱藏列上：上一可見列到本列之間有分頁點就換頁
            if cur and prev is not None and any(prev <= b < r for b in s.breaks):
                pages.append(cur)
                cur, h = [], 0.0
            rh = s.row_h[r]
            if cur and h + rh > avail + 0.01:
                pages.append(cur)
                cur, h = [], 0.0
            cur.append(r)
            h += rh
            prev = r
        if cur:
            pages.append(cur)
        return pages

    def draw_page(self, rows):
        s = self.s
        total_w = sum(s.col_w[c] for c in range(s.c1, s.c2 + 1))
        if s.h_centered:
            self.x0 = (self.pw - total_w * self.f) / 2 + LEFT_OFFSET
        else:
            self.x0 = s.margins.left * 72
        self.y0 = s.margins.top * 72 + TOP_OFFSET  # 從頁面頂端算
        self.row_y, y = {}, 0.0
        for r in rows:
            self.row_y[r] = y
            y += s.row_h[r]
        self.rows = rows
        self.rowset = set(rows)
        self._draw_header()
        cells = self._collect_cells(rows)
        for cell in cells:
            self._fill(cell)
        for cell in cells:
            self._text(cell)
        for cell in cells:
            self._borders(cell)
        self._draw_charts(rows)

    # 座標轉換：工作表 -> 頁面（reportlab 原點在左下）
    def X(self, x):
        return self.x0 + x * self.f

    def Y(self, y):
        return self.ph - (self.y0 + y * self.fy)

    def _draw_header(self):
        s = self.s
        if not s.header_image:
            return
        img, w, h = s.header_image
        w, h = w * HDR_IMG_W * self.fs, h * HDR_IMG_H * self.fs
        x = self.pw - s.margins.right * 72 - w
        top = s.margins.header * 72
        self.cv.drawImage(img, x, self.ph - top - h, w, h, mask="auto")

    def _collect_cells(self, rows):
        s = self.s
        out = []
        first = rows[0]
        for r in rows:
            for c in range(s.c1, s.c2 + 1):
                if (r, c) in s.covered:
                    # 合併範圍跨頁時，於本頁第一列補畫
                    top = next((k for k, v in s.merged.items() if v[0] < r <= v[2] and v[1] <= c <= v[3]
                                and (r, c) != (v[0], v[1])), None)
                    if r == first and top and top[1] == c and top[0] not in self.rowset:
                        out.append(self._cell_box(top[0], c, start=r))
                    continue
                out.append(self._cell_box(r, c))
        return out

    def _cell_box(self, r, c, start=None):
        s = self.s
        r1, c1, r2, c2 = s.merged.get((r, c), (r, c, r, c))
        r_start = start or r1
        vis = [k for k in range(r_start, r2 + 1) if k in self.rowset]
        y = self.row_y[vis[0]]
        h = sum(s.row_h[k] for k in vis)
        x = s.col_x[c1]
        w = sum(s.col_w[k] for k in range(c1, min(c2, s.c2) + 1))
        return {"r": r1, "c": c1, "r2": r2, "c2": c2, "x": x, "y": y, "w": w, "h": h,
                "cell": s.ws.cell(row=r1, column=c1), "rows": vis}

    def _fill(self, box):
        fill = box["cell"].fill
        if fill is None or fill.patternType not in ("solid",):
            return
        rgb = resolve_color(fill.fgColor, self.theme)
        if not rgb:
            return
        self.cv.setFillColorRGB(*hex_to_rgb(rgb))
        self.cv.rect(self.X(box["x"]), self.Y(box["y"] + box["h"]), box["w"] * self.f, box["h"] * self.fy,
                     stroke=0, fill=1)

    def _display(self, box):
        cell = box["cell"]
        v = self.b.value(self.s.name, cell.coordinate)
        return v, cell

    def _text(self, box):
        v, cell = self._display(box)
        if v is None or v == "":
            return
        font_def = cell.font
        size = (font_def.sz or 11) * self.fs
        bold, italic = bool(font_def.b), bool(font_def.i)
        al = cell.alignment
        if isinstance(v, bool):
            self._checkbox(box, v, size)
            return
        text = format_value(v, cell.number_format)
        rl_font = fonts.font(font_def.name, bold, italic)
        if fonts.needs_cjk(text):
            rl_font = fonts.cjk()
        elif not fonts.has_glyphs(rl_font, text):
            rl_font = fonts.symbol()
        color = resolve_color(font_def.color, self.theme, "000000") or "000000"
        is_num = isinstance(v, (int, float, dt.date, dt.time)) and cell.number_format != "@"
        horiz = al.horizontal or ("right" if is_num else "left")
        if horiz == "general":
            horiz = "right" if is_num else "left"
        indent = (al.indent or 0) * 3 * 7 * 0.75  # 每階 3 個字元寬

        pad = PAD_X * self.f
        bx, bw = self.X(box["x"]), box["w"] * self.f
        top_y = self.Y(box["y"])
        bh = box["h"] * self.fy

        lines = []
        avail = bw - 2 * pad - indent * self.f
        for para in text.split("\n"):
            if al.wrap_text:
                lines.extend(_wrap(para, rl_font, size, avail))
            else:
                lines.append(para)
        lh = size * LINE_H
        asc, desc = fonts.ascent_descent(rl_font, size)
        block = lh * len(lines)
        vert = al.vertical or "bottom"
        pad_y = 1.2 * self.fy
        if vert == "top":
            first_top = top_y - pad_y
        elif vert in ("center", "justify", "distributed"):
            first_top = top_y - (bh - block) / 2
        else:
            first_top = top_y - bh + block + pad_y
        # 每行基線：行框頂 - (行高 - 下降)
        clip_x, clip_w = bx, bw
        if not al.wrap_text and len(lines) == 1:
            tw = fonts.width(lines[0], rl_font, size)
            if tw > avail:
                clip_x, clip_w = self._overflow(box, horiz, tw + 2 * pad)
        cv = self.cv
        cv.saveState()
        p = cv.beginPath()
        p.rect(clip_x, top_y - bh, clip_w, bh)
        cv.clipPath(p, stroke=0, fill=0)
        cv.setFillColorRGB(*hex_to_rgb(color))
        for i, line in enumerate(lines):
            base = first_top - i * lh - (lh - desc) + (lh - asc - desc) / 2 - TEXT_DY * self.fy
            tw = fonts.width(line, rl_font, size)
            if horiz in ("center", "centerContinuous"):
                x = bx + (bw - tw) / 2
            elif horiz == "right":
                x = bx + bw - pad - tw - indent * self.f
            else:
                x = bx + pad + indent * self.f
            cv.setFont(rl_font, size)
            cv.drawString(x, base, line)
            if font_def.u:
                cv.setLineWidth(size * 0.06)
                cv.setStrokeColorRGB(*hex_to_rgb(color))
                cv.line(x, base - size * 0.12, x + tw, base - size * 0.12)
        cv.restoreState()

    def _overflow(self, box, horiz, need):
        """未換行文字可溢出到右側（或左側）空白儲存格。"""
        s = self.s
        r = box["r"]
        x, w = box["x"], box["w"]
        if horiz in ("left", "general", None):
            c = box["c2"] + 1
            while w * self.f < need and c <= s.c2:
                if self._occupied(r, c):
                    break
                w += s.col_w[c]
                c += 1
            return self.X(x), w * self.f
        if horiz == "right":
            c = box["c"] - 1
            while w * self.f < need and c >= s.c1:
                if self._occupied(r, c):
                    break
                x -= s.col_w[c]
                w += s.col_w[c]
                c -= 1
            return self.X(x), w * self.f
        return self.X(x) - need, w * self.f + 2 * need

    def _occupied(self, r, c):
        s = self.s
        if (r, c) in s.covered or (r, c) in s.merged:
            return True
        v = self.b.value(s.name, s.ws.cell(row=r, column=c).coordinate)
        return v not in (None, "")

    def _checkbox(self, box, checked, size):
        """Excel 儲存格核取方塊：圓角方框，勾選時為深色底白勾。"""
        cv = self.cv
        side = size
        bx, bw = self.X(box["x"]), box["w"] * self.f
        top = self.Y(box["y"])
        bh = box["h"] * self.fy
        x = bx + (bw - side) / 2
        al = box["cell"].alignment
        vert = al.vertical or "bottom"
        if vert == "top":
            y = top - 1.2 * self.fy - side
        elif vert in ("center", "justify", "distributed"):
            y = top - (bh + side) / 2
        else:
            y = top - bh + 1.2 * self.fy
        r = side * 0.18
        cv.saveState()
        dark = (0.2, 0.2, 0.2)
        if checked:
            cv.setFillColorRGB(*dark)
            cv.roundRect(x, y, side, side, r, stroke=0, fill=1)
            cv.setStrokeColorRGB(1, 1, 1)
            cv.setLineWidth(side * 0.12)
            cv.setLineCap(1)
            cv.setLineJoin(1)
            p = cv.beginPath()
            p.moveTo(x + side * 0.22, y + side * 0.50)
            p.lineTo(x + side * 0.42, y + side * 0.28)
            p.lineTo(x + side * 0.78, y + side * 0.74)
            cv.drawPath(p, stroke=1, fill=0)
        else:
            cv.setStrokeColorRGB(*dark)
            cv.setLineWidth(side * 0.09)
            inset = side * 0.045
            cv.roundRect(x + inset, y + inset, side - 2 * inset, side - 2 * inset, r, stroke=1, fill=0)
        cv.restoreState()

    def _borders(self, box):
        s = self.s
        r1, c1, r2, c2 = box["r"], box["c"], box["r2"], box["c2"]
        ws = s.ws
        x, y, w, h = box["x"], box["y"], box["w"], box["h"]
        # 合併範圍：外框取四邊各儲存格的設定
        edges = []
        bottom_row = box["rows"][-1]
        for c in range(c1, min(c2, s.c2) + 1):
            cx, cw = s.col_x[c], s.col_w[c]
            edges.append(("top", ws.cell(row=r1, column=c).border.top, cx, y, cx + cw, y))
            edges.append(("bottom", ws.cell(row=r2, column=c).border.bottom, cx, y + h, cx + cw, y + h))
        for r in box["rows"]:
            ry, rh = self.row_y[r], s.row_h[r]
            edges.append(("left", ws.cell(row=r, column=c1).border.left, x, ry, x, ry + rh))
            edges.append(("right", ws.cell(row=r, column=c2).border.right, x + w, ry, x + w, ry + rh))
        # 鄰格的框線也要畫（Excel 會合併相鄰儲存格設定）
        if c2 < s.c2:
            for r in box["rows"]:
                ry, rh = self.row_y[r], s.row_h[r]
                edges.append(("right", ws.cell(row=r, column=c2 + 1).border.left, x + w, ry, x + w, ry + rh))
        nxt = self._next_row(bottom_row)
        if r2 == bottom_row and nxt is not None:
            for c in range(c1, min(c2, s.c2) + 1):
                cx, cw = s.col_x[c], s.col_w[c]
                edges.append(("bottom", ws.cell(row=nxt, column=c).border.top, cx, y + h, cx + cw, y + h))
        for _, side, xa, ya, xb, yb in edges:
            if side is None or not side.style:
                continue
            self._line(side, xa, ya, xb, yb)

    def _next_row(self, r):
        rows = self.rows
        i = rows.index(r)
        return rows[i + 1] if i + 1 < len(rows) else None

    def _line(self, side, xa, ya, xb, yb):
        cv = self.cv
        style = side.style
        lw = BORDER_W.get(style, 0.48)
        rgb = resolve_color(side.color, self.theme, "000000") or "000000"
        cv.saveState()
        cv.setStrokeColorRGB(*hex_to_rgb(rgb))
        cv.setLineWidth(lw)
        cv.setLineCap(2)
        if style in DASH:
            cv.setDash(DASH[style])
        X1, Y1, X2, Y2 = self.X(xa), self.Y(ya), self.X(xb), self.Y(yb)
        if style == "double":
            off = 0.72
            if Y1 == Y2:
                cv.line(X1, Y1 + off, X2, Y2 + off)
                cv.line(X1, Y1 - off, X2, Y2 - off)
            else:
                cv.line(X1 - off, Y1, X2 - off, Y2)
                cv.line(X1 + off, Y1, X2 + off, Y2)
        else:
            cv.line(X1, Y1, X2, Y2)
        cv.restoreState()

    def _draw_charts(self, rows):
        if OPTIMIZE_CHARTS:
            for chart, x, top, w, h in self._optimized_charts(rows):
                draw_chart(self.cv, chart, self.X(x), self.Y(top) - h * self.fy, w * self.f, h * self.fy,
                           self.fs, self._chart_values(chart), optimize=True)
            return
        s = self.s
        for (fx, frow, foff), (tx, trow, toff), chart in s.charts:
            if frow not in self.row_y:
                # 錨點落在隱藏列（高度 0）時，Excel 會從下一個可見列開始畫
                nxt = next((r for r in rows if r > frow), None)
                if nxt is None or s.row_h.get(frow, 1) != 0 or any(
                        s.row_h.get(k, 0) for k in range(frow, nxt)):
                    continue
                frow, foff = nxt, 0.0
            y1 = self.row_y[frow] + foff
            y2 = (self.row_y[trow] + toff) if trow in self.row_y else (self.row_y[rows[-1]] + s.row_h[rows[-1]])
            x1, y1p = self.X(fx), self.Y(y1)
            w, h = (tx - fx) * self.f, (y2 - y1) * self.fy
            vals = self._chart_values(chart)
            draw_chart(self.cv, chart, x1, y1p - h, w, h, self.fs, vals)

    def _optimized_charts(self, rows):
        """每張圖對齊最近一個統計表（含「Qty」的表頭列）的上緣；同一工作表的圖寬、高、左緣統一，
        高度不超過下一個統計表或本頁底部。回傳本頁要畫的 (chart, x, 頁內 top, w, h)（工作表 pt）。"""
        s = self.s
        if not hasattr(self, "_chart_plan"):
            import statistics

            abs_y, y = {}, 0.0
            for r in s.rows:
                abs_y[r] = y
                y += s.row_h[r]
            page_of = {}
            for i, pg in enumerate(self.pages()):
                for r in pg:
                    page_of[r] = (i, pg)
            headers = [r for r in s.rows if any(
                isinstance(c.value, str) and c.value.strip() == "Qty"
                for c in s.ws[r][s.c1 - 1:s.c2])]
            items = []
            for (fx, frow, foff), (tx, trow, toff), chart in s.charts:
                fr = frow if frow in abs_y else next((r for r in s.rows if r > frow), None)
                tr = trow if trow in abs_y else next((r for r in s.rows if r > trow), None)
                if fr is None or tr is None:
                    continue
                top = abs_y[fr] + (foff if fr == frow else 0)
                items.append((chart, fx, tx - fx, top, abs_y[tr] + (toff if tr == trow else 0) - top))
            plan = []
            if items:
                tw = statistics.median(i[2] for i in items)
                th = statistics.median(i[4] for i in items)
                tx = statistics.median(i[1] for i in items)
                for chart, fx, w, top, h in items:
                    cands = [r for r in headers if abs(abs_y[r] - top) < max(h, 40)]
                    if not cands:
                        plan.append((chart, fx, None, top, w, h))
                        continue
                    hr = min(cands, key=lambda r: abs(abs_y[r] - top))
                    _, pg = page_of[hr]
                    nxt = [r for r in headers if r > hr and r in pg]
                    limit = (abs_y[nxt[0]] - 8) if nxt else (abs_y[pg[-1]] + s.row_h[pg[-1]] - 4)
                    plan.append((chart, tx, hr, abs_y[hr], tw, max(40.0, min(th, limit - abs_y[hr]))))
            self._chart_plan = (plan, abs_y)
        plan, abs_y = self._chart_plan
        base = abs_y[rows[0]]
        out = []
        for chart, x, hr, top, w, h in plan:
            anchor = hr if hr is not None else next((r for r in s.rows if abs_y[r] >= top), None)
            if anchor in self.rowset:
                out.append((chart, x, top - base, w, h))
        return out

    def _chart_values(self, chart):
        out = []
        for ref in chart.get("val_refs", []):
            out.append(self._range_values(ref))
        cats = [self._range_values(r) for r in chart.get("cat_refs", [])]
        return {"vals": out, "cats": cats}

    def _range_values(self, ref):
        if "!" in ref:
            sheet, rng = ref.rsplit("!", 1)
            sheet = sheet.strip("'")
        else:
            sheet, rng = self.s.name, ref
        c1, r1, c2, r2 = range_boundaries(rng.replace("$", ""))
        vals = []
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                vals.append(self.b.value(sheet, f"{get_column_letter(c)}{r}"))
        return vals


WRAP_K = 1.035  # Excel 以螢幕字寬（GDI hinting 後較寬）決定換行位置


def _wrap(text, rl_font, size, avail):
    if not text:
        return [""]
    avail = avail / WRAP_K
    words = re.split(r"(\s+)", text)
    lines, cur = [], ""
    for wd in words:
        trial = cur + wd
        if cur and fonts.width(trial.rstrip(), rl_font, size) > avail:
            lines.append(cur.rstrip())
            cur = wd.lstrip()
            # 單字本身過長：逐字切
            while fonts.width(cur, rl_font, size) > avail and len(cur) > 1:
                i = len(cur)
                while i > 1 and fonts.width(cur[:i], rl_font, size) > avail:
                    i -= 1
                lines.append(cur[:i])
                cur = cur[i:]
        else:
            cur = trial
    lines.append(cur.rstrip())
    return lines


def render_sheets(book, sheet_names, canvas, page_size):
    """把多個工作表依序畫到 canvas，回傳頁數。"""
    pw, ph = page_size
    n = 0
    for name in sheet_names:
        sh = book.sheet(name)
        painter = SheetPainter(book, sh, canvas, pw, ph)
        for rows in painter.pages():
            painter.draw_page(rows)
            canvas.showPage()
            n += 1
    return n
