#!/usr/bin/env python3
"""創業情報站：加密儲存同頁面權限。

資料全部存喺 docs/desk/data/store.enc.json（AES-256-GCM 加密，密碼由 DESK_PASSWORD 提供）。
desk/pages.json 決定每一頁係 "public"（明文，任何人睇到）定 "private"（要密碼先睇到）。
`build` 會按 pages.json 重新產生 docs/desk/data/public.json，裡面只有公開頁面嘅內容。

用法（密碼只經環境變數，唔好放入指令或 repo）：
  DESK_PASSWORD=... python desk/desk.py init --from <資料夾>   # 第一次，由 days/ weeks/ meta/ 匯入
  DESK_PASSWORD=... python desk/desk.py add --kind day  --file day.json
  DESK_PASSWORD=... python desk/desk.py add --kind week --file week.json
  DESK_PASSWORD=... python desk/desk.py build                    # 改咗 pages.json 之後
  DESK_PASSWORD=... DESK_PASSWORD_NEW=... python desk/desk.py rekey
"""
import argparse, base64, glob, json, os, sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "desk" / "data"
STORE = DATA / "store.enc.json"
PUBLIC = DATA / "public.json"
PAGES = ROOT / "desk" / "pages.json"
ITER = 600_000
SECTIONS = ["finance", "pain", "content", "weekly"]
DAY_SECTIONS = ["finance", "pain", "content"]


def b64(b): return base64.b64encode(b).decode()
def unb64(s): return base64.b64decode(s)


def derive(password, salt, iters):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iters)
    return kdf.derive(password.encode("utf-8"))


def encrypt(obj, password):
    salt, iv = os.urandom(16), os.urandom(12)
    key = derive(password, salt, ITER)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(blob, password):
    key = derive(password, unb64(blob["salt"]), blob["iter"])
    try:
        pt = AESGCM(key).decrypt(unb64(blob["iv"]), unb64(blob["ct"]), None)
    except Exception:
        sys.exit("密碼唔啱，或者 store.enc.json 已損壞。")
    return json.loads(pt.decode("utf-8"))


def get_password(name="DESK_PASSWORD"):
    pw = os.environ.get(name, "")
    if len(pw) < 12:
        sys.exit(f"請用環境變數 {name} 提供密碼（至少 12 個字元）。")
    return pw


def load_pages():
    pages = json.loads(PAGES.read_text(encoding="utf-8"))
    for s in SECTIONS:
        if pages.get(s) not in ("public", "private"):
            sys.exit(f"desk/pages.json：「{s}」要係 public 或 private。")
    return {s: pages[s] for s in SECTIONS}


def load_store(password):
    return decrypt(json.loads(STORE.read_text(encoding="utf-8")), password)


def save_store(store, password):
    DATA.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(encrypt(store, password), separators=(",", ":")), encoding="utf-8")


def build_public(store):
    """只輸出 pages.json 標明 public 嘅頁面。meta.sources 唔敏感，一律公開。"""
    pages = load_pages()
    pub_days = {}
    for date, day in store["days"].items():
        keep = {k: day[k] for k in DAY_SECTIONS if pages[k] == "public" and k in day}
        keep["date"] = date
        if "updatedAt" in day: keep["updatedAt"] = day["updatedAt"]
        pub_days[date] = keep
    pub_weeks = store["weeks"] if pages["weekly"] == "public" else {}
    out = {"pages": pages, "days": pub_days, "weeks": pub_weeks,
           "meta": {"sources": store.get("meta", {}).get("sources", {}), "updatedAt": store.get("meta", {}).get("updatedAt")}}
    # 全部私人時，日期都唔需要公開
    if all(v == "private" for v in pages.values()):
        out["days"] = {}
    DATA.mkdir(parents=True, exist_ok=True)
    PUBLIC.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return pages


def read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def cmd_init(a):
    pw = get_password()
    src = Path(a.src)
    store = {"days": {}, "weeks": {}, "meta": {}}
    for f in sorted(glob.glob(str(src / "days" / "*.json"))):
        d = read_json(f); d = d.get("data", d)
        if not d.get("example"): store["days"][d["date"]] = d
    for f in sorted(glob.glob(str(src / "weeks" / "*.json"))):
        d = read_json(f); d = d.get("data", d)
        if not d.get("example"): store["weeks"][d["week"]] = d
    m = src / "meta" / "status.json"
    if m.exists():
        d = read_json(m); store["meta"] = d.get("data", d)
    save_store(store, pw)
    pages = build_public(store)
    print(f"已匯入 {len(store['days'])} 日、{len(store['weeks'])} 週。頁面權限：{pages}")


def cmd_add(a):
    pw = get_password()
    store = load_store(pw)
    d = read_json(a.file); d = d.get("data", d)
    if a.kind == "day": store["days"][d["date"]] = d
    else: store["weeks"][d["week"]] = d
    if d.get("updatedAt"):
        store.setdefault("meta", {})["updatedAt"] = d["updatedAt"]
    if a.sources:
        store.setdefault("meta", {})["sources"] = read_json(a.sources)
    save_store(store, pw)
    build_public(store)
    print("已更新。")


def cmd_build(a):
    pw = get_password()
    pages = build_public(load_store(pw))
    print(f"已重建 public.json。頁面權限：{pages}")


def cmd_rekey(a):
    old, new = get_password(), get_password("DESK_PASSWORD_NEW")
    store = load_store(old)
    save_store(store, new)
    print("已用新密碼重新加密。記得更新 GitHub Secret DESK_PASSWORD。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--from", dest="src", required=True); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("add"); p.add_argument("--kind", choices=["day", "week"], required=True)
    p.add_argument("--file", required=True); p.add_argument("--sources"); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("build"); p.set_defaults(fn=cmd_build)
    p = sub.add_parser("rekey"); p.set_defaults(fn=cmd_rekey)
    a = ap.parse_args(); a.fn(a)
