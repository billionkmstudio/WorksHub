#!/usr/bin/env python3
"""Works Hub: self-computed historical statistics for the three lights (早期跌 / 見底 / 升浪確認).

Everything here is defined and computed by this site from one input only: the S&P 500 index daily close.
Long history (from 1950) is used so that each light has many more past signals to learn from.

Data: Yahoo Finance chart API -> Stooq -> FRED (first one that works), merged into the cache
      data/spx_history.csv (committed, so a failed download never loses history).

Environment:
  WH_DATA    path to data.json   (default public/data.json)
  WH_CACHE   path to the cache   (default data/spx_history.csv)
"""
import io
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA_PATH = Path(os.environ.get("WH_DATA", "public/data.json"))
CACHE = Path(os.environ.get("WH_CACHE", "data/spx_history.csv"))
UA = {"User-Agent": "Mozilla/5.0 (compatible; WorksHubBot/1.0)"}

HORIZON_DROP = 120      # 早期跌 observation window (trading days, about 6 months)
HORIZON_1Y = 252        # one year
WARN_DAYS = 120         # 早期跌 警戒期 length
LIT_RECENT = 20         # 見底 / 升浪確認 count as "亮燈" for this many trading days after the signal


# ------------------------------------------------------------------ data
def fetch_yahoo():
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
           f"?period1=-631152000&period2={int(time.time())}&interval=1d&events=history")
    j = requests.get(url, headers=UA, timeout=60).json()
    r = j["chart"]["result"][0]
    s = pd.Series(r["indicators"]["quote"][0]["close"],
                  index=pd.to_datetime(r["timestamp"], unit="s").normalize(), dtype="float64")
    return s.dropna()


def fetch_stooq():
    txt = requests.get("https://stooq.com/q/d/l/?s=%5Espx&i=d", headers=UA, timeout=60).text
    df = pd.read_csv(io.StringIO(txt), parse_dates=["Date"])
    return df.set_index("Date")["Close"].astype("float64").dropna()


def fetch_fred():
    txt = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500", headers=UA, timeout=60).text
    df = pd.read_csv(io.StringIO(txt))
    df.columns = ["Date", "Close"]
    df["Date"] = pd.to_datetime(df["Date"])
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    return df.set_index("Date")["Close"].dropna()


def load_history():
    cached = pd.Series(dtype="float64")
    if CACHE.exists():
        df = pd.read_csv(CACHE, parse_dates=["Date"])
        cached = df.set_index("Date")["Close"].astype("float64")
    got, used = None, None
    for name, fn in (("Yahoo", fetch_yahoo), ("Stooq", fetch_stooq), ("FRED", fetch_fred)):
        try:
            s = fn()
            if len(s) > 1000 or (len(s) > 200 and name == "FRED"):
                got, used = s, name
                break
            print(f"{name}: too few rows ({len(s)}), skipped")
        except Exception as e:  # noqa: BLE001 - any failure just means try the next source
            print(f"{name}: failed ({type(e).__name__}: {e})")
    if got is None:
        if cached.empty:
            sys.exit("no data source reachable and no cache")
        print("all sources failed; using cache only")
        s = cached
    else:
        print(f"source: {used}, {len(got)} rows, {got.index[0].date()} .. {got.index[-1].date()}")
        # the newest download wins on overlapping days; older cached days are kept
        s = pd.concat([cached, got])
        s = s[~s.index.duplicated(keep="last")]
    s = s.sort_index()
    s = s[s > 0]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    s.rename("Close").rename_axis("Date").to_csv(CACHE, float_format="%.2f")
    return s


# ------------------------------------------------------------------ signals
def pick_events(mask, cooldown):
    """First True, then ignore further True days until `cooldown` trading days have passed."""
    out, last = [], -10 ** 9
    for i in np.flatnonzero(mask.fillna(False).to_numpy()):
        if i - last >= cooldown:
            out.append(int(i))
            last = int(i)
    return out


