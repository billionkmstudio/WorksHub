#!/usr/bin/env python3
"""Works Hub: daily US market-risk monitor updater.

Data: Polygon.io "grouped daily" (one request = every US stock for one day) + Cboe VIX history.
First run backfills about 400 trading days (about 90 minutes on Polygon's free rate limit);
later runs download only the days that are missing.

Environment:
  POLYGON_API_KEY   required
  WH_DATA           path to data.json   (default public/data.json)
  WH_STATE          state folder        (default state, not committed)
  WH_SLEEP          seconds between Polygon calls (default 12.5, i.e. 5 calls a minute)
"""
import io
import json
import os
import sys
import time
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

API = "https://api.polygon.io"
KEY = os.environ.get("POLYGON_API_KEY", "")
DATA_PATH = Path(os.environ.get("WH_DATA", "public/data.json"))
STATE_DIR = Path(os.environ.get("WH_STATE", "state"))
SLEEP = float(os.environ.get("WH_SLEEP", "12.5"))

ROWS_KEEP = 120          # rows shown in the daily record table
SCORE_HIST_KEEP = 260    # trading days of [date, score] kept for the 12-month score chart
HISTORY_DAYS = 560       # trading days kept: 252 warm-up + 252 for the "1 年前" comparison + slack
BENCH = ["SPY", "QQQ", "SOXX"]
MIN_PRICE = 5.0          # universe: close >= $5
MIN_AVG_VOL = 300_000    # universe: 20-day average volume >= 300k shares
STOCK_TYPES = ("CS",)    # common stock; add "ADRC" to include ADRs

# Yellow = 1 point, red = 2 points. These cut-offs were inferred so that the 18 days we could
# see on the reference site reproduce its published scores. Edit here if you want to tune them.
def f_vix(v):  return 2 if v >= 25 else 1 if v >= 17 else 0
def f_idx(v):  return 2 if v < -2.5 else 1 if v < 0 else 0
def f_p50(v):  return 2 if v < 30 else 1 if v < 50 else 0
def f_d52(v):  return 2 if v <= -20 else 1 if v <= -19 else 0
def f_p200(v): return 2 if v < 45 else 1 if v <= 61 else 0
def f_ma(v):   return 2 if v < 0 else 1 if v < 2 else 0
def f_vol(v):  return 2 if v >= 22 else 1 if v >= 14 else 0
def f_spx(v):  return 2 if v <= -8 else 1 if v <= -3 else 0
# row index -> rule (matches the layout of data.json monitor.rows)
FLAG_COLS = {6: f_vix, 7: f_idx, 8: f_p50, 9: f_d52, 10: f_p200, 11: f_ma, 12: f_vol, 13: f_spx}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Polygon / VIX
class NotAvailable(Exception):
    pass


def api_get(url, params=None, tries=6):
    for i in range(tries):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(15 * (i + 1))
            continue
        if r.status_code in (403, 401):
            raise NotAvailable(f"{r.status_code}: {r.text[:200]}")
        if r.status_code >= 500:
            time.sleep(5 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"giving up on {url}")


def fetch_grouped(d):
    j = api_get(f"{API}/v2/aggs/grouped/locale/us/market/stocks/{d:%Y-%m-%d}",
                {"adjusted": "true", "apiKey": KEY})
    res = j.get("results") or []
    if not res:
        return None  # weekend or market holiday
    df = pd.DataFrame(res)[["T", "c", "v"]].rename(columns={"T": "ticker", "c": "close", "v": "volume"})
    return df.drop_duplicates("ticker").set_index("ticker")


