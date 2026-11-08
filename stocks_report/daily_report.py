#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
========================================================================
每日美股報表產生器  (Daily US-Equity Report Generator)
Billion Studio — Milestone 1 of the automated system (READ-ONLY, 不下單)
------------------------------------------------------------------------
流程:  yfinance 抓 EOD 資料 → 決定性 Python 算指標 + BSPS-lite 評分
       → 分組 (🔴 需注意 / 🚀 進場訊號 / 🟡 觀察 / 🟢 正常)
       → (可選) Claude API 寫自然語言解讀
       → Telegram 推「一眼掃」摘要 + Email 送完整 HTML 報表
------------------------------------------------------------------------
⚠️ 重要:此處的 BSPS-lite 是「近似版」評分,供報表快速篩選用,
         並非你 v2.3.1 Pine Script 的 parity-tested 移植版。
         進場決策仍以你 TradingView 上的 BSPS-HC + Breakout Fired 為準。
         「分數高 ≠ 進場」,Breakout Fired = YES 才是觸發。
========================================================================
用法:
  python daily_report.py            # 正式跑 (抓真實資料 + 依 DRY_RUN 決定是否送出)
  python daily_report.py --mock     # 用模擬資料測試,不抓網路、只存本機 HTML
先設好下方環境變數,再把 DRY_RUN 設 False 才會真的送出 Telegram/Email。
"""

import os
import sys
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import numpy as np
import pandas as pd
import requests

# ======================================================================
# 1) 設定區 CONFIG  —— 你主要改這裡
# ======================================================================

# --- 觀察清單 (可自行增減;之後可改成從 T212 API 抓實際持倉) ---
WATCHLIST = [
    "AAPL", "GOOGL",                                # 大型科技
    "NVDA", "AVGO", "MU", "MRVL", "NVTS",           # AI 半導體
    "ASTS", "RKLB", "ONDS", "SPCX",                 # 太空 / 衛星 (SPCX=SpaceX,2026 IPO 新股)
    "QBTS", "IBM",                                  # 量子
    "IREN", "BIDU", "HBAN", "GILD", "PYPL", "NOK",  # 其他
]

# --- 門檻參數 (可調) ---
VOL_SPIKE_MULT     = 1.5    # 成交量 > 20 日均量 × 此倍數 → Volume Spike
OVEREXTEND_PCT     = 0.12   # 收盤高於 MA20 超過此比例 → Overextended
RSI_OVERBOUGHT     = 70
CRASH_DROP_PCT     = -0.08  # 單日跌幅 (負數) 觸發 Event Crash Override 的門檻
BREAKOUT_LOOKBACK  = 20     # 突破前高的回看天數
OBV_LOOKBACK       = 10     # OBV 是否上升的回看天數
BSPS_WATCH_MIN     = 4      # BSPS 分數達此值且未突破 → 放「觀察」區
BIG_DROP_WATCH     = -0.05  # 單日跌幅大於此 → 放「需注意」

# --- 交付開關 (可由 run.bat 的環境變數控制;預設安全:不送出) ---
DRY_RUN           = os.environ.get("DRY_RUN", "true").lower() != "false"      # 設 DRY_RUN=false 才會真的送出
USE_AI_COMMENTARY = os.environ.get("USE_AI_COMMENTARY", "false").lower() == "true"  # 設 true 才呼叫 Claude API
REPORT_TZ_LABEL   = "UK"    # 只用於報表標題顯示

# --- 憑證 (請用環境變數,勿硬編碼進檔案) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")          # 寄件信箱
SMTP_PASS = os.environ.get("SMTP_PASS", "")          # Gmail 需用「應用程式密碼」App Password
EMAIL_TO  = os.environ.get("EMAIL_TO", SMTP_USER)    # 收件信箱

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-5"   # 成本更低可改 "claude-haiku-4-5-20251001";最新型號請查 docs.claude.com

# --- 資料源 (可切換:tiingo / yfinance) ---
DATA_SOURCE    = os.environ.get("DATA_SOURCE", "tiingo").lower()  # 主用 Tiingo,壞了可切 yfinance
TIINGO_API_KEY = os.environ.get("TIINGO_API_KEY", "")

# ======================================================================
# 2) 資料抓取
# ======================================================================

def fetch_ohlcv_yfinance(ticker: str, period: str = "1y") -> pd.DataFrame:
    """用 yfinance 抓日線 OHLCV (備援用)。"""
    import yfinance as yf  # 延後 import,--mock 模式可完全不需要
    df = yf.download(ticker, period=period, interval="1d",
                     auto_adjust=False, progress=False)
    if df.empty:
        raise ValueError(f"{ticker}: 無資料")
    if isinstance(df.columns, pd.MultiIndex):  # yfinance 有時回傳 MultiIndex 欄位,壓平
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

def fetch_ohlcv_tiingo(ticker: str, period: str = "1y") -> pd.DataFrame:
    """用 Tiingo 抓日線 OHLCV (主用,較可靠)。回傳與 yfinance 相同格式。"""
    if not TIINGO_API_KEY:
        raise ValueError("未設定 TIINGO_API_KEY,請在 run.bat 填入,或把 DATA_SOURCE 設成 yfinance")
    start = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    r = requests.get(
        f"https://api.tiingo.com/tiingo/daily/{ticker}/prices",
        params={"startDate": start, "token": TIINGO_API_KEY},
        headers={"Content-Type": "application/json"}, timeout=30)
    if r.status_code == 404:
        raise ValueError(f"{ticker}: Tiingo 查無此代碼")
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"{ticker}: Tiingo 無資料")
    df = pd.DataFrame(data)
    df.index = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)  # 統一成無時區日期
    df = df.sort_index()
    # 用還原權值 (adj*) 計算,避免除權息/分割造成 MA 斷層;最新一根 adjClose == close
    out = pd.DataFrame({
        "Open": df["adjOpen"], "High": df["adjHigh"], "Low": df["adjLow"],
        "Close": df["adjClose"], "Volume": df["adjVolume"],
    }).dropna()
    if out.empty:
        raise ValueError(f"{ticker}: Tiingo 資料為空")
    return out

def fetch_ohlcv(ticker: str, period: str = "1y") -> pd.DataFrame:
    """依 DATA_SOURCE 分派到對應資料源。"""
    if DATA_SOURCE == "yfinance":
        return fetch_ohlcv_yfinance(ticker, period)
    return fetch_ohlcv_tiingo(ticker, period)

# ======================================================================
# 3) 技術指標 (決定性計算)
# ======================================================================

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def _macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    line = ema12 - ema26
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal

def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()

def compute_indicators(df: pd.DataFrame) -> dict:
    close, high, vol = df["Close"], df["High"], df["Volume"]
    ma = {n: close.rolling(n).mean() for n in (10, 20, 50, 60, 200)}
    rsi = _rsi(close)
    macd_line, macd_sig, macd_hist = _macd(close)
    obv = _obv(close, vol)
    vol_avg20 = vol.rolling(20).mean()
    prior_high = high.shift(1).rolling(BREAKOUT_LOOKBACK).max()  # 不含今日的前高

    last = -1
    px = float(close.iloc[last])
    prev_px = float(close.iloc[last - 1])
    return {
        "close": px,
        "prev_close": prev_px,
        "day_chg": px / prev_px - 1,
        "week_chg": px / float(close.iloc[-6]) - 1 if len(close) > 6 else np.nan,
        "ma10": float(ma[10].iloc[last]),
        "ma20": float(ma[20].iloc[last]),
        "ma50": float(ma[50].iloc[last]),
        "ma60": float(ma[60].iloc[last]),
        "ma200": float(ma[200].iloc[last]) if not np.isnan(ma[200].iloc[last]) else np.nan,
        "ma50_prev": float(ma[50].iloc[last - 1]),
        "rsi": float(rsi.iloc[last]),
        "macd_hist": float(macd_hist.iloc[last]),
        "macd_bull": bool(macd_line.iloc[last] > macd_sig.iloc[last]),
        "vol": float(vol.iloc[last]),
        "vol_avg20": float(vol_avg20.iloc[last]),
        "obv_now": float(obv.iloc[last]),
        "obv_past": float(obv.iloc[last - OBV_LOOKBACK]) if len(obv) > OBV_LOOKBACK else float(obv.iloc[0]),
        "prior_high": float(prior_high.iloc[last]) if not np.isnan(prior_high.iloc[last]) else px,
        "bars": len(close),
        "data_date": pd.Timestamp(df.index[last]).strftime("%Y-%m-%d"),  # 這根 K 棒的真實日期
    }

# ======================================================================
# 4) BSPS-lite 評分 (0–6) + Breakout + Event Crash Override + 陷阱旗標
# ======================================================================

def bsps_score(m: dict) -> dict:
    px = m["close"]
    has_ma200 = not np.isnan(m["ma200"])

    # (1) Trend: MA50>MA200 或 early-recovery(MA50 上升且價站上 MA50)
    early_recovery = (m["ma50"] > m["ma50_prev"]) and (px > m["ma50"])
    trend = ((has_ma200 and m["ma50"] > m["ma200"]) or early_recovery)

    # (2) Momentum: RSI>50 且 MACD 多頭
    momentum = (m["rsi"] > 50) and m["macd_bull"]

    # (3) Volume Spike
    vol_spike = m["vol"] > VOL_SPIKE_MULT * m["vol_avg20"]

    # (4) OBV Rising
    obv_rising = m["obv_now"] > m["obv_past"]

    # (5) Price > MA20 且 > MA50
    above_ma = (px > m["ma20"]) and (px > m["ma50"])

    # (6) Not Overextended
    ext = (px - m["ma20"]) / m["ma20"] if m["ma20"] else 0
    not_extended = (ext < OVEREXTEND_PCT) and (m["rsi"] < RSI_OVERBOUGHT)

    checks = {
        "Trend": trend, "Momentum": momentum, "VolSpike": vol_spike,
        "OBV": obv_rising, ">MA20/50": above_ma, "NotExt": not_extended,
    }
    score = sum(checks.values())

    # Breakout Fired: 突破前高 + 量能確認 + notExtended 閘門 (簡化版)
    breakout = (px > m["prior_high"]) and vol_spike and not_extended

    # Event Crash Override: 單日 <-8% + 爆量 + 跌破 MA20/MA50
    override = (m["day_chg"] <= CRASH_DROP_PCT and m["vol"] > m["vol_avg20"]
               and px < m["ma20"] and px < m["ma50"])

    # 陷阱旗標
    flags = []
    is_ipo = m.get("bars", 999) < 67   # 上市不足 ~67 交易日 → 新股,標準 BSPS 不適用
    if is_ipo:
        flags.append(f"⚠ IPO 新股 (僅 {m.get('bars','?')} 交易日),BSPS/MA200 不適用,請套用 IPO 框架")
    downtrend = has_ma200 and (px < m["ma200"]) and (m["ma50"] < m["ma200"])
    if not_extended and downtrend:
        flags.append("⚠ NotExt PASS 但處下降趨勢 → 情境陷阱,勿當進場依據")
    if obv_rising and (override or m["day_chg"] < BIG_DROP_WATCH):
        flags.append("⚠ 賣壓中 OBV 滯後,Rising 不代表進場")
    if ext >= OVEREXTEND_PCT:
        flags.append(f"⚠ 高於 MA20 {ext*100:.0f}% → 追高風險")

    return {"score": score, "checks": checks, "breakout": breakout, "override": override,
            "flags": flags, "downtrend": downtrend, "ext": ext, "is_ipo": is_ipo}

# ======================================================================
# 5) 分析單一標的 + 分組
# ======================================================================

def analyze_ticker(ticker: str, fetch=fetch_ohlcv) -> dict:
    df = fetch(ticker)
    m = compute_indicators(df)
    s = bsps_score(m)
    return {"ticker": ticker, **m, **s}

def bucket_results(results: list) -> dict:
    b = {"crit": [], "entry": [], "watch": [], "normal": [], "stale": [], "ipo": []}
    for r in results:
        if r.get("stale"):                 # 資料過期 → 不做任何訊號判斷,單獨列出
            b["stale"].append(r)
            continue
        if r.get("is_ipo"):                # 新股 → 標準 BSPS 不適用,單獨列出(套 IPO 框架)
            b["ipo"].append(r)
            continue
        if r["override"] or r["day_chg"] <= BIG_DROP_WATCH or \
           (r["prev_close"] > r["ma50"] >= r["close"]):   # 今日跌破 MA50
            b["crit"].append(r)
        elif r["breakout"]:
            b["entry"].append(r)
        elif r["score"] >= BSPS_WATCH_MIN or r["close"] >= 0.98 * r["prior_high"]:
            b["watch"].append(r)
        else:
            b["normal"].append(r)
    return b

# ======================================================================
# 6) 摘要 / AI 解讀
# ======================================================================

def _reason(r: dict) -> str:
    if r["override"]:
        return f"單日 {r['day_chg']*100:+.1f}% 爆量跌破 MA20/50 → Event Crash Override,轉 watch-only"
    if r["day_chg"] <= BIG_DROP_WATCH:
        return f"單日 {r['day_chg']*100:+.1f}%,留意趨勢"
    if r["prev_close"] > r["ma50"] >= r["close"]:
        return "今日跌破 MA50,趨勢轉弱"
    if r["breakout"]:
        return f"Breakout Fired ✓ (BSPS {r['score']}/6)"
    if r["score"] >= BSPS_WATCH_MIN:
        return f"BSPS {r['score']}/6 但 Breakout=NO,尚未進場"
    return f"逼近前高 {r['prior_high']:.2f}"

def deterministic_summary(b: dict) -> str:
    """不需 API 的規則式摘要 (純文字)。"""
    lines = []
    def blk(title, items):
        if items:
            lines.append(title)
            for r in items:
                extra = ("  " + " / ".join(r["flags"])) if r["flags"] else ""
                lines.append(f"• {r['ticker']}  {r['close']:.2f}  {_reason(r)}{extra}")
            lines.append("")
    blk("🔴 需注意", b["crit"])
    blk("🚀 進場訊號", b["entry"])
    blk("🟡 觀察", b["watch"])
    if b.get("ipo"):
        lines.append("🆕 IPO 新股(標準 BSPS 不適用,套 IPO 框架)")
        for r in b["ipo"]:
            lines.append(f"• {r['ticker']}  {r['close']:.2f}  日 {r['day_chg']*100:+.1f}%(僅 {r.get('bars','?')} 交易日)")
        lines.append("")
    if b.get("stale"):
        lines.append("⚠️ 資料異常/過期(已排除訊號,請以券商為準)")
        for r in b["stale"]:
            lines.append(f"• {r['ticker']}  資料日期 {r['data_date']}(非最新交易日)")
        lines.append("")
    if b["normal"]:
        lines.append("🟢 正常: " + ", ".join(r["ticker"] for r in b["normal"]))
    return "\n".join(lines).strip()

def ai_commentary(b: dict) -> str:
    """呼叫 Claude API,把結構化數字轉成排序過的自然語言解讀。失敗則回退規則式摘要。"""
    if not (USE_AI_COMMENTARY and ANTHROPIC_API_KEY):
        return deterministic_summary(b)
    # 只餵需注意/進場/觀察的精簡數字,省 token
    payload_rows = []
    for k in ("crit", "entry", "watch"):
        for r in b[k]:
            payload_rows.append({
                "ticker": r["ticker"], "bucket": k, "close": round(r["close"], 2),
                "day%": round(r["day_chg"]*100, 1), "bsps": r["score"],
                "breakout": r["breakout"], "override": r["override"], "flags": r["flags"],
            })
    system = (
        "你是嚴謹的美股紀律檢查員,以繁體中文、保留英文術語作答。根據提供的量化數據,"
        "挑出今晚真正需要動作的標的並排序。嚴守規則:分數高≠進場,Breakout Fired=YES 才是觸發;"
        "Event Crash Override 標的一律 watch-only;下降趨勢中的 NotExt PASS 與賣壓中 OBV Rising 皆非進場訊號;"
        "勿把下跌中的價位越調越低當進場(knife-catching)。輸出 6 行內,先講最該注意的 2–3 檔。"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 700,
                  "system": system,
                  "messages": [{"role": "user", "content": str(payload_rows)}]},
            timeout=40,
        )
        resp.raise_for_status()
        parts = [c.get("text", "") for c in resp.json().get("content", []) if c.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        return text or deterministic_summary(b)
    except Exception as e:
        return deterministic_summary(b) + f"\n\n(AI 解讀失敗,已回退規則式摘要: {e})"

# ======================================================================
# 7) 報表產生 (Email HTML + Telegram)
# ======================================================================

def _arrow(above: bool) -> str:
    return "▲" if above else "▼"

def build_html_report(results: list, b: dict, commentary: str, today: str) -> str:
    def row(r):
        px = r["close"]
        chip = ("#c0392b" if (r["override"] or r["day_chg"] <= BIG_DROP_WATCH)
                else "#e67e22" if r["breakout"]
                else "#f1c40f" if r["score"] >= BSPS_WATCH_MIN else "#95a5a6")
        flags = "<br>".join(f"<span style='color:#c0392b'>{f}</span>" for f in r["flags"])
        return f"""<tr>
