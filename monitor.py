"""美股風險監測：抓數據 → 計 8 指標 → 0–16 分 → 預警 → 出網頁。

用法：
  python monitor.py            正式（抓 Yahoo Finance 數據）
  python monitor.py --demo     用模擬數據測試，唔需要網絡
  python monitor.py --no-alert 唔發 Telegram
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
from render import render_page

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
DATA.mkdir(exist_ok=True)
DOCS.mkdir(exist_ok=True)


# ---------- 數據 ----------
def get_universe():
    cache = DATA / "universe.csv"
    try:
        import requests
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        df = pd.read_html(io.StringIO(r.text))[0]
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        pd.Series(tickers).to_csv(cache, index=False, header=False)
        return tickers
    except Exception as e:
        print(f"[警告] 攞成份股名單失敗（{e}），改用快取", file=sys.stderr)
        return pd.read_csv(cache, header=None)[0].tolist()


def fetch_prices(tickers):
    import yfinance as yf
    frames = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        raw = yf.download(chunk, period="2y", auto_adjust=True,
                          progress=False, threads=True)
        frames.append(raw["Close"])
    close = pd.concat(frames, axis=1)
    close = close.dropna(axis=1, how="all")
    return close


def fetch_all():
    import yfinance as yf
    stocks = fetch_prices(get_universe())
    idx = yf.download(["SPY", "QQQ", "^VIX"], period="2y", auto_adjust=True,
                      progress=False)["Close"]
    return stocks, idx["SPY"].dropna(), idx["QQQ"].dropna(), idx["^VIX"].dropna()


def demo_data(seed=7, n_stocks=480, n_days=560):
    """模擬數據：最後 3 日製造一次急跌，用嚟測試預警。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n_days)
    mkt = rng.normal(0.0004, 0.008, n_days)
    mkt[-3:] = [-0.012, -0.022, -0.030]
    idio = rng.normal(0, 0.014, (n_days, n_stocks))
    beta = rng.uniform(0.6, 1.6, n_stocks)
    ret = mkt[:, None] * beta + idio
    stocks = pd.DataFrame(100 * np.exp(np.cumsum(ret, axis=0)), index=dates,
                          columns=[f"S{i:03d}" for i in range(n_stocks)])
    spy = pd.Series(450 * np.exp(np.cumsum(mkt)), index=dates)
    qqq = pd.Series(380 * np.exp(np.cumsum(mkt * 1.3)), index=dates)
    vix = pd.Series(16 + np.cumsum(-mkt * 120) * 0.1, index=dates).clip(10, 60)
    vix.iloc[-3:] = [19, 24, 31]
    return stocks, spy, qqq, vix


# ---------- 指標 ----------
def compute_indicators(stocks, spy, qqq, vix):
    c = stocks.reindex(spy.index)
    ind = pd.DataFrame(index=spy.index)
    ind["vix5"] = vix.reindex(spy.index).rolling(5).mean()
    ind["idx50"] = ((spy / spy.rolling(50).mean() - 1) +
                    (qqq.reindex(spy.index) / qqq.reindex(spy.index).rolling(50).mean() - 1)) / 2 * 100
    for key, win in (("b50", 50), ("b200", 200)):
        ma = c.rolling(win).mean()
        ind[key] = (c > ma).sum(axis=1) / ma.notna().sum(axis=1).replace(0, np.nan) * 100
    ind["dist52"] = (c / c.rolling(252, min_periods=200).max() - 1).mean(axis=1) * 100
    ind["cross"] = (spy.rolling(50).mean() / spy.rolling(200).mean() - 1) * 100
    ind["vol21"] = np.log(spy).diff().rolling(21).std() * np.sqrt(252) * 100
    ind["spyhigh"] = (spy / spy.rolling(252, min_periods=200).max() - 1) * 100
    return ind.dropna()


def compute_levels(ind):
    lv = pd.DataFrame(index=ind.index)
    for key, (_, direction, y, r, *_rest) in config.INDICATORS.items():
        s = ind[key]
        if direction == "high":
            lv[key] = (s >= y).astype(int) + (s >= r).astype(int)
        else:
            lv[key] = (s < y).astype(int) + (s < r).astype(int)
    return lv


