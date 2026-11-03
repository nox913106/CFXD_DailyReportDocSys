"""匯入檢查表與截圖章節主檔。

checklist.json 由原始 Excel「CFXD SLA - SCADA Daily Inspection CheckList」轉出，
項目編號已依群組順序重新編號（Excel 會把 11.10 存成 11.1）。
"""

import json
from pathlib import Path

from .. import db
from ..models import ChecklistItem, ScreenshotSection

SEED_DIR = Path(__file__).parent


def seed_master_data():
    if not ChecklistItem.query.first():
        rows = json.loads((SEED_DIR / "checklist.json").read_text(encoding="utf-8"))
        for r in rows:
            db.session.add(
                ChecklistItem(
                    sheet=r["sheet"],
                    order=r["order"],
                    category=r["category"],
                    no=r["no"],
                    device_type=r["device_type"],
                    group_item=r["group_item"],
                    device_name=r["device_name"],
                    item_no=r["item_no"],
                    location=r["location"],
                    status_type=r["status_type"],
                    description=r["description"],
                )
            )
    if not ScreenshotSection.query.first():
        sections = json.loads((SEED_DIR / "screenshot_sections.json").read_text(encoding="utf-8"))
        for i, (code, title) in enumerate(sections, 1):
            db.session.add(ScreenshotSection(code=code, title=title, order=i))
    db.session.commit()
