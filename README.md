# PM Capital Allocation Dashboard

自動化每日更新的 PM 資產配置儀表板。每天 08:00 HKT 由 GitHub Actions 執行，
從 Finnhub 拉取市場數據，計算信心指數與自選股評分，輸出單頁 `index.html`。

## ⚠️ 重要限制（請先讀）

- **這不是投資建議**，只是個人篩選輔助工具。所有邏輯、門檻都可在 `config/` 調整。
- Finnhub 免費方案**不提供美股歷史 K 線**與**期權鏈 / IV 數據**。因此：
  - 走勢分數（SMA/RSI）改用程式自建的 `data/history.json` 逐日累積歷史，
    需要約 **20 個交易日**才會脫離「proxy 模式」（頁面上會有黃色提示）。
  - Conviction 分數 / 策略標籤（如 "Put Credit Spread"）是**技術動能的簡化演算法**，
    並非真實期權分析。若需要真實 IV / Greeks，需升級 Finnhub 方案或改接其他數據源。

## 一次性設定

1. **建立 Finnhub 帳戶**並取得免費 API Key：https://finnhub.io/register
2. **建立這個 repo**（把整個資料夾 push 上去），或在 GitHub 建立空 repo 後把檔案上傳。
3. **加入 Secret**：Settings → Secrets and variables → Actions → New repository secret
   - Name: `FINNHUB_API_KEY`
   - Value: 你的 Finnhub API Key
4. **開啟 GitHub Pages**：Settings → Pages → Source 選 `main` branch / `/ (root)`。
   幾分鐘後可透過 `https://<你的帳號>.github.io/<repo名稱>/` 看到頁面。
5. **編輯自選股池**：改 `config/watchlist.json`，加入你自己的 tickers 同埋
   `theme`（長線首選卡片顯示用的標籤，例如 "AI Networking Leader"）。
6. （可選）調整 `config/settings.json` 內的信心指數門檻／權重、板塊 ETF 清單。

## （建議）即刻補齊真實歷史數據，唔使等 20 個交易日

Finnhub 免費方案唔提供美股歷史K線，所以主程式預設會逐日自己累積歷史，
需要約 20 個交易日先脫離「proxy 模式」。想即刻攞到真實歷史數據，可以加多一個
**Twelve Data**（免費 800 次/日，遠超所需）：

1. 去 https://twelvedata.com/pricing 註冊免費帳戶攞 API Key（唔使信用卡）
2. Settings → Secrets and variables → Actions → New repository secret
   - Name: `TWELVEDATA_API_KEY`
   - Value: 你的 Twelve Data API Key
3. **Actions** 分頁 → 揀 **"Backfill Real History (Twelve Data)"** → **"Run workflow"**
   （呢個 workflow 只可以手動觸發，唔會自動每日跑，因為淨係需要跑一次）
4. 等大約 3 分鐘（22隻股票 × 8秒間隔，避免撞 rate limit）跑完，
   `data/history.json` 就會有真實嘅60日歷史收盤價，個網頁下次更新即刻脫離 proxy 模式。

之後日常仍然用 Finnhub 做每日報價（`FINNHUB_API_KEY`），Twelve Data 淨係用嚟一次性補歷史，
或者想重新補一次（例如加咗好多新股票）都可以再手動跑一次呢個 workflow。

## 自動選股（4因子篩選，股票池自動追蹤標普500成分股）

想唔使手動編輯 `watchlist.json`，改用程式**自動掃描現時全部503隻標普500成分股**，
篩選出符合以下4個條件嘅股票：

- 股價 > SMA200（趨勢向上）
- 總市值 > 20億美元
- Beta（1年）> 1（波動性高過大盤）
- 平均每日成交金額（近1個月）> 9億美元（流動性足夠）

門檻可以喺 `scripts/screen_stocks.py` 頂部 `MIN_MARKET_CAP_USD` / `MIN_BETA` /
`MIN_DOLLAR_VOLUME_1MO` 幾個變數自行調整。

**候選股票池：自動跟隨 S&P 500 指數**

