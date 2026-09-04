"""Catalog sync: merchant ke /agent/catalog se index bharna.

Pehli baar poori pagination, uske baad sirf delta — `updated_since` ke saath. Webhooks
nahi hain (D-13): merchant kuch push nahi karta, Layer poochti hai.
"""
from datetime import datetime, timedelta, timezone

import db
import registry

PAGE_LIMIT = 100
ISO = "%Y-%m-%dT%H:%M:%SZ"


def now_iso():
    return datetime.now(timezone.utc).strftime(ISO)


def rewind_one_second(stamp):
    """Watermark ko ek second peeche karke poochta hai. Ye defence-in-depth hai.

    Asli guarantee SPEC 4 deta hai: `updated_at` **store-wide** monotonic hai, yaani
    watermark T ke baad hua koi bhi change `> T` hi hoga. Wo rule conformance suite
    test karti hai.

    Par ye rule teesri party nibhati hai. Merchant se galti hui - maan lo usne
    per-product monotonic likh diya, jo bilkul sahi lagta hai - to us galti ka nateeja
    ye hota hai ki kuch products **chupchap** index se gayab ho jate hain. Koi error
    nahi, koi crash nahi, bas catalog dheere-dheere jhootha. Ye wo tarah ki failure hai
    jo mahinon chhupi reh sakti hai.

    Boundary second dobara maangna ek subtraction hai aur ek page ka re-fetch. Upsert
    idempotent hai, to dobara aayi rows ka koi nuksaan nahi - wo `changed` me nahi
    gintin. Itni si keemat pe ek chupi hui data-loss failure kam ho jati hai.

    ponytail: ye 1 second ki khidki sirf same-second collision bachati hai. Merchant
    agar store-wide rule tode aur uska drift 1s se zyada ho, to bhi rows chhoot sakti
    hain - us haalat me conformance suite red hogi, aur wahi sahi jagah hai use pakadne
    ki.
    """
    if not stamp:
        return None
    return (datetime.strptime(stamp, ISO).replace(tzinfo=timezone.utc)
            - timedelta(seconds=1)).strftime(ISO)


def sync_merchant(conn, merchant_id, page_limit=PAGE_LIMIT):
    """Ek merchant ka catalog index me lata hai. Lauta ta hai kya hua.

    Do baatein jo chupke se todti hain, aur dono yahin sambhali gayi hain:

    1. **Watermark `now()` nahi hota — ingested rows ka `max(updated_at)` hota hai.**
       `now()` likhne ka matlab: sync ke *dauran* merchant ne jo rows badleen, unka
       `updated_at` watermark se peeche reh jayega, aur `updated_since` strictly-greater
       hai — wo rows dobara kabhi nahi aayengi. Chupchap, bina kisi error ke.

    2. **Watermark tabhi likhta hai jab poori pagination khatam ho.** Beech me merchant
       gir gaya to purana watermark bacha rehta hai, to agli baar wahin se dobara shuru
       hoga. Aadha-adhoora watermark likhna beech ke pages ko hamesha ke liye kho deta.
       Failed sync purana index chhodta hai — stale search theek hai, khaali catalog nahi
       (ARCHITECTURE 6.1).
    """
    row = conn.execute("SELECT * FROM merchants WHERE merchant_id=?",
                       (merchant_id,)).fetchone()
    if row is None:
        raise KeyError("merchant not registered: " + merchant_id)

    since = row["watermark"]
    ask_since = rewind_one_second(since)     # boundary second dobara maango
    started = now_iso()
    cursor, pages, fetched, changed = None, 0, 0, 0
    high_water = since

    try:
        with registry.client(row) as api:
            while True:
                params = {"limit": page_limit}
                if ask_since:
                    params["updated_since"] = ask_since
                if cursor:
                    params["cursor"] = cursor
                response = api.get("/agent/catalog", params=params)
                if response.status_code == 429:
                    raise RuntimeError("merchant rate limited the sync (429)")
                response.raise_for_status()
                page = response.json()

                for product in page["products"]:
                    _, was_changed = db.upsert_product(conn, merchant_id, product, started)
                    fetched += 1
                    changed += 1 if was_changed else 0
                    if high_water is None or product["updated_at"] > high_water:
                        high_water = product["updated_at"]

                pages += 1
                if not page.get("has_more"):
                    break
                cursor = page.get("cursor")
                if not cursor:
                    raise RuntimeError("has_more was true but no cursor was returned")
                if pages > 1000:
                    raise RuntimeError("pagination did not terminate")
    except Exception as e:
        conn.execute("UPDATE merchants SET healthy=0, last_error=?, last_checked_at=?"
                     " WHERE merchant_id=?", (str(e)[:300], now_iso(), merchant_id))
        return {"merchant_id": merchant_id, "ok": False, "error": str(e)[:300],
                "fetched": fetched, "changed": changed, "pages": pages,
                "watermark": since}

    # Poori pagination kaamyaab hui, yaani is merchant ka poora catalog abhi ke
    # hisaab se sach hai - sirf wo rows nahi jo is baar badli thin. Delta sync me
    # "kuch nahi badla" bhi ek jawab hai: purani row abhi bhi current hai. Sirf badli
    # hui rows ka synced_at chhune se index ki umar hamesha purani dikhti thi, jisse
    # freshness metric jhootha ho jata aur TTL check har baar sync karwata.
    conn.execute("UPDATE products SET synced_at=? WHERE merchant_id=?",
                 (started, merchant_id))
    if high_water and high_water != since:
        conn.execute("UPDATE merchants SET watermark=? WHERE merchant_id=?",
                     (high_water, merchant_id))
    return {"merchant_id": merchant_id, "ok": True, "fetched": fetched,
            "changed": changed, "pages": pages, "was_full_sync": since is None,
            "watermark": high_water}


