"""Teenon dukaanon ke shop pages ko static HTML me nikalta hai, deploy ke liye.

    .venv/Scripts/python.exe scripts/export_static.py

**Ye jaan-boojhkar sirf STOREFRONT hai, poora merchant nahi.**

Deploy karne layak sawaal ye tha: kya teen chalte hue merchants ko kisi host par chadha
dein? Jawab naapkar nahi nikla — teenon ka paisa wala raasta SQLite file me likhta hai
(orders, stock, idempotency), aur ek serverless host par wo writes bachti hi nahi: order
ban jata aur agla `get_order` 404 deta, stock kabhi ghatti nahi, idempotency kabhi dedupe
na karti. Yaani deployed merchants **apni hi conformance suite fail karte** — theek wo
cheez jo dekhne wala sabse pehle jaanchta hai. (Aur Marigold to ek Rust TCP server hai;
wo aise host par chalta hi nahi.)

Isliye jo deploy hota hai wo wahi hai jo bina jhooth bole deploy ho sakta hai: **dukaan ka
shop front, static.** Har page par ek banner hai jo saaf kehta hai ki ye preview hai aur
asli system — stock, orders, payment, audit — repo se local chalta hai. Ek label laga hua
preview imaandaar hai; ek bina label ki copy wo daawa todti hai jo is project ka sabse
mazboot hissa hai: *ek hi database, do darwaze.*

Nikala hua output `deploy/<merchant-id>/` me jata hai — sirf HTML, koi runtime nahi.
"""
import json
import os
import pathlib
import re
import shutil
import sys

import httpx
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "layer"))
load_dotenv(ROOT / ".env")

OUT = ROOT / "deploy"
REPO = "https://github.com/voldemort9999/agent-commerce-protocol"

BANNER = """<div style="background:#111827;color:#f9fafb;font:14px/1.5 system-ui,sans-serif;
 padding:11px 18px;display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;
 border-bottom:2px solid #f59e0b">
<strong style="color:#fbbf24">Static preview</strong>
<span style="opacity:.85">These pages are a snapshot of the shop front. The live shop
&mdash; stock, orders, payments, the agent API and the audit log &mdash; runs locally
from the repository; nothing here can be bought.</span>
<a href="__REPO__" style="color:#fbbf24;margin-left:auto;white-space:nowrap">the repo
&rarr;</a></div>"""


def shops():
    config = json.loads((ROOT / "layer" / "merchants.json").read_text(encoding="utf-8"))
    for merchant in config:
        key = os.getenv(merchant["key_env"])
        if not key:
            print("  SKIP %s — %s set nahi hai" % (merchant["merchant_id"],
                                                   merchant["key_env"]))
            continue
        yield merchant, key


def rewrite(html, categories):
    """Server ke raaste ko file ke raaste me badlo.

    Ek static host par `/p/nw-86` jaisa koi route hai hi nahi — sirf files hain. Har link
    ko uski file par bhejna padta hai, warna preview ka har link 404 deta hai (aur ek
    toota hua preview, koi preview na hone se bura hai).
    """
    html = html.replace('href="/"', 'href="index.html"')
    # Product link ke saath query bhi aa sakti hai — Voltline ka variant/shot
    # switcher `/p/vl-100?variant=vl-100-std&shot=1` jaisa link deta hai. Query ko
    # filename me kheench lena `p-vl-100?variant=...&shot=1.html` banata tha, yaani
    # ek aisi file jo kabhi banti hi nahi — naapa: 309 toote link, aur sab sirf is
    # ek dukaan me. Ek static page variant badal hi nahi sakta, to har aisa link usi
    # product page par jata hai.
    html = re.sub(r'href="/p/([^"?]+)(?:\?[^"]*)?"', r'href="p-\1.html"', html)

    def category_link(match):
        raw = match.group(1)
        name = raw.split("category=")[-1].split("&")[0] if "category=" in raw else ""
        return ('href="index.html"' if not name
                else 'href="category-%s.html"' % name) if name in categories or not name \
            else 'href="index.html"'

    html = re.sub(r'href="/\?([^"]*)"', category_link, html)
    # Jo bhi absolute raasta bacha, wo is host par kuch nahi hai — use ghar bhej do.
    html = re.sub(r'href="/(?!/)[^"]*"', 'href="index.html"', html)
    # `<body>` par literal replace kaafi NAHI hai: Northwind ka tag
    # `<body class="bg-stone-50 ...">` hai, aur us ek chook se uske saathon page BINA
    # label ke chale gaye the — theek wo cheez jiske liye ye banner hai. Naapkar pakda:
    # 196/257 pages par banner tha, aur 60 gayab wale sab ek hi dukaan ke the.
    banner = BANNER.replace("__REPO__", REPO)
    html, count = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + banner, html, count=1)
    if not count:
        raise SystemExit("is page me <body> mila hi nahi — banner bina page deploy "
                         "nahi hoga")
    return html


