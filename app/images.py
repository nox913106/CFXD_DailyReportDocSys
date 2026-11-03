"""截圖/照片上傳、顯示、刪除與排序（前端以 fetch 呼叫，回傳 JSON）。"""

import os
import uuid

from flask import Blueprint, abort, current_app, jsonify, request, send_from_directory, url_for
from flask_login import current_user, login_required

from . import db
from .helpers import get_report
from .models import ReportImage, ScreenshotSection
from .ocr import queue_image

bp = Blueprint("images", __name__, url_prefix="/reports/<int:report_id>/images")

# 以檔頭判斷格式，不信任副檔名
SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]
KINDS = ("screenshot", "onsite", "alarm")


@bp.before_request
@login_required
def _require_login():
    pass


def _detect(head):
    for sig, ext in SIGNATURES:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _report_dir(report_id):
    path = os.path.join(current_app.config["UPLOAD_DIR"], str(report_id))
    os.makedirs(path, exist_ok=True)
    return path


def _json(img):
    return {
        "id": img.id,
        "url": url_for("images.show", report_id=img.report_id, image_id=img.id),
        "name": img.original_name,
    }


def _editable_report(report_id):
    report = get_report(report_id)
    if not report.editable:
        resp = jsonify(error=f"此日報目前為「{report.status_label}」，不能修改。")
        resp.status_code = 409
        abort(resp)
    return report


def _siblings(img):
    return (
        ReportImage.query.filter_by(report_id=img.report_id, kind=img.kind, section_id=img.section_id)
        .order_by(ReportImage.order, ReportImage.id)
        .all()
    )


@bp.route("", methods=["POST"])
def upload(report_id):
    report = _editable_report(report_id)
    kind = request.form.get("kind")
    if kind not in KINDS:
        abort(400)
    section_id = None
    if kind == "screenshot":
        section = db.session.get(ScreenshotSection, request.form.get("section_id", type=int) or 0)
        if not section:
            abort(400)
        section_id = section.id

    order = (
        db.session.query(db.func.max(ReportImage.order))
        .filter(
            ReportImage.report_id == report.id,
            ReportImage.kind == kind,
            ReportImage.section_id == section_id,
        )
        .scalar()
        or 0
    )
    saved, errors = [], []
    for f in request.files.getlist("files"):
        head = f.stream.read(16)
        f.stream.seek(0)
        ext = _detect(head)
        if not ext:
            errors.append(f"{f.filename}：不是支援的圖片格式（PNG/JPG/GIF/WebP）")
            continue
        filename = f"{uuid.uuid4().hex}.{ext}"
        f.save(os.path.join(_report_dir(report.id), filename))
        order += 1
        img = ReportImage(
            report_id=report.id,
            kind=kind,
            section_id=section_id,
            filename=filename,
            original_name=(f.filename or "")[:256],
            order=order,
            uploaded_by_id=current_user.id,
        )
        queue_image(img)  # 截圖排入背景 OCR
        db.session.add(img)
        saved.append(img)
    db.session.commit()
    return jsonify(images=[_json(i) for i in saved], errors=errors)


@bp.route("/<int:image_id>")
def show(report_id, image_id):
    img = ReportImage.query.filter_by(id=image_id, report_id=report_id).first_or_404()
    return send_from_directory(_report_dir(report_id), img.filename, max_age=86400)


@bp.route("/<int:image_id>", methods=["DELETE"])
def delete(report_id, image_id):
    _editable_report(report_id)
    img = ReportImage.query.filter_by(id=image_id, report_id=report_id).first_or_404()
    path = os.path.join(_report_dir(report_id), img.filename)
    db.session.delete(img)
    db.session.commit()
    if os.path.exists(path):
        os.remove(path)
    return jsonify(ok=True)


@bp.route("/<int:image_id>/move", methods=["POST"])
def move(report_id, image_id):
    """direction=up/down：與同一區塊中相鄰的圖片交換順序。"""
    _editable_report(report_id)
    img = ReportImage.query.filter_by(id=image_id, report_id=report_id).first_or_404()
    siblings = _siblings(img)
    i = siblings.index(img)
    j = i - 1 if request.form.get("direction") == "up" else i + 1
    if 0 <= j < len(siblings):
        siblings[i], siblings[j] = siblings[j], siblings[i]
        for n, s in enumerate(siblings, 1):
            s.order = n
        db.session.commit()
    return jsonify(ok=True)


@bp.route("/ocr", methods=["POST"])
def requeue_ocr(report_id):
    """重新辨識這份日報的所有截圖。"""
    report = _editable_report(report_id)
    n = 0
    for img in report.images:
        if img.kind == "screenshot":
            queue_image(img)
            n += 1
    db.session.commit()
    return jsonify(ok=True, queued=n)