def build(close: pd.Series):
    s = close
    ma20, ma50, ma200 = (s.rolling(w).mean() for w in (20, 50, 200))
    hi252 = s.rolling(252).max()
    dd = s / hi252 - 1
    prev = lambda x, k=1: x.shift(k)  # noqa: E731

    # forward outcomes (NaN where the future is not fully known yet)
    fmin = s.shift(-1)[::-1].rolling(HORIZON_DROP).min()[::-1] / s - 1
    ret = {h: s.shift(-h) / s - 1 for h in (60, 120, 252)}

    below200 = s < ma200
    below20 = s <= ma20
    streak10 = prev(below20).rolling(10).sum() == 10

    lights = {}

    # 早期跌: still above the 200-day line, but price falls X% below its 1-year high for the first time
    pre = s > ma200
    lights["drop"] = dict(
        pre_main=pre, pre_sub=pre,
        main=pre & (dd <= -0.05) & (prev(dd) > -0.05), cd_main=WARN_DAYS,
        sub=pre & (dd <= -0.03) & (prev(dd) > -0.03), cd_sub=60)

    # 見底: after a deep fall (>=15% / >=10% below 1-year high within 30 days), first close above 20-day line
    # after 10 straight closes below it
    pre_m = dd.rolling(30).min() <= -0.15
    pre_s = dd.rolling(30).min() <= -0.10
    lights["bottom"] = dict(
        pre_main=pre_m, pre_sub=pre_s,
        main=pre_m & (s > ma20) & streak10, cd_main=60,
        sub=pre_s & (s > ma20) & streak10, cd_sub=60)

    # 升浪確認: closes back above the 200-day line after a weak spell
    weak60 = prev(below200).rolling(60).sum() >= 30
    weak40 = prev(below200).rolling(40).sum() >= 15
    cross = prev(below200) & (s > ma200)
    lights["rise"] = dict(
        pre_main=weak60, pre_sub=weak40,
        main=weak60 & cross & (ma50 > prev(ma50, 10)), cd_main=WARN_DAYS,
        sub=weak40 & cross, cd_sub=60)

    return s, fmin, ret, lights


# ------------------------------------------------------------------ stats
def pct(x):
    return "—" if x is None or np.isnan(x) else f"{x * 100:.0f}%"


def sret(x):
    return "—" if x is None or np.isnan(x) else f"{x * 100:+.1f}%"


def mean_ok(series, idx):
    v = series.iloc[idx].dropna()
    return (float(v.mean()), len(v)) if len(v) else (float("nan"), 0)


def prob_ok(series, idx, thr):
    v = series.iloc[idx].dropna()
    return (float((v <= thr).mean()), int((v <= thr).sum()), len(v)) if len(v) else (float("nan"), 0, 0)


def recent_dates(s, idx, k=6):
    return [s.index[i].strftime("%Y-%m-%d") for i in idx[-k:]][::-1]


def status(s, idx, kind):
    if not idx:
        return False, "未亮"
    since = len(s) - 1 - idx[-1]
    if kind == "drop":
        return (since < WARN_DAYS), (f"警戒期 · 第 {since + 1}／{WARN_DAYS} 日" if since < WARN_DAYS else "未亮")
    return (since < LIT_RECENT), (f"亮燈 · {since} 日前" if since < LIT_RECENT else "未亮")


