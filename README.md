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
4. 核准後日報鎖定。「完整檢視」頁顯示完整日報 PDF，可開啟列印或下載。

### PDF 輸出

`/reports/<id>/pdf` 依資料庫內容產生與原始範本**相同排版**的完整日報（不經 Word/Excel 轉檔，直接繪製）：

| 頁次 | 來源 | 做法 |
|---|---|---|
| Coverpage（8 頁起） | `app/pdf/cover.py` | 依原始 Word 輸出量測的座標、字型與列高繪製；記錄筆數多時自動換頁並更新目錄頁碼與 Page x of N |
| CheckList（25 頁） | `app/pdf/xlsx_print.py`、`xlsx_chart.py`、`formula.py` | 讀取 CheckList 範本 xlsx 本身的欄寬、列高、合併儲存格、框線、列印範圍、縮放、分頁、頁首 Logo 與圓餅圖，重算公式後畫出，等同 Excel 的列印結果 |
| Screenshot（52 頁起） | `app/pdf/screenshot.py` | 封面、含頁碼的目錄、46 個章節與上傳的截圖 |

CheckList 的圓餅圖數值由範本公式（COUNTIFS、Qty / SUM）依日報資料重算。圖表排列經過整理（表格、框線、頁首不變）：
每張圖對齊旁邊統計表的上緣、同頁圖表尺寸一致，百分比直接標在圖例上，不再使用範本中位置不一的浮動標籤。
若要恢復範本原始的圖表位置，將 `app/pdf/xlsx_print.py` 的 `OPTIMIZE_CHARTS` 設為 `False`。

檔名沿用原本格式：`CFXD-SCA-RLY-DPR-000896_0_IFI_2026-11-03_CFXD_SLA_Daily_Work_Report.pdf`。
產生一次約 20 秒，結果會快取在 `instance/pdf_cache/`，日報或截圖有變動時自動重產。

**需要的檔案（不放在 git 裡）：**

- CheckList 範本：把「CFXD SLA - SCADA Daily Inspection CheckList.xlsx」複製為 `instance/templates/checklist.xlsx`
  （或用 `.env` 的 `PDF_TEMPLATE_XLSX` 指定路徑）。修改範本的版面（欄寬、列高、分頁）後，PDF 會跟著改變。
- 字型：Ubuntu 需安裝 `fonts-crosextra-carlito fonts-liberation fonts-dejavu-core`（與 Calibri / Arial 字寬相同，版面位置不變）。
  要連字形都一模一樣，可把 Windows `C:\Windows\Fonts` 的 `calibri*.ttf`、`arial*.ttf`、`times*.ttf` 複製到 `instance/fonts/`
  （或 `.env` 的 `FONT_DIR`），系統會優先使用。

### 權限

| 角色 | 可執行的動作 |
|---|---|
| 系統管理員 | 所有動作。本機帳號（不經 AD），平時不使用；在「帳號管理」設定其他人的角色 |
| 檔案管理員 | 指派（撰寫人、核准人）、審核、退件、作廢、送審、新增、編輯、刪除；授權二線管理員代理 |
| 二線管理員 | 送審、新增、編輯、刪除；經檔案管理員授權後可代審核、代指派、代退件、代作廢 |
| 一線使用者 | 送審、新增、編輯（含別人的草稿 / 退回）、申請作廢 |

- AD 帳號首次登入建立為一線使用者；`APPROVER_USERS` 或 `LDAP_APPROVER_GROUP` 內的帳號首次登入時預設為檔案管理員。
  之後角色一律由系統管理員在右上角「帳號管理」調整，登入時不會再變更。
- 核准人：每份日報在「基本資料」指定一位檔案管理員為核准人（新日報沿用前一份；只有一位檔案管理員時自動指定）。
  只有該核准人或其授權的二線管理員能核准 / 退件；未指定時任何檔案管理員都可以審核。
