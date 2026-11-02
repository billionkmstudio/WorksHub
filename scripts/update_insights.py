#!/usr/bin/env python3
"""Works Hub: daily 痛點 (pain points) and 靈惑 (content/product sparks).

Sources that have a public, free interface: Reddit (top posts of the day) and Hacker News (Algolia API).
X, Facebook, Threads and Instagram have no usable public interface, so they are not read.
Gemini picks 5 pain points and 5 sparks from the day's most discussed posts and writes the analysis in
Traditional Chinese. Links, platform names and engagement numbers come from the source, not from the model.

Environment: GEMINI_API_KEY (required), WH_DATA, WH_TZ, GEMINI_MODEL (optional)
"""
import json
import os
import re
import sys
import time
import datetime as dt
from pathlib import Path

import requests

import update_news as un  # shares the Gemini helper (model auto-pick, retries)

UA = {"User-Agent": "Mozilla/5.0 (compatible; WorksHub/1.0; +https://workshub.me)"}

PAIN_SUBS = ["smallbusiness", "Entrepreneur", "freelance", "UKPersonalFinance", "ExperiencedDevs",
             "sysadmin", "LegalAdviceUK", "AskUK", "personalfinance", "microsaas"]
SPARK_SUBS = ["technology", "Futurology", "ArtificialInteligence", "singularity", "investing", "stocks", "SideProject", "InternetIsBeautiful"]

CONTEXT = ("The reader runs a one-person digital studio in the UK serving UK small businesses and "
           "Chinese-speaking (Hong Kong) audiences: web development, AI tools, small SaaS products, "
           "and YouTube/short-form content. Ideas should be something one person could build or make in days to weeks.")


def get_json(url, params=None, tries=3):
    for a in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"  {url}: HTTP {r.status_code}", file=sys.stderr)
            if r.status_code in (403, 404):
                return None
        except Exception as e:  # noqa: BLE001
            print(f"  {url}: {e}", file=sys.stderr)
        time.sleep(3 * (a + 1))
    return None


def reddit(subs, per=10):
    out = []
    for sub in subs:
        j = get_json(f"https://www.reddit.com/r/{sub}/top.json", {"t": "day", "limit": per, "raw_json": 1})
        time.sleep(1.5)
        if not j:
            continue
        got = 0
        for c in j.get("data", {}).get("children", []):
            d = c.get("data", {})
            if d.get("over_18") or d.get("stickied") or not d.get("title"):
                continue
            out.append({"platform": "Reddit", "where": f"r/{sub}", "title": d["title"],
                        "text": re.sub(r"\s+", " ", d.get("selftext", ""))[:600],
                        "points": d.get("score", 0), "comments": d.get("num_comments", 0),
                        "url": "https://www.reddit.com" + d.get("permalink", "")})
            got += 1
        print(f"  r/{sub}: {got}")
    return out


def hackernews(per=30):
    since = int(time.time()) - 36 * 3600
    out = []
    for tag, where in (("ask_hn", "Ask HN"), ("front_page", "HN 首頁")):
        j = get_json("https://hn.algolia.com/api/v1/search",
                     {"tags": tag, "numericFilters": f"created_at_i>{since}", "hitsPerPage": per})
        if not j:
            continue
        for h in j.get("hits", []):
            if not h.get("title"):
                continue
            text = re.sub(r"<[^>]+>", " ", h.get("story_text") or "")
            out.append({"platform": "Hacker News", "where": where, "title": h["title"],
                        "text": re.sub(r"\s+", " ", text)[:600],
                        "points": h.get("points") or 0, "comments": h.get("num_comments") or 0,
                        "url": f"https://news.ycombinator.com/item?id={h['objectID']}"})
        print(f"  HN {where}: {len(j.get('hits', []))}")
    return out


