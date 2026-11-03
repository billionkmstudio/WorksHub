#!/usr/bin/env python3
"""Works Hub: weekly summary (每週總結).

Reads the week's already-collected data from public/data.json (news, pain points, sparks, risk scores)
and asks Gemini to write a short Traditional Chinese summary. It adds no new sources: everything the
model may mention comes from that week's data.

The week is Monday to Sunday. The script writes the most recent Sunday-ending week, so it also works
when it runs late (Monday morning) as a retry.

Environment: GEMINI_API_KEY (required), WH_DATA, WH_TZ, FORCE=1 (rewrite a week that already exists)
"""
import json
import sys
import datetime as dt

import update_news as un  # shared Gemini helper (model auto-pick, retries) and data path
import update_insights as ui  # shared reader context

HEADINGS = ["市場", "風險監測", "國際", "本週最值得跟進", "下週留意"]
KEEP_WEEKS = 26
MIN_DAYS = 3  # fewer days of news than this and there is nothing worth summarising

PROMPT = """You write the weekly summary for a personal news and ideas site. Write in Traditional Chinese
(Hong Kong), concise written style, no emoji.

Reader: {context}

Week: {start} to {end}.

STRICT RULES
- Use only facts that appear in the data below. Do not add events, numbers, prices or names from your own knowledge.
- A figure (percentage, index level, score) may be written only if it appears in the data below.
- Market and risk sections describe what happened. Never tell the reader to buy, sell or hold anything.
- The ideas are leads, not proven demand: say they need checking before building.

Return ONE JSON object, nothing else:
{{"headline": "<8 to 24 characters, the main story of the week>",
 "sections": [
  {{"h": "市場", "p": "<2-4 sentences on the finance news themes>"}},
  {{"h": "風險監測", "p": "<2-3 sentences on how the risk score moved over the week; say so plainly if no scores are given>"}},
  {{"h": "國際", "p": "<2-4 sentences on the main international stories>"}},
  {{"h": "本週最值得跟進", "p": "<exactly 3 ideas picked from the pain points and sparks, written as （1）… （2）… （3）…; each names the topic and one concrete step the reader can take next week; end with a reminder to check real demand first>"}},
  {{"h": "下週留意", "p": "<2-3 things to watch next week, ending with: 以上為觀察項目，不構成買賣建議。>"}}
 ]}}

Risk score scale: 0-16 points. 0-4 healthy, 5-9 caution, 10-16 meltdown.

DATA
{data}
"""


def week_bounds(today):
    end = today - dt.timedelta(days=(today.weekday() + 1) % 7)  # most recent Sunday (today if Sunday)
    return end - dt.timedelta(days=6), end


def gather(data, start, end):
    s, e = start.isoformat(), end.isoformat()
    days = {d: v for d, v in sorted(data.get("days", {}).items()) if s <= d <= e}
    out = []
    for d, v in days.items():
        out.append({
            "date": d,
            "intl": [{"t": x["t"], "h": x.get("h") or x.get("s", "")} for x in (v.get("intl") or [])[:5]],
            "finance": [{"t": x["t"], "h": x.get("h") or x.get("s", "")} for x in (v.get("finance") or [])[:5]],
            "pain": [{"t": x["t"], "why": x.get("why", ""), "idea": x.get("idea", "")} for x in (v.get("pain") or [])],
            "spark": [{"t": x["t"], "angle": x.get("angle", ""), "idea": x.get("idea", "")} for x in (v.get("spark") or [])],
        })
    rows = (data.get("monitor") or {}).get("rows") or []
    scores = sorted(([r[0], r[1]] for r in rows if s <= str(r[0]) <= e), key=lambda r: r[0])
    return out, scores


def main():
    if not un.KEY:
        sys.exit("GEMINI_API_KEY is not set")
    data = json.loads(un.DATA_PATH.read_text(encoding="utf-8"))
    start, end = week_bounds(dt.datetime.now(un.TZ).date())
    weeks = data.setdefault("weeks", [])
    if not un.FORCE and any(w.get("start") == start.isoformat() for w in weeks):
        print(f"week {start} - {end}: already written, nothing to do")
        return
    days, scores = gather(data, start, end)
    news_days = [d for d in days if d["intl"] or d["finance"]]
    if len(news_days) < MIN_DAYS:
        print(f"week {start} - {end}: only {len(news_days)} day(s) of news, not enough to summarise")
        return

    def check(obj):
        if not isinstance(obj, dict):
            raise RuntimeError("not an object")
        head = str(obj.get("headline", "")).strip()
        secs = obj.get("sections")
        if not (4 <= len(head) <= 40):
            raise RuntimeError(f"bad headline: {head!r}")
        if not isinstance(secs, list) or [str(s.get("h", "")).strip() for s in secs] != HEADINGS:
            raise RuntimeError("sections do not match the required headings")
        clean = []
        for s in secs:
            p = str(s.get("p", "")).strip()
            if not (20 <= len(p) <= 900):
                raise RuntimeError(f"section {s.get('h')} has {len(p)} characters")
            clean.append({"h": str(s["h"]).strip(), "p": p})
        return {"headline": head, "sections": clean}

    payload = {"days": days, "risk_scores": [{"date": d, "score": sc} for d, sc in scores]}
    prompt = PROMPT.format(context=ui.CONTEXT, start=start, end=end, data=json.dumps(payload, ensure_ascii=False))
    res = un.call_gemini(prompt, check)
    week = {"start": start.isoformat(), "end": end.isoformat(), **res}
    weeks[:] = [w for w in weeks if w.get("start") != week["start"]] + [week]
    weeks.sort(key=lambda w: w["start"], reverse=True)
    del weeks[KEEP_WEEKS:]
    data["updated"] = dt.datetime.now(un.TZ).strftime("%Y-%m-%d %H:%M")
    un.DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"week {start} - {end}: written ({len(news_days)} days of news)")


if __name__ == "__main__":
    main()
