#!/usr/bin/env python3
"""Does the 9th indicator (SMH vs its 50-day average) add information, or only raise the score?

Long-history test (SPY / QQQ / SMH / VIX daily, about 2000 to today), using only the five thermometer
indicators that can be rebuilt from index prices and VIX:
  1 VIX 5-day average, 2 SPY+QQQ vs 50-day average, 6 50-day vs 200-day, 7 21-day volatility, 8 SPY vs 52-week high
The breadth indicators (3, 4, 5) need every US stock's history and cannot be rebuilt this far back.

  score5 = those five indicators          score6 = score5 + SMH vs 50-day average

The cut-offs are the ones the site already uses (update_monitor.f_*), nothing is re-fitted here.
Outcome: does SPY fall 5% / 10% or more from that day's close within the next 60 trading days.
Because a higher score alone would flag more days, versions are compared at about the same share of flagged
days, and also by AUC (ranking quality, independent of any cut-off).

Output: reports/indicator-compare.md (and .json). Windows overlap heavily, so read the numbers as descriptive,
not as a significance test.
"""
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_monitor as um  # noqa: E402  (same flag functions as the live site)

OUT = Path("reports")
UA = {"User-Agent": "Mozilla/5.0 (compatible; WorksHubBot/1.0)"}
HORIZON = 60


# ------------------------------------------------------------------ data
def yahoo(ticker):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1=725846400&period2={int(time.time())}&interval=1d&events=history")
    r = requests.get(url, headers=UA, timeout=60).json()["chart"]["result"][0]
    return pd.Series(r["indicators"]["quote"][0]["close"],
                     index=pd.to_datetime(r["timestamp"], unit="s").normalize(), dtype="float64").dropna()