<td style="padding:6px 8px;font-weight:700">{r['ticker']}</td>
<td style="padding:6px 8px;text-align:right">{px:.2f}</td>
<td style="padding:6px 8px;text-align:right;color:{'#27ae60' if r['day_chg']>=0 else '#c0392b'}">{r['day_chg']*100:+.1f}%</td>
<td style="padding:6px 8px;text-align:right">{('%+.1f%%'%(r['week_chg']*100)) if not np.isnan(r['week_chg']) else '—'}</td>
<td style="padding:6px 8px;text-align:right">{r['vol']/r['vol_avg20']:.1f}×</td>
<td style="padding:6px 8px;text-align:center">{_arrow(px>r['ma20'])}{_arrow(px>r['ma50'])}{_arrow(px>r['ma200']) if not np.isnan(r['ma200']) else '·'}</td>
<td style="padding:6px 8px;text-align:right">{r['rsi']:.0f}</td>
<td style="padding:6px 8px;text-align:center;font-weight:700"><span style="background:{chip};color:#fff;border-radius:4px;padding:1px 7px">{r['score']}</span></td>
<td style="padding:6px 8px;text-align:center">{'✅' if r['breakout'] else '—'}</td>
<td style="padding:6px 8px;font-size:12px">{flags}</td>
</tr>"""

    order = b["crit"] + b["entry"] + b["watch"] + b["normal"] + b.get("ipo", []) + b.get("stale", [])
    rows = "".join(row(r) for r in order)
    stale_n = len(b.get("stale", []))
    ipo_n = len(b.get("ipo", []))
    counts = (f"🔴 {len(b['crit'])} ｜ 🚀 {len(b['entry'])} ｜ "
              f"🟡 {len(b['watch'])} ｜ 🟢 {len(b['normal'])}"
              + (f" ｜ 🆕 {ipo_n} 新股" if ipo_n else "")
              + (f" ｜ ⚠️ {stale_n} 資料異常" if stale_n else ""))
    commentary_html = commentary.replace("\n", "<br>")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body{{font-family:-apple-system,'Segoe UI','PingFang TC','Microsoft JhengHei',sans-serif;
background:#f4f6f8;color:#1c2833;margin:0;padding:20px}}
.card{{max-width:840px;margin:0 auto;background:#fff;border-radius:12px;
box-shadow:0 2px 12px rgba(0,0,0,.06);overflow:hidden}}
.head{{background:#1c2833;color:#fff;padding:18px 22px}}
.head h1{{margin:0;font-size:18px}}.head p{{margin:6px 0 0;font-size:13px;color:#aeb6bf}}
.sec{{padding:18px 22px}}.sec h2{{font-size:14px;margin:0 0 10px;color:#34495e}}
.summary{{background:#fbfcfc;border-left:4px solid #2e86de;padding:14px 16px;
border-radius:6px;font-size:14px;line-height:1.7}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#eaf0f6;padding:8px;text-align:left;font-size:12px;color:#5d6d7e}}
tr:nth-child(even){{background:#fbfcfc}}
.foot{{padding:14px 22px;font-size:11px;color:#95a5a6;border-top:1px solid #ecf0f1}}
</style></head><body><div class="card">
<div class="head"><h1>📊 每日美股報表 · {today}</h1><p>{counts}</p></div>
<div class="sec"><h2>今日重點</h2><div class="summary">{commentary_html}</div></div>
<div class="sec"><h2>完整明細</h2><table>
<tr><th>Ticker</th><th style="text-align:right">收盤</th><th style="text-align:right">日%</th>
<th style="text-align:right">週%</th><th style="text-align:right">Vol×</th>
<th style="text-align:center">MA20/50/200</th><th style="text-align:right">RSI</th>
<th style="text-align:center">BSPS</th><th style="text-align:center">Breakout</th><th>旗標</th></tr>
{rows}</table></div>
<div class="foot">BSPS-lite 為報表用近似評分,非 v2.3.1 parity 版;進場以 TradingView BSPS-HC + Breakout Fired 為準。<br>
本報表僅供研究參考,非投資建議。</div>
</div></body></html>"""

