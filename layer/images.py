"""Chune hue product ki tasveerein — ek labelled collage, ek tay canvas.

**Kyun ye file hai.** Aaj tak product ka aadha sach — *dikhta kaisa hai* — poori pipeline
me kahin maujood hi nahi tha. `images[]` me CDN ke URL jate the aur unhe **koi nahi
kholta**: na MCP client, na model. Naapkar dekha gaya ki ek asli buyer model MCP image
content ko sach me dekh leta hai aur chaar views alag-alag pehchanta hai (D-101), yaani
raasta pehle se tha, humne bheja hi nahi tha.

**Ek collage, N images nahi (D-106).** Sasta hona sabse kamzor wajah hai. Asli wajah ye
hai ki collage kharche ki ek **chhat** bana deta hai jo merchant ke bartav se azaad hai:
`SPEC.md` §4 me `images[]` par koi upper limit hai hi nahi, to per-image downscale me
kharcha merchant ke saath badhta jayega, jabki tay canvas me 3 image hon ya 10, pixel
budget wahi rehta hai. Panels pe **number likhe** jate hain taaki model "doosri tasveer"
jaisi baat theek se keh sake — chaar alag images me wo apne aap milta tha.

**Guards merchant ke input ki wajah se hain (D-108), kalpana ki wajah se nahi.** Ye URL
merchant deta hai aur fetch **humara** server karta hai — yaani wahi trust boundary jo
description ki hai, sirf bytes me. Isliye: `https` only, per-image size cap, content-type
check, timeout, count cap, aur `MAX_PIXELS` — ek merchant 50,000x50,000 ka image serve
karke Layer ki memory kha sakta hai, aur ye ek asli decompression bomb hai.

Aspect ratio galat ho to **pad karo, crop mat karo** — crop kapde ka aadha hissa kaat
deta hai, aur us tasveer ka poora maqsad hi ye batana hai ki cheez dikhti kaisi hai.
"""
import io
import math
import os
from functools import lru_cache

import httpx
from PIL import Image, ImageDraw, ImageFont

# Pillow ka apna bomb guard, humare hisaab se sakht kiya hua. Ye global hai (PIL ka
# module-level constant), isliye ek hi jagah set hota hai aur neeche header padhkar
# dobara explicit check bhi hota hai — Image.open() lazy hai, wo bytes decode karne se
# pehle sirf header padhti hai.
MAX_PIXELS = 25_000_000                 # 5000x5000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

CANVAS = 1024                           # 720p aur 1080p ke beech; panels isi me batte hain
MAX_PANELS = 6
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 8.0
JPEG_QUALITY = 80
PAD = (245, 245, 245)                   # panel ka background — crop ki jagah yahi bharta hai


class Rejected(Exception):
    """Ek image guard pe ruki. Poora collage nahi marta — baaki panels ban jate hain."""


def _fetch(url, client):
    global FETCHES
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise Rejected("not an https url")
    FETCHES += 1
    with client.stream("GET", url, timeout=TIMEOUT, follow_redirects=True) as response:
        if response.status_code != 200:
            raise Rejected("HTTP %d" % response.status_code)
        if not str(response.url).lower().startswith("https://"):
            raise Rejected("redirected off https")
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            raise Rejected("content-type is not an image")
        # Cap padhte waqt lagta hai, content-length maankar nahi: wo header merchant ka
        # bheja hua hai aur jhooth bol sakta hai. Jaise hi budget toota, stream chhod dete
        # hain — poori 50 MB kabhi memory me aati hi nahi.
        blob = bytearray()
        for chunk in response.iter_bytes(64 * 1024):
            blob.extend(chunk)
            if len(blob) > MAX_BYTES:
                raise Rejected("larger than %d bytes" % MAX_BYTES)
    return bytes(blob)


def _decode(blob):
    image = Image.open(io.BytesIO(blob))          # lazy — abhi sirf header padha gaya
    width, height = image.size
    if width * height > MAX_PIXELS:
        raise Rejected("%dx%d is above the %d pixel ceiling" % (width, height, MAX_PIXELS))
    if image.mode in ("RGBA", "LA", "P"):
        # Transparent product shots (catalog ke saare webp aise hi hain) ko seedha
        # `convert("RGB")` karne se alpha KAALA ho jata hai — kapda ek kaale dabbe me
        # baith jata hai aur "dikhta kaisa hai" ka poora maqsad mar jata hai. Isliye use
        # wahi background milta hai jo panel ka hai.
        image = image.convert("RGBA")
        flat = Image.new("RGBA", image.size, PAD + (255,))
        return Image.alpha_composite(flat, image).convert("RGB")
    return image.convert("RGB")


