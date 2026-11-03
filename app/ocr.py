"""截圖系統時間辨識（OCR）。規則沿用 RLY-ITO_DDoc_compare 的 screenshot_clocks / check_clocks：

每張截圖都帶有拍攝當下的系統時鐘：一般是右下角 Windows 工作列（"8:11 AM / 10/10/2026"），
全螢幕程式（SCADA HMI）在右上角（"9:07:25 AM 2026/10/10"）。只辨識這兩個區域，
避免把畫面內容（告警清單、日誌）裡的日期誤認為截圖時間。

OCR 引擎（rapidocr + onnxruntime）約佔 1 GB 記憶體、一張 1–2 秒，因此不在網頁程序裡執行，
而是由背景服務 `flask --app wsgi ocr-worker` 依序處理上傳後排隊的截圖。
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta

CLOCK_REGIONS = [("taskbar", (0.85, 0.94, 1.0, 1.0)), ("top-right", (0.55, 0.0, 1.0, 0.07))]
# 不要放寬工作列的裁切範圍：SCADA 截圖工作列正上方是告警清單，裡面的日期會被誤認為截圖日期。
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
# OCR 常把 ':' 讀成 '.'，也會把欄位黏在一起（"9.14.22AM2026/10/11"）
TIME_RE = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([AP]M)|(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?", re.I)
IP_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
DATE_FRAGMENT_RE = re.compile(
    r"\d{1,4}\s*[/.\-]\s*\d{1,2}|(?<!\d)20\d{2}(?!\d)|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)

# 沒有可用日期的原因不同，後續處理也不同，所以分開呈現：
#   unreadable：畫面上有日期，但 OCR 讀不出來 -> 需要人工看圖
#   time_only ：時鐘只顯示時間，畫面沒有日期（例如 NVR / XProtect）
#   no_clock  ：兩個時鐘位置都沒有時鐘（全螢幕程式、裁切過的截圖）
CLOCK_STATUS = {
    "ok": "日期正確",
    "mismatch": "日期不符",
    "unreadable": "有日期但無法辨識",
    "time_only": "只有時間、畫面沒有日期",
    "no_clock": "畫面沒有時鐘",
}
OCR_STATE = {"pending": "等待辨識", "running": "辨識中", "done": "已辨識", "error": "辨識失敗"}
STALE_AFTER = timedelta(minutes=10)  # 背景服務中斷時，卡在 running 的截圖重新排隊

log = logging.getLogger(__name__)
_ENGINE = None


def find_dates(txt):
    """OCR 文字中的日期（M/D/YYYY、YYYY/MM/DD、YYYY.MM.DD、YYYY-MM-DD、'Oct 10, 2026'），回傳 ISO 日期。"""
    out = []
    t = txt.replace("O", "0") if re.search(r"\d", txt) else txt
    for m in re.finditer(r"(?<!\d)(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?!\d)", t):
        out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r"(?<![\d.:])(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})(?!\d)", t):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out.append((y, a, b) if a <= 12 else (y, b, a))  # Windows en-US：M/D/YYYY
    for m in re.finditer(r"\b([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", txt):
        mo = MONTHS.get(m.group(1).lower())
        if mo:
            out.append((int(m.group(3)), mo, int(m.group(2))))
    return [f"{y:04d}-{mo:02d}-{dd:02d}" for y, mo, dd in out if 1 <= mo <= 12 and 1 <= dd <= 31 and 2000 < y < 2100]


def classify_no_date(rec):
    """時鐘區域沒有同時讀到時間和日期時，判斷原因。"""
    for txt in rec["ocr"].values():
        rest = IP_RE.sub(" ", TIME_RE.sub(" ", txt))  # 去掉時間與遠端桌面列的 IP
        if re.search(r"\d", rest):
            rest = rest.translate(str.maketrans("OoIl|", "00111"))
        if find_dates(rest) or DATE_FRAGMENT_RE.search(rest):
            return "unreadable"
    return "time_only" if rec["time"] else "no_clock"


def engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR

        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
        _ENGINE = RapidOCR()
    return _ENGINE


def read_clock(path, ocr=None):
    """辨識一張截圖的系統時鐘。ocr(np.ndarray) -> 文字清單；預設使用 rapidocr。"""
    import numpy as np
    from PIL import Image

    if ocr is None:
        eng = engine()

        def ocr(arr):
            return list(eng(arr).txts or [])

    img = Image.open(path).convert("RGB")
    W, H = img.size
    rec = {"region": None, "text": "", "dates": [], "time": "", "status": "no_clock", "ocr": {}}
    for name, (x0, y0, x1, y1) in CLOCK_REGIONS:
        c = img.crop((int(W * x0), int(H * y0), int(W * x1), int(H * y1)))
        sc = max(1, min(4, int(1200 / max(1, c.size[0]))))
        c = c.resize((c.size[0] * sc, c.size[1] * sc), Image.LANCZOS)
        txt = " ".join(ocr(np.array(c)))
        rec["ocr"][name] = txt
        ds = find_dates(txt)
        tm = TIME_RE.search(txt)
        if ds and tm:  # 時鐘一定同時顯示時間與日期
            rec.update(region=name, text=txt, dates=sorted(set(ds)), time=tm.group(0), status="ok")
            break
        if tm and not rec["time"]:
            rec.update(region=name, text=txt, time=tm.group(0))
    if rec["status"] != "ok":
        rec["status"] = classify_no_date(rec)
    return rec


def clock_of(img):
    """ReportImage 的辨識結果（dict）或 None。"""
    if img.ocr_status != "done" or not img.ocr_result:
        return None
    try:
        return json.loads(img.ocr_result)
    except ValueError:
        return None


def status_for(img, doc_date_iso):
    """畫面顯示用：(代碼, 文字)。日期正確但不是文件日期 -> mismatch。"""
    if img.ocr_status in (None, "pending", "running", "error"):
        return img.ocr_status or "pending", OCR_STATE[img.ocr_status or "pending"]
    rec = clock_of(img) or {}
    st = rec.get("status", "no_clock")
    if st == "ok" and doc_date_iso not in rec.get("dates", []):
        st = "mismatch"
    return st, CLOCK_STATUS[st]


# ---------------------------------------------------------------- 背景服務


def queue_image(img):
    if img.kind == "screenshot":
        img.ocr_status = "pending"
        img.ocr_result = None
        img.ocr_error = None


def claim_next(db, ReportImage):
    """原子性地取得下一張待辨識截圖（多個程序同時執行也不會重複處理）。"""
    now = datetime.now()
    stale = ReportImage.query.filter(ReportImage.ocr_status == "running", ReportImage.ocr_started_at < now - STALE_AFTER)
    for img in stale:
        img.ocr_status = "pending"
    db.session.commit()
    cand = (ReportImage.query.filter_by(ocr_status="pending").order_by(ReportImage.id).first())
    if cand is None:
        return None
    n = (ReportImage.query.filter_by(id=cand.id, ocr_status="pending")
         .update({"ocr_status": "running", "ocr_started_at": now}, synchronize_session=False))
    db.session.commit()
    return db.session.get(ReportImage, cand.id) if n else None


def process_one(db, ReportImage, path_of, ocr=None):
    img = claim_next(db, ReportImage)
    if img is None:
        return False
    try:
        rec = read_clock(path_of(img), ocr=ocr)
        img.ocr_result = json.dumps(rec, ensure_ascii=False)
        img.ocr_status = "done"
        img.ocr_error = None
    except Exception as e:  # 單張失敗不影響其他截圖
        log.exception("截圖 %s 辨識失敗", img.id)
        img.ocr_status = "error"
        img.ocr_error = str(e)[:500]
    img.ocr_at = datetime.now()
    db.session.commit()
    return True


def run_worker(app, once=False, idle_seconds=5):
    import os

    from . import db
    from .models import ReportImage

    def path_of(img):
        return os.path.join(app.config["UPLOAD_DIR"], str(img.report_id), img.filename)

    with app.app_context():
        engine()  # 先載入模型，失敗就直接結束並顯示原因
        log.warning("OCR 背景服務啟動")
        while True:
            if not process_one(db, ReportImage, path_of):
                if once:
                    return
                db.session.remove()
                time.sleep(idle_seconds)