def build_report_json(results: list, b: dict, commentary: str, data_date: str) -> dict:
    """給 workshub.me 「美股日報」用的結構化資料 (網站原生排版,唔再嵌 HTML)。"""
    def _num(x):
        try:
            return None if x is None or np.isnan(x) else round(float(x), 2)
        except Exception:
            return None
    def reason(r, key):
        try:
            if key == "ipo":
                return f"日 {r['day_chg']*100:+.1f}%(僅 {r.get('bars','?')} 交易日)"
            if key == "stale":
                return f"資料日期 {r['data_date']}(非最新交易日)"
            return _reason(r)
        except Exception:
            return ""
    groups = []
    gmap = {}
    for key in ("crit", "entry", "watch", "ipo", "stale"):
        items = []
        for r in b.get(key, []):
            gmap[r["ticker"]] = key
            items.append({"t": r["ticker"], "px": _num(r["close"]), "r": reason(r, key), "f": list(r["flags"])})
        if items:
            groups.append({"k": key, "items": items})
    for r in b.get("normal", []):
        gmap[r["ticker"]] = "normal"
    order = b["crit"] + b["entry"] + b["watch"] + b["normal"] + b.get("ipo", []) + b.get("stale", [])
    rows = []
    for r in order:
        px = r["close"]
        has200 = not np.isnan(r["ma200"])
        rows.append({
            "t": r["ticker"], "px": _num(px),
            "d": _num(r["day_chg"] * 100),
            "w": None if np.isnan(r["week_chg"]) else _num(r["week_chg"] * 100),
            "v": _num(r["vol"] / r["vol_avg20"]) if r["vol_avg20"] else None,
            "ma": _arrow(px > r["ma20"]) + _arrow(px > r["ma50"]) + (_arrow(px > r["ma200"]) if has200 else "·"),
            "rsi": _num(r["rsi"]), "s": int(r["score"]), "b": bool(r["breakout"]),
            "f": list(r["flags"]), "g": gmap.get(r["ticker"], "normal"),
        })
    return {
        "date": data_date,
        "counts": {k: len(b.get(k, [])) for k in ("crit", "entry", "watch", "normal", "ipo", "stale")},
        "groups": groups,
        "normal": [r["ticker"] for r in b.get("normal", [])],
        "rows": rows,
        "commentary": commentary if (USE_AI_COMMENTARY and ANTHROPIC_API_KEY) else None,
    }

