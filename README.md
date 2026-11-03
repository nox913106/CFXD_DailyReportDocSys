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
| 系統管理員 | 所有動作，另可管理帳號與「系統設定」。由檔案管理員或二線管理員兼任（AD 帳號可），在「帳號管理」指派；**一線使用者強制排除** |
| 本機緊急帳號 | 系統管理員權限的本機帳號（不經 AD），AD 無法驗證時使用，平時不使用 |
| 檔案管理員 | 指派（撰寫人、核准人）、審核、退件、作廢、送審、新增、編輯、刪除；授權二線管理員代理 |
| 二線管理員 | 送審、新增、編輯、刪除；經檔案管理員授權後可代審核、代指派、代退件、代作廢 |
| 一線使用者 | 送審、新增、編輯（含別人的草稿 / 退回）、申請作廢 |

- AD 帳號首次登入建立為一線使用者；`APPROVER_USERS` 或 `LDAP_APPROVER_GROUP` 內的帳號首次登入時預設為檔案管理員。
  之後角色一律由系統管理員在右上角「帳號管理」調整，登入時不會再變更。
- 核准人：每份日報在「基本資料」指定一位檔案管理員為核准人（新日報沿用前一份；只有一位檔案管理員時自動指定）。
  只有該核准人或其授權的二線管理員能核准 / 退件；未指定時任何檔案管理員都可以審核。
- 職責分離：日報的撰寫人與送審人都不能核准該份日報。被指派的系統管理員同樣受限，只有本機緊急帳號例外。
- 系統管理員：一線使用者不能被指派；把系統管理員改成一線使用者時，權限自動移除。不能移除自己的系統管理員權限，
  也不能把自己改成一線使用者（避免沒有人能管理）。指派、移除都寫入稽核紀錄。
- **作廢**：保留日期與文件編號（流水號不會被後續日報沖掉），內容唯讀，處理紀錄保留原因。
  一線使用者送出「申請作廢」，由檔案管理員（或其代理人）核定或駁回。
- **刪除**：移除整份日報與截圖，該日期可重新建立；需輸入文件編號確認，並寫入稽核紀錄。

本機緊急帳號（密碼至少 12 字元）與初次指派：

