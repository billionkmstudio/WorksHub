"""將計算結果輸出成單一中文靜態網頁（docs/index.html）。"""
import math
from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import config

COLORS = {"g": "#2e9d6a", "y": "#d4a02c", "r": "#d9534f"}


def zone_of(score):
    z = config.ZONES[0]
    for item in config.ZONES:
        if score >= item[0]:
            z = item
    return z


def _pt(cx, cy, r, score):
    a = math.pi * (1 - score / 16)
    return cx + r * math.cos(a), cy - r * math.sin(a)


def _arc(cx, cy, r, s0, s1, color, w=22):
    x0, y0 = _pt(cx, cy, r, s0)
    x1, y1 = _pt(cx, cy, r, s1)
    return (f'<path d="M{x0:.1f} {y0:.1f} A{r} {r} 0 0 1 {x1:.1f} {y1:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="{w}" opacity="0.25"/>')


def gauge_svg(score):
    cx, cy, r = 150, 150, 110
    z = zone_of(score)
    parts = [_arc(cx, cy, r, 0, 5, COLORS["g"]),
             _arc(cx, cy, r, 5, 10, COLORS["y"]),
             _arc(cx, cy, r, 10, 16, COLORS["r"])]
    if score > 0:  # 實色進度
        x0, y0 = _pt(cx, cy, r, 0)
        x1, y1 = _pt(cx, cy, r, score)
        parts.append(f'<path d="M{x0:.1f} {y0:.1f} A{r} {r} 0 0 1 {x1:.1f} {y1:.1f}" '
                     f'fill="none" stroke="{COLORS[z[2]]}" stroke-width="22" stroke-linecap="round"/>')
    px, py = _pt(cx, cy, r, score)
    parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="9" fill="#fff" stroke="{COLORS[z[2]]}" stroke-width="4"/>')
    for s in (0, 5, 10, 16):
        lx, ly = _pt(cx, cy, r + 26, s)
        parts.append(f'<text x="{lx:.0f}" y="{ly:.0f}" font-size="12" fill="#888" text-anchor="middle">{s}</text>')
    parts.append(f'<text x="{cx}" y="{cy - 4}" font-size="64" font-weight="700" text-anchor="middle" fill="currentColor">{score}</text>')
    parts.append(f'<text x="{cx}" y="{cy + 22}" font-size="16" text-anchor="middle" fill="#888">/ 16</text>')
    return f'<svg viewBox="0 0 300 185" class="gauge">{"".join(parts)}</svg>'


def fmt(key, v):
    if pd.isna(v):
        return "–"
    sign = "+" if key in ("idx50", "cross") and v > 0 else ""
    return f"{sign}{v:.2f}" if key in ("vix5", "idx50", "cross", "dist52", "spyhigh") else f"{v:.1f}"


def compare_cards(df):
    last = df.index[-1]
    targets = [("前日", df.index[-2] if len(df) > 1 else last),
               ("1 週前", last - timedelta(days=7)),
               ("1 個月前", last - timedelta(days=30)),
               ("1 年前", last - timedelta(days=365))]
    cards = []
    for label, t in targets:
        sub = df.loc[:t]
        if sub.empty:
            cards.append(f'<div class="cmp"><div class="cl">{label}</div><div class="cs">–</div></div>')
            continue
        d, row = sub.index[-1], sub.iloc[-1]
        s = int(row["score"])
        z = zone_of(s)
        cards.append(f'<div class="cmp"><div class="cl">{label} <small>{d:%m-%d}</small></div>'
                     f'<div class="cs {z[2]}t">● {s}</div></div>')
    return "".join(cards)


