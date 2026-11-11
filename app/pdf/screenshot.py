"""Daily Inspection Screenshot（原 Word 範本）的重現：封面、目錄（含頁碼）、
各章節標題與截圖。座標與間距取自原始 Word 輸出的 PDF。

截圖一律縮放到內容寬度 487.3pt（原範本每張都是 487.3 × 274.1）。
章節第一層一定換頁；第二層依範本設定（page_break）決定是否換頁，放不下時自動換頁。
"""

from reportlab.lib.utils import ImageReader

from . import fonts
from .cover import DOT_EM, GRAY_TEXT, HEAD_COLOR, PAGE_H, PAGE_W, dot_leader, text

H2_COLOR = (0.118, 0.369, 0.624)
CONTENT_X = 54.0
CONTENT_W = 487.3
BOTTOM = PAGE_H - 72.0
IMG_GAP = 7.73


def F(bold=False):
    return fonts.font("Calibri", bold=bold)


def jpeg_reader(path, max_px=1500):
    """截圖以 JPEG 嵌入（約 220 dpi、品質 82），避免 PNG 原檔讓 PDF 過大。"""
    import io

    from PIL import Image

    im = Image.open(path)
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, "white")
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    buf.seek(0)
    return ImageReader(buf)


class ScreenshotDoc:
    def __init__(self, sections, logo_path):
        """sections: [{'code','title','level','page_break','images':[path,...]}]"""
        self.sections = sections
        self.logo = logo_path
        self.toc_pages = {}

    # ---------------------------------------------------------- 版面
    def _layout_body(self, first_page_no):
        pages = []  # list of list of ops: ("h1"/"h2", code, title, top) / ("img", path, top, h)
        cur = None
        y = None
        last = None  # 上一個元素類型
        page_no = first_page_no - 1

        def new_page():
            nonlocal cur, y, last, page_no
            cur = []
            pages.append(cur)
            page_no += 1
            y = None
            last = None

        for sec in self.sections:
            lvl = sec["level"]
            if lvl == 1:
                new_page()
                cur.append(("h1", sec["code"], sec["title"], 77.36))
                last = "h1"
                y = 77.36
            else:
                if cur is None or sec.get("page_break"):
                    new_page()
                if last is None:
                    top = 76.97
                elif last == "h1":
                    top = 109.97
                elif last == "img":
                    top = y + 11.1
                else:
                    top = y + 32.61
                # 標題與第一張圖需放在同一頁
                first_h = self._img_h(sec["images"][0]) if sec["images"] else 0
                if last is not None and top + 25.95 + first_h > BOTTOM:
                    new_page()
                    top = 76.97
                cur.append(("h2", sec["code"], sec["title"], top))
                last = "h2"
                y = top
            self.toc_pages[sec["code"]] = page_no
            for path in sec["images"]:
                h = self._img_h(path)
                if last == "h1":
                    top = y + 29.26
                elif last == "h2":
                    top = y + 25.95
                elif last == "img":
                    top = y + IMG_GAP
                else:
                    top = 73.66
                if top + h > BOTTOM and last == "img":
                    new_page()
                    top = 73.66
                cur.append(("img", path, top, h))
                y = top + h
                last = "img"
        return pages

    @staticmethod
    def _img_h(path):
        iw, ih = ImageReader(path).getSize()
        return CONTENT_W * ih / iw

    def _toc_entries(self):
        return [(s["level"], s["code"], s["title"]) for s in self.sections]

    def _layout_toc(self):
        """回傳 [[(level, code, title, top), ...], ...]（每頁）。"""
        pages, cur = [], []
        prev = None
        top = 107.57
        for lvl, code, title in self._toc_entries():
            if prev is not None:
                step = {(1, 1): 23.04, (1, 2): 22.65, (2, 2): 14.64, (2, 1): 21.1}[(prev, lvl)]
                top += step
            size = 14.04 if lvl == 1 else 12
            if top + size > 762.0:
                pages.append(cur)
                cur = []
                top = 76.1
            cur.append((lvl, code, title, top))
            prev = lvl
        pages.append(cur)
        return pages

    # ---------------------------------------------------------- 繪製
    def _header(self, cv, title_page=False):
        if title_page:
            cv.drawImage(self.logo, 384.75, PAGE_H - 25.65 - 45.05, 149.32, 45.05, mask="auto")
            return
        cv.drawImage(self.logo, 383.25, PAGE_H - 24.85 - 45.05, 149.32, 45.05, mask="auto")
        text(cv, "Project Name: CFXD SLA Project", 54.0, 37.53, 9.96, color=GRAY_TEXT)
        text(cv, "Document Title: CFXD SLA Daily Report_Screenshot", 54.0, 49.77, 9.96, color=GRAY_TEXT)

    def _draw_toc_line(self, cv, lvl, code, title, top):
        if lvl == 1:
            size, bold = 14.04, True
            label = f"{code} {title.upper()}"
            num_right, anchor = 504.14, 494.86 - DOT_EM[True] * 14.04
        else:
            size, bold = 12, False
            label = f"{code} {title}"
            num_right, anchor = 504.9, 54.02
        num = str(self.toc_pages.get(code, ""))
        end = 54.0
        if "​" in title:
            # 原範本此兩行編號以 Times New Roman 呈現
            end += text(cv, code, 54.0, top - 0.41, size, font=fonts.font("Times New Roman"))
            end += text(cv, " " + title.replace("​", "").strip(), end, top, size, bold)
        else:
            end += text(cv, label.replace("​", ""), 54.0, top, size, bold)
        nw = fonts.width(num, F(bold), size)
        dot_leader(cv, end, num_right - nw, top, size, bold, anchor)
        text(cv, num, num_right - nw, top, size, bold)
        if num:
            from .cover import link

            link(cv, f"ss_{code}", 54.0, top, num_right, size)

    def render(self, cv):
        toc = self._layout_toc()
        body_first = 1 + len(toc) + 1
        body = self._layout_body(body_first)  # 先算頁碼
        total = 1 + len(toc) + len(body)

        # 封面（PDF 書籤：Screenshot → Contents → 各章節）
        cv.bookmarkPage("ss_cover")
        cv.addOutlineEntry("Screenshot", "ss_cover", level=0)
        self._header(cv, title_page=True)
        title = "CFXD SLA Daily Report _Screenshot"
        w = fonts.width(title, F(), 27.96)
        text(cv, title, (PAGE_W - w) / 2, 382.76, 27.96)
        cv.showPage()

        for i, entries in enumerate(toc):
            self._header(cv)
            if i == 0:
                cv.bookmarkPage("ss_toc")
                cv.addOutlineEntry("Contents", "ss_toc", level=1)
                text(cv, "Contents", 54.0, 77.93, 15.96, True)
            for e in entries:
                self._draw_toc_line(cv, *e)
            cv.showPage()

        prev_level = 0
        for ops in body:
            self._header(cv)
            for op in ops:
                if op[0] in ("h1", "h2"):
                    _, code, title, top = op
                    cv.bookmarkPage(f"ss_{code}", fit="XYZ", left=0, top=PAGE_H - top + 30)
                    lvl = min(1 if op[0] == "h1" else 2, prev_level + 1)
                    cv.addOutlineEntry(f"{code} {title.replace(chr(0x200b), '').strip()}", f"ss_{code}", level=lvl)
                    prev_level = lvl
                if op[0] == "h1":
                    _, code, title, top = op
                    end = 54.0 + text(cv, code, 54.0, top, 18, True, HEAD_COLOR)
                    text(cv, title, end + 5.05, top, 18, True, HEAD_COLOR)
                elif op[0] == "h2":
                    _, code, title, top = op
                    end = 54.0 + text(cv, code, 54.0, top, 15.96, False, H2_COLOR)
                    text(cv, title.replace("​", "").strip(), end + 4.53, top, 15.96, False, H2_COLOR)
                else:
                    _, path, top, h = op
                    cv.drawImage(jpeg_reader(path), CONTENT_X, PAGE_H - top - h, CONTENT_W, h)
            cv.showPage()
        return total