- 職責分離：日報的撰寫人與送審人都不能核准該份日報（系統管理員除外）。
- **作廢**：保留日期與文件編號（流水號不會被後續日報沖掉），內容唯讀，處理紀錄保留原因。
  一線使用者送出「申請作廢」，由檔案管理員（或其代理人）核定或駁回。
- **刪除**：移除整份日報與截圖，該日期可重新建立；需輸入文件編號確認，並寫入稽核紀錄。

系統管理員帳號（本機密碼，至少 12 字元）：

```bash
flask --app wsgi create-admin --username sysadmin      # 建立或重設密碼
```

### 代理（二線管理員代檔案管理員）

- 檔案管理員在右上角「代理設定」授權二線管理員，可設定期間與備註，隨時撤銷；角色改成非二線時自動撤銷。
- 代理人首頁會列出可代為審核的日報與待核定的作廢申請，畫面註明「代 Alan Lin 簽核」。
- **文件顯示不變**：代理核准後，PDF 封面的 Approval、Revision Control 的 Approval 與 Shift Record 的
  Document Approved 仍顯示檔案管理員本人（例如 Alan Lin）。
- 稽核：系統記錄實際操作者與被代理者；日報頁的「處理紀錄」列出每次指派、核准、退件、作廢申請與作廢，
  以及是本人或由誰代理。刪除日報與角色變更記在「帳號管理」頁的稽核紀錄。

### 日報檢查（送審前後自動顯示）

日報頁的「檢查結果」直接用資料庫內容檢查，並與**日期較早、最近一份已核准**的日報比對，
審核者可據此決定核准或退回；待審清單也會顯示每份的錯誤 / 警告數。規則沿用 RLY-ITO_DDoc_compare，
但不需解析 PDF 或 OCR（文件編號、表頭、頁碼、Reports Control 由系統產生，不再檢查）。

| 類別 | 檢查內容 |
|---|---|
| 檢查表 | 未勾選；勾 Warning / Failed 但 Note 空白 |
| 告警 vs 檢查表 | Alarm Summary 每一筆都要對應到 Warning / Failed 勾選，反之亦然；Reason 與 Note 是否一致 |
| 告警表 | 缺 Reason、缺 Category / Device、重複項目 |
| 告警數量 | New + Known + False = 告警項數；False Alarm 數 = 標 Y 的項數；有 New Alarm 但 True Alarm Record 為 N/A |
| 記錄 | 說明寫「沒有問題 / 沒有現場作業」卻有記錄，或沒寫卻是 N/A |
| 基本資料 / 截圖 | 值班人員、核准人空白；沒有上傳截圖的章節 |
| 跨日比對 | 新增 / 解除 / 內容變更 / 延續的告警；New Alarm 數 = 實際新增數；Known Alarm 數 = 延續數；檢查結果與統計數字的變化 |

嚴重度：錯誤（建議退回）、警告（請確認）、提醒（參考）。說明欄的連結可直接跳到要修正的分頁。

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
sudo apt update && sudo apt install -y python3-venv nginx git \
    fonts-crosextra-carlito fonts-liberation fonts-dejavu-core
sudo useradd --system --home /opt/dailyreport --shell /usr/sbin/nologin dailyreport
sudo git clone https://github.com/nox913106/CFXD_DailyReportDocSys.git /opt/dailyreport
cd /opt/dailyreport
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # 填入 SECRET_KEY 與 AD 設定
sudo mkdir -p instance/templates                  # 放入 CheckList 範本（見「PDF 輸出」）
sudo cp "/path/to/CFXD SLA - SCADA Daily Inspection CheckList.xlsx" instance/templates/checklist.xlsx
sudo chown -R dailyreport:www-data /opt/dailyreport
sudo chmod 640 .env
sudo -u dailyreport .venv/bin/flask --app wsgi create-admin --username sysadmin   # 本機系統管理員

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
