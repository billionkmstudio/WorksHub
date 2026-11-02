#!/usr/bin/env python3
"""Works Hub: daily international + finance news updater.

1. Reads public RSS feeds (headline order = how prominently each outlet shows the story).
2. Merges stories that several outlets cover, scores them (position in feed + number of outlets).
3. Asks Gemini to pick the 10 most important per category and write a Traditional Chinese
   headline and summary. Links and source names always come from the feed, never from the model.
4. Writes days[<date>].intl / .finance in public/data.json. Other keys of that day stay untouched.

Environment:
  GEMINI_API_KEY   required
  GEMINI_MODEL     optional; default = newest "flash" model your key can use
  WH_DATA          path to data.json (default public/data.json)
  WH_TZ            time zone that decides "today" (default Europe/London)
"""
import json
import os
import re
import sys
import time
import html
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DATA_PATH = Path(os.environ.get("WH_DATA", "public/data.json"))
KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "")  # empty = pick automatically from the models your key can use
TZ = ZoneInfo(os.environ.get("WH_TZ", "Europe/London"))
KEEP_NEWS_ONLY_DAYS = 90
PER_FEED = 15
CANDIDATES = 40
UA = {"User-Agent": "WorksHub/1.0 (+https://workshub.me)"}

FEEDS = {
    "intl": [
        ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
        ("NPR", "https://feeds.npr.org/1004/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("DW", "https://rss.dw.com/rdf/rss-en-world"),
    ],
    "finance": [
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("The Guardian Business", "https://www.theguardian.com/business/rss"),
    ],
}
LABEL = {"intl": "international news", "finance": "finance and markets news"}


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def fetch_feed(name, url):
    """Return [(title, description, link)] in feed order. Never raises."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            out = []
            for it in root.iter():
                if local(it.tag) not in ("item", "entry"):
                    continue
                f = {}
                for ch in it:
                    k = local(ch.tag)
                    if k == "link":
                        f["link"] = (ch.text or "").strip() or ch.attrib.get("href", "")
                    elif k in ("title", "description", "summary") and k not in f:
                        f[k] = clean(ch.text)
                t = f.get("title", "")
                d = f.get("description") or f.get("summary", "")
                if t and f.get("link", "").startswith("http"):
                    out.append((t, d[:400], f["link"]))
                if len(out) >= PER_FEED:
                    break
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            print(f"  feed {name}: attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return []


def words(t):
    return {w for w in re.findall(r"[a-z0-9']{4,}", t.lower())}


def collect(cat):
    items = []
    for name, url in FEEDS[cat]:
        got = fetch_feed(name, url)
        print(f"  {name}: {len(got)} items")
        for pos, (t, d, link) in enumerate(got):
            items.append({"src": name, "t": t, "d": d, "url": link, "pos": pos, "w": words(t)})
    # merge stories that several outlets cover (>= 3 shared longer words, or 50% overlap)
    merged = []
    for it in items:
        for m in merged:
            inter = len(it["w"] & m["w"])
            if inter >= 3 and inter / max(1, min(len(it["w"]), len(m["w"]))) >= 0.5 and it["src"] not in m["srcs"]:
                m["srcs"].append(it["src"])
                m["score"] += 3 + (PER_FEED - it["pos"]) / PER_FEED
                break
        else:
            merged.append({**it, "srcs": [it["src"]], "score": (PER_FEED - it["pos"]) / PER_FEED * 2})
    merged.sort(key=lambda m: -m["score"])
    return merged[:CANDIDATES]


PROMPT = """You are the editor of a private Traditional Chinese (Hong Kong style) daily briefing.
Below are candidate {label} stories collected today from RSS feeds. "outlets" lists every outlet
that covers the story; "rank" is its prominence score (higher = more prominent).

Choose the {n} most important and widely followed stories, ordered from most to least important.
Prefer stories covered by several outlets. Avoid duplicates and avoid pure entertainment or sport
unless it moves markets or politics. For each chosen story write:
  "t": a Traditional Chinese headline, under 32 characters, factual, no clickbait.
  "s": a Traditional Chinese summary of 1 to 2 sentences, under 90 characters, using ONLY facts in
       the title and description given. Do not add facts, numbers, names or causes that are not there.
Keep proper nouns accurate (use common Hong Kong Chinese names, or the English name if unsure).

Return JSON only: an array of {n} objects {{"id": <candidate id>, "t": "...", "s": "..."}}.

Candidates:
{cands}
"""


def pick_models():
    """Models this key can call, best first. Model names change, so ask the API instead of hard-coding."""
    if MODEL:
        return [MODEL]
    names = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                         headers={"x-goog-api-key": KEY}, timeout=30)
        r.raise_for_status()
        for m in r.json().get("models", []):
            n = m["name"].split("/", 1)[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in n or any(b in n for b in ("lite", "image", "tts", "live", "audio", "thinking", "robotics", "computer", "embedding", "gemma")):
                continue
            names.append(n)
    except Exception as e:  # noqa: BLE001
        print(f"  could not list models: {e}", file=sys.stderr)

    def ver(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (float(m.group(1)) if m else 0, "preview" not in n and "exp" not in n, n)

    names.sort(key=ver, reverse=True)
    print("  models to try:", names[:4])
    return names[:4] or ["gemini-flash-latest"]


def ask_gemini(cat, cands, n=10):
    lines = []
    for i, c in enumerate(cands):
        lines.append(json.dumps({"id": i, "title": c["t"], "description": c["d"],
                                 "outlets": c["srcs"], "rank": round(c["score"], 1)}, ensure_ascii=False))
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(label=LABEL[cat], n=n, cands="\n".join(lines))}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    last = None
    for model in pick_models():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, json=body, headers={"x-goog-api-key": KEY}, timeout=120)
                if r.status_code in (404, 403):
                    raise LookupError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
                if r.status_code in (429, 500, 503):
                    raise RuntimeError(f"{model}: HTTP {r.status_code}")
                r.raise_for_status()
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                arr = json.loads(txt)
                res, seen = [], set()
                for o in arr:
                    i = int(o["id"])
                    if i in seen or not (0 <= i < len(cands)):
                        continue
                    t, s = str(o["t"]).strip(), str(o["s"]).strip()
                    if t and s:
                        seen.add(i)
                        c = cands[i]
                        res.append({"t": t, "s": s, "src": " / ".join(c["srcs"]), "url": c["url"]})
                if len(res) >= min(n, 5):
                    print(f"  model used: {model}")
                    return res[:n]
                raise RuntimeError(f"{model}: only {len(res)} usable items")
            except LookupError as e:
                last = e
                print(f"  {e}", file=sys.stderr)
                break  # try next model
            except Exception as e:  # noqa: BLE001
                last = e
                print(f"  gemini attempt {attempt + 1} failed: {e}", file=sys.stderr)
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Gemini failed for {cat}: {last}")


def main():
    if not KEY:
        sys.exit("GEMINI_API_KEY is not set")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    today = dt.datetime.now(TZ).strftime("%Y-%m-%d")
    result = {}
    for cat in ("intl", "finance"):
        print(f"[{cat}]")
        cands = collect(cat)
        if len(cands) < 10:
            sys.exit(f"too few candidates for {cat}: {len(cands)} (feeds blocked?) - data.json left unchanged")
        result[cat] = ask_gemini(cat, cands)
        print(f"  -> {len(result[cat])} stories")
    day = data.setdefault("days", {}).setdefault(today, {})
    day.update(result)
    day.pop("note", None)
    if isinstance(day.get("sample"), dict):
        day["sample"].pop("intl", None)
        day["sample"].pop("finance", None)
    # drop old days that hold nothing but news
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=KEEP_NEWS_ONLY_DAYS)).strftime("%Y-%m-%d")
    for d in [d for d in data["days"] if d < cutoff and not (data["days"][d].get("pain") or data["days"][d].get("spark"))]:
        del data["days"][d]
    data["updated"] = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("updated", today)


if __name__ == "__main__":
    main()