def compute(close):
    s, fmin, ret, L = build(close)
    out = []

    # ---- 早期跌
    d = L["drop"]
    em, es = pick_events(d["main"], d["cd_main"]), pick_events(d["sub"], d["cd_sub"])
    p5, k5, n5 = prob_ok(fmin, em, -0.05)
    p10, _, _ = prob_ok(fmin, em, -0.10)
    ps5, _, ns5 = prob_ok(fmin, es, -0.05)
    base = fmin[d["pre_main"]].dropna()
    b5, b10 = float((base <= -0.05).mean()), float((base <= -0.10).mean())
    on, pill = status(s, em, "drop")
    sub_false = ns5 - int(round(ps5 * ns5)) if ns5 else 0
    out.append({
        "name": "早期跌", "color": "bad", "pill": pill, "pillOn": on,
        "big": f"{k5}／{n5} 次", "bigNote": "之後半年內再跌 5% 以上",
        "rows": [["半年內再跌 ≥5%", pct(p5), "", pct(b5)],
                 ["半年內再跌 ≥10%", pct(p10), "", pct(b10)],
                 ["副燈（空心）", pct(ps5), f"{ns5} 次，再跌 ≥5%", pct(b5)]],
        "false": f"假警號：主燈 {n5 - k5}／{n5} 次 · 副燈 {sub_false}／{ns5} 次",
        "recent": recent_dates(s, em)})

    # ---- 見底
    d = L["bottom"]
    em, es = pick_events(d["main"], d["cd_main"]), pick_events(d["sub"], d["cd_sub"])
    a60, _ = mean_ok(ret[60], em)
    a252, n252 = mean_ok(ret[252], em)
    up252 = int((ret[252].iloc[em].dropna() > 0).sum())
    sa60, sn60 = mean_ok(ret[60], es)
    sfalse = int((ret[60].iloc[es].dropna() <= 0).sum())
    b60, b252 = float(ret[60][d["pre_main"]].mean()), float(ret[252][d["pre_main"]].mean())
    bs60 = float(ret[60][d["pre_sub"]].mean())
    on, pill = status(s, em, "bottom")
    out.append({
        "name": "見底", "color": "good", "pill": pill, "pillOn": on,
        "big": f"{up252}／{n252} 次", "bigNote": "一年後標普升",
        "rows": [["60 日後（平均）", sret(a60), "", sret(b60)],
                 ["一年後（平均）", sret(a252), "", sret(b252)],
                 ["副燈（空心）", sret(sa60), f"{sn60} 次，60 日後", sret(bs60)]],
        "false": f"假警號：主燈 {n252 - up252}／{n252} 次 · 副燈 {sfalse}／{sn60} 次",
        "recent": recent_dates(s, em)})

    # ---- 升浪確認
    d = L["rise"]
    em, es = pick_events(d["main"], d["cd_main"]), pick_events(d["sub"], d["cd_sub"])
    a120, _ = mean_ok(ret[120], em)
    a252, n252 = mean_ok(ret[252], em)
    up252 = int((ret[252].iloc[em].dropna() > 0).sum())
    sa120, sn120 = mean_ok(ret[120], es)
    sfalse = int((ret[120].iloc[es].dropna() <= 0).sum())
    b120, b252 = float(ret[120][d["pre_main"]].mean()), float(ret[252][d["pre_main"]].mean())
    bs120 = float(ret[120][d["pre_sub"]].mean())
    on, pill = status(s, em, "rise")
    out.append({
        "name": "升浪確認", "color": "accent", "pill": pill, "pillOn": on,
        "big": f"{up252}／{n252} 次", "bigNote": "一年後標普升",
        "rows": [["120 日後（平均）", sret(a120), "", sret(b120)],
                 ["一年後（平均）", sret(a252), "", sret(b252)],
                 ["副燈（空心）", sret(sa120), f"{sn120} 次，120 日後", sret(bs120)]],
        "false": f"假警號：主燈 {n252 - up252}／{n252} 次 · 副燈 {sfalse}／{sn120} 次",
        "recent": recent_dates(s, em)})
    return out


META_DEFS = [
    {"name": "早期跌",
     "main": "標普 500 仍在 200 日平均線之上，但收市價首次較過去一年高位低 5%。亮燈後 120 個交易日為警戒期。",
     "sub": "同上，但門檻放寬到低 3%（兩次亮燈至少相隔 60 日）。",
     "result": "亮燈日之後 120 個交易日內，收市價有沒有比亮燈日再低 5% / 10% 或以上。",
     "base": "對比 = 所有同樣處於 200 日線之上的日子。"},
    {"name": "見底",
     "main": "近 30 日內曾較一年高位低 15% 或以上，之後連續 10 日收市低於 20 日平均線，然後首次收在 20 日線之上。",
     "sub": "同上，但深度門檻放寬到 10%。",
     "result": "亮燈日之後 60 日和一年的標普表現；一年後高於亮燈日就算「升」。",
     "base": "對比 = 所有同樣處於深度回落（近 30 日曾低於高位 15% / 10%）的日子。"},
    {"name": "升浪確認",
     "main": "過去 60 日有 30 日或以上低於 200 日線，之後收市升穿 200 日線，而且 50 日線正在上升。",
     "sub": "過去 40 日有 15 日或以上低於 200 日線，之後收市升穿 200 日線（不要求 50 日線上升）。",
     "result": "亮燈日之後 120 日和一年的標普表現；一年後高於亮燈日就算「升」。",
     "base": "對比 = 所有同樣剛經歷過弱勢期（低於 200 日線的日子偏多）的日子。"},
]


def main():
    close = load_history()
    lights = compute(close)
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    m = data["monitor"]
    m["experimental"] = lights
    first, last = close.index[0].strftime("%Y"), close.index[-1].strftime("%Y-%m-%d")
    m["experimentalSource"] = f"本站自行計算：標普 500 指數 {first} 年至 {last} 的每日收市價，定義見下方說明"
    m["lightsMeta"] = {"from": close.index[0].strftime("%Y-%m-%d"), "to": last, "days": int(len(close)),
                       "defs": META_DEFS}
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for e in lights:
        print(e["name"], e["pill"], e["big"], e["rows"], e["false"])


if __name__ == "__main__":
    main()