```bash
flask --app wsgi create-admin --username sysadmin      # 建立或重設本機緊急帳號的密碼
flask --app wsgi grant-admin --username <AD帳號>       # 初次設定時指派系統管理員（之後在「帳號管理」操作）
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
但不需解析 PDF（文件編號、表頭、頁碼、Reports Control 由系統產生，不再檢查）；只有截圖日期仍需 OCR。

| 類別 | 檢查內容 |
|---|---|
| 檢查表 | 未勾選；勾 Warning / Failed 但 Note 空白 |
| 告警 vs 檢查表 | Alarm Summary 每一筆都要對應到 Warning / Failed 勾選，反之亦然；Reason 與 Note 是否一致 |
| 告警表 | 缺 Reason、缺 Category / Device、重複項目 |
| 告警數量 | New + Known + False = 告警項數；False Alarm 數 = 標 Y 的項數；有 New Alarm 但 True Alarm Record 為 N/A |
| 記錄 | 說明寫「沒有問題 / 沒有現場作業」卻有記錄，或沒寫卻是 N/A |
| 基本資料 / 截圖 | 值班人員、核准人空白；沒有上傳截圖的章節 |
| 跨日比對 | 新增 / 解除 / 內容變更 / 延續的告警；New Alarm 數 = 實際新增數；Known Alarm 數 = 延續數；檢查結果與統計數字的變化 |
| 告警歷程 | 每筆告警已連續出現幾天、上一次出現是哪一天（回溯 365 天）；連續 30 天以上、或新增告警在 30 天內曾出現過（解除後又再發生）會提醒 |
| 截圖日期（OCR） | 截圖右下角工作列 / 右上角的系統時間必須是文件日期；有日期但讀不出來、只有時間、沒有時鐘分開列出；尚未辨識完成也會提示 |

嚴重度：錯誤（建議退回）、警告（請確認）、提醒（參考）。說明欄的連結可直接跳到要修正的分頁。

**告警歷程**只顯示在檢查結果，不會出現在日報 PDF。只計算已核准日報，沒有已核准日報的日子（未寫、仍在審核、被退回）
不會中斷連續天數；告警以 Category + Item + Device + Description 判斷是否為同一筆。查詢固定回溯 365 天
（`app/checks.py` 的 `HISTORY_DAYS`，提醒門檻為 `LONG_RUNNING_DAYS`、`RECUR_WITHIN_DAYS`），連續超過一年顯示「超過 365 天」，
因此查詢量不會隨資料累積變大：以 10 年、3,650 份日報、9.5 萬筆告警模擬，計算約 0.09 秒、固定 3 次查詢。
待審清單不計算告警歷程。SQLite 啟用 WAL 與 busy_timeout，網頁讀取不會被 OCR 背景服務的寫入擋住。

**截圖日期 OCR** 是確認截圖確實是當天畫面的唯一依據，規則沿用 DDoc_compare（只辨識兩個時鐘位置，
不會把畫面內容的日期誤當截圖時間）。截圖上傳後排隊，由背景服務 `dailyreport-ocr`（單一程序，約 1 GB 記憶體）
依序辨識，每張約 2 秒、85 張約 3 分鐘，網頁不會被拖慢。截圖分頁顯示每張的辨識結果，可「全部重新辨識」；
更新規則後可執行 `flask --app wsgi ocr-requeue [--date YYYY-MM-DD]`。
以 2026-11-03 的 85 張實際截圖驗證：77 張日期正確，8 張 12 NVR 畫面本身沒有日期（6 張只有時間、2 張沒有時鐘）。

### 檢查結果.md 與 AI 建議

**檢查結果.md**：送審、核准、退回時，系統把當下的檢查結果存成一份 Markdown（資料庫一份，並匯出到
`<匯出位置>/<年>/<月>/<文件編號>_R<版次>_<時機>_<時間>_檢查結果.md`）。結構與 DDoc_compare 的
`<DocNo>_比對報告.md` 相同（一-1 ～ 一-8 今日文件內部一致性、二-1 ～ 二-4 跨日比對，另加二-6 告警歷程），
所以兩邊的檔案可以放進同一個 RAG 知識庫：

- 檔案開頭有 YAML front matter（文件編號、日期、時機、錯誤 / 警告 / 提醒數、比對基準），RAG 可依此過濾。
- 核准 / 退回的檔案多一節「三、審核結果」（決定、簽核者、代簽、意見），AI 可以學到哪些問題會被退回。
- 檔案先寫成 `.part` 再改名，收錄程式不會讀到寫一半的檔案；匯出失敗不影響審核。
- 日報頁可下載任一版本的檢查結果.md；匯出位置在「系統設定」設定（預設 `instance/check_reports`，可設成 NAS 路徑）。
- `flask --app wsgi check-md-backfill [--since YYYY-MM-DD]`：替舊的已核准日報補產生檢查結果.md。
- `flask --app wsgi import-check-md <資料夾>`：把 DDoc_compare 產生的比對報告.md 複製到 `<匯出位置>/ddoc_compare/`。
  DDoc_compare 的 .md 是每次比對的輸出，存在它設定的 NAS 資料夾，不在 DDoc_compare 的程式庫裡，因此要從 NAS 匯入。

**AI 建議與提醒**：檢查結果最上方保留一個區塊。依據只有檢查結果.md：

- 送審時自動排入；撰寫人也可以在送審前按「產生 AI 建議」自我檢查，審核者可「重新產生」。
- 背景服務 `dailyreport-ai` 呼叫 AI，網頁不等 AI 回應。截圖還在 OCR 時先等辨識完成（最多 30 分鐘），
  並在產生前依最新辨識結果更新檢查結果.md。
- 送給 AI 的內容：提示詞、本次檢查結果.md（不含截圖逐張明細），加上 RAG 參考：同一份日報先前被退回時的檢查結果、
  最近 60 天其他日報被退回時的檢查結果（`app/ai.py` 的 `retrieve_context()`，日後改用向量檢索只需替換這個函式）。
- AI 必須標出依據的步驟編號（一-2、二-6…），不可編造檢查結果裡沒有的內容；回覆一律跳脫 HTML 後才顯示。
- 產生後檢查結果若有變化（數量不同），區塊會提示重新產生。AI 建議僅供參考，不會自動核准或退回。

### 系統設定（只有系統管理員）

右上角「系統設定」。所有 AI 相關設定都在這裡，存在資料庫，背景服務每次產生前重新讀取，不需要改 `.env` 或重啟。

- **一般設定**：總開關、送審時自動產生、是否允許公司外部的 AI 服務（開啟時必須勾選確認資料會送到外部）、
  RAG 參考份數、最大字數、逾時、輸出上限、temperature、檢查結果.md 匯出位置（可設 NAS 路徑）。
- **AI 服務**：可設定多個，依順序嘗試；前一個失敗、停用、本期預算或額度用完時，自動改用下一個（例如地端主機為主、雲端備援）。
  每個服務可「測試連線」；金鑰加密後存在資料庫（以 `SECRET_KEY` 衍生的金鑰加密，畫面只顯示末 4 碼；更換 `SECRET_KEY` 後需重新輸入）。
- **提示詞**：AI 的審核規則，可修改或還原預設。
- **用量**：每個服務本期的呼叫次數、token、費用（按量）或額度使用率（包月），以及最近 30 次呼叫紀錄。

| 計費方式 | 範本 | 額度控制 |
|---|---|---|
| 按量計費（token） | OpenAI、Anthropic Claude（預設 Claude Opus 5.5，每百萬 token US$4 / US$20）、Google Gemini、Azure OpenAI、AWS Bedrock、Mistral、DeepSeek、xAI、OpenRouter、Groq、Together、其他 OpenAI 相容服務 | 填 input / output 單價，可設每期預算上限 |
| 包月（固定月費） | Azure OpenAI PTU、AWS Bedrock Provisioned Throughput、Anthropic 企業包月 / 預付、公司 AI 閘道（LiteLLM 等） | 填月費，可設每期 token 額度 |
| 自建主機（地端） | Ollama、LM Studio、vLLM、llama.cpp、LocalAI、其他 OpenAI 相容服務 | 可填每月攤提成本供比較，資料不離開公司 |

呼叫協定共五種：OpenAI 相容 Chat Completions、Azure OpenAI、Anthropic Claude API（官方 SDK；Claude Opus 5.5 等模型啟用伺服器端 fallback，
被安全機制拒絕時自動改用其他 Claude 模型）、Google Gemini API、AWS Bedrock Converse API（Bedrock API 金鑰）。
進階欄位可選輸出上限參數名稱（`max_tokens` / `max_completion_tokens`）、是否傳送 temperature，以及併入請求的額外 JSON 參數。
ChatGPT Plus、Claude Pro 這類個人訂閱不提供 API，不能接入。

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

## 部署（一行指令）

介紹首頁：<https://nox913106.github.io/CFXD_DailyReportDocSys/>（`docs/index.html`，GitHub Pages）

支援 Ubuntu 24.04 / 26.04、Debian 13（x86_64 / aarch64，Python 3.12 以上；已實測 Ubuntu 24.04，其餘未實測）；建議 2 核心、4 GB 記憶體、20 GB 磁碟。
主機需要能連到 GitHub、PyPI 與 OCR 模型下載站（公司有 Proxy 時先設定 `https_proxy`；私有 PyPI 鏡像用 `PIP_INDEX_URL`）。

```bash
# 安裝：所有步驟自動完成（可重跑，不會覆蓋 .env、資料庫、憑證與緊急帳號密碼）
curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh | sudo bash