def _panel(image, box_w, box_h, number, font):
    """Ek panel: image ko box me fit karo (pad, crop nahi) aur uspe number likho."""
    canvas = Image.new("RGB", (box_w, box_h), PAD)
    fitted = image.copy()
    fitted.thumbnail((box_w, box_h), Image.LANCZOS)
    canvas.paste(fitted, ((box_w - fitted.width) // 2, (box_h - fitted.height) // 2))

    draw = ImageDraw.Draw(canvas)
    label = str(number)
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    pad = 10
    draw.rectangle((0, 0, right - left + 2 * pad, bottom - top + 2 * pad), fill=(20, 20, 20))
    draw.text((pad - left, pad - top), label, fill=(255, 255, 255), font=font)
    draw.rectangle((0, 0, box_w - 1, box_h - 1), outline=(210, 210, 210))
    return canvas


def _grid(count):
    """Panels ka arrangement. 1->1x1, 2->2x1, 3/4->2x2, 5/6->3x2."""
    cols = math.ceil(math.sqrt(count))
    return cols, math.ceil(count / cols)


def _font():
    try:
        return ImageFont.load_default(size=44)
    except TypeError:                              # bahut purani Pillow
        return ImageFont.load_default()


def build(urls, client=None):
    """URLs se ek JPEG collage. Lauta ta hai `(bytes | None, meta dict)`.

    `None` tab hai jab ek bhi image nahi mili — us soorat me `get_product` bina tasveer
    ke chalti rehti hai. Ek product ki tasveer na milna kharid rok dene laayak baat nahi
    hai; wo error nahi, ek kami hai, aur meta usay batata hai.
    """
    urls = [u for u in (urls or []) if isinstance(u, str)][:MAX_PANELS]
    meta = {"requested": len(urls), "panels": 0, "rejected": []}
    if not urls:
        return None, meta

    owned = client is None
    client = client or httpx.Client()
    decoded = []
    try:
        for url in urls:
            try:
                decoded.append(_decode(_fetch(url, client)))
            except Exception as e:                 # guard, network, ya kharab bytes
                meta["rejected"].append({"url": url[:120], "why": str(e)[:120]})
    finally:
        if owned:
            client.close()

    if not decoded:
        return None, meta

    cols, rows = _grid(len(decoded))
    box_w, box_h = CANVAS // cols, CANVAS // rows
    sheet = Image.new("RGB", (box_w * cols, box_h * rows), PAD)
    font = _font()
    for index, image in enumerate(decoded):
        sheet.paste(_panel(image, box_w, box_h, index + 1, font),
                    ((index % cols) * box_w, (index // cols) * box_h))

    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    meta.update(panels=len(decoded), grid="%dx%d" % (cols, rows),
                canvas="%dx%d" % (sheet.width, sheet.height), bytes=out.tell())
    return out.getvalue(), meta


DISABLE_ENV = "ACP_DEMO_NO_IMAGES"      # naap ke liye: tasveer ka kharcha kitna hai

# Do alag counters, kyunki wo do alag cheezein naapte hain — aur ek hi rakhne se cache
# ka test **vacuous** ho gaya tha. Pehla roop sirf `_cached` ke miss ginta tha, to cache
# hata dene par bhi wo counter nahi hilta tha aur test green rehta tha. Todkar dekha,
# tabhi pata chala: `FETCHES` ab wahan badhta hai jahan sach me CDN chhua jata hai.
FETCHES = 0             # CDN se kitni baar bytes maange gaye
BUILDS = 0              # collage kitni baar dobara bana (cache miss)


@lru_cache(maxsize=32)
def _cached(_merchant_id, _product_id, _updated_at, urls):
    global BUILDS
    BUILDS += 1
    return build(list(urls))


def for_product(merchant_id, product, cache=True):
    """`(bytes | None, meta)` — cache key `(merchant_id, product_id, updated_at, urls)`.

    Cache D-10 ko todta nahi. D-10 kehta hai paisa index se kabhi nahi chalega, aur wo
    **price aur stock** ke baare me hai — money facts. Tasveer money fact nahi hai. Aur
    key khud saaf ho jati hai: `SPEC.md` §4 merchant se maangti hai ki images badalne par
    bhi `updated_at` badle, to purani key apne aap bekaar ho jati hai (URLs bhi key me
    hain, to ek laparwah merchant bhi purani tasveer nahi chipka sakta).

    Bina cache ke har live `get_product` par 3-4 CDN fetch + resize hota — yaani wo call
    jo aaj milliseconds me lautti hai, seconds leti, aur wahi call order banane se pehle
    bhi hoti hai.
    """
    if os.getenv(DISABLE_ENV) == "1":
        # Naap wala switch, sanitizer wale (D-87) ke bilkul saanche me. Tasveer ka
        # kharcha ek asli number hai aur wo sirf "images ON vs OFF" chalakar hi nikalta
        # hai. Ise patch script se nahi, ek declared switch se karna hai — us tareeke ne
        # ek baar `server.py` ko toota hua disk pe chhod diya tha. Production me ye
        # switch nahi jata (ARCHITECTURE 12).
        return None, {"requested": 0, "panels": 0, "rejected": [],
                      "disabled_by": DISABLE_ENV}
    urls = tuple(u for u in (product.get("images") or []) if isinstance(u, str))
    if not cache:
        return build(list(urls[:MAX_PANELS]))
    return _cached(merchant_id, product.get("product_id"), product.get("updated_at"), urls)


if __name__ == "__main__":          # runnable check: guards sach me rokte hain
    import json
    assert _grid(1) == (1, 1) and _grid(3) == (2, 2) and _grid(6) == (3, 2)
    for bad in ("http://x/a.jpg", "ftp://x/a.jpg", None, ""):
        try:
            _fetch(bad, httpx.Client())
            raise AssertionError("guard let %r through" % (bad,))
        except Rejected:
            pass
    bomb = Image.new("RGB", (10, 10))
    blob = io.BytesIO()
    bomb.save(blob, format="PNG")
    assert _decode(blob.getvalue()).size == (10, 10)
    sheet, meta = build(["https://cdn.dummyjson.com/product-images/mens-shirts/"
                         "man-short-sleeve-shirt/1.webp"])
    print(json.dumps(meta, indent=2))
    if sheet:
        print("collage bytes:", len(sheet))
