# 美股風險監測（自用）

每個美股交易日收市後自動計 8 個指標、0–16 分，出中文網頁，並用 Telegram 預警。
只描述市況，唔係買賣建議。

## 設定（約 10 分鐘）

1. **GitHub**：開一個新 repo（建議 private），將呢個資料夾全部上傳。
2. **Telegram**
   - 搵 `@BotFather` → `/newbot` → 攞到 token。
   - 喺 Telegram 同你個 bot 講一句話，然後開 `https://api.telegram.org/bot<TOKEN>/getUpdates`，搵 `chat` → `id`。
3. **Repo → Settings → Secrets and variables → Actions**
   - Secrets：`TG_TOKEN`、`TG_CHAT_ID`
   - Variables（可選）：`SITE_URL`（預警訊息會附上網頁連結）
4. **網頁**：Settings → Pages → Deploy from branch → `main` / `/docs`。
   - 想私人用：GitHub 免費版 Pages 係公開嘅；可以改用 Cloudflare Pages + Access，或者只喺本機開 `docs/index.html`。
5. **首次執行**：Actions → daily-market-monitor → Run workflow。

## 本機測試

```bash
pip install -r requirements.txt
python monitor.py --demo      # 模擬數據，唔需要網絡
python monitor.py --no-alert  # 真數據，唔發 Telegram
```

## 預警規則（config.py 可改）

- 分數轉區（健康 ⇄ 警示 ⇄ Meltdown）
- 單日急升 ≥ 3 分
- 分數 ≥ 10 時每日提醒

## 8 個指標

| # | 指標 | 計法 |
|---|------|------|
| 1 | VIX 5日均 | ^VIX 5 日平均 |
| 2 | 指數 vs 50日線 | SPY、QQQ 偏離 50 日線嘅平均 |
| 3 | % 股 > 50日線 | S&P 500 成份股 |
| 4 | 個股距52週高 | 成份股平均（收市價計） |
| 5 | % 股 > 200日線 | 成份股 |
| 6 | 50日 vs 200日 | SPY 兩條線差距 |
| 7 | 21日波幅 | SPY 年化實現波幅 |
| 8 | 標普距52週高 | SPY 收市價計 |

## 重要：門檻要校準

`config.py` 嘅黃/紅門檻係我預設嘅，唔係 cashmate 嘅參數（佢哋冇公開）。
市場寬度只用 S&P 500，同全市場數字唔同。建議行幾星期後，
用 `data/history.csv` 睇每個指標嘅分布，再調到你覺得「平時多數健康、真係有事先亮紅」。

## 已知限制

- 數據來自 Yahoo Finance（yfinance），屬非官方，偶爾會失敗或延遲。
- 升跌家數只計 S&P 500 成份股，絕對數字會比全市場細好多，所以「≥300 隻」呢啲門檻對你嚟講好少出現，可以改細。
- 成份股名單用目前版本回推歷史，會有倖存者偏差。