def sync_all(conn):
    return [sync_merchant(conn, r["merchant_id"]) for r in
            conn.execute("SELECT merchant_id FROM merchants WHERE healthy=1")]


FRESHNESS_SECONDS = 15 * 60
RECHECK_SECONDS = 60


def recheck_unhealthy(conn, backoff=RECHECK_SECONDS):
    """Jo merchant unhealthy pade hain, unhe wapas aane ka mauka do.

    Iske bina ek band loop ban jata hai, aur wo loop chupchap poore Layer ko andha kar
    deta hai:

        merchant ek pal ke liye neeche gaya  -> healthy = 0
        search  `WHERE m.healthy = 1` maangti hai        -> hamesha 0 results
        sync_all bhi `WHERE healthy=1` pe chalti hai     -> us merchant ka sync hi nahi
        healthy = 1 sirf check_health() likhta hai, aur use koi bulata hi nahi tha

    Yaani merchant wapas aa jaye, index poori bhari ho, aur phir bhi Layer use kabhi na
    dikhaye - jab tak koi haath se `python layer/sync.py` na chalaye. Ye chize wo hai jo
    recording ke din merchant restart karte hi demo maar deti.

    `ARCHITECTURE.md` 5.2 shuru se kehta tha ki manifest periodically liveness check ke
    roop me fetch hota hai. Wo likha to tha, hua nahi tha - ye us vaade ka code hai.

    Backoff isliye ki ek down merchant har search me apna timeout na jode: har merchant
    ko minute me ek koshish. Healthy merchants ko chhua hi nahi jata - unke liye ye
    function muft hai.

    ponytail: recovery search ke saath piggyback karti hai; alag scheduler tab jab
    merchants itne ho jayen ki ek-ek minute ki koshish bhi mehngi lage.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=backoff)).strftime(ISO)
    stale = conn.execute(
        "SELECT merchant_id FROM merchants WHERE healthy = 0"
        " AND (last_checked_at IS NULL OR last_checked_at < ?)", (cutoff,)).fetchall()
    recovered = []
    for row in stale:
        try:
            if registry.check_health(conn, row["merchant_id"])["healthy"]:
                recovered.append(row["merchant_id"])
        except Exception:
            pass        # abhi bhi neeche hai - check_health khud last_error likh chuka hai
    return recovered


def maybe_sync(conn, ttl=FRESHNESS_SECONDS):
    """Index purana ho to search se PEHLE delta kheench lo. Scheduler nahi hai.

    Ek background scheduler ke liye ek aur process, uska lifecycle, aur "wo chal raha
    hai ya nahi" wala ek aur sawaal chahiye — sirf isliye ki index taaza rahe. Delta
    sync waise bhi sasta hai (badli hui rows hi aati hain, aksar zero), to jis waqt
    kisi ko taaza data chahiye usi waqt kheench lena kaafi hai. Isse "sync freshness"
    metric ek dawa nahi, ek guarantee ban jati hai: search kabhi ttl se zyada purana
    nahi.

    Sync fail hone pe search rukti NAHI — purana index stale hai, khaali nahi (D-47).

    ponytail: per-search TTL check; kai Layer process ya bade catalog pe scheduler.
    """
    # Pehle recovery, phir freshness. Ulta karne se ek wapas aaya hua merchant apni baari
    # ka intezaar karta rehta: uski rows purani hain par baaki merchants taaza hain, to
    # median age chhoti nikalti hai aur TTL gate kabhi khulta hi nahi.
    recovered = recheck_unhealthy(conn)
    age = index_age_seconds(conn)
    if not recovered and age is not None and age < ttl:
        return None
    try:
        return sync_all(conn)
    except Exception:
        return None


def index_age_seconds(conn):
    """Sync freshness metric (US 15): index row ko aakhri baar current confirm kiye
    kitna waqt hua, search ke waqt.

    "Umar" ka matlab "row kab likhi gayi" nahi hai - delta sync me ek row mahinon na
    badle aur phir bhi current ho. Umar wo hai jab merchant ne aakhri baar poora catalog
    confirm kiya (dekho sync_merchant ka aakhri UPDATE).
    """
    rows = conn.execute("SELECT synced_at FROM products").fetchall()
    if not rows:
        return None
    now = datetime.now(timezone.utc)
    ages = sorted((now - datetime.strptime(r["synced_at"], "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc)).total_seconds() for r in rows)
    return ages[len(ages) // 2]


def forget_watermark(conn, merchant_id=None):
    """Watermark mita do, taaki agli sync poora catalog dobara padhe.

    **Ye ek RESEED ke baad chahiye hi chahiye, aur wajah chup-chaap kaam karti hai.**
    Delta sync `updated_since` par **strictly greater** hai, aur ek reseed catalog ke
    saare `updated_at` ko peeche le jata hai (humare seed scripts base ko `now - 30 din`
    par rakhte hain, jaan-boojhkar). Nateeja: reseed ke baad `sync` **0 rows** lauta ti
    hai — koi error nahi, koi chetavni nahi — aur index purana stock aur purane daam
    dikhata rehta hai jabki dukaan me kuch aur pada hai.

    Ye merchant ki galti hai, Layer ki nahi: SPEC 4 store-wide monotonic `updated_at`
    maangti hai aur reseed usi rule ko todta hai. Par reseed ek jayaz dev operation hai,
    aur Layer ke paas use pehchanne ka koi raasta nahi — isliye uske baad ye command
    chalani hai.

    ponytail: ye rows hataata nahi, sirf sabko dobara padhta hai. Agar reseed ne koi
    product **hata** diya ho to uski purani row index me reh jayegi. Aaj tak seed ids
    sthir hain; jis din na rahen, yahan delete bhi jodna padega.
    """
    if merchant_id:
        conn.execute("UPDATE merchants SET watermark=NULL WHERE merchant_id=?", (merchant_id,))
    else:
        conn.execute("UPDATE merchants SET watermark=NULL")


if __name__ == "__main__":
    import sys

    full = "--full" in sys.argv
    only = next((a for a in sys.argv[1:] if not a.startswith("-")), None)

    conn = db.connect()
    for result in registry.register_all(conn):
        print("registry:", result)
    if full:
        forget_watermark(conn, only)
        print("watermark: forgotten for", only or "every merchant",
              "— agli sync poora catalog padhegi")
    for result in sync_all(conn):
        if only and result["merchant_id"] != only:
            continue
        print("sync    :", result)
