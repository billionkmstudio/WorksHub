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
- 黃紅燈界線（`update_monitor.py` 頂部的 `f_*` 函數）是由 cashmate 18 日可見分數反推，可以自行調整。
- `state/`（價格歷史）不會 commit，只存在 GitHub Actions 的快取。快取失效時，下次執行會自動重新回補。
- 三個統計燈（早期跌、見底、升浪確認）目前是參考數據，不是自動計算。

## 新聞自動更新

- 需要 GitHub Secret `GEMINI_API_KEY`（到 aistudio.google.com 取得免費金鑰）。
- 國際：BBC、Guardian、NPR、Al Jazeera、DW；財經：CNBC、MarketWatch、Yahoo Finance、BBC Business、Guardian Business。來源清單在 `update_news.py` 頂部的 `FEEDS`。
- 排名 = 各媒體頭條位置 + 多少媒體同時報道，再由 Gemini 選出 10 篇。這不是真實閱讀量。
- 摘要只根據 RSS 的標題和簡介寫成，連結和來源名稱直接取自 RSS。
