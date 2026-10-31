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

---

# 創業情報站（`/desk`）

網址：`https://<你的帳號>.github.io/<repo>/desk/`

每日財經、痛點挖掘、內容靈感，加每週總結。資料全部以 AES-256-GCM 加密存喺 `docs/desk/data/store.enc.json`，
即使 repo 係 Public，冇密碼都睇唔到私人頁面嘅內容。

## 頁面權限

`desk/pages.json` 控制每頁係 `public`（任何人睇到）定 `private`（要密碼）：

```json
{ "finance": "private", "pain": "private", "content": "private", "weekly": "private" }
```

1. 喺 GitHub 直接改呢個檔案（例如 `"finance": "public"`）→ commit。
2. `desk-rebuild` workflow 會自動重建 `public.json`，約一分鐘後生效。
3. 公開頁面嘅內容會以明文放喺 repo，任何人都可以下載，請只公開你想公開嘅嘢。

預設全部私人。所有私人頁面共用同一個密碼。

## 一次性設定

Repo → Settings → Secrets and variables → Actions → New repository secret：`DESK_PASSWORD`（同你解鎖網頁用嘅密碼一樣）。

## 本機工具

```bash
pip install cryptography
export DESK_PASSWORD='你的密碼'                      # 至少 12 個字元，唔好寫入任何檔案
python desk/desk.py add --kind day  --file day.json   # 加入或更新某日內容
python desk/desk.py add --kind week --file week.json  # 加入或更新某週總結
python desk/desk.py build                             # 按 pages.json 重建公開資料
DESK_PASSWORD_NEW='新密碼' python desk/desk.py rekey  # 換密碼（之後要更新 Secret）
```

## 要知道嘅限制

- 加密喺瀏覽器入面解密，所以任何人都可以下載密文，再離線嘗試估密碼。密碼要夠長夠亂，唔好用常見字詞。
- 呢個唔係有帳戶嘅登入系統，冇「邊個睇過」嘅紀錄。如果之後要分人、分權限，要改用 Cloudflare Access 之類。
- 每日自動更新目前仍由 Claude 排程寫入原本嘅 Claude 頁面，未接駁到 GitHub。
