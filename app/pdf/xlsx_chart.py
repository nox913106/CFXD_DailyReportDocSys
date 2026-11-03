"""Excel 圓餅圖（DrawingML chart）的重現：標題、斜線/點狀圖樣填色、資料標籤
（含指引框）、圖例，位置與樣式取自圖表 XML。"""

import math
from xml.etree import ElementTree as ET

from . import fonts

NS = {
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
EMU_PT = 12700


def _q(el, path):
    return el.find(path, NS) if el is not None else None


def _color(el, theme, default=None):
    """解析 a:solidFill / a:fgClr 之類容器內的顏色（含 lumMod/lumOff/tint）。"""
    if el is None:
        return default
    for tag in ("srgbClr", "schemeClr", "sysClr", "prstClr"):
        c = el.find(f"a:{tag}", NS)
        if c is None:
            continue
        if tag == "srgbClr":
            rgb = c.get("val")
        elif tag == "schemeClr":
            rgb = theme.by_name(c.get("val"))
        elif tag == "sysClr":
            rgb = c.get("lastClr") or ("000000" if c.get("val") == "windowText" else "FFFFFF")
        else:
            rgb = {"black": "000000", "white": "FFFFFF"}.get(c.get("val"), "000000")
        r, g, b = (int(rgb[i : i + 2], 16) / 255 for i in (0, 2, 4))
        mod = c.find("a:lumMod", NS)
        off = c.find("a:lumOff", NS)
        if mod is not None or off is not None:
            h, l, s = _rgb_to_hls(r, g, b)
            l = l * (int(mod.get("val")) / 100000 if mod is not None else 1) + (
                int(off.get("val")) / 100000 if off is not None else 0)
            r, g, b = _hls_to_rgb(h, min(1, max(0, l)), s)
        return (r, g, b)
    return default


def _rgb_to_hls(r, g, b):
    import colorsys

    return colorsys.rgb_to_hls(r, g, b)


def _hls_to_rgb(h, l, s):
    import colorsys

    return colorsys.hls_to_rgb(h, l, s)


def _fill(sppr, theme):
    """回傳 {'kind': 'solid'|'patt'|'none', 'fg', 'bg', 'prst'}。"""
    if sppr is None:
        return None
    if sppr.find("a:noFill", NS) is not None:
        return {"kind": "none"}
    sf = sppr.find("a:solidFill", NS)
    if sf is not None:
        return {"kind": "solid", "fg": _color(sf, theme)}
    pf = sppr.find("a:pattFill", NS)
    if pf is not None:
        return {
            "kind": "patt",
            "prst": pf.get("prst"),
            "fg": _color(pf.find("a:fgClr", NS), theme, (0, 0, 0)),
            "bg": _color(pf.find("a:bgClr", NS), theme, (1, 1, 1)),
        }
    return None


def _line(sppr, theme):
    if sppr is None:
        return None
    ln = sppr.find("a:ln", NS)
    if ln is None or ln.find("a:noFill", NS) is not None:
        return None
    color = _color(ln.find("a:solidFill", NS), theme)
    if color is None:
        return None
    w = int(ln.get("w", "9525")) / EMU_PT
    return {"w": w, "color": color}


def _text_style(txpr, theme, default_size=10):
    rpr = txpr.find(".//a:defRPr", NS) if txpr is not None else None
    if rpr is None:
        return {"size": default_size, "bold": False, "color": (0.35, 0.35, 0.35), "caps": False, "spc": 0}
    return {
        "size": int(rpr.get("sz", default_size * 100)) / 100,
        "bold": rpr.get("b") == "1",
        "color": _color(rpr.find("a:solidFill", NS), theme, (0.35, 0.35, 0.35)),
        "caps": rpr.get("cap") == "all",
        "spc": int(rpr.get("spc", "0")) / 100,
    }


def _layout(el):
    ml = _q(el, "c:layout/c:manualLayout")
    if ml is None:
        return None
    return {k: float(ml.find(f"c:{k}", NS).get("val")) for k in ("x", "y", "w", "h") if ml.find(f"c:{k}", NS) is not None}


def load_chart(xml_bytes, theme):
    root = ET.fromstring(xml_bytes)
    chart = root.find("c:chart", NS)
    pie = chart.find(".//c:pieChart", NS)
    ser = pie.find("c:ser", NS) if pie is not None else None
    out = {
        "kind": "pie" if pie is not None else "other",
        "space_fill": _fill(root.find("c:spPr", NS), theme),
        "space_line": _line(root.find("c:spPr", NS), theme),
        "plot": _layout(chart.find("c:plotArea", NS)),
        "first_angle": float(_q(pie, "c:firstSliceAng").get("val", 0)) if _q(pie, "c:firstSliceAng") is not None else 0,
        "val_refs": [],
        "cat_refs": [],
        "points": {},
        "labels": {},
    }
    title = chart.find("c:title", NS)
    deleted = _q(chart, "c:autoTitleDeleted")
    if title is not None and not (deleted is not None and deleted.get("val") == "1"):
        rich = title.findall(".//c:tx//a:t", NS)
        out["title"] = {
            "text": "".join(t.text or "" for t in rich) if rich else None,
            "style": _text_style(title.find("c:txPr", NS) or title.find(".//c:rich", NS), theme, 14),
            "layout": _layout(title),
        }
    if ser is not None:
        tx = ser.find("c:tx//c:v", NS)
        out["series_name"] = tx.text if tx is not None else ""
        f = ser.find("c:val//c:f", NS)
        if f is not None:
            out["val_refs"].append(f.text)
        f = ser.find("c:cat//c:f", NS)
        if f is not None:
            out["cat_refs"].append(f.text)
        out["val_cache"] = [float(p.find("c:v", NS).text) for p in ser.findall("c:val//c:pt", NS)]
        out["cat_cache"] = [p.find("c:v", NS).text for p in ser.findall("c:cat//c:pt", NS)]
        fmt = ser.find("c:val//c:formatCode", NS)
        out["val_format"] = fmt.text if fmt is not None else "General"
        default_sp = ser.find("c:spPr", NS)
        for dpt in ser.findall("c:dPt", NS):
            idx = int(dpt.find("c:idx", NS).get("val"))
            sp = dpt.find("c:spPr", NS)
            out["points"][idx] = {"fill": _fill(sp, theme), "line": _line(sp, theme)}
        out["default_point"] = {"fill": _fill(default_sp, theme), "line": _line(default_sp, theme)}
        dl = ser.find("c:dLbls", NS)
        if dl is not None:
            sp = dl.find("c:spPr", NS)
            out["label_box"] = {"fill": _fill(sp, theme), "line": _line(sp, theme)}
            out["label_style"] = _text_style(dl.find("c:txPr", NS), theme, 9)
            def flags(el, base=None):
                base = dict(base or {"val": False, "cat": False, "pct": False, "sep": None})
                for key, tag in (("val", "showVal"), ("cat", "showCatName"), ("pct", "showPercent")):
                    f = el.find(f"c:{tag}", NS)
                    if f is not None:
                        base[key] = f.get("val") == "1"
                sep = el.find("c:separator", NS)
                if sep is not None:
                    base["sep"] = sep.text
                return base

            nf = dl.find("c:numFmt", NS)
            out["label_numfmt"] = nf.get("formatCode") if nf is not None else None
            out["label_flags"] = flags(dl)
            for lbl in dl.findall("c:dLbl", NS):
                idx = int(lbl.find("c:idx", NS).get("val"))
                lay = _layout(lbl) or {}
                deleted = lbl.find("c:delete", NS)
                out["labels"][idx] = {"dx": lay.get("x", 0), "dy": lay.get("y", 0),
                                      "deleted": deleted is not None and deleted.get("val") == "1",
                                      "flags": flags(lbl, out["label_flags"])}
    leg = chart.find("c:legend", NS)
    if leg is not None:
        out["legend"] = {
            "pos": _q(leg, "c:legendPos").get("val") if _q(leg, "c:legendPos") is not None else "r",
            "layout": _layout(leg),
            "line": _line(leg.find("c:spPr", NS), theme),
            "style": _text_style(leg.find("c:txPr", NS), theme, 9),
        }
    return out


# ---------------------------------------------------------------- 繪製


def _set_fill(cv, rgb):
    cv.setFillColorRGB(*rgb)


def _pattern(cv, path_fn, fill, bbox, fs):
    """以裁切路徑畫圖樣填色。path_fn(cv) 需回傳 reportlab path。"""
    kind = fill["kind"] if fill else "solid"
    x0, y0, x1, y1 = bbox
    cv.saveState()
    p = path_fn()
    if kind == "none":
        cv.restoreState()
        return
    if kind == "solid":
        _set_fill(cv, fill["fg"])
        cv.drawPath(p, stroke=0, fill=1)
        cv.restoreState()
        return
    cv.clipPath(p, stroke=0, fill=0)
    _set_fill(cv, fill["bg"])
    cv.rect(x0, y0, x1 - x0, y1 - y0, stroke=0, fill=1)
    prst = fill["prst"]
    cv.setStrokeColorRGB(*fill["fg"])
    if prst in ("ltUpDiag", "dkUpDiag", "wdUpDiag", "upDiag", "ltDnDiag", "dkDnDiag", "wdDnDiag", "dnDiag"):
        # Excel 以印表機解析度光柵化圖樣，間距與縮放比無關
        step = {"ltUpDiag": 1.45, "ltDnDiag": 1.45, "dkUpDiag": 1.45, "dkDnDiag": 1.45}.get(prst, 2.2)
        cv.setLineWidth({"dkUpDiag": 0.7, "dkDnDiag": 0.7}.get(prst, 0.36))
        up = "Up" in prst or prst == "upDiag"
        span = (x1 - x0) + (y1 - y0)
        t = -span
        while t < span:
            if up:
                cv.line(x0 + t, y0, x0 + t + (y1 - y0), y1)
            else:
                cv.line(x0 + t, y1, x0 + t + (y1 - y0), y0)
            t += step
    else:  # pct 系列：點狀
        density = int(prst[3:]) / 100 if prst.startswith("pct") and prst[3:].isdigit() else 0.5
        cell = 1.1
        dot = cell * math.sqrt(density) * 0.7
        _set_fill(cv, fill["fg"])
        y = y0
        row = 0
        while y < y1:
            x = x0 + (cell / 2 if row % 2 else 0)
            while x < x1:
                cv.rect(x, y, dot, dot, stroke=0, fill=1)
                x += cell
            y += cell / 1.0
            row += 1
    cv.restoreState()


def _round_half_up(x, dec):
    from decimal import ROUND_HALF_UP, Decimal

    q = Decimal(1).scaleb(-dec)
    return str(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def _fmt_val(v, code):
    if v is None:
        return ""
    if "%" in (code or ""):
        dec = 0
        if "." in code:
            dec = len(code.split(".")[1].split("%")[0])
        return _round_half_up(v * 100, dec) + "%"
    return ("%g" % v) if isinstance(v, float) else str(v)


def draw_chart(cv, ch, x, y, w, h, fs, values):
    """x,y 為圖表左下角（reportlab 座標）。"""
    if ch["kind"] != "pie":
        return
    vals = values["vals"][0] if values["vals"] else ch.get("val_cache", [])
    vals = [float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0 for v in vals]
    cats = values["cats"][0] if values["cats"] else ch.get("cat_cache", [])
    cats = [str(c) if c is not None else "" for c in cats]
    top = y + h

    # 背景與外框
    cv.saveState()
    if ch["space_fill"] and ch["space_fill"]["kind"] == "solid":
        _set_fill(cv, ch["space_fill"]["fg"])
        cv.rect(x, y, w, h, stroke=0, fill=1)
    if ch["space_line"]:
        cv.setStrokeColorRGB(*ch["space_line"]["color"])
        cv.setLineWidth(ch["space_line"]["w"] * fs)
        cv.rect(x, y, w, h, stroke=1, fill=0)
    cv.restoreState()

    font = fonts.font("Calibri")
    font_b = fonts.font("Calibri", bold=True)

    # 標題
    if "title" in ch:
        t = ch["title"]
        st = t["style"]
        text = t["text"] or ch.get("series_name", "")
        if st["caps"]:
            text = text.upper()
        size = st["size"] * fs
        fname = font_b if st["bold"] else font
        spc = st["spc"] * fs
        tw = fonts.width(text, fname, size) + spc * max(len(text) - 1, 0)
        if t.get("layout") and "x" in t["layout"]:
            tx = x + t["layout"]["x"] * w
            ty = top - t["layout"]["y"] * h - size
        else:
            tx = x + (w - tw) / 2
            ty = top - 7.2 * fs - size * 0.9
        cv.saveState()
        cv.setFillColorRGB(*(st["color"] or (0, 0, 0)))
        to = cv.beginText(tx, ty)
        to.setFont(fname, size)
        to.setCharSpace(spc)
        to.textOut(text)
        cv.drawText(to)
        cv.restoreState()

    # 圓餅
    lay = ch.get("plot") or {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.75}
    px, py, pw, ph = x + lay["x"] * w, top - lay["y"] * h, lay["w"] * w, lay["h"] * h
    r = min(pw, ph) / 2
    cx, cy = px + pw / 2, py - ph / 2
    total = sum(vals) or 1.0
    ang = ch.get("first_angle", 0)
    slices = []
    for i, v in enumerate(vals):
        sweep = v / total * 360
        slices.append((i, ang, sweep))
        ang += sweep

    def wedge_path(a0, sweep):
        def make():
            p = cv.beginPath()
            if sweep >= 359.999:
                p.circle(cx, cy, r)
                return p
            p.moveTo(cx, cy)
            # reportlab arc：角度自 3 點鐘方向逆時針；Excel 自 12 點鐘順時針
            start = 90 - a0 - sweep
            p.arcTo(cx - r, cy - r, cx + r, cy + r, startAng=start, extent=sweep)
            p.close()
            return p

        return make

    for i, a0, sweep in slices:
        if sweep <= 0:
            continue
        pt = ch["points"].get(i) or ch.get("default_point") or {}
        _pattern(cv, wedge_path(a0, sweep), pt.get("fill") or {"kind": "solid", "fg": (0.27, 0.45, 0.77)},
                 (cx - r, cy - r, cx + r, cy + r), fs)
    for i, a0, sweep in slices:
        pt = ch["points"].get(i) or ch.get("default_point") or {}
        ln = pt.get("line")
        if ln and sweep <= 0 and len([1 for s_ in slices if s_[2] > 0]) >= 1:
            cv.saveState()
            cv.setStrokeColorRGB(*ln["color"])
            cv.setLineWidth(ln["w"] * fs * 0.6)
            a = math.radians(a0)
            cv.line(cx, cy, cx + r * math.sin(a), cy + r * math.cos(a))
            cv.restoreState()
        if ln and sweep > 0:
            cv.saveState()
            cv.setStrokeColorRGB(*ln["color"])
            cv.setLineWidth(ln["w"] * fs * 0.6)
            cv.drawPath(wedge_path(a0, sweep)(), stroke=1, fill=0)
            cv.restoreState()

    # 資料標籤
    if ch.get("label_flags") is not None:
        st = ch["label_style"]
        size = st["size"] * fs
        lh = size * 1.22
        for i, a0, sweep in slices:
            lab = ch["labels"].get(i) or {"dx": 0, "dy": 0, "deleted": False, "flags": ch["label_flags"]}
            fl = lab["flags"]
            if lab.get("deleted") or not (fl["val"] or fl["cat"] or fl["pct"]):
                continue
            parts = []
            if fl["cat"]:
                parts.append(cats[i] if i < len(cats) else "")
            if fl["val"]:
                parts.append(_fmt_val(vals[i], ch.get("label_numfmt") or ch.get("val_format")))
            if fl["pct"]:
                parts.append(_fmt_val(vals[i] / total, ch.get("label_numfmt") or "0%"))
            sep = fl["sep"] if fl["sep"] is not None else "\n"
            lines = sep.join(parts).split("\n")
            bw = max(fonts.width(s, font, size) for s in lines) + 2 * 2.88 * fs
            bh = lh * len(lines) + 2 * 1.44 * fs
            mid = math.radians(a0 + sweep / 2)
            dx, dy = math.sin(mid), math.cos(mid)
            gap = 3.0 * fs
            # Excel bestFit 預設：標籤框底邊落在扇形外緣延伸點上
            lx = cx + dx * (r + gap) + dx * bw / 2
            ly = cy + dy * (r + gap) + bh / 2
            lx += lab["dx"] * w
            ly -= lab["dy"] * h
            bx0, by0 = lx - bw / 2, ly - bh / 2
            # 指引線（wedge callout）：從標籤框指向扇形邊緣
            ex, ey = cx + dx * r, cy + dy * r
            if not (bx0 <= ex <= bx0 + bw and by0 <= ey <= by0 + bh) and math.hypot(lx - ex, ly - ey) > bh:
                cv.saveState()
                cv.setStrokeColorRGB(0, 0, 0)
                cv.setLineWidth(0.5 * fs)
                ax = max(bx0, min(ex, bx0 + bw))
                ay = max(by0, min(ey, by0 + bh))
                half = min(bw, bh) * 0.25
                if ax in (bx0, bx0 + bw):
                    pts = [(ax, ay - half), (ex, ey), (ax, ay + half)]
                else:
                    pts = [(ax - half, ay), (ex, ey), (ax + half, ay)]
                p = cv.beginPath()
                p.moveTo(*pts[0])
                p.lineTo(*pts[1])
                p.lineTo(*pts[2])
                cv.drawPath(p, stroke=1, fill=0)
                cv.restoreState()
            box = ch.get("label_box") or {}
            cv.saveState()
            if box.get("fill") and box["fill"]["kind"] == "solid":
                _set_fill(cv, box["fill"]["fg"])
                cv.rect(bx0, by0, bw, bh, stroke=0, fill=1)
            if box.get("line"):
                cv.setStrokeColorRGB(*box["line"]["color"])
                cv.setLineWidth(0.5 * fs)
                cv.rect(bx0, by0, bw, bh, stroke=1, fill=0)
            cv.setFillColorRGB(*(st["color"] or (0.35, 0.35, 0.35)))
            cv.setFont(font, size)
            for k, s in enumerate(lines):
                tw = fonts.width(s, font, size)
                cv.drawString(lx - tw / 2, by0 + bh - 1.44 * fs - lh * (k + 1) + size * 0.3, s)
            cv.restoreState()

    # 圖例
    leg = ch.get("legend")
    if leg:
        st = leg["style"]
        size = st["size"] * fs
        lay = leg.get("layout") or {"x": 0.78, "y": 0.4, "w": 0.2, "h": 0.25}
        lx, ltop, lw, lh_ = x + lay["x"] * w, top - lay["y"] * h, lay["w"] * w, lay["h"] * h
        if leg.get("line"):
            cv.saveState()
            cv.setStrokeColorRGB(*leg["line"]["color"])
            cv.setLineWidth(leg["line"]["w"] * fs)
            cv.rect(lx, ltop - lh_, lw, lh_, stroke=1, fill=0)
            cv.restoreState()
        n = len(cats)
        row_h = size * 1.775
        start = ltop - (lh_ - row_h * n) / 2
        marker = size * 0.55
        for i, cat in enumerate(cats):
            rowy = start - row_h * (i + 0.5) + size * 0.12
            mx = lx + size * 0.6
            pt = ch["points"].get(i) or ch.get("default_point") or {}

            def sq(mx=mx, rowy=rowy):
                p = cv.beginPath()
                p.rect(mx, rowy - marker / 2, marker, marker)
                return p

            _pattern(cv, sq, pt.get("fill") or {"kind": "solid", "fg": (0.3, 0.3, 0.8)},
                     (mx, rowy - marker / 2, mx + marker, rowy + marker / 2), fs)
            cv.saveState()
            cv.setFillColorRGB(*(st["color"] or (0.35, 0.35, 0.35)))
            cv.setFont(font, size)
            cv.drawString(mx + marker + size * 1.21, rowy - size * 0.33, cat)
            cv.restoreState()
