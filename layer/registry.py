"""Merchant registry: kaun registered hai, kahan rehta hai, zinda hai ya nahi.

Config file me sirf `key_env` hota hai — ENV variable ka NAAM, key khud nahi. Registry
file repo me ja sakti hai; keys `.env` me rehti hain aur wahin rehni chahiye.
"""
import json
import os
import pathlib
import time

import httpx
from dotenv import load_dotenv

import db
import policy

CONFIG_PATH = pathlib.Path(__file__).parent / "merchants.json"
load_dotenv(pathlib.Path(__file__).parents[1] / ".env")

TIMEOUT = 10.0
RETRY_ATTEMPTS = 3
MAX_RETRY_AFTER = 10.0      # merchant jo bhi maange, itni der se zyada nahi rukte


class HonourRetryAfter(httpx.BaseTransport):
    """`429` pe `Retry-After` maankar dobara koshish karta hai.

    Ye sirf ek behtari nahi hai - **SPEC 2.7 merchant se ye vaada karti hai:** *"You may
    return 429 with a Retry-After header in seconds. The Layer honours it."* Wo vaada
    likha hua tha aur kahin nibhaya nahi ja raha tha: `sync.py` 429 pe poora sync fail
    kar deti thi aur `orders.py` us header ko dekhti hi nahi thi. Yaani ek merchant jo
    theek waisa hi bartav karta jaisa contract kehta hai, use Layer se ek nakaam order
    milta - aur galti humari hoti, uski nahi. (Wahi shakal jo ARCHITECTURE 5.2 ke health
    check ki thi: doc me likha, code me kahin nahi.)

    Retry order-create pe bhi surakshit hai kyunki `Idempotency-Key` lazmi hai (D-16):
    dobara bheji gayi wahi request wahi order lauta ti hai, doosra nahi banati.

    ponytail: transport pe isliye lagaya hai ki `registry.client()` se hokar **har**
    merchant call guzarti hai - ek jagah, aur koi naya call site ise bhoolkar nahi likh
    sakta. Per-call retry likhna matlab har call site pe ek aur cheez yaad rakhna.
    """

    def __init__(self, inner=None, attempts=RETRY_ATTEMPTS, sleep=time.sleep):
        self.inner = inner or httpx.HTTPTransport()
        self.attempts = attempts
        self.sleep = sleep

    def handle_request(self, request):
        for attempt in range(self.attempts):
            response = self.inner.handle_request(request)
            if response.status_code != 429 or attempt == self.attempts - 1:
                return response
            response.read()          # stream band kiye bina response phenka nahi ja sakta
            response.close()
            self.sleep(self.wait_for(response))
        return response

    @staticmethod
    def wait_for(response):
        """`Retry-After` seconds me hota hai (SPEC 2.7). Kachra aaye to 1 second."""
        try:
            seconds = float(response.headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            seconds = 1.0
        return min(max(seconds, 0.0), MAX_RETRY_AFTER)


class MerchantBudget(httpx.BaseTransport):
    """Layer kisi ek merchant ko ek window me itni hi call bhejegi — kaun bhi maange.

    Ye wo vaada hai jo `ARCHITECTURE.md` §7.3 merchant se karta hai: *"N agents -> 1
    Layer -> M merchants; a merchant trusts exactly one caller."* Wo vaada likha hua tha
    aur nibhaya nahi ja raha tha. Har agent ki call seedha aage bhej dena matlab merchant
    ko N callers mil rahe the, sirf ek IP se.

    **Transport pe isliye, kisi call site pe nahi.** `registry.client()` se hokar har
    merchant call guzarti hai — yaani koi naya call site ise bhoolkar nahi likh sakta.
    Yahi wajah D-78 me `HonourRetryAfter` ke liye thi, aur wahi yahan bhi lagti hai.

    **Ye sabse BAHAR hai, `HonourRetryAfter` ke upar.** Ek agent ki ek request = budget ki
    ek ginti. Retry uske andar tabhi hota hai jab **merchant ne khud** 429 kaha ho, aur wo
    merchant ka apna bachav hai — use humare budget ke against ginna ulta hoga.

    Budget tootne pe ek asli `429` lauta ta hai, SPEC ke envelope me, aur `origin: layer`
    ke saath — kyunki ye merchant ka rate limit nahi hai, humara apna hai, aur agent ko
    jhooth batana ("merchant busy hai") use galat jagah dekhne bhejta hai.
    """

    def __init__(self, merchant_id, inner=None):
        self.merchant_id = merchant_id
        self.inner = inner or HonourRetryAfter()

    def handle_request(self, request):
        verdict = policy.check_merchant_rate(self.merchant_id)
        if verdict["allowed"]:
            return self.inner.handle_request(request)
        return httpx.Response(
            429, headers={"Retry-After": str(verdict["retry_after"])},
            json={"error": {"code": "RATE_LIMITED", "message": verdict["reason"],
                            "details": {"origin": "layer",
                                        "merchant_id": self.merchant_id,
                                        "retry_after_seconds": verdict["retry_after"],
                                        "window_seconds": policy.RATE_WINDOW_SECONDS,
                                        "limit_per_window":
                                            policy.MERCHANT_CALLS_PER_WINDOW}}},
            request=request)


class MerchantUnreachable(Exception):
    pass


def load_config(path=CONFIG_PATH):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def agent_key(merchant_row_or_cfg):
    """Key hamesha env se aati hai, DB se ya config se kabhi nahi."""
    env_name = merchant_row_or_cfg["key_env"]
    key = os.getenv(env_name)
    if not key:
        raise MerchantUnreachable(env_name + " is not set in the environment")
    return key


def no_retry(merchant_id):
    """Retry ke bina transport — un callers ke liye jo KHUD ek retry loop hain.

    Budget phir bhi lagta hai (`MerchantBudget`): retry chhodna ek baat hai, merchant ko
    bina seema ke call karna bilkul doosri.

    Backoff do jagah lagana ek 429 ko guna kar deta hai: `pay_order` ka poll loop 6 baar
    poochta tha, aur har poochne ke andar transport 3 baar aur poochta tha. Naapa gaya:
    12 second ka socha hua budget **70 second** ban gaya aur jis merchant ne "slow down"
    kaha tha usi ko **18** call gayin. Jo caller khud retry karta hai, wahi `Retry-After`
    ka hisaab bhi rakhega - ek hi jagah.
    """
    return MerchantBudget(merchant_id, httpx.HTTPTransport())


def client(merchant, transport=None):
    return httpx.Client(base_url=merchant["base_url"],
                        headers={"X-Agent-Key": agent_key(merchant)}, timeout=TIMEOUT,
                        transport=transport or MerchantBudget(merchant["merchant_id"]))


def register_all(conn, path=CONFIG_PATH):
    """Config ki har entry ko DB me daalta hai (base_url badla to update), phir health
    check karta hai. Watermark chhua nahi jata - re-register se index reset nahi hota."""
    results = []
    for cfg in load_config(path):
        conn.execute(
            "INSERT INTO merchants (merchant_id, base_url, key_env) VALUES (?,?,?)"
            " ON CONFLICT(merchant_id) DO UPDATE SET"
            "   base_url=excluded.base_url, key_env=excluded.key_env",
            (cfg["merchant_id"], cfg["base_url"], cfg["key_env"]))
        results.append(check_health(conn, cfg["merchant_id"]))
    return results


def check_health(conn, merchant_id, now_iso=None):
    """Manifest fetch karke declared policies store karta hai. Ye registration bhi hai
    aur liveness check bhi — dono ek hi call, kyunki manifest cheap aur cacheable hai
    (SPEC 3).

    Jo merchant jawab nahi deta wo `healthy = 0` ho jata hai aur search se **hat** jata
    hai — flow ke beech me error phenkne se behlate hue browse karna behtar hai
    (ARCHITECTURE 5.2). Uska index nahi mitta: purana data stale hai, khaali nahi.
    """
    from datetime import datetime, timezone
    now_iso = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute("SELECT * FROM merchants WHERE merchant_id = ?",
                       (merchant_id,)).fetchone()
    if row is None:
        raise KeyError("merchant not registered: " + merchant_id)

    try:
        with client(row) as api:
            response = api.get("/agent/manifest")
        if response.status_code != 200:
            raise MerchantUnreachable("manifest returned HTTP %d" % response.status_code)
        manifest = response.json()
        if manifest.get("spec_version") != "1.0":
            raise MerchantUnreachable(
                "unsupported spec_version " + repr(manifest.get("spec_version")))
        if manifest.get("merchant_id") != merchant_id:
            raise MerchantUnreachable(
                "manifest declares merchant_id " + repr(manifest.get("merchant_id"))
                + " but is registered as " + repr(merchant_id))
    except (httpx.HTTPError, MerchantUnreachable, ValueError) as e:
        conn.execute("UPDATE merchants SET healthy=0, last_error=?, last_checked_at=?"
                     " WHERE merchant_id=?", (str(e)[:300], now_iso, merchant_id))
        return {"merchant_id": merchant_id, "healthy": False, "error": str(e)[:300]}

    conn.execute(
        "UPDATE merchants SET name=?, currency=?, categories=?, payment_modes=?,"
        " shipping=?, policies=?, product_count=?, healthy=1, last_error=NULL,"
        " last_checked_at=? WHERE merchant_id=?",
        (manifest["name"], manifest["currency"], db.jd(manifest["categories"]),
         db.jd(manifest["payment_modes"]), db.jd(manifest["shipping"]),
         db.jd(manifest["policies"]), (manifest.get("catalog") or {}).get("product_count"),
         now_iso, merchant_id))
    return {"merchant_id": merchant_id, "healthy": True,
            "name": manifest["name"], "product_count":
                (manifest.get("catalog") or {}).get("product_count")}


def list_merchants(conn, healthy_only=True):
    sql = "SELECT * FROM merchants"
    if healthy_only:
        sql += " WHERE healthy = 1"
    return [{
        "merchant_id": r["merchant_id"], "name": r["name"], "currency": r["currency"],
        "categories": db.jl(r["categories"], []),
        "payment_modes": db.jl(r["payment_modes"], []),
        "shipping": db.jl(r["shipping"], {}), "policies": db.jl(r["policies"], {}),
        "healthy": bool(r["healthy"]), "last_error": r["last_error"],
        "last_checked_at": r["last_checked_at"],
        "indexed_products": conn.execute(
            "SELECT COUNT(*) c FROM products WHERE merchant_id=?",
            (r["merchant_id"],)).fetchone()["c"],
    } for r in conn.execute(sql + " ORDER BY merchant_id")]