def build_telegram_text(b: dict, commentary: str, today: str) -> str:
    stale_n = len(b.get("stale", []))
    ipo_n = len(b.get("ipo", []))
    counts = (f"🔴{len(b['crit'])} 🚀{len(b['entry'])} "
              f"🟡{len(b['watch'])} 🟢{len(b['normal'])}"
              + (f" 🆕{ipo_n}" if ipo_n else "")
              + (f" ⚠️{stale_n}" if stale_n else ""))
    body = commentary if len(commentary) < 3500 else commentary[:3500] + "…"
    return (f"<b>📊 每日美股報表 · {today}</b>\n{counts}\n\n"
            f"{body}\n\n<i>完整明細見下方 HTML 附件。僅供研究參考。</i>")

# ======================================================================
# 8) 交付
# ======================================================================

def send_telegram(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[telegram] 未設定 token/chat_id,略過"); return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30)
    print("[telegram]", "OK" if r.ok else f"FAIL {r.status_code} {r.text[:200]}")

def send_telegram_document(path: str, caption: str = ""):
    """把完整 HTML 報表當附件檔傳到 Telegram (點附件用瀏覽器開)。"""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[telegram-doc] 未設定 token/chat_id,略過"); return
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
            files={"document": ("每日美股報表.html", f, "text/html")},
            timeout=60)
    print("[telegram-doc]", "OK" if r.ok else f"FAIL {r.status_code} {r.text[:200]}")