預設會即場攞返最新嘅 S&P 500 成分股名單（免費、唔使 API key，數據源自動跟 Wikipedia 更新）：
```
https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv
```
即係話 S&P 500 換馬（例如剔走A股加入B股）嗰陣，下次跑呢個 screener 就會自動用返最新名單，
唔使你手動更新。想額外追蹤幾隻未入標普500嘅股票（例如 CRWV、MSTR），寫入
`config/universe.json`，會自動加埋落去一齊掃描。

**運行時間：大約 25-35 分鐘掃描全部503隻**

大部分股票會先用快速嘅 Finnhub 查詢（市值、Beta）篩走，淨係通過咗嘅先會用受限流嘅
Twelve Data 查歷史（免費方案每次要等8秒），所以唔使成個鐘。想加快測試跑，可以喺
`config/settings.json` 嘅 `screener.max_candidates` 填個數字（例如 `100`），咁就會隨機
抽嗰個數量嚟掃描；填 `null`（預設）即係掃描全部。

如果想改用自己固定嘅候選名單（唔跟 S&P 500），將 `config/settings.json` 入面
`screener.source` 改做 `"manual"`，噉就會淨係讀 `config/universe.json`（要自己維護張清單）。

**設定：**

1. （用預設 S&P 500 模式就唔使做呢步）如果揀咗 `"manual"` 模式，編輯 `config/universe.json`
   填入你想掃描嘅候選股票
2. **Actions** 分頁 → 揀 **"Auto-Screen Stocks (4-Factor Filter)"** → **"Run workflow"**
3. 預設全掃描約25-35分鐘，跑完會自動：篩選 → 幫新增股票補歷史 → 重新產生儀表板 → 全部 commit 返

**運作邏輯：**

- 呢個 workflow 會**覆蓋** `config/watchlist.json`，改成篩選出嚟嘅股票
- 如果某隻股票之前手動填咗嘅 `theme` 標籤、而佢依然通過篩選，個標籤會保留；
  新通過嘅股票會自動用 Finnhub 嘅行業分類做 theme
- 純技術面篩選，唔睇新聞、估值質素等因素，建議當係候選名單自己覆核，唔係買入清單
- 想擴大或者收窄候選池，隨時編輯 `config/universe.json` 再重新跑

## 手動測試

在 Actions 分頁選 "Daily PM Capital Allocation Dashboard" → Run workflow，
可以立即觸發一次，不用等到明早 08:00 HKT。

也可以本機跑（需要自己 `pip install -r requirements.txt`）：

```bash
export FINNHUB_API_KEY=your_key_here
python scripts/generate_dashboard.py
```

## 檔案結構

```
config/watchlist.json   自選股池 + 主題標籤（可手動編輯，或由 screen_stocks.py 自動產生）
config/universe.json    自動選股用：額外追蹤嘅非標普500股票
config/settings.json    信心指數門檻／權重、板塊 ETF、VIX 代理 symbol、screener 設定
data/history.json       逐日收盤價歷史（自動累積，或由 backfill_history.py 一次性補齊）
scripts/generate_dashboard.py   主程式：產生 index.html
scripts/backfill_history.py     一次性攞真實歷史數據（Twelve Data）
scripts/screen_stocks.py        自動選股：4因子篩選標普500成分股
.github/workflows/daily-dashboard.yml     每日排程（00:00 UTC = 08:00 HKT）
.github/workflows/backfill-history.yml    手動觸發：補歷史數據
.github/workflows/screen-stocks.yml       手動觸發：自動選股
index.html               輸出頁面（每日自動更新並 commit 回 repo）
```

## 信心指數計算方式

```
Confidence = Trend×0.4 + VIX×0.3 + Breadth×0.3   (0-100)
  Trend   = SPY/QQQ 相對 SMA20/SMA50 位置（history 不足時用當日漲跌幅代理）
  VIX     = VIXY（VIX 代理 ETF）當日變動反向評分，波動越大分數越低
  Breadth = 11 個板塊 ETF 中今日上漲的比例
```

分類：`>75` 全倉部署 · `60-75` 選擇性部署 25-50% · `<60` 空倉觀望
（門檻可在 `config/settings.json` 修改）

## 想接真實期權數據？

Finnhub 付費方案有期權鏈端點；如果升級了，可以在
`scripts/generate_dashboard.py` 的 `strategy_for()` 同 `score_watchlist()`
入面加入 IV rank / put-call skew 等真實指標，取代目前的技術動能簡化版。