def stooq(ticker):
    txt = requests.get(f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d", headers=UA, timeout=60).text
    df = pd.read_csv(io.StringIO(txt), parse_dates=["Date"])
    return df.set_index("Date")["Close"].astype("float64").dropna()


def get_price(ticker):
    for name, fn in (("Yahoo", yahoo), ("Stooq", stooq)):
        try:
            s = fn(ticker)
            if len(s) > 3000:
                print(f"{ticker}: {name}, {s.index[0].date()} .. {s.index[-1].date()}")
                return s[~s.index.duplicated(keep="last")].sort_index()
        except Exception as e:  # noqa: BLE001
            print(f"{ticker}: {name} failed ({type(e).__name__}: {e})")
    sys.exit(f"cannot get {ticker}")


# ------------------------------------------------------------------ frame
def build_frame(spy, qqq, smh, vix):
    d = pd.concat({"spy": spy, "qqq": qqq, "smh": smh, "vix": vix}, axis=1).dropna()
    dev = lambda s, n=50: s / s.rolling(n, min_periods=n).mean() - 1  # noqa: E731
    x = pd.DataFrame(index=d.index)
    x["vix5"] = d.vix.rolling(5).mean()
    x["idx50"] = (dev(d.spy) + dev(d.qqq)) / 2 * 100
    x["ma"] = (d.spy.rolling(50).mean() / d.spy.rolling(200).mean() - 1) * 100
    x["vol21"] = np.log(d.spy / d.spy.shift(1)).rolling(21).std() * np.sqrt(252) * 100
    x["spx"] = (d.spy / d.spy.rolling(252, min_periods=200).max() - 1) * 100
    x["smh50"] = dev(d.smh) * 100
    x = x.dropna()
    fl = pd.DataFrame(index=x.index)
    fl["vix"] = x.vix5.map(um.f_vix)
    fl["idx"] = x.idx50.map(um.f_idx)
    fl["ma"] = x.ma.map(um.f_ma)
    fl["vol"] = x.vol21.map(um.f_vol)
    fl["spx"] = x.spx.map(um.f_spx)
    fl["smh"] = x.smh50.map(um.f_smh)
    out = fl.copy()
    out["score5"] = fl[["vix", "idx", "ma", "vol", "spx"]].sum(axis=1)
    out["score6"] = out.score5 + fl.smh
    s = d.spy.reindex(out.index)
    fut = s.shift(-1)[::-1].rolling(HORIZON).min()[::-1] / s - 1   # worst close in the next HORIZON days
    out["fmin"] = fut
    out["y5"] = (fut <= -0.05).astype(float).where(fut.notna())
    out["y10"] = (fut <= -0.10).astype(float).where(fut.notna())
    return out.dropna(subset=["fmin"])


# ------------------------------------------------------------------ metrics
def auc(score, y):
    r = score.rank(method="average")
    pos = y == 1
    n1, n0 = int(pos.sum()), int((~pos).sum())
    return float((r[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


def flagged_stats(score, df, share):
    """Threshold whose flagged share is closest to `share`; return stats for that threshold."""
    thr = min(sorted(score.unique()), key=lambda t: abs((score >= t).mean() - share))
    f = score >= thr
    n = int(f.sum())
    return {"thr": float(thr), "share": float(f.mean()), "n": n,
            "p5": float(df.y5[f].mean()), "p10": float(df.y10[f].mean()),
            "recall10": float(f[df.y10 == 1].mean()), "avg_fmin": float(df.fmin[f].mean() * 100)}


def analyse(df):
    res = {"span": [str(df.index[0].date()), str(df.index[-1].date())], "days": int(len(df)),
           "base": {"p5": float(df.y5.mean()), "p10": float(df.y10.mean()), "avg_fmin": float(df.fmin.mean() * 100)}}
    res["auc"] = {}
    for tag, sub in (("all", df), ("early", df[df.index < "2013-01-01"]), ("late", df[df.index >= "2013-01-01"])):
        res["auc"][tag] = {"days": int(len(sub)),
                           "y5": {"score5": auc(sub.score5, sub.y5), "score6": auc(sub.score6, sub.y5)},
                           "y10": {"score5": auc(sub.score5, sub.y10), "score6": auc(sub.score6, sub.y10)}}
    res["matched"] = {}
    for share in (0.15, 0.25, 0.35):
        res["matched"][str(share)] = {"score5": flagged_stats(df.score5, df, share),
                                      "score6": flagged_stats(df.score6, df, share)}
    # incremental: given the index-trend flag, does SMH change the odds?
    inc = []
    for fi in (0, 1, 2):
        for fs in (0, 1, 2):
            sub = df[(df.idx == fi) & (df.smh == fs)]
            inc.append({"idx": fi, "smh": fs, "n": int(len(sub)),
                        "p5": float(sub.y5.mean()) if len(sub) else None,
                        "p10": float(sub.y10.mean()) if len(sub) else None})
    res["incremental"] = inc
    res["smh_flag_share"] = {str(k): float(v) for k, v in df.smh.value_counts(normalize=True).sort_index().items()}
    res["mean_extra_points"] = float(df.smh.mean())
    res["corr_smh_idx_flag"] = float(df.smh.corr(df.idx))
    return res


def pct(x, d=0):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def report(r):
    L = []
    L.append("# 第 9 個指標（SMH vs 50 日線）：有冇額外資訊？\n")
    L.append(f"數據：SPY、QQQ、SMH、VIX 每日收市，{r['span'][0]} 至 {r['span'][1]}，共 {r['days']} 個交易日（已扣除預熱期同最近 {HORIZON} 日未有結果的日子）。")
    L.append(f"比較對象只用溫度計入面可由指數同 VIX 重建的 5 個指標（score5，滿分 10）對 score5 + SMH（score6，滿分 12）；"
             f"廣度指標（3、4、5）需要全部美股歷史，無法重建到咁遠。界線用網站現行界線，冇重新擬合。\n")
    L.append(f"結果定義：亮燈日之後 {HORIZON} 個交易日內，SPY 收市價有冇比亮燈日低 5% / 10% 或以上。"
             f"基準（任何一日）：跌 ≥5% {pct(r['base']['p5'])}，跌 ≥10% {pct(r['base']['p10'])}，之後 {HORIZON} 日最差平均 {r['base']['avg_fmin']:.1f}%。\n")
    L.append("## 1. SMH 令分數高幾多\n")
    sh = r["smh_flag_share"]
    L.append(f"SMH 指標：0 分 {pct(sh.get('0'))}、黃燈 {pct(sh.get('1'))}、紅燈 {pct(sh.get('2'))}，平均每日加 {r['mean_extra_points']:.2f} 分。"
             f"同「指數 vs 50 日線」燈號的相關係數 {r['corr_smh_idx_flag']:.2f}。\n")
    L.append("## 2. 排序能力（AUC，0.5 = 同擲毫無分別，愈高愈好）\n")
    L.append("| 時期 | 日數 | 預測「跌≥5%」score5 | score6 | 預測「跌≥10%」score5 | score6 |\n|---|---|---|---|---|---|")
    names = {"all": "全期", "early": "2013 前", "late": "2013 起"}
    for k in ("all", "early", "late"):
        a = r["auc"][k]
        L.append(f"| {names[k]} | {a['days']} | {a['y5']['score5']:.3f} | {a['y5']['score6']:.3f} | {a['y10']['score5']:.3f} | {a['y10']['score6']:.3f} |")
    L.append("\n## 3. 同樣亮燈頻率下比較（排除「分數高咗所以亮得多」嘅影響）\n")
    L.append("| 目標亮燈比例 | 版本 | 實際亮燈比例 | 門檻 | 亮燈後跌≥5% | 跌≥10% | 抓到幾多 ≥10% 前嘅日子 | 之後最差平均 |\n|---|---|---|---|---|---|---|---|")
    for sh_, v in r["matched"].items():
        for tag, nm in (("score5", "5 個指標"), ("score6", "5 個 + SMH")):
            s = v[tag]
            L.append(f"| {float(sh_) * 100:.0f}% | {nm} | {pct(s['share'])} | ≥{s['thr']:.0f} | {pct(s['p5'])} | {pct(s['p10'])} | {pct(s['recall10'])} | {s['avg_fmin']:.1f}% |")
    L.append("\n## 4. 指數趨勢相同時，SMH 有冇改變後果\n")
    L.append("| 指數 vs 50 日線 燈號 | SMH 燈號 | 日數 | 跌≥5% | 跌≥10% |\n|---|---|---|---|---|")
    lab = {0: "正常", 1: "黃", 2: "紅"}
    for c in r["incremental"]:
        L.append(f"| {lab[c['idx']]} | {lab[c['smh']]} | {c['n']} | {pct(c['p5'])} | {pct(c['p10'])} |")
    L.append("\n注意：相鄰日子嘅結果互相重疊，樣本實際獨立程度遠低於日數，數字只作描述，不代表統計上顯著。")
    return "\n".join(L) + "\n"


def main():
    spy, qqq, smh = get_price("SPY"), get_price("QQQ"), get_price("SMH")
    vix = um.fetch_vix()
    df = build_frame(spy, qqq, smh, vix)
    r = analyse(df)
    OUT.mkdir(exist_ok=True)
    (OUT / "indicator-compare.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    md = report(r)
    (OUT / "indicator-compare.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
