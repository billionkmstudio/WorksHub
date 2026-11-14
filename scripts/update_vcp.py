#!/usr/bin/env python3
"""Works Hub: daily VCP (volatility contraction pattern) scanner.

Scans every liquid US common stock and writes at most MAX_SHOW names a day:
  green  = all 7 conditions true today (close crosses above the pivot on volume)
  orange = the 5 set-up conditions true, waiting for the breakout
The rules and default numbers are the same as the TradingView script (vcp_breakout.pine).

Data: Polygon.io "grouped daily" (one request = every US stock for one day), same as update_monitor.py.
First run backfills about 320 trading days (about 70 minutes on Polygon's free rate limit);
later runs download only the days that are missing. Prices are stored in their own state folder,
separate from the market monitor, because this scan also needs daily highs and lows.

Environment:
  POLYGON_API_KEY   required
  WH_VCP_DIR        output folder        (default public/vcp)
  WH_VCP_STATE      state folder         (default state_vcp, not committed)
  WH_SLEEP          seconds between Polygon calls (default 12.5, i.e. 5 calls a minute)
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from update_monitor import API, KEY, SLEEP, NotAvailable, api_get, latest_complete_day, log

OUT_DIR = Path(os.environ.get("WH_VCP_DIR", "public/vcp"))
STATE_DIR = Path(os.environ.get("WH_VCP_STATE", "state_vcp"))

HISTORY_DAYS = 320       # trading days kept (252 for the 52-week numbers and 1-year return, plus slack)
KEEP_DAYS = 14           # daily result files kept; the page shows the latest 7
MAX_SHOW = 10            # at most this many names a day (green first, then orange closest to the pivot)
BENCH = "SPY"            # relative strength is measured against this
MIN_PRICE = 5.0          # skip stocks under $5
STOCK_TYPES = ("CS",)    # common stock; add "ADRC" to include ADRs

# ---- rule numbers: keep in step with vcp_breakout.pine ----
SEG_LEN = 15             # days in each of the three segments
MIN_FIRST_DEPTH = 10.0   # first segment range, at least (%)
MAX_LAST_DEPTH = 12.0    # last segment range, at most (%)
CONTRACT_RATIO = 0.7     # last segment range / first segment range, at most
STRICT_STEP = False      # True = each segment must be tighter than the one before (the original article)
VOL_DRY_RATIO = 1.0      # 10-day / 50-day average volume before the breakout, below this
VOL_BREAK = 1.3          # breakout-day volume / 50-day average, above this
MIN_DOLLAR_M = 20.0      # 50-day average dollar volume, at least (million USD)
LOW_MULT = 1.3           # close at least this multiple of the 52-week low
HIGH_PCT = 25.0          # close within this % of the 52-week high
MA200_UP_BARS = 20       # 200-day average must be higher than this many days ago
RS_LEN = 252             # days for the return comparison against BENCH
RS_MIN_DIFF = 0.0        # stock return must beat BENCH by at least this (%)

FIELDS = (("h", "h"), ("l", "l"), ("c", "c"), ("v", "v"))


# ---------------------------------------------------------------- Polygon
def fetch_grouped_ohlcv(d):
    j = api_get(f"{API}/v2/aggs/grouped/locale/us/market/stocks/{d:%Y-%m-%d}",
                {"adjusted": "true", "apiKey": KEY})
    res = j.get("results") or []
    if not res:
        return None  # weekend or market holiday
    df = pd.DataFrame(res)[["T", "h", "l", "c", "v"]].drop_duplicates("T").set_index("T")
    return df.astype("float32")


def fetch_names():
    """{ticker: company name} for active common stocks, refreshed monthly."""
    path = STATE_DIR / "names.json"
    if path.exists():
        j = json.loads(path.read_text(encoding="utf-8"))
        if (dt.date.today() - dt.date.fromisoformat(j["date"])).days < 30:
            return j["names"]
    names = {}
    try:
        for typ in STOCK_TYPES:
            url, params = f"{API}/v3/reference/tickers", {
                "market": "stocks", "type": typ, "active": "true", "limit": 1000, "apiKey": KEY}
            while url:
                j = api_get(url, params)
                for x in j.get("results", []):
                    names[x["ticker"]] = x.get("name") or ""
                url = j.get("next_url")
                params = {"apiKey": KEY} if url else None
                time.sleep(SLEEP)
        if not names:
            raise RuntimeError("empty ticker list")
        path.write_text(json.dumps({"date": dt.date.today().isoformat(), "names": names}, ensure_ascii=False),
                        encoding="utf-8")
        log(f"names: {len(names)} common stocks")
    except Exception as e:  # fall back to the previous list
        log("name list fetch failed:", e)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["names"]
        return None
    return names


# ---------------------------------------------------------------- state
def load_state():
    p = STATE_DIR / "state.pkl.gz"
    if p.exists():
        return pd.read_pickle(p)
    return {k: pd.DataFrame() for k, _ in FIELDS}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for k, _ in FIELDS:
        st[k] = st[k].tail(HISTORY_DAYS)
    pd.to_pickle(st, STATE_DIR / "state.pkl.gz")


def merge(st, new):
    """Append {date: DataFrame(h,l,c,v)} to the wide per-field frames."""
    if not new:
        return st
    for k, col in FIELDS:
        add = pd.DataFrame({d: df[col] for d, df in new.items()}).T
        st[k] = pd.concat([st[k], add]).sort_index()
        st[k] = st[k][~st[k].index.duplicated(keep="last")]
    return st


def update_prices(st, target):
    if st["c"].empty:
        start = target - dt.timedelta(days=int(HISTORY_DAYS * 1.47))
        log(f"first run: backfilling {start} to {target}")
    else:
        start = st["c"].index.max().date() + dt.timedelta(days=1)
    new, calls, d = {}, 0, start
    try:
        while d <= target:
            if d.weekday() < 5:
                df = fetch_grouped_ohlcv(d)
                calls += 1
                if df is not None:
                    new[pd.Timestamp(d)] = df
                    log(f"{d}: {len(df)} tickers")
                time.sleep(SLEEP)
                if calls % 40 == 0 and new:  # checkpoint so a cancelled run can resume
                    st = merge(st, new)
                    new = {}
                    save_state(st)
            d += dt.timedelta(days=1)
    except NotAvailable as e:
        log(f"data for {d} not available yet ({e}); using what we have")
    st = merge(st, new)
    save_state(st)
    return st


# ---------------------------------------------------------------- the scan
def scan(st, names):
    """Returns (data_date, scanned_count, DataFrame of the latest day's conditions, one row per stock)."""
    h, l, c, v = (st[k].astype("float64") for k, _ in FIELDS)
    if len(c) < RS_LEN + 2:
        return None, 0, pd.DataFrame()
    last = c.index[-1]

    # stocks to scan: common stock, traded on the latest day, full history, no split-like jump in stored prices
    cols = c.columns[c.iloc[-1].notna()]
    if names:
        cols = cols.intersection(pd.Index(list(names)))
    cols = cols[c[cols].tail(RS_LEN + 1).notna().all().values]
    ratio = c[cols] / c[cols].shift(1)
    jump = ((ratio > 1.8) | (ratio < 0.55)).any()
    cols = cols[~jump.values]
    cols = cols[(c.loc[last, cols] >= MIN_PRICE).values]
    h, l, c, v = h[cols], l[cols], c[cols], v[cols]
    seg = SEG_LEN

    ma50, ma150, ma200 = c.rolling(50).mean(), c.rolling(150).mean(), c.rolling(200).mean()
    low52, high52 = l.rolling(252).min(), h.rolling(252).max()
    trend = ((c > ma50) & (c > ma150) & (c > ma200) & (ma50 > ma150) & (ma50 > ma200) & (ma150 > ma200)
             & (ma200 > ma200.shift(MA200_UP_BARS)) & (c >= low52 * LOW_MULT) & (c >= high52 * (1 - HIGH_PCT / 100)))

    # three segments, oldest (1) to newest (3), none of them includes the current day
    h3, l3 = h.shift(1).rolling(seg).max(), l.shift(1).rolling(seg).min()
    h2, l2 = h.shift(seg + 1).rolling(seg).max(), l.shift(seg + 1).rolling(seg).min()
    h1, l1 = h.shift(2 * seg + 1).rolling(seg).max(), l.shift(2 * seg + 1).rolling(seg).min()
    d1, d2, d3 = (h1 - l1) / h1 * 100, (h2 - l2) / h2 * 100, (h3 - l3) / h3 * 100
    contract = (d1 >= MIN_FIRST_DEPTH) & (d3 <= MAX_LAST_DEPTH) & (d3 <= d1 * CONTRACT_RATIO) & (d2 <= d1)
    if STRICT_STEP:
        contract &= (d1 > d2) & (d2 > d3)

    avg10, avg50 = v.shift(1).rolling(10).mean(), v.shift(1).rolling(50).mean()
    vol_ratio = avg10 / avg50
    vol_dry = vol_ratio < VOL_DRY_RATIO
    break_vol = v / avg50
    vol_break = break_vol > VOL_BREAK
    liq = (c * v).rolling(50).mean() >= MIN_DOLLAR_M * 1e6

    stk_roc = (c / c.shift(RS_LEN) - 1) * 100
    bench = st["c"][BENCH].astype("float64") if BENCH in st["c"].columns else None
    idx_roc = float((bench.iloc[-1] / bench.shift(RS_LEN).iloc[-1] - 1) * 100) if bench is not None else float("nan")
    if np.isnan(idx_roc):
        log(f"no {BENCH} history: relative-strength filter is off for this run")
        rs = pd.Series(True, index=cols)
    else:
        rs = stk_roc.iloc[-1] >= idx_roc + RS_MIN_DIFF

    pivot = h3
    cross_up = (c > pivot) & (c.shift(1) <= pivot.shift(1))

    t = lambda f: f.iloc[-1]  # noqa: E731  latest day only
    out = pd.DataFrame({
        "px": t(c), "chg": (t(c) / c.iloc[-2] - 1) * 100, "pv": t(pivot),
        "d1": t(d1), "d2": t(d2), "d3": t(d3), "vr": t(vol_ratio), "bv": t(break_vol), "roc": t(stk_roc),
        "trend": t(trend), "contract": t(contract), "vol_dry": t(vol_dry), "rs": rs, "liq": t(liq),
        "cross_up": t(cross_up), "vol_break": t(vol_break),
    })
    out["setup"] = out["trend"] & out["contract"] & out["vol_dry"] & out["rs"] & out["liq"]
    out["signal"] = out["setup"] & out["cross_up"] & out["vol_break"]
    out["gap"] = (out["pv"] / out["px"] - 1) * 100   # how far the pivot is above the close (%)
    out.attrs["idx_roc"] = idx_roc
    return last.date(), len(cols), out


def pick(out, names):
    """Green first (biggest volume surge first), then orange (closest to the pivot first); MAX_SHOW in total."""
    green = out[out["signal"]].sort_values("bv", ascending=False)
    orange = out[out["setup"] & ~out["signal"]].sort_values("gap")
    items = []
    for st_code, df in (("g", green), ("o", orange)):
        for tk, r in df.iterrows():
            if len(items) >= MAX_SHOW:
                break
            items.append({
                "t": tk, "n": (names or {}).get(tk, ""), "st": st_code,
                "px": round(float(r["px"]), 2), "chg": round(float(r["chg"]), 2),
                "pv": round(float(r["pv"]), 2), "gap": round(float(r["gap"]), 2),
                "d": [round(float(r["d1"]), 1), round(float(r["d2"]), 1), round(float(r["d3"]), 1)],
                "vr": round(float(r["vr"]), 2), "bv": round(float(r["bv"]), 2), "roc": round(float(r["roc"]), 1),
            })
    return items, len(green), len(orange)


def write_result(data_date, scanned, out, names, now_utc):
    items, n_green, n_orange = pick(out, names)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ds = data_date.isoformat()
    idx_roc = out.attrs.get("idx_roc")
    res = {
        "date": ds, "updated": now_utc.strftime("%Y-%m-%d %H:%M UTC"), "scanned": scanned,
        "green": n_green, "orange": n_orange, "max": MAX_SHOW, "bench": BENCH,
        "benchRoc": None if idx_roc is None or np.isnan(idx_roc) else round(idx_roc, 1),
        "params": {"seg": SEG_LEN, "first": MIN_FIRST_DEPTH, "last": MAX_LAST_DEPTH, "ratio": CONTRACT_RATIO,
                   "dry": VOL_DRY_RATIO, "brk": VOL_BREAK, "dollarM": MIN_DOLLAR_M},
        "items": items,
    }
    (OUT_DIR / f"{ds}.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    idx = OUT_DIR / "index.json"
    dates = []
    if idx.exists():
        try:
            dates = json.loads(idx.read_text(encoding="utf-8")).get("dates", [])
        except Exception:
            dates = []
    dates = sorted(set(dates + [ds]))[-KEEP_DAYS:]
    idx.write_text(json.dumps({"dates": dates}, ensure_ascii=False), encoding="utf-8")
    for p in OUT_DIR.glob("*.json"):  # drop files older than the ones we keep
        if p.name != "index.json" and len(p.stem) == 10 and p.stem not in dates:
            p.unlink()
    log(f"done: {ds} scanned {scanned}, green {n_green}, orange {n_orange}, shown {len(items)}")


def main():
    if not KEY:
        sys.exit("POLYGON_API_KEY is not set")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    st = update_prices(load_state(), latest_complete_day(now))
    if st["c"].empty:
        sys.exit("no price data")
    names = fetch_names()
    data_date, scanned, out = scan(st, names)
    if data_date is None:
        sys.exit("not enough history to scan yet")
    write_result(data_date, scanned, out, names, now)


if __name__ == "__main__":
    main()