def send_email(html: str, today: str):  # 保留備用 (走 Gmail 方案時才會用到)
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("[email] 未設定 SMTP,略過"); return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 每日美股報表 · {today}"
    msg["From"], msg["To"] = SMTP_USER, EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(); s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
    print("[email] OK →", EMAIL_TO)


# ======================================================================
# 8b) 推送到 WorksHub 網站 (workshub.me 「美股日報」分頁)
#     只在設定了 GITHUB_TOKEN 時執行;失敗只印警告,不影響 Telegram。
# ======================================================================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "billionkmstudio/WorksHub")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
KEEP_REPORTS = 14   # 網站只顯示最近 7 日,多留幾日備用

def publish_to_workshub(html_path: str, data_date: str, report_json: dict = None):
    if not GITHUB_TOKEN:
        print("[workshub] 未設定 GITHUB_TOKEN,略過"); return
    import base64, json
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
    hdr = {"Authorization": f"Bearer {GITHUB_TOKEN}",
           "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}

    def get_sha(path):
        r = requests.get(f"{api}/{path}", headers=hdr, params={"ref": GITHUB_BRANCH}, timeout=30)
        return (r.json().get("sha"), r) if r.status_code == 200 else (None, r)

    def put(path, raw: bytes, msg):
        sha, _ = get_sha(path)
        body = {"message": msg, "branch": GITHUB_BRANCH,
                "content": base64.b64encode(raw).decode()}
        if sha: body["sha"] = sha
        return requests.put(f"{api}/{path}", headers=hdr, json=body, timeout=60)

    try:
        with open(html_path, "rb") as f:
            raw = f.read()
        r = put(f"public/reports/{data_date}.html", raw, f"Add 美股日報 {data_date}")
        if not r.ok:
            print(f"[workshub] FAIL 上傳報表 {r.status_code} {r.text[:200]}"); return

        if report_json is not None:
            rj = put(f"public/reports/{data_date}.json",
                     json.dumps(report_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                     f"Add 美股日報 data {data_date}")
            if not rj.ok:
                print(f"[workshub] FAIL 上傳 JSON {rj.status_code} {rj.text[:200]}"); return

        # 更新 index.json (日期清單)
        _, ri = get_sha("public/reports/index.json")
        dates = []
        if ri.status_code == 200:
            try:
                dates = json.loads(base64.b64decode(ri.json()["content"]).decode("utf-8")).get("dates", [])
            except Exception:
                dates = []
        if data_date not in dates:
            dates.append(data_date)
        dates = sorted(set(dates))[-KEEP_REPORTS:]
        r2 = put("public/reports/index.json",
                 json.dumps({"dates": dates}, ensure_ascii=False).encode("utf-8"),
                 f"Update 美股日報 index {data_date}")
        print("[workshub]", "OK" if r2.ok else f"FAIL index {r2.status_code} {r2.text[:200]}")
    except Exception as e:
        print("[workshub] 例外,略過:", e)


# 8c) GitHub Actions 模式:直接寫入 repo 檔案 (由 workflow commit),唔使 GITHUB_TOKEN
WORKSHUB_LOCAL_DIR = os.environ.get("WORKSHUB_LOCAL_DIR", "")   # 例如 public/reports
FORCE_SEND = os.environ.get("FORCE_SEND", "false").lower() == "true"

def already_published(data_date: str) -> bool:
    return bool(WORKSHUB_LOCAL_DIR) and os.path.exists(os.path.join(WORKSHUB_LOCAL_DIR, f"{data_date}.json"))

def publish_local(html_path: str, data_date: str, report_json: dict):
    import json, shutil
    os.makedirs(WORKSHUB_LOCAL_DIR, exist_ok=True)
    shutil.copyfile(html_path, os.path.join(WORKSHUB_LOCAL_DIR, f"{data_date}.html"))
    with open(os.path.join(WORKSHUB_LOCAL_DIR, f"{data_date}.json"), "w", encoding="utf-8") as f:
        json.dump(report_json, f, ensure_ascii=False, separators=(",", ":"))
    idx = os.path.join(WORKSHUB_LOCAL_DIR, "index.json")
    dates = []
    if os.path.exists(idx):
        try:
            with open(idx, encoding="utf-8") as f:
                dates = json.load(f).get("dates", [])
        except Exception:
            dates = []
    if data_date not in dates:
        dates.append(data_date)
    dates = sorted(set(dates))[-KEEP_REPORTS:]
    with open(idx, "w", encoding="utf-8") as f:
        json.dump({"dates": dates}, f, ensure_ascii=False)
    # 刪走超出保留數量嘅舊檔
    for fn in os.listdir(WORKSHUB_LOCAL_DIR):
        d = fn.split(".")[0]
        if fn != "index.json" and len(d) == 10 and d not in dates:
            os.remove(os.path.join(WORKSHUB_LOCAL_DIR, fn))
    print("[workshub-local] OK", data_date)

# ======================================================================
# 9) 主流程
# ======================================================================

def run(fetch=fetch_ohlcv):
    results = []
    for tk in WATCHLIST:
        try:
            results.append(analyze_ticker(tk, fetch=fetch))
        except Exception as e:
            print(f"[skip] {tk}: {e}")
    if not results:
        print("無任何標的成功,結束"); return

    # ---- 資料新鮮度防線 ----
    # yfinance 對某些股票可能回傳過期舊資料。以「最新的那根日期」為基準,
    # 任何資料日期比它舊的都標為過期(stale),並排除其訊號判斷。
    ref_date = max(r["data_date"] for r in results)
    stale_list = []
    for r in results:
        r["stale"] = r["data_date"] < ref_date
        if r["stale"]:
            r["flags"] = [f"⚠ 資料非最新 ({r['data_date']}),已排除訊號,請以券商為準"] + r["flags"]
            stale_list.append(r["ticker"])
    if stale_list:
        print(f"[資料警告] 以下標的資料非最新交易日 ({ref_date}),已排除訊號: {', '.join(stale_list)}")

    # 整批延遲偵測:若連「最新那根」都離今天太遠,可能整份資料都過期
    try:
        age = (dt.date.today() - dt.datetime.strptime(ref_date, "%Y-%m-%d").date()).days
        if age > 4:
            print(f"[資料警告] 整份報表最新資料日期 {ref_date},距今 {age} 天,可能整批延遲——"
                  f"建議改在美股收盤數小時後(較晚的英國深夜或隔天早上)執行")
    except Exception:
        pass

    today = f"{ref_date} · 資料日期"   # 標題直接顯示「真實資料日期」,不再用系統時鐘冒充

    # GitHub Actions:美股休市日 (資料日期冇更新) 唔重複發 Telegram
    if already_published(ref_date) and not FORCE_SEND:
        print(f"[skip] {ref_date} 嘅報表已發佈 (可能係休市日),唔重複發送。要強制重發請設 FORCE_SEND=true")
        return

    b = bucket_results(results)
    commentary = ai_commentary(b)
    html = build_html_report(results, b, commentary, today)
    tg = build_telegram_text(b, commentary, today)

    # 一律先把 HTML 寫到檔案 (Telegram 附件與本機預覽都要用)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    if DRY_RUN:
        print("=" * 60, "\n[DRY_RUN] 已存報表:", out, "\n", "=" * 60)
        print("\n--- Telegram 摘要預覽 ---\n")
        print(tg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    else:
        send_telegram(tg)                                   # ① 一眼掃摘要 (訊息)
        send_telegram_document(out, caption="📄 完整報表 · " + today)  # ② 完整報表 (HTML 附件)
        _rj = build_report_json(results, b, commentary, ref_date)
        if WORKSHUB_LOCAL_DIR:
            publish_local(out, ref_date, _rj)               # ③ GitHub Actions: 寫入 repo,由 workflow commit
        else:
            publish_to_workshub(out, ref_date, _rj)         # ③ 本機: 經 GitHub API 上傳 (需 GITHUB_TOKEN)

if __name__ == "__main__":
    if "--mock" in sys.argv:
        from mock_data import mock_fetch   # 測試用,見 mock_data.py
        globals()["DRY_RUN"] = True
        run(fetch=mock_fetch)
    else:
        run()