def fetch_universe():
    """Set of active common-stock tickers, refreshed monthly."""
    path = STATE_DIR / "universe.json"
    if path.exists():
        j = json.loads(path.read_text())
        if (dt.date.today() - dt.date.fromisoformat(j["date"])).days < 30:
            return set(j["tickers"])
    tickers = set()
    try:
        for typ in STOCK_TYPES:
            url, params = f"{API}/v3/reference/tickers", {
                "market": "stocks", "type": typ, "active": "true", "limit": 1000, "apiKey": KEY}
            while url:
                j = api_get(url, params)
                tickers.update(x["ticker"] for x in j.get("results", []))
                url = j.get("next_url")
                params = {"apiKey": KEY} if url else None
                time.sleep(SLEEP)
        path.write_text(json.dumps({"date": dt.date.today().isoformat(), "tickers": sorted(tickers)}))
        log(f"universe: {len(tickers)} common stocks")
    except Exception as e:  # fall back to the previous list, or a name pattern
        log("universe fetch failed:", e)
        if path.exists():
            return set(json.loads(path.read_text())["tickers"])
        return None
    return tickers


def fetch_vix():
    """VIX daily close as a Series indexed by date. Cboe first, FRED as fallback."""
    try:
        r = requests.get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip().upper() for c in df.columns]
        s = pd.Series(df["CLOSE"].values, index=pd.to_datetime(df["DATE"]))
        return s.sort_index().astype(float)
    except Exception as e:
        log("Cboe VIX failed, trying FRED:", e)
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS", timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["DATE", "CLOSE"]
        df["CLOSE"] = pd.to_numeric(df["CLOSE"], errors="coerce")
        return pd.Series(df["CLOSE"].values, index=pd.to_datetime(df["DATE"])).dropna().sort_index()


# ---------------------------------------------------------------- calculations
def compute_frame(closes, vols, vix, cs_set):
    """Return a DataFrame (index = date) with every field of one data.json row, plus n_univ."""
    c, v = closes, vols
    ret = c / c.shift(1) - 1

    def sma(n):
        return c.rolling(n, min_periods=n).mean()

    sma40, sma50, sma200 = sma(40), sma(50), sma(200)
    hi252 = c.rolling(252, min_periods=200).max()
    vol20 = v.rolling(20, min_periods=20).mean()

    cols = [t for t in c.columns if (cs_set is None and t.isalpha() and t.isupper() and len(t) <= 5)
            or (cs_set is not None and t in cs_set)]
    cols = [t for t in cols if t not in BENCH]
    U = (c[cols] >= MIN_PRICE) & (vol20[cols] >= MIN_AVG_VOL)

    def pct_above(s):
        valid = U & s[cols].notna()
        return ((c[cols] > s[cols]) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan) * 100

    out = pd.DataFrame(index=c.index)
    spy, qqq = c["SPY"], c["QQQ"]
    out["spy"], out["spy_chg"] = spy, (spy / spy.shift(1) - 1) * 100
    out["qqq"], out["qqq_chg"] = qqq, (qqq / qqq.shift(1) - 1) * 100
    out["vix5"] = vix.rolling(5, min_periods=5).mean().reindex(c.index, method="ffill")
    out["idx50"] = ((spy / spy.rolling(50, min_periods=50).mean() - 1) +
                    (qqq / qqq.rolling(50, min_periods=50).mean() - 1)) / 2 * 100
    out["p50"] = pct_above(sma50)
    out["d52"] = (c[cols] / hi252[cols] - 1).where(U).mean(axis=1) * 100
    out["p200"] = pct_above(sma200)
    out["ma"] = (spy.rolling(50, min_periods=50).mean() / spy.rolling(200, min_periods=200).mean() - 1) * 100
    out["vol21"] = np.log(spy / spy.shift(1)).rolling(21, min_periods=21).std() * np.sqrt(252) * 100
    out["spx"] = (spy / spy.rolling(252, min_periods=200).max() - 1) * 100
    up = ((ret[cols] >= 0.04) & U).sum(axis=1)
    dn = ((ret[cols] <= -0.04) & U).sum(axis=1)
    out["up"], out["dn"] = up, dn
    out["r5"] = up.rolling(5).sum() / dn.rolling(5).sum().replace(0, np.nan)
    out["r10"] = up.rolling(10).sum() / dn.rolling(10).sum().replace(0, np.nan)
    out["r40"] = pct_above(sma40)
    out["n_univ"] = U.sum(axis=1)
    return out.dropna()


