# 台股個股融資維持率篩選（FinMind API + GitHub Actions + GitHub Pages）

自動抓取 FinMind API 的台股融資資料，估算「個股融資維持率」，篩選出低於門檻
（預設 130%）的股票，每個交易日排程更新，並用 GitHub Pages 呈現成網頁表格。

> **重要：這是估算值，不是官方數字。** 台灣證交所／櫃買中心只公布「大盤」融資維持率，
> 並未公布個股數值，因為維持率牽涉每筆融資的實際成本，這只有券商知道。本專案用
> 「加權平均成本法」回推估算個股維持率，方法與 XQ 等看盤軟體公開說明的邏輯相同，
> 但仍是**估算**，僅供篩選觀察參考，**不構成投資建議**。詳細方法與限制寫在
> `scripts/fetch_margin_ratio.py` 檔頭註解，網頁最下方也會顯示同樣的提醒。

---

## 這個專案會做什麼

1. 從 FinMind 拿到全市場（上市＋上櫃）股票清單。
2. 對每一檔股票，抓最近 60 個交易日的融資買賣與收盤價，用加權平均成本法回推
   估計的「融資成本」。
3. 計算：`估算維持率 = 現在股價 ÷ (估算融資成本 × 融資成數) × 100%`
   （融資成數：一般股票 60%、ETF 90%，皆為概略假設，可在腳本中調整）。
4. 篩選出估算維持率 < 130%（可調整）的股票，輸出成一個網頁表格
   （`docs/index.html`，同時也會產生 `docs/margin_ratio_alert.csv` /
   `.json` 供下載）。
5. 用 GitHub Actions 排程（週一到週五台北時間 21:30，也就是台股當日融資資料
   通常已經更新之後）自動重新執行、更新網頁，並用 GitHub Pages 對外呈現。

## 執行時間會很長，這是正常的

FinMind 個股資料 API 對每個帳號等級都有「每小時請求次數」限制（免費會員約
600 次/小時）。要掃完全市場約 1700 檔股票，每檔股票需要 2 次請求
（融資資料 + 股價歷史），總共約 3000 多次請求，**單次完整執行可能需要 3～6 小時**。

腳本已經做了幾個優化：
- 會先用台灣證交所公開資料一次抓「所有上市股票」的今日收盤價與融資餘額，
  跳過目前完全沒有融資餘額、或不在你設定股價區間內的股票，減少要對 FinMind
  查詢的檔數（這個優化失敗時會自動退回逐檔查詢，不影響正確性）。
- 內建速率限制與自動重試：遇到 FinMind 的「已達每小時上限」訊息時，會自動睡到
  下一個小時再繼續，不會讓整個工作直接失敗。

GitHub Actions 對「public（公開）repo」的 Actions 執行時間是免費且沒有每月分鐘數
上限的，只有單次 job 最多跑 6 小時的硬性限制（本專案 workflow 已設定
`timeout-minutes: 350`，接近上限但留了緩衝）。若你想跑快一點，可以用下面會
提到的 `PRICE_MIN` / `PRICE_MAX` / `STOCK_LIMIT` 參數縮小範圍。

---

## Step by step：從零開始架設

### 1. 準備 FinMind API Token（強烈建議）

