"""字型註冊。

優先使用 FONT_DIR（預設 instance/fonts）中的 Microsoft 原版字型（從 Windows 的
C:\\Windows\\Fonts 複製 calibri*.ttf、arial*.ttf、times*.ttf、kaiu.ttf），
找不到時改用字寬相同的開源替代字型（Carlito、Liberation），版面位置不變。
"""

import os

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

SYSTEM_DIRS = [
    "/usr/share/fonts/truetype/crosextra",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/liberation2",
    "/usr/share/fonts/truetype/wqy",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/arphic",
    "/usr/share/fonts/opentype/noto",
    "C:/Windows/Fonts",
]

# 家族 -> {樣式: [候選檔名...]}；先找原版，再找替代字型
FAMILIES = {
    "Calibri": {
        "": ["calibri.ttf", "Carlito-Regular.ttf"],
        "B": ["calibrib.ttf", "Carlito-Bold.ttf"],
        "I": ["calibrii.ttf", "Carlito-Italic.ttf"],
        "BI": ["calibriz.ttf", "Carlito-BoldItalic.ttf"],
    },
    "Arial": {
        "": ["arial.ttf", "LiberationSans-Regular.ttf"],
        "B": ["arialbd.ttf", "LiberationSans-Bold.ttf"],
        "I": ["ariali.ttf", "LiberationSans-Italic.ttf"],
        "BI": ["arialbi.ttf", "LiberationSans-BoldItalic.ttf"],
    },
    "Times New Roman": {
        "": ["times.ttf", "LiberationSerif-Regular.ttf"],
        "B": ["timesbd.ttf", "LiberationSerif-Bold.ttf"],
        "I": ["timesi.ttf", "LiberationSerif-Italic.ttf"],
        "BI": ["timesbi.ttf", "LiberationSerif-BoldItalic.ttf"],
    },
    "Symbol": {
        "": ["seguisym.ttf", "DejaVuSans.ttf"],
    },
    "CJK": {
        "": ["kaiu.ttf", "msjh.ttc", "mingliu.ttc", "wqy-zenhei.ttc", "NotoSansCJK-Regular.ttc"],
    },
}
ALIASES = {
    "calibri": "Calibri",
    "calibri light": "Calibri",
    "arial": "Arial",
    "times new roman": "Times New Roman",
    "segoe ui symbol": "Symbol",
    "新細明體": "CJK",
    "細明體": "CJK",
    "microsoft jhenghei": "CJK",
    "微軟正黑體": "CJK",
    "標楷體": "CJK",
    "dfkai-sb": "CJK",
}

_registered = {}
_font_dirs = []


def configure(font_dir=None):
    _font_dirs.clear()
    if font_dir:
        _font_dirs.append(font_dir)
    _font_dirs.extend(SYSTEM_DIRS)
    _registered.clear()


def _find(candidates):
    for name in candidates:
        for d in _font_dirs or SYSTEM_DIRS:
            for fname in (name, name.upper(), name.capitalize()):
                p = os.path.join(d, fname)
                if os.path.exists(p):
                    return p
    return None


def font(family, bold=False, italic=False):
    """回傳 reportlab 字型名稱（必要時註冊）。"""
    fam = family if family in FAMILIES else ALIASES.get((family or "Calibri").lower(), "Calibri")
    style = ("B" if bold else "") + ("I" if italic else "")
    key = (fam, style)
    if key in _registered:
        return _registered[key]
    styles = FAMILIES[fam]
    path = _find(styles.get(style) or styles[""])
    if path is None and style:
        path = _find(styles[""])
    if path is None:
        if fam != "Calibri":
            return font("Calibri", bold, italic)
        raise RuntimeError("找不到 Calibri 或 Carlito 字型，請安裝 fonts-crosextra-carlito")
    rl_name = f"{fam.replace(' ', '')}-{style or 'R'}"
    kwargs = {"subfontIndex": 0} if path.lower().endswith(".ttc") else {}
    pdfmetrics.registerFont(TTFont(rl_name, path, **kwargs))
    _registered[key] = rl_name
    return rl_name


def cjk():
    return font("CJK")


def symbol():
    return font("Symbol")


def has_glyphs(rl_font, text):
    face = pdfmetrics.getFont(rl_font).face
    cmap = getattr(face, "charToGlyph", None)
    if not cmap:
        return True
    return all(ord(ch) in cmap or ch in "\n\r\t" for ch in text)


def width(text, rl_font, size):
    return pdfmetrics.stringWidth(text, rl_font, size)


def ascent_descent(rl_font, size):
    face = pdfmetrics.getFont(rl_font).face
    return face.ascent / 1000 * size, -face.descent / 1000 * size


def needs_cjk(text):
    return any(ord(ch) > 0x2E80 for ch in text)