def to_rows(frame):
    rows = []
    for ts, r in frame.iterrows():
        row = [ts.strftime("%Y-%m-%d"), 0,
               round(r.spy, 2), round(r.spy_chg, 1), round(r.qqq, 2), round(r.qqq_chg, 1),
               round(r.vix5, 2), round(r.idx50, 2), round(r.p50, 1), round(r.d52, 2), round(r.p200, 1),
               round(r.ma, 2), round(r.vol21, 1), round(r.spx, 2),
               int(r.up), int(r.dn), round(r.r5, 2), round(r.r10, 2), round(r.r40, 1)]
        row = [x.item() if hasattr(x, "item") else x for x in row]
        row[1] = sum(f(row[k]) for k, f in FLAG_COLS.items())
        rows.append(row)
    rows.reverse()  # newest first
    return rows


def status_of(score):
    return "Meltdown" if score >= 10 else "警示" if score >= 5 else "健康"


def update_data(data, rows, closes, n_univ, now_utc):
    m = data["monitor"]
    m["rows"] = rows[:ROWS_KEEP]
    # compact score-only history (oldest first) for the 12-month line chart; rows is newest first
    m["scoreHistory"] = [[r[0], r[1]] for r in reversed(rows[:SCORE_HIST_KEEP])]
    latest, prev = rows[0], rows[1] if len(rows) > 1 else rows[0]
    score = latest[1]
    flags = [f(latest[k]) for k, f in FLAG_COLS.items()]
    reds, yellows = flags.count(2), flags.count(1)
    diff = score - prev[1]
    word = "升" if diff > 0 else "降" if diff < 0 else "持平"
    m.update({
        "date": latest[0], "score": score, "max": 16, "status": status_of(score),
        "summary": ("較前日" + (f"{word} {abs(diff)} 分：" if diff else "持平：") +
                    f"{reds} 個紅燈、{yellows} 個黃燈。"),
        "source": "本站自行計算",
        "notice": (f"本站自行計算：Polygon 每日收市價，股票池為美國普通股（收市 ≥ $5、20 日平均成交 ≥ 30 萬股），"
                   f"最新一日共 {n_univ} 隻。黃紅界線為推算值，與其他網站的分數可能不同。"),
    })
    # comparisons
    dates = [pd.Timestamp(r[0]) for r in rows]
    t = dates[0]

    def at_or_before(ts):
        for d, r in zip(dates, rows):
            if d <= ts:
                return r
        return None
    comp = [{"label": "前日", "date": prev[0], "score": prev[1]}]
    for label, back in (("1 週前", pd.Timedelta(days=7)), ("1 個月前", pd.DateOffset(months=1)),
                        ("1 年前", pd.DateOffset(years=1))):
        goal = t - back
        row = at_or_before(goal)
        # only use a row that is close to the target day (we keep about 6 months, so 1 年前 appears later)
        if row is not None and (goal - pd.Timestamp(row[0])).days <= 5:
            comp.append({"label": label, "date": row[0], "score": row[1]})
    m["compare"] = comp
    # ETF strip
    etf = []
    for sym, name in (("SPY", "標普 500"), ("QQQ", "納指 100"), ("SOXX", "半導體")):
        if sym in closes.columns and closes[sym].notna().sum() >= 2:
            s = closes[sym].dropna()
            etf.append({"n": sym, "d": name, "p": round(float(s.iloc[-1]), 1),
                        "c": round(float((s.iloc[-1] / s.iloc[-2] - 1) * 100), 2)})
    m["etf"] = etf
    hk, uk = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong")), now_utc.astimezone(ZoneInfo("Europe/London"))
    m["stamp"] = f"更新：香港時間 {hk.month} 月 {hk.day} 日 {hk:%H:%M} · 英國時間 {uk.month} 月 {uk.day} 日 {uk:%H:%M}"
    m.setdefault("experimentalSource", "參考數據：cashmate.hk（非本站計算）")
    data["updated"] = uk.strftime("%Y-%m-%dT%H:%M")
    return data


# ---------------------------------------------------------------- state + main
def load_state():
    p = STATE_DIR / "state.pkl.gz"
    if p.exists():
        return pd.read_pickle(p)
    return {"closes": pd.DataFrame(), "vols": pd.DataFrame()}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    st["closes"] = st["closes"].tail(HISTORY_DAYS)
    st["vols"] = st["vols"].tail(HISTORY_DAYS)
    pd.to_pickle(st, STATE_DIR / "state.pkl.gz")


