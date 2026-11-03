"""文件編號規則，沿用原 Excel 公式：
"CFXD-SCA-RLY-DPR-" & TEXT(387 + (日期 - DATE(2025,6,12)), "000000")
例：2026-11-03 → CFXD-SCA-RLY-DPR-000896、RT230018-CFXD SLA-RP-000796
"""

from datetime import date

BASE_DATE = date(2025, 6, 12)
BASE_SEQ = 387
CONTRACTOR_OFFSET = 100


def report_seq(d):
    return BASE_SEQ + (d - BASE_DATE).days


def doc_no(d):
    return f"CFXD-SCA-RLY-DPR-{report_seq(d):06d}"


def contractor_doc_no(d):
    return f"RT230018-CFXD SLA-RP-{report_seq(d) - CONTRACTOR_OFFSET:06d}"
