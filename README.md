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
config/watchlist.json   自選股池 + 主題標籤
config/settings.json    信心指數門檻／權重、板塊 ETF、VIX 代理 symbol
data/history.json       程式自建的逐日收盤價歷史（自動累積，勿手動刪除）
scripts/generate_dashboard.py   主程式
.github/workflows/daily-dashboard.yml   排程設定（每日 00:00 UTC = 08:00 HKT）
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
