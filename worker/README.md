# 文章庫（/library）

由 Threads / IG / Facebook 分享嘅文章，經 Worker 存入 Cloudflare D1，Gemini 自動分類。資料唔會入 repo（repo 係 public）。

## 一次性部署（順序好重要：先做 1–3，先好 merge / deploy）

1. 建資料庫：`npx wrangler d1 create workshub-library`，將輸出嘅 `database_id` 貼入 `wrangler.jsonc` 取代 `REPLACE_WITH_D1_ID`。
2. 建表：`npx wrangler d1 execute workshub-library --remote --file worker/schema.sql`
3. 設 secret：
   - `npx wrangler secret put LIB_TOKEN`（自己諗一個長密碼，例如 32 位隨機字串；頁面同 iPhone 捷徑都用佢）
   - `npx wrangler secret put GEMINI_API_KEY`（同新聞腳本用同一條 key 都得；唔設就全部「未分類」）
4. 確認 GitHub Secrets 嘅 `CLOUDFLARE_API_TOKEN` 有 **D1 Edit** 權限（原本 Edit Cloudflare Workers 範本未必有），否則 workflow deploy 會失敗。
5. Merge 後 deploy。開 `https://workshub.me/library`，輸入 LIB_TOKEN。

## iPhone 分享鍵捷徑（Shortcuts App）

新增捷徑，名稱「儲去 WorksHub」，喺設定開啟 **在「分享表單」中顯示**，接收類型揀 **URL、文字**：

1. 動作「取得 URL 內容」
   - URL：`https://workshub.me/api/lib`
   - 方法：POST
   - 標頭：`Authorization` = `Bearer 你的LIB_TOKEN`；`Content-Type` = `application/json`
   - 要求本文：JSON，加兩個欄位：`url`（文字）= 「捷徑輸入」；`note`（文字）= 留空（想加備註就先加一個「要求輸入」動作）
2. 動作「顯示通知」，內容用上一步結果，確認已儲存。

之後喺 Threads / IG / FB 撳分享 → 揀「儲去 WorksHub」就完成。分享出嚟嘅如果係一段文字（Facebook 有時咁），本文改傳 `text` 欄位，Worker 會自動由文字入面抽連結。

## API

全部要 `Authorization: Bearer <LIB_TOKEN>`。

- `GET /api/lib` 全部文章 + 分類清單
- `POST /api/lib` `{url?, text?, note?}` 儲存（相同連結自動去重，會清走 utm / igshid / fbclid 等追蹤參數）
- `PATCH /api/lib/:id` `{category?, tags?, note?, title?}`
- `POST /api/lib/:id/ai` 重新 AI 分類
- `DELETE /api/lib/:id`

分類清單喺 `worker/index.js` 頂部 `CATEGORIES`，想改直接改嗰行。

## 已知限制

- 社交平台貼文要登入先睇到內容，Worker 抓唔到全文時只靠連結、標題同你嘅備註去分類，會較粗疏。手動加入時連文字一齊貼會準好多。
- 密碼係單一 token，適合個人用；唔好分享個頁面畀其他人。