PAIN = """You are a researcher for a private Traditional Chinese (Hong Kong style) daily briefing.
{context}

Below are today's most discussed posts. Pick the {n} that best reveal a real PAIN POINT: a repeated, costly or
frustrating problem that people would pay to solve. Prefer concrete problems with many comments. Pick posts from
different communities and avoid duplicates. Ignore news, memes, politics and pure opinion.

For each pick return:
  "id": candidate id
  "t": the pain point in one Traditional Chinese sentence (under 45 characters), phrased as the user's problem.
  "why": 1-2 Traditional Chinese sentences (under 110 characters) on who has it and why it hurts, using only what the post shows.
  "idea": 1-2 Traditional Chinese sentences (under 110 characters): a product or service this studio could build.
Return JSON only: an array of {n} objects.

Candidates:
{cands}
"""

SPARK = """You are a content strategist for a private Traditional Chinese (Hong Kong style) daily briefing.
{context}

Below are today's most discussed posts. Pick the {n} that give the best SPARK: a topic people are curious about
or arguing about, which could become a video, article, tool or product. Pick different subjects and avoid duplicates.
Avoid anything already used: ids {used}.

For each pick return:
  "id": candidate id
  "t": the topic as one Traditional Chinese sentence (under 45 characters).
  "angle": 1-2 Traditional Chinese sentences (under 110 characters): a fresh angle for a Chinese-speaking audience.
  "idea": 1-2 Traditional Chinese sentences (under 110 characters): a concrete content or product idea.
Return JSON only: an array of {n} objects.

Candidates:
{cands}
"""


def pick(prompt_t, pool, fields, n=5, **kw):
    pool = sorted(pool, key=lambda p: -(p["comments"] * 2 + p["points"]))[:45]
    lines = [json.dumps({"id": i, "platform": p["where"], "title": p["title"], "text": p["text"],
                         "points": p["points"], "comments": p["comments"]}, ensure_ascii=False) for i, p in enumerate(pool)]

    def check(arr):
        res, seen = [], set()
        for o in arr:
            i = int(o["id"])
            if i in seen or not (0 <= i < len(pool)):
                continue
            if not all(str(o.get(f, "")).strip() for f in ("t",) + fields):
                continue
            seen.add(i)
            p = pool[i]
            item = {"slot": p["platform"], "t": str(o["t"]).strip()}
            for f in fields:
                item[f] = str(o[f]).strip()
            item.update({"src": f"{p['where']}　{p['comments']} 則留言", "url": p["url"], "_i": i})
            res.append(item)
        if len(res) < 3:
            raise RuntimeError(f"only {len(res)} usable items")
        return res[:n]

    res = un.call_gemini(prompt_t.format(context=CONTEXT, n=n, cands="\n".join(lines), **kw), check)
    return res, pool


def main():
    if not un.KEY:
        sys.exit("GEMINI_API_KEY is not set")
    data = json.loads(un.DATA_PATH.read_text(encoding="utf-8"))
    today = dt.datetime.now(un.TZ).strftime("%Y-%m-%d")
    print("[sources]")
    hn = hackernews()
    pain_pool = reddit(PAIN_SUBS) + [p for p in hn if p["where"] == "Ask HN"]
    spark_pool = reddit(SPARK_SUBS) + [p for p in hn if p["where"] != "Ask HN"]
    print(f"  pain pool {len(pain_pool)}, spark pool {len(spark_pool)}")
    if len(pain_pool) < 10 or len(spark_pool) < 10:
        sys.exit("too few posts (Reddit may be blocking this server) - data.json left unchanged")
    pain, _ = pick(PAIN, pain_pool, ("why", "idea"))
    spark, sp = pick(SPARK, [p for p in spark_pool if p["url"] not in {x["url"] for x in pain}], ("angle", "idea"), used="[]")
    for it in pain + spark:
        it.pop("_i", None)
    day = data.setdefault("days", {}).setdefault(today, {})
    day["pain"], day["spark"] = pain, spark
    if isinstance(day.get("sample"), dict):
        day["sample"].pop("pain", None)
        day["sample"].pop("spark", None)
    data["updated"] = dt.datetime.now(un.TZ).strftime("%Y-%m-%d %H:%M")
    un.DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("updated", today, len(pain), len(spark))


if __name__ == "__main__":
    main()