1. 到 [FinMind 官網](https://finmindtrade.com/) 註冊帳號並完成信箱驗證。
2. 登入後在會員頁面複製你的 API Token（一長串英數字）。
3. 沒有 token 也能跑（匿名，300 次/小時），但會更慢，建議還是註冊一下。

### 2. 建立新的 GitHub Repository

1. 登入 [github.com](https://github.com)，右上角 `+` → **New repository**。
2. 隨意取名，例如 `finmind-margin-ratio`，Visibility 選 **Public**
   （Public repo 的 GitHub Actions 完全免費、不限分鐘數；若選 Private，
   每月有一定的免費分鐘額度，這個專案跑很久，Private 可能會超額）。
3. 建立空的 repository（不用勾選 Add README，等一下會直接上傳整包檔案）。

### 3. 上傳這個專案的檔案

把我準備好的整個資料夾（`finmind-margin-ratio/`，包含
`.github/workflows/update.yml`、`scripts/fetch_margin_ratio.py`、
`requirements.txt`、`docs/index.html`、`README.md`）上傳到你剛建立的 repo。
兩種方式擇一：

**方式 A：直接在網頁上傳（不用裝任何工具）**
1. 進入你的新 repo 頁面 → `Add file` → `Upload files`。
2. 把整個資料夾內的檔案（保留資料夾結構）拖拉上傳。
   GitHub 網頁上傳目前支援拖拉整個資料夾結構，若瀏覽器不支援，
   可以分次上傳，記得 `.github/workflows/update.yml` 這個路徑一定要維持一致。
3. 送出 commit。

**方式 B：用 git 指令**
```bash
git clone https://github.com/<你的帳號>/<repo名稱>.git
cd <repo名稱>
# 把我提供的檔案複製進來這個資料夾，保留原本的路徑結構
git add .
git commit -m "init: finmind margin ratio project"
git push
```

### 4. 設定 FinMind Token 為 GitHub Secret

1. 進入 repo → `Settings` → 左側 `Secrets and variables` → `Actions`。
2. 點 `New repository secret`。
3. Name 填 `FINMIND_TOKEN`，Value 貼上你在步驟 1 拿到的 token → `Add secret`。

（如果你先前選擇不用 token，可以跳過這步，腳本會自動用匿名模式執行，
只是速度較慢。）

### 5. 開啟 GitHub Actions 並手動跑第一次

1. 進入 repo → `Actions` 分頁。若出現提示詢問是否啟用 workflow，按下啟用。
2. 左側會看到「更新台股融資維持率篩選網頁」，點進去。
3. 右上角 `Run workflow` → 可以視需要調整參數（見下方「可調整參數」），
   第一次建議先把 `stock_limit` 設成例如 `50` 測試流程是否正常、
   幾分鐘內就能看到結果，確認沒問題後，再手動重新執行一次、
   這次把 `stock_limit` 設回 `0`（代表全市場，正式跑）。
4. 點下面的 `Run workflow` 綠色按鈕開始執行，可以點進去看即時 log。

### 6. 開啟 GitHub Pages

1. 進入 repo → `Settings` → 左側 `Pages`。
2. `Build and deployment` → `Source` 選 `Deploy from a branch`。
3. `Branch` 選 `main`，資料夾選 `/docs` → `Save`。
4. 存檔後 GitHub 會給你一個網址，格式通常是
   `https://<你的帳號>.github.io/<repo名稱>/`，等 1～2 分鐘生效即可打開。

之後每次 workflow 執行完、`docs/` 資料夾有更新，這個網址的內容就會自動跟著更新。

---

## 可調整參數

在 `Actions` → 該 workflow → `Run workflow` 手動觸發時，或是修改
`.github/workflows/update.yml` 的排程觸發區塊，可以調整：

| 參數 | 說明 | 預設 |
|---|---|---|
| `margin_ratio_threshold` | 篩選門檻（維持率 < 多少 %） | 130 |
| `price_min` / `price_max` | 只篩選股價落在此區間的股票，留空=不限制。可用來縮小範圍、加快執行速度 | 不限制 |
| `stock_limit` | 只處理前 N 檔（測試用），正式跑請設 `0` | 0（不限制）|

其餘進階參數（`LOOKBACK_DAYS` 回推交易日數、`MARKETS` 掃描市場等）可以直接
修改 `.github/workflows/update.yml` 裡 `env:` 區塊，或修改
`scripts/fetch_margin_ratio.py` 檔頭註解列出的環境變數。

## 排程時間

預設是週一到週五台北時間 **21:30**（`cron: "30 13 * * 1-5"`，UTC 時間），
這個時間點台股當日的融資融券資料通常已經公布更新。如果想改時間，
修改 `update.yml` 裡的 `cron` 表達式即可（cron 用的是 UTC 時間，
台北時間 = UTC + 8 小時）。

## 本機測試（選用）

如果想在自己電腦先測試（例如你有裝 Python）：

```bash
pip install -r requirements.txt
export FINMIND_TOKEN=你的token   # 選用，但建議設定
export STOCK_LIMIT=30            # 測試用，先跑一小部分
python scripts/fetch_margin_ratio.py
# 完成後打開 docs/index.html 就能看到結果
```

## 檔案結構

```
.
├── .github/workflows/update.yml   # GitHub Actions 排程設定
├── scripts/fetch_margin_ratio.py  # 主程式：抓資料、估算、產生網頁
├── requirements.txt
├── docs/                          # GitHub Pages 網站根目錄（自動產生/更新）
│   ├── index.html                 # 篩選結果網頁
│   ├── margin_ratio_alert.csv     # 篩選結果 CSV
│   └── margin_ratio_alert.json    # 篩選結果 JSON
└── README.md
```

## 免責聲明

本專案僅為技術示範與資料觀察工具，融資維持率為模型估算值，可能與券商實際
計算結果有落差，不保證資料即時性、完整性或正確性，使用者需自行承擔依此
資訊做出任何投資決策的風險，本專案與作者不負任何法律或財務責任。
