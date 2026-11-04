"""截圖批次上傳（暫存區 → 確認 → 一次放入報告）。原則：系統絕不自己猜位置。

章節只用確定的方式決定：
  1. 檔名開頭是章節編號：4.3_DI.png、4.3-2.png、04.03 DI.png（前導 0 會去掉）
  2. 資料夾名稱開頭是章節編號：4.3 DI/任意檔名.png（拖整個資料夾或上傳 zip）
  檔名與資料夾的編號不一致、編號不存在、都沒有編號 → 待分類，必須由人指定。
與前一份正式日報同章節截圖的畫面相似度只當「建議」，要按確認才算數。

放入報告前必須全部通過：沒有待分類、截圖時間辨識完成、日期不是別天、不是以前日報用過的同一張圖；
章節張數與前一份正式日報不同時要勾選確認。放入在同一個交易內完成（全部成功或全部不放）。
"""

import hashlib
import io
import os
import re
import uuid
import zipfile
from datetime import datetime

from flask import current_app

from . import db
from .models import OFFICIAL_STATUSES, Report, ReportImage, ReviewLog, ScreenshotSection
from .ocr import clock_of, queue_image, status_for

CODE_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})?)(?=$|[\s_\-()（）\[\]#])")
IMAGE_SIGS = [(b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif")]
MAX_ZIP_FILES = 500
SUGGEST_MAX_DIST = 10  # dHash 漢明距離（64 位元）；同一個系統畫面通常在 0–8
SUGGEST_MARGIN = 4  # 最像與第二像的章節差距不夠大就不建議


# ---------------------------------------------------------------- 檔名規則


def norm_code(raw):
    """'04.03' → '4.3'；不是編號回傳 None。"""
    try:
        return ".".join(str(int(p)) for p in raw.split("."))
    except ValueError:
        return None


def code_of(name):
    stem = os.path.splitext(name)[0] if re.search(r"\.[A-Za-z]{3,4}$", name) else name
    m = CODE_RE.match(stem)
    return norm_code(m.group(1)) if m else None


def parse_path(path, codes):
    """(章節編號 or None, 來源 filename/folder or None, 說明)。codes：所有章節編號。"""
    parts = [p for p in re.split(r"[\\/]+", path or "") if p]
    if not parts:
        return None, None, "沒有檔名"
    name, folders = parts[-1], parts[:-1]
    fc = code_of(name)
    dc = next((c for c in (code_of(f) for f in reversed(folders)) if c), None)
    if fc and dc and fc != dc:
        return None, None, f"檔名編號 {fc} 與資料夾編號 {dc} 不一致"
    code, src = (fc, "filename") if fc else ((dc, "folder") if dc else (None, None))
    if code is None:
        return None, None, "檔名與資料夾都沒有章節編號"
    if code not in codes:
        return None, None, f"章節編號 {code} 不存在"
    return code, src, ""


def suggested_names():
    """每個章節建議的檔名（同一章節多張加 _2、_3）。"""
    out = []
    for s in ScreenshotSection.query.order_by(ScreenshotSection.order):
        title = re.sub(r"\s*/\s*", "-", s.title).strip()
        title = re.sub(r'[\\:*?"<>|]', "", title)
        out.append((s, f"{s.code}_{title}.png"))
    return out


# ---------------------------------------------------------------- 指紋


def _detect(head):
    for sig, ext in IMAGE_SIGS:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def dhash(data):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        g = im.convert("L").resize((9, 8))
        px = g.tobytes()  # L 模式：每個像素一個位元組
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def image_path(img):
    return os.path.join(current_app.config["UPLOAD_DIR"], str(img.report_id), img.filename)


def backfill(images):
    """舊截圖沒有指紋時補上（只讀檔案，不改內容）。"""
    changed = False
    for img in images:
        if img.sha256 and img.dhash:
            continue
        try:
            with open(image_path(img), "rb") as fh:
                data = fh.read()
            img.sha256 = img.sha256 or hashlib.sha256(data).hexdigest()
            img.dhash = img.dhash or dhash(data)
            changed = True
        except (OSError, ValueError):
            continue
    if changed:
        db.session.flush()


# ---------------------------------------------------------------- 上傳到暫存區


def expand(files):
    """[(相對路徑, bytes)]：一般圖片與 zip 內的圖片。zip 內不是圖片的檔案略過。"""
    out = []
    for path, data in files:
        if path.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n
                             and not os.path.basename(n).startswith(".")]
                    if len(names) > MAX_ZIP_FILES:
                        raise ValueError(f"zip 內檔案超過 {MAX_ZIP_FILES} 個")
                    for n in names:
                        out.append((f"{path}/{n}", z.read(n)))
            except (zipfile.BadZipFile, ValueError) as e:
                out.append((path, None, f"無法讀取 zip：{e}"))
        else:
            out.append((path, data))
    return out


def stage(report, files, user):
    """把檔案放進暫存區。回傳 [{path, status: staged/skipped, section, note}]。"""
    sections = {s.code: s for s in ScreenshotSection.query.all()}
    mine = ReportImage.query.filter(ReportImage.report_id == report.id,
                                    ReportImage.kind.in_(("screenshot", "staged"))).all()
    backfill(mine)
    seen = {i.sha256: i for i in mine if i.sha256}
    results = []
    for entry in expand(files):
        if len(entry) == 3:
            results.append({"path": entry[0], "status": "skipped", "note": entry[2]})
            continue
        path, data = entry
        ext = _detect(data[:16])
        if not ext:
            results.append({"path": path, "status": "skipped", "note": "不是支援的圖片格式（PNG / JPG / GIF / WebP）"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen:
            dup = seen[sha]
            where = "暫存區" if dup.kind == "staged" else "報告中"
            results.append({"path": path, "status": "skipped",
                            "note": f"與{where}的 {dup.source_path or dup.original_name} 是同一張圖，略過"})
            continue
        try:
            dh = dhash(data)
        except Exception:
            results.append({"path": path, "status": "skipped", "note": "圖片無法開啟"})
            continue
        code, src, note = parse_path(path, sections)
        fname = f"{uuid.uuid4().hex}.{ext}"
        folder = os.path.join(current_app.config["UPLOAD_DIR"], str(report.id))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, fname), "wb") as fh:
            fh.write(data)
        img = ReportImage(report_id=report.id, kind="staged", section_id=sections[code].id if code else None,
                          filename=fname, original_name=os.path.basename(path)[:256], source_path=path[:512],
                          place_source=src, stage_note=note[:256] or None, sha256=sha, dhash=dh,
                          uploaded_by_id=user.id)
        queue_image(img)
        db.session.add(img)
        seen[sha] = img
        results.append({"path": path, "status": "staged", "section": code, "note": note})
    db.session.commit()
    return results


# ---------------------------------------------------------------- 檢查與建議


def previous_official(report):
    return (Report.query.filter(Report.report_date < report.report_date, Report.status.in_(OFFICIAL_STATUSES))
            .order_by(Report.report_date.desc()).first())


def review(report):
    """暫存區狀態：每張的問題、每章節的張數比較、能不能放入。"""
    sections = ScreenshotSection.query.order_by(ScreenshotSection.order).all()
    staged = (ReportImage.query.filter_by(report_id=report.id, kind="staged")
              .order_by(ReportImage.source_path, ReportImage.id).all())
    placed = [i for i in report.images if i.kind == "screenshot"]
    prev = previous_official(report)
    prev_shots = [i for i in prev.images if i.kind == "screenshot"] if prev else []
    backfill(prev_shots + placed)
    # 以前任何一份日報用過的同一張圖（沿用舊圖）
    old = {}
    shas = [i.sha256 for i in staged if i.sha256]
    if shas:
        for o in (ReportImage.query.join(Report).filter(ReportImage.sha256.in_(shas), ReportImage.report_id != report.id,
                                                       ReportImage.kind == "screenshot")):
            old.setdefault(o.sha256, o)
    day = report.report_date.isoformat()
    by_prev = {}
    for p in prev_shots:
        by_prev.setdefault(p.section_id, []).append(p)
    items = []
    for img in staged:
        problems = []
        if img.section_id is None:
            problems.append(("block", "待分類：" + (img.stage_note or "請指定章節")))
        st = status_for(img, day)[0]
        if st in ("pending", "running"):
            problems.append(("wait", "截圖時間辨識中"))
        elif st == "mismatch":
            rec = clock_of(img) or {}
            problems.append(("block", f"截圖日期是 {', '.join(rec.get('dates', []))}，不是 {day}"))
        elif st == "error":
            problems.append(("warn", "時間辨識失敗，請確認畫面"))
        if img.sha256 in old:
            o = db.session.get(Report, old[img.sha256].report_id)
            problems.append(("block", f"與 {o.report_date} 日報（{o.doc_no}）用過的截圖完全相同，請換成今天的截圖"))
        items.append({"img": img, "problems": problems, "suggest": None if img.section_id else _suggest(img, by_prev)})
    counts = []
    for s in sections:
        now_n = sum(1 for i in placed if i.section_id == s.id)
        add_n = sum(1 for i in staged if i.section_id == s.id)
        prev_n = len(by_prev.get(s.id, []))
        if add_n or now_n or prev_n:
            counts.append({"section": s, "placed": now_n, "staged": add_n, "prev": prev_n,
                           "diff": bool(prev) and (now_n + add_n) != prev_n and (add_n or now_n)})
    blocking = sum(1 for it in items for k, _ in it["problems"] if k in ("block", "wait"))
    return {"sections": sections, "entries": items, "counts": counts, "prev": prev, "by_prev": by_prev,
            "blocking": blocking, "count_diff": [c for c in counts if c["diff"]],
            "waiting": any(k == "wait" for it in items for k, _ in it["problems"]),
            "can_commit": bool(staged) and blocking == 0}


def _suggest(img, by_prev):
    """與前一份正式日報各章節截圖比較畫面相似度；明顯最像的章節才建議。"""
    if not img.dhash or not by_prev:
        return None
    best = []
    for sid, imgs in by_prev.items():
        d = min((hamming(img.dhash, p.dhash) for p in imgs if p.dhash), default=None)
        if d is not None:
            best.append((d, sid))
    best.sort()
    if not best or best[0][0] > SUGGEST_MAX_DIST:
        return None
    if len(best) > 1 and best[1][0] - best[0][0] < SUGGEST_MARGIN and best[1][1] != best[0][1]:
        return None
    return {"section": db.session.get(ScreenshotSection, best[0][1]), "distance": best[0][0]}


# ---------------------------------------------------------------- 指定章節、移除、放入


def assign(report, img_id, section_id, source="manual"):
    img = ReportImage.query.filter_by(id=img_id, report_id=report.id, kind="staged").first()
    if not img:
        return False
    sec = db.session.get(ScreenshotSection, section_id) if section_id else None
    img.section_id = sec.id if sec else None
    img.place_source = source if sec else None
    img.stage_note = None if sec else "請指定章節"
    return True


def remove(report, img_id):
    img = ReportImage.query.filter_by(id=img_id, report_id=report.id, kind="staged").first()
    if not img:
        return False
    try:
        os.remove(image_path(img))
    except OSError:
        pass
    db.session.delete(img)
    return True


def commit(report, user, confirm_counts=False):
    """全部通過才放入；同一個交易內完成。回傳 (成功, 訊息)。"""
    r = review(report)
    if not r["entries"]:
        return False, "暫存區沒有截圖。"
    if r["blocking"]:
        return False, f"還有 {r['blocking']} 個問題要處理（待分類、辨識中、日期不符或沿用舊圖），處理完才能放入。"
    if r["count_diff"] and not confirm_counts:
        return False, "有章節的張數與前一份正式日報不同，請確認後勾選「已確認張數差異」。"
    now = datetime.now()
    by_src = {}
    for it in r["entries"]:
        img = it["img"]
        top = (db.session.query(db.func.max(ReportImage.order))
               .filter(ReportImage.report_id == report.id, ReportImage.kind == "screenshot",
                       ReportImage.section_id == img.section_id).scalar() or 0)
        img.kind, img.order = "screenshot", top + 1
        img.placed_by_id, img.placed_at = user.id, now
        img.stage_note = None
        by_src[img.place_source or "manual"] = by_src.get(img.place_source or "manual", 0) + 1
        db.session.flush()
    labels = {"filename": "檔名", "folder": "資料夾", "manual": "人工指定", "suggest": "採用建議"}
    detail = "、".join(f"{labels.get(k, k)} {n}" for k, n in by_src.items())
    if r["count_diff"]:
        detail += "；已確認張數差異：" + "、".join(f"{c['section'].code}（前一份 {c['prev']}、今天 {c['placed'] + c['staged']}）"
                                           for c in r["count_diff"])
    db.session.add(ReviewLog(report_id=report.id, action="screenshots", actor_id=user.id,
                             comment=f"放入 {len(r['entries'])} 張：{detail}"))
    report.updated_by_id = user.id
    db.session.commit()
    return True, f"已放入 {len(r['entries'])} 張截圖（{detail}）。"


def staged_count(report):
    return ReportImage.query.filter_by(report_id=report.id, kind="staged").count()