def latest_complete_day(now_utc):
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    d = ny.date() if ny.hour >= 20 else ny.date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def main():
    if not KEY:
        sys.exit("POLYGON_API_KEY is not set")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    st = load_state()
    closes, vols = st["closes"], st["vols"]
    target = latest_complete_day(now)
    if closes.empty:
        start = target - dt.timedelta(days=int(HISTORY_DAYS * 1.45))
        log(f"first run: backfilling {start} to {target}")
    else:
        start = closes.index.max().date() + dt.timedelta(days=1)

    new_c, new_v, calls = {}, {}, 0
    d = start
    try:
        while d <= target:
            if d.weekday() < 5:
                df = fetch_grouped(d)
                calls += 1
                if df is not None:
                    new_c[pd.Timestamp(d)], new_v[pd.Timestamp(d)] = df["close"], df["volume"]
                    log(f"{d}: {len(df)} tickers")
                time.sleep(SLEEP)
                if calls % 40 == 0 and new_c:  # checkpoint so a cancelled run can resume
                    closes = pd.concat([closes, pd.DataFrame(new_c).T]).sort_index()
                    vols = pd.concat([vols, pd.DataFrame(new_v).T]).sort_index()
                    new_c, new_v = {}, {}
                    save_state({"closes": closes, "vols": vols})
            d += dt.timedelta(days=1)
    except NotAvailable as e:
        log(f"data for {d} not available yet ({e}); using what we have")
    if new_c:
        closes = pd.concat([closes, pd.DataFrame(new_c).T]).sort_index()
        vols = pd.concat([vols, pd.DataFrame(new_v).T]).sort_index()
    # older history: extend backwards when the saved history is shorter than HISTORY_DAYS (one-off)
    need_start = target - dt.timedelta(days=int(HISTORY_DAYS * 1.45))
    if not closes.empty and len(closes) < HISTORY_DAYS and closes.index.min().date() > need_start + dt.timedelta(days=12):
        old_c, old_v, calls = {}, {}, 0
        d = need_start
        log(f"extending history back to {need_start}")
        try:
            while d < closes.index.min().date():
                if d.weekday() < 5:
                    df = fetch_grouped(d)
                    calls += 1
                    if df is not None:
                        old_c[pd.Timestamp(d)], old_v[pd.Timestamp(d)] = df["close"], df["volume"]
                        log(f"{d}: {len(df)} tickers")
                    time.sleep(SLEEP)
                    if calls % 40 == 0 and old_c:
                        closes = pd.concat([pd.DataFrame(old_c).T, closes]).sort_index()
                        vols = pd.concat([pd.DataFrame(old_v).T, vols]).sort_index()
                        old_c, old_v = {}, {}
                        save_state({"closes": closes, "vols": vols})
                d += dt.timedelta(days=1)
        except NotAvailable as e:
            log(f"older data for {d} not available ({e}); skipping the rest")
        if old_c:
            closes = pd.concat([pd.DataFrame(old_c).T, closes]).sort_index()
            vols = pd.concat([pd.DataFrame(old_v).T, vols]).sort_index()
    closes = closes[~closes.index.duplicated(keep="last")]
    vols = vols[~vols.index.duplicated(keep="last")]
    save_state({"closes": closes, "vols": vols})
    if closes.empty:
        sys.exit("no price data")

    cs_set = fetch_universe()
    vix = fetch_vix()
    frame = compute_frame(closes.tail(HISTORY_DAYS), vols.tail(HISTORY_DAYS), vix, cs_set)
    if frame.empty:
        sys.exit("not enough history to compute indicators yet")
    rows = to_rows(frame)  # all rows; update_data keeps ROWS_KEEP for the table but compares against the full history
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data = update_data(data, rows, closes, int(frame["n_univ"].iloc[-1]), now)
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"done: {rows[0][0]} score {rows[0][1]}/16, {len(rows)} rows")


if __name__ == "__main__":
    main()
