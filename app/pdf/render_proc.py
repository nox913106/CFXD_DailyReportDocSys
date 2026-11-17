"""PDF 產生的記憶體控制。

一份 80 多頁、含大量截圖的日報，產生 PDF 時程式會暫用約 0.7–1 GB 記憶體，而且 Python 用完不會還給系統。
網頁服務的 worker 若自己產生預覽，每個 worker 都會一直佔著那麼多記憶體；兩個 worker 加上 OCR、維護服務同時產生，
小記憶體主機就會記憶體不足（OOM），系統強制結束程序。因此：

1. 預覽 PDF 在獨立的子程序產生，產生完子程序結束，記憶體全部還給系統，網頁服務本身維持小。
2. 全系統同一時間只產生一份 PDF（預覽與正式 PDF 共用一把檔案鎖），尖峰記憶體只有一份的量；其他人排隊等候。
3. 正式 PDF（維護服務）產生後呼叫 malloc_trim，把用完的記憶體還給系統。
"""

import contextlib
import ctypes
import os
import subprocess
import sys

from flask import current_app

TEMPLATE_MISSING_EXIT = 3


@contextlib.contextmanager
def render_lock():
    """全系統一次只產生一份 PDF（網頁服務各 worker、維護服務共用）。"""
    import fcntl

    path = os.path.join(current_app.instance_path, "pdf_render.lock")
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def trim():
    """把 Python 已釋放的記憶體還給系統（glibc）。"""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def preview_to_file(report_id, out_path, timeout=170):
    """在子程序產生預覽 PDF 寫到 out_path。"""
    from .report_pdf import TemplateMissing

    root = os.path.dirname(current_app.root_path)
    r = subprocess.run([sys.executable, "-m", "app.pdf.render_proc", str(report_id), out_path],
                       cwd=root, capture_output=True, text=True, timeout=timeout)
    if r.returncode == TEMPLATE_MISSING_EXIT:
        raise TemplateMissing(r.stderr.strip())
    if r.returncode != 0 or not os.path.isfile(out_path):
        tail = "\n".join((r.stderr or "").strip().splitlines()[-5:])
        raise RuntimeError(f"預覽 PDF 產生失敗（代碼 {r.returncode}）：{tail}")


def _main(report_id, out_path):
    from app import create_app, db
    from app.models import Report

    from .report_pdf import TemplateMissing, render_pdf

    app = create_app()
    with app.app_context():
        report = db.session.get(Report, report_id)
        if report is None:
            print(f"找不到日報 {report_id}", file=sys.stderr)
            return 1
        try:
            data = render_pdf(report, preview=True)
        except TemplateMissing as e:
            print(str(e), file=sys.stderr)
            return TEMPLATE_MISSING_EXIT
    tmp = out_path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(_main(int(sys.argv[1]), sys.argv[2]))
