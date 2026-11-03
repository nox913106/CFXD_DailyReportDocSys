# CFXD 日報系統 (CFXD_DailyReportDocSys)

公司內部日報 Web 輸入系統。Flask + SQLite，使用公司 AD 帳號登入，部署於 Ubuntu 24.04 以上。

以網頁輸入取代原本的三份範本，資料存在資料庫：

| 原始範本 | 系統中的分頁 |
|---|---|
| Daily Inspection Coverpage.docx（封面、Reports Control、Inspection / On-site / Alarm 記錄） | 基本資料、值班 / 問題 / 現場記錄、告警 |
| SCADA Daily Inspection CheckList.xlsx（685 個檢查項目 + Alarm Summary） | 檢查表（VMs 89、Software 10、Physical devices 581、PI 5）、告警 |
| Daily Inspection Screenshot.docx（46 個章節的截圖） | 截圖（每章節可貼上 / 拖曳 / 選檔上傳） |

### 流程

1. 值班人員按「新增日報」選日期 → 文件編號依日期自動產生（沿用 Excel 公式，2026-11-03 → `CFXD-SCA-RLY-DPR-000896`）。
   新日報會沿用前一份的值班人員、核准人、Alarm Summary 清單與檢查表備註；檢查結果每天重新勾選。
2. 填寫各分頁，檢查表每項都要勾選 Normal / Warning / Failed。「從檢查表帶入異常」可把 Warning/Failed 項目加入 Alarm Summary。
3. 「送出審核」→ 審核者在「完整檢視」頁核准或退回（退回須填原因，撰寫人可修改後再送）。不能審核自己寫的日報。
4. 核准後日報鎖定；「完整檢視」頁可直接列印。PDF 輸出待後續開發。

### 權限

- 所有能登入的 AD 帳號都可以新增與編輯草稿 / 退回的日報。
- 審核者：`LDAP_APPROVER_GROUP` 群組成員，或列在 `APPROVER_USERS` 的帳號（每次登入時更新）。

### 檢查表主檔

`app/seed/checklist.json` 由原始 Excel 轉出，首次啟動時匯入資料庫。項目編號已依群組順序重新編號
（Excel 會把 11.10 存成 11.1，且原檔 NVR / Firewall 有幾處編號筆誤）。

## 本機開發

```bash
python -m venv .venv
.venv/Scripts/activate        # Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 開發時可設 AUTH_MODE=dev、APPROVER_USERS=alan 免連 AD
FLASK_DEBUG=1 flask --app wsgi run
```

開啟 http://127.0.0.1:5000

> `AUTH_MODE=dev` 允許任何帳號登入，只能在 `FLASK_DEBUG=1` 下啟用，正式環境請用 `ldap`。

### 測試

```bash
pip install -r requirements-dev.txt
pytest -q
```

`tests/test_flow.py` 涵蓋登入、建立日報、各分頁編輯、檢查表、截圖上傳、送審、退回、核准，以及隔天日報沿用前日資料。測試使用暫存資料庫，不會動到 `instance/`。

## 部署到 Ubuntu 24.04

```bash
sudo apt update && sudo apt install -y python3-venv nginx git
sudo useradd --system --home /opt/dailyreport --shell /usr/sbin/nologin dailyreport
sudo git clone https://github.com/nox913106/CFXD_DailyReportDocSys.git /opt/dailyreport
cd /opt/dailyreport
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # 填入 SECRET_KEY 與 AD 設定
sudo chown -R dailyreport:www-data /opt/dailyreport
sudo chmod 640 .env

sudo cp deploy/dailyreport.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now dailyreport

sudo cp deploy/nginx-dailyreport.conf /etc/nginx/sites-available/dailyreport
sudo ln -s /etc/nginx/sites-available/dailyreport /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

產生 SECRET_KEY：`python3 -c "import secrets; print(secrets.token_hex(32))"`

## AD 驗證設定 (.env)

| 變數 | 說明 | 範例 |
|---|---|---|
| `LDAP_URL` | 網域控制站，建議用 LDAPS | `ldaps://dc01.corp.local:636` |
| `AD_DOMAIN` | 網域名稱（組成 `user@domain` 登入） | `corp.local` |
| `LDAP_BASE_DN` | 搜尋使用者的根 DN | `DC=corp,DC=local` |
| `LDAP_REQUIRED_GROUP` | 選填，只允許此群組（含巢狀）成員登入 | `CN=DailyReportUsers,OU=Groups,DC=corp,DC=local` |
| `LDAP_CA_CERT` | 選填，內部 CA 憑證路徑（LDAPS 驗證用） | `/etc/ssl/certs/corp-ca.pem` |

使用者可輸入 `帳號`、`DOMAIN\帳號` 或 `帳號@domain` 登入。