# 也可以一次帶入 AD、主機名稱與範本（都可以之後再補）
curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh | sudo bash -s -- install \
  --hostname report.corp.local \
  --ldap-url ldaps://dc01.corp.local:636 --ad-domain corp.local --base-dn DC=corp,DC=local \
  --template "/tmp/CFXD SLA - SCADA Daily Inspection CheckList.xlsx"

# 檢查部署結果（失敗項目會印出修正指令；有失敗時結束代碼為 1）
curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh | sudo bash -s -- doctor

# 升級（先備份，再更新程式、相依套件、重啟並健檢）
curl -fsSL https://raw.githubusercontent.com/nox913106/CFXD_DailyReportDocSys/main/scripts/bootstrap.sh | sudo bash -s -- upgrade
```

安裝腳本（`scripts/dailyreport.sh install`）會：檢查作業系統、記憶體與磁碟 → 安裝 Python、nginx、SQLite、字型與相依套件 →
建立系統帳號 `dailyreport` 與虛擬環境 → 產生 `.env`（隨機 `SECRET_KEY`）→ 建立本機緊急帳號 `sysadmin`（密碼只在結束時印一次，
也存在 `/etc/dailyreport/.admin-initial-password`，僅 root 可讀）→ 下載 OCR 模型 → 自簽 HTTPS 憑證（`--tls custom --cert … --key …`
可換公司 CA 憑證）→ nginx（HTTP 轉 HTTPS、安全標頭）→ 網頁、OCR、AI 三個服務與每日 02:30 備份排程 → 等服務啟動後執行健檢。

| 指令（`sudo bash /opt/dailyreport/scripts/dailyreport.sh …`） | 做什麼 |
|---|---|
| `doctor` | 檢查系統、版本、權限、設定、資料庫、PDF 範本與字型、OCR / AI 佇列、服務、HTTPS、備份 |
| `upgrade` | 備份 → 更新程式 → 相依套件 → 重啟 → 健檢 |
| `backup` | 線上備份資料庫、截圖、`.env`（`/var/backups/dailyreport`，保留 14 份，`DR_BACKUP_KEEP` 可調） |
| `restore <資料夾>` | 驗證檢查碼後還原；還原前先備份目前的資料 |
| `template <xlsx>` | 放入或更新 CheckList 範本 |
| `restart` / `status` | 重新啟動 / 查看狀態 |

安裝後：設定 AD（若安裝時沒帶，編輯 `/opt/dailyreport/.env` 後 `restart`）→ 放入 CheckList 範本 → 用 AD 帳號登入一次 →
用緊急帳號在「帳號管理」把檔案管理員指派為系統管理員 →（選用）在「系統設定」設定 AI 服務。

`/healthz` 不需登入，只回報資料庫是否可用（`{"status": "ok"}`），可給監控使用。

## AD 驗證設定 (.env)

| 變數 | 說明 | 範例 |
|---|---|---|
| `LDAP_URL` | 網域控制站，建議用 LDAPS | `ldaps://dc01.corp.local:636` |
| `AD_DOMAIN` | 網域名稱（組成 `user@domain` 登入） | `corp.local` |
| `LDAP_BASE_DN` | 搜尋使用者的根 DN | `DC=corp,DC=local` |
| `LDAP_REQUIRED_GROUP` | 選填，只允許此群組（含巢狀）成員登入 | `CN=DailyReportUsers,OU=Groups,DC=corp,DC=local` |
| `LDAP_CA_CERT` | 選填，內部 CA 憑證路徑（LDAPS 驗證用） | `/etc/ssl/certs/corp-ca.pem` |

使用者可輸入 `帳號`、`DOMAIN\帳號` 或 `帳號@domain` 登入。