def breadth_counts(stocks, spy):
    ret = stocks.reindex(spy.index).pct_change(fill_method=None)
    up = (ret >= config.BIG_MOVE).sum(axis=1)
    dn = (ret <= -config.BIG_MOVE).sum(axis=1)
    out = pd.DataFrame({"up4": up, "dn4": dn})
    for n in (5, 10):
        out[f"r{n}"] = up.rolling(n).sum() / dn.rolling(n).sum().replace(0, np.nan)
    return out


def zone_of(score):
    z = config.ZONES[0]
    for item in config.ZONES:
        if score >= item[0]:
            z = item
    return z


# ---------- 預警 ----------
def decide_alert(df, state):
    """df: 有 score 欄嘅 DataFrame。返回 (訊息或 None)。"""
    last_date = str(df.index[-1].date())
    if state.get("last_date") == last_date:
        return None  # 同一日已經處理過
    score = int(df["score"].iloc[-1])
    prev = int(df["score"].iloc[-2]) if len(df) > 1 else score
    z_now, z_prev = zone_of(score), zone_of(prev)
    reasons = []
    if z_now[1] != z_prev[1]:
        arrow = "🔺" if score > prev else "🔻"
        reasons.append(f"{arrow} 由「{z_prev[1]}」轉入「{z_now[1]}」")
    if score - prev >= config.ALERT_JUMP:
        reasons.append(f"⚡ 單日急升 {score - prev} 分")
    if score >= config.ALERT_EVERY_DAY_FROM and not reasons:
        reasons.append("🚨 分數維持喺 Meltdown 區")
    if not reasons:
        return None
    return reasons


def send_telegram(text):
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    if not token or not chat:
        print("[提示] 未設定 TG_TOKEN / TG_CHAT_ID，跳過 Telegram")
        return False
    import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
    print("Telegram:", r.status_code)
    return r.ok


def build_message(df, lv, reasons, site_url):
    d = df.index[-1]
    score = int(df["score"].iloc[-1])
    prev = int(df["score"].iloc[-2])
    z = zone_of(score)
    red = [config.INDICATORS[k][0] for k in lv.columns if lv[k].iloc[-1] == 2]
    yel = [config.INDICATORS[k][0] for k in lv.columns if lv[k].iloc[-1] == 1]
    lines = [f"📊 美股風險監測 {d:%Y-%m-%d}", *reasons,
             f"Score {score}/16（{z[1]}），前日 {prev}"]
    if red:
        lines.append("🔴 " + "、".join(red))
    if yel:
        lines.append("🟡 " + "、".join(yel))
    if site_url:
        lines.append(site_url)
    lines.append("只係描述市況，唔係買賣建議。")
    return "\n".join(lines)


# ---------- 主程式 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args()

    stocks, spy, qqq, vix = demo_data() if args.demo else fetch_all()
    ind = compute_indicators(stocks, spy, qqq, vix)
    lv = compute_levels(ind)
    cnt = breadth_counts(stocks, spy).reindex(ind.index)

    df = ind.copy()
    df["score"] = lv.sum(axis=1)
    df["spy"], df["qqq"] = spy.reindex(df.index), qqq.reindex(df.index)
    df["spy_chg"] = spy.pct_change().reindex(df.index) * 100
    df["qqq_chg"] = qqq.pct_change().reindex(df.index) * 100
    df = df.join(cnt)

    state_file = DATA / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    reasons = decide_alert(df, state)

    now = datetime.now(timezone.utc)
    html = render_page(df, lv, now, n_stocks=stocks.shape[1])
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    df.tail(400).round(4).to_csv(DATA / "history.csv")

    if reasons and not args.no_alert and not args.demo:
        send_telegram(build_message(df, lv, reasons, os.getenv("SITE_URL", "")))
    elif reasons:
        print("[預警（未發送）]", reasons)
    if not args.demo:
        state_file.write_text(json.dumps({"last_date": str(df.index[-1].date()),
                                          "last_score": int(df["score"].iloc[-1])}))
    print(f"完成：{df.index[-1].date()} Score {int(df['score'].iloc[-1])}/16")


if __name__ == "__main__":
    main()