def export():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    index_rows = []

    for merchant, key in shops():
        mid = merchant["merchant_id"]
        base = merchant["base_url"].rstrip("/")
        folder = OUT / mid
        folder.mkdir()
        api = httpx.Client(base_url=base, headers={"X-Agent-Key": key}, timeout=30)
        page = api.get("/agent/catalog", params={"limit": 500}).json()
        products = page["products"]
        categories = sorted({p["category"] for p in products})
        name = api.get("/agent/manifest").json()["name"]

        written = 0
        for path, filename in ([("/", "index.html")] +
                               [("/?category=%s" % c, "category-%s.html" % c)
                                for c in categories] +
                               [("/p/%s" % p["product_id"], "p-%s.html" % p["product_id"])
                                for p in products]):
            r = httpx.get(base + path, timeout=30)
            if r.status_code != 200:
                print("    MISS %s -> %d" % (path, r.status_code))
                continue
            (folder / filename).write_text(rewrite(r.text, categories), encoding="utf-8")
            written += 1
        api.close()
        print("  %-22s %3d pages  (%d products, %d categories)"
              % (mid, written, len(products), len(categories)))
        index_rows.append((mid, name, len(products), len(categories)))

    (OUT / "index.html").write_text(landing(index_rows), encoding="utf-8")
    print("\n  -> %s" % OUT)
    print("     har folder ek alag static site hai; `vercel deploy --prod` folder ke andar se")


def landing(rows):
    cards = "".join(
        '<a class="c" href="%s/index.html"><b>%s</b><span>%s</span>'
        '<small>%d products &middot; %d categories</small></a>' % (mid, name, mid, n, c)
        for mid, name, n, c in rows)
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Commerce Protocol — shop previews</title><style>
body{margin:0;background:#0b1220;color:#e5edf7;font:15px/1.6 system-ui,sans-serif}
.w{max-width:860px;margin:0 auto;padding:56px 24px}
h1{font-size:28px;margin:0 0 8px}p{opacity:.8;margin:0 0 28px}
.c{display:block;background:#131c2e;border:1px solid #24334d;border-radius:14px;
padding:18px 20px;margin-bottom:12px;text-decoration:none;color:inherit}
.c:hover{border-color:#f59e0b}.c b{display:block;font-size:18px}
.c span{opacity:.6;font-size:13px}.c small{display:block;opacity:.55;margin-top:6px}
a.r{color:#fbbf24}</style></head><body><div class="w">
<h1>Agent Commerce Protocol</h1>
<p>Three reference merchants, built in three different languages from one specification,
so that any AI buyer can transact with any of them. <strong>These pages are static
snapshots of the shop fronts.</strong> The live system — stock, orders, real test-mode
payments, the agent API and the audit log — runs locally from
<a class="r" href="__REPO__">the repository</a>.</p>
__CARDS__
</div></body></html>""".replace("__CARDS__", cards).replace("__REPO__", REPO)


if __name__ == "__main__":
    export()