def indicator_cards(df, lv):
    out = []
    for key, (name, _d, y, r, unit, desc) in config.INDICATORS.items():
        l = int(lv[key].iloc[-1])
        cls = ("g", "y", "r")[l]
        out.append(f'<div class="ic {cls}b"><div class="in">{name}</div>'
                   f'<div class="iv">{fmt(key, df[key].iloc[-1])}<small>{unit}</small></div>'
                   f'<div class="id">{desc}</div>'
                   f'<div class="ith">黃 {y:g}{unit} · 紅 {r:g}{unit}</div></div>')
    return "".join(out)


def history_table(df, lv):
    head = "".join(f"<th>{i + 1}. {config.INDICATORS[k][0]}</th>" for i, k in enumerate(config.INDICATORS))
    rows = []
    for d in df.index[::-1][:config.HISTORY_DAYS]:
        row, lrow = df.loc[d], lv.loc[d]
        z = zone_of(int(row["score"]))
        cells = "".join(f'<td class="{("n", "yc", "rc")[int(lrow[k])]}">{fmt(k, row[k])}</td>'
                        for k in config.INDICATORS)
        def chg(v):
            return f'<small class="{"up" if v >= 0 else "dn"}">{v:+.1f}%</small>' if pd.notna(v) else ""
        up, dn = row["up4"], row["dn4"]
        r5 = row["r5"]
        r5c = "" if pd.isna(r5) else ("rc" if r5 < 0.5 else "gc" if r5 >= 2 else "")
        rows.append(
            f'<tr><td class="dt">{d:%Y-%m-%d}</td>'
            f'<td class="sc {z[2]}bg">{int(row["score"])}</td>'
            f'<td>{row["spy"]:.2f}<br>{chg(row["spy_chg"])}</td>'
            f'<td>{row["qqq"]:.2f}<br>{chg(row["qqq_chg"])}</td>{cells}'
            f'<td class="{"gc" if up >= 300 else ""}">{int(up)}</td>'
            f'<td class="{"rc" if dn >= 300 else ""}">{int(dn)}</td>'
            f'<td class="{r5c}">{"–" if pd.isna(r5) else f"{r5:.2f}"}</td></tr>')
    return (f'<table><thead><tr><th>日期</th><th>分數</th><th>SPY</th><th>QQQ</th>{head}'
            f'<th>升 ≥4%</th><th>跌 ≥4%</th><th>5日比率</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


CSS = """
:root{--bg:#fff;--fg:#1f2430;--mut:#6b7280;--card:#f6f7f9;--bd:#e5e7eb;--g:#2e9d6a;--y:#d4a02c;--r:#d9534f}
@media(prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e6e8ee;--mut:#9aa1b0;--card:#1b1f2a;--bd:#2c3140}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang HK","Noto Sans TC",sans-serif;padding:0 16px 48px}
.w{max-width:1100px;margin:0 auto}h1{font-size:24px;margin:24px 0 4px}h2{font-size:18px;margin:32px 0 12px}
.sub{color:var(--mut);font-size:13px}
.banner{border-left:4px solid;border-radius:8px;padding:12px 14px;margin:16px 0;background:var(--card)}
.banner.g{border-color:var(--g)}.banner.y{border-color:var(--y)}.banner.r{border-color:var(--r)}
.tag{display:inline-block;padding:1px 10px;border-radius:99px;font-size:12px;color:#fff;margin-right:6px}
.tag.g{background:var(--g)}.tag.y{background:var(--y)}.tag.r{background:var(--r)}
.top{display:grid;grid-template-columns:minmax(260px,340px) 1fr;gap:20px;align-items:center}
@media(max-width:640px){.top{grid-template-columns:1fr}}
.gauge{width:100%;color:var(--fg)}
.cmps{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.cmp{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px 12px}
.cl{color:var(--mut);font-size:13px}.cs{font-size:24px;font-weight:700}
.gt{color:var(--g)}.yt{color:var(--y)}.rt{color:var(--r)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.ic{background:var(--card);border:1px solid var(--bd);border-top:4px solid;border-radius:10px;padding:12px}
.gb{border-top-color:var(--g)}.yb{border-top-color:var(--y)}.rb{border-top-color:var(--r)}
.in{font-weight:600}.iv{font-size:26px;font-weight:700}.iv small{font-size:13px;color:var(--mut);margin-left:2px}
.id,.ith{color:var(--mut);font-size:12px}
.tw{overflow-x:auto;border:1px solid var(--bd);border-radius:10px;max-height:70vh}
table{border-collapse:collapse;width:100%;font-size:12.5px;text-align:center;white-space:nowrap}
th{position:sticky;top:0;background:var(--card);padding:8px 6px;border-bottom:1px solid var(--bd);font-weight:600}
td{padding:6px;border-bottom:1px solid var(--bd)}td small{color:var(--mut)}small.up{color:var(--g)}small.dn{color:var(--r)}
.dt{text-align:left;font-weight:600}.sc{font-weight:700;font-size:15px}
.gbg{background:rgba(46,157,106,.2)}.ybg{background:rgba(212,160,44,.25)}.rbg{background:rgba(217,83,79,.25)}
.yc{background:rgba(212,160,44,.2)}.rc{background:rgba(217,83,79,.22);font-weight:600}.gc{background:rgba(46,157,106,.2);font-weight:600}
.legend,.foot{color:var(--mut);font-size:12.5px;margin-top:10px}
"""


def render_page(df, lv, now_utc, n_stocks):
    last = df.index[-1]
    score = int(df["score"].iloc[-1])
    prev = int(df["score"].iloc[-2]) if len(df) > 1 else score
    z = zone_of(score)
    n_red, n_yel = int((lv.iloc[-1] == 2).sum()), int((lv.iloc[-1] == 1).sum())
    delta = score - prev
    dtxt = "與前日持平" if delta == 0 else (f"較前日升 {delta} 分" if delta > 0 else f"較前日跌 {-delta} 分")
    hk = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong"))
    uk = now_utc.astimezone(ZoneInfo("Europe/London"))
    return f"""<!doctype html><html lang="zh-HK"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股風險監測</title><style>{CSS}</style></head><body><div class="w">
<h1>美股風險監測</h1>
<div class="sub">自用 · 每個美股交易日收市後自動更新 · 只描述市況，唔係買賣建議</div>
<div class="banner {z[2]}"><span class="tag {z[2]}">{z[1]}</span>
<b>今日 Score {score} / 16</b>，{dtxt}：{n_red} 個紅燈、{n_yel} 個黃燈。
<div class="sub">數據日期 {last:%Y-%m-%d}（美股收市） · 更新：香港 {hk:%m月%d日 %H:%M} · 英國 {uk:%m月%d日 %H:%M}</div></div>
<div class="top"><div>{gauge_svg(score)}
<div class="legend" style="text-align:center">健康 0–4 · 警示 5–9 · Meltdown 10–16</div></div>
<div><h2 style="margin-top:0">同過去比較</h2><div class="cmps">{compare_cards(df)}</div></div></div>
<h2>8 個指標（黃 = 1 分，紅 = 2 分）</h2><div class="grid">{indicator_cards(df, lv)}</div>
<h2>每日記錄：0–16 分同升跌家數</h2>
<div class="sub">最近 {config.HISTORY_DAYS} 個交易日，最新喺最上面。升跌家數以 {n_stocks} 隻 S&amp;P 500 成份股計。</div>
<div class="tw">{history_table(df, lv)}</div>
<div class="legend">指標格：黃底 = 1 分、紅底 = 2 分。單日升/跌 ≥4% ≥ 300 隻 = 深色。5 日比率 = 5 日內升 ≥4% 隻數 ÷ 跌 ≥4% 隻數：&lt; 0.5 紅、≥ 2 綠。</div>
<div class="foot">門檻為自訂參數，未經回測保證。市場寬度只計 S&amp;P 500 成份股，同全市場數字會有差異。歷史數據只係描述，唔代表將來。</div>
</div></body></html>"""
