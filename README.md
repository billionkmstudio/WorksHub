# Works Hub

靜態網站（`public/index.html` + `public/data.json`）加一個每日自動計算美股風險分數的排程。

## 資料夾

- `public/` 網站本身。每日只有 `data.json` 會被更新。
- `scripts/update_monitor.py` 每日計算腳本。
- `.github/workflows/daily-monitor.yml` 排程：香港時間約 14:30（週二至週六）自動執行。
- `scripts/update_news.py` 每日新聞腳本（RSS → Gemini 整理成繁中）。
- `.github/workflows/daily-news.yml` 每日 05:40 UTC（香港約 13:40）執行。
- `wrangler.jsonc` Cloudflare 部署設定（讓排程可以順便重新部署網站）。

## 一次性設定

1. **Polygon API 金鑰**：到 polygon.io 註冊免費帳戶，拿到 API key。
2. **GitHub Secrets**：repo → Settings → Secrets and variables → Actions → New repository secret，加入：
   - `POLYGON_API_KEY`：上面的金鑰。
   - `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`：可選。有了它們，每次更新後會自動把網站部署到 Cloudflare。Token 要用「Edit Cloudflare Workers」範本建立。
3. **首次執行**：repo → Actions → Daily market monitor → Run workflow。第一次會回補約 400 個交易日的歷史價格，免費方案每分鐘 5 次請求，大約需要 90 分鐘。完成後 `public/data.json` 會被自動更新並 commit。
4. 之後每個交易日自動執行，只下載最新一天。

## 說明

- 股票池：美國普通股，收市 ≥ $5、20 日平均成交 ≥ 30 萬股（每月更新名單一次）。
- 溫度計共 9 個指標（滿分 18）；第 9 個是 SMH（半導體 ETF）距自己 50 日線的百分比，黃燈 < 0%、紅燈 < −5%（本站自訂起始值，在 `update_monitor.py` 的 `f_smh`）。
- 黃紅燈界線（`update_monitor.py` 頂部的 `f_*` 函數）是由 cashmate 18 日可見分數反推，可以自行調整。
- `state/`（價格歷史）不會 commit，只存在 GitHub Actions 的快取。快取失效時，下次執行會自動重新回補。
- 三個統計燈（早期跌、見底、升浪確認）由 `scripts/update_lights.py` 自行計算：只用標普 500 指數 1950 年起的每日收市價（Yahoo → Stooq → FRED，快取在 `data/spx_history.csv`），定義寫在腳本的 `META_DEFS`，並顯示在網頁。排程：`.github/workflows/lights.yml`（週二至週六 07:30 UTC，也可手動 Run workflow）。
- 版本號：每次發佈新功能，要手動更新 `public/index.html` 頁尾的 `Works Hub vX.Y.Z`。

## 新聞自動更新

- 需要 GitHub Secret `GEMINI_API_KEY`（到 aistudio.google.com 取得免費金鑰）。
- 國際：BBC、Guardian、NPR、Al Jazeera、DW；財經：CNBC、MarketWatch、Yahoo Finance、BBC Business、Guardian Business。來源清單在 `update_news.py` 頂部的 `FEEDS`。
- 排名 = 各媒體頭條位置 + 多少媒體同時報道，再由 Gemini 選出 10 篇。這不是真實閱讀量。
- 摘要只根據 RSS 的標題和簡介寫成，連結和來源名稱直接取自 RSS。

## 痛點與靈惑

- `scripts/update_insights.py` 讀取 Reddit 各版熱門帖和 Hacker News，由 Gemini 選出 5 個痛點和 5 個靈惑。X、Facebook、Threads、IG 沒有公開接口，所以沒有讀取。
- Reddit 有時會擋 GitHub 的伺服器；這一步失敗不會影響新聞更新。

## 自動重試與每週總結

- 新聞每日跑 3 次（05:40、09:40、13:40 UTC）。已完成的部分會跳過，所以後面兩次只係補返 Gemini 繁忙（HTTP 503）或者來源失敗留下嘅空位。
- Gemini 繁忙時，腳本會逐輪重試（每輪試晒所有模型，等待時間逐步加長），唔會一次失敗就放棄。國際同財經分開處理，其中一個失敗唔會丟棄另一個。
- 新聞失敗時，痛點同靈感仍然會更新，已成功嘅資料亦會照常 commit。
- `scripts/update_weekly.py` + `.github/workflows/weekly.yml`：逢星期日 18:00 UTC（之後 21:30 同星期一 07:00 各重試一次）自動寫每週總結，內容只根據當週已收集嘅新聞、痛點、靈感同風險分數。當週新聞少過 3 日就唔寫。
- 手動重做某日或某週：Actions → 揀 workflow → Run workflow，`FORCE=1` 可喺 workflow 檔案加入 env。

## 1 年前對比

`update_monitor.py` 現在保留 560 個交易日。第一次執行時會自動回補較舊的約 160 天（約 35 分鐘），之後「1 年前」才會出現。
