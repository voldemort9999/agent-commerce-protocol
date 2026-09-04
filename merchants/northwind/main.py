"""Northwind Apparel - Agent Commerce Protocol v1.0 merchant (SPEC.md).

    uvicorn merchants.northwind.main:app --port 8001 --reload

Do darwaze, ek database: /agent/* agents ke liye, / aur /p/{id} insaanon ke liye.
"""
import base64
import binascii
import hashlib
import json
import os
import pathlib
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import db  # noqa: E402
import models as m  # noqa: E402

load_dotenv(pathlib.Path(__file__).parents[2] / ".env")

# ---------------------------------------------------------------- store config
MERCHANT_ID = "northwind-apparel"
NAME = "Northwind Apparel"
CURRENCY = "INR"
PAYMENT_MODES = ["payment_link", "checkout", "cod"]
SHIPPING_FLAT_PAISE = 4900
FREE_SHIPPING_ABOVE_PAISE = 99900
CANCEL_WINDOW_HOURS = 48
MAX_QTY_PER_VARIANT = 10
ETA_DAYS = 3

# Bina pay hue order kitni der stock roke rakh sakta hai. Iske baad order `failed` hota
# hai aur stock wapas shelf pe (SPEC v1.5 9). Ye merchant ki apni ghadi hai, provider ki
# nahi - Razorpay ka checkout order khud kabhi expire nahi hota.
UNPAID_ORDER_TTL_MINUTES = 30

AGENT_KEY = os.getenv("NORTHWIND_AGENT_KEY", "nw_agentkey_local_dev_only")

# E.164 (SPEC 6): `+`, ek non-zero country digit, kul 8-15 ank.
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


# Q-04: Northwind poorvottar (pincode "7" se shuru) deliver nahi karta. Voltline karega -
# isse "ek merchant ne mana kiya, agent ne doosre se le liya" wala failure-recovery shot
# banta hai. Ek line ka rule, video me bolne layak.
def serviceable(pincode: str) -> bool:
    return not pincode.startswith("7")


# Q-05: teesra coupon jaan-boojhkar toota hua hai - INVALID_COUPON demo ke liye.
COUPONS = {
    "WELCOME10": {"kind": "pct", "value": 10, "cap_paise": 50000, "min_paise": 0},
    "FLAT200": {"kind": "flat", "value": 20000, "cap_paise": None, "min_paise": 150000},
    "EXPIRED50": None,
}

app = FastAPI(title=NAME + " - Agent Commerce Protocol", version=m.SPEC_VERSION)
templates = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))
templates.env.filters["from_json"] = json.loads   # JSON columns template me seedhe khulen


# ---------------------------------------------------------------- error envelope
class SpecError(Exception):
    def __init__(self, status, code, message, details=None, headers=None):
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = details, headers


@app.exception_handler(SpecError)
async def spec_error_handler(_request, exc: SpecError):
    body = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status, content={"error": body},
                        headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_handler(_request, exc: RequestValidationError):
    """FastAPI khud 422 phenkta hai apne shape me. SPEC 11 kehta hai required field
    missing = 400 MISSING_FIELD, humare envelope me. Ek jagah badal do - har endpoint
    ke har body/query field pe lag jata hai."""
    return JSONResponse(status_code=400, content={"error": {
        "code": "MISSING_FIELD",
        "message": "A required field is absent or malformed.",
        "details": {"errors": json.loads(json.dumps(exc.errors(), default=str))[:5]}}})


def require_key(x_agent_key):
    if x_agent_key != AGENT_KEY:
        raise SpecError(401, "UNAUTHORIZED", "Missing or invalid X-Agent-Key.")


# ---------------------------------------------------------------- helpers
def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def shipping_for(items_total_paise: int) -> int:
    return 0 if items_total_paise >= FREE_SHIPPING_ABOVE_PAISE else SHIPPING_FLAT_PAISE


def discount_for(code, items_total_paise: int) -> int:
    if not code:
        return 0
    rule = COUPONS.get(code.upper(), "unknown")
    if rule is None or rule == "unknown":
        raise SpecError(409, "INVALID_COUPON", "Coupon " + code + " is not valid.",
                        {"coupon_code": code})
    if items_total_paise < rule["min_paise"]:
        raise SpecError(409, "INVALID_COUPON",
                        "Coupon " + code + " needs a higher order value.",
                        {"coupon_code": code, "min_items_total_paise": rule["min_paise"],
                         "items_total_paise": items_total_paise})
    amount = (items_total_paise * rule["value"] // 100) if rule["kind"] == "pct" else rule["value"]
    if rule["cap_paise"]:
        amount = min(amount, rule["cap_paise"])
    return min(amount, items_total_paise)


def encode_cursor(updated_at: str, product_id: str) -> str:
    raw = (updated_at + "|" + product_id).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str):
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        updated_at, product_id = raw.split("|", 1)
        return updated_at, product_id
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise SpecError(400, "MISSING_FIELD", "cursor is malformed.", {"cursor": cursor})


_rzp = None


def razorpay_client():
    """Lazy - keys ke bina bhi read endpoints aur UI chalne chahiye."""
    global _rzp
    if _rzp is None:
        import razorpay
        key_id, secret = os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")
        if not key_id or not secret:
            raise SpecError(500, "INTERNAL_ERROR", "Razorpay keys are not configured.")
        _rzp = razorpay.Client(auth=(key_id, secret))
    return _rzp


def razorpay_call(fn, *args):
    """Provider ki dikkat ko SPEC 11 ke error code me badalta hai.

    Razorpay payment-link creation pe rate limit lagata hai. Bina iske wo 500 banta tha -
    aur 500 batata hai 'humara code toota', jabki asal me 'thodi der baad try karo' hai.
    Farq mayne rakhta hai: 429 pe agent retry karta hai, 500 pe haar jata hai.
    """
    try:
        return fn(*args)
    except SpecError:
        raise
    except Exception as e:
        text = str(e).lower()
        if "too many requests" in text or "rate limit" in text:
            raise SpecError(429, "RATE_LIMITED", "Payment provider is rate limiting. Retry shortly.",
                            {"provider": "razorpay"}, headers={"Retry-After": "5"})
        if "exceeds maximum amount" in text or "amount exceeds" in text:
            # Har instrument ka apna ceiling hota hai (link, UPI, card - sabka alag).
            # Ye 500 nahi hai: humara code theek chala, provider ne IS amount ko is
            # instrument pe mana kiya. Aur ye 429 bhi nahi hai - dobara try karne se
            # kabhi nahi chalega. Agent ko ye farq chahiye, warna wo ya to haar jayega
            # ya hamesha ke liye retry karta rahega.
            raise SpecError(409, "AMOUNT_LIMIT_EXCEEDED",
                            "This total is above what the selected payment mode can carry.",
                            {"provider": "razorpay", "provider_message": str(e)[:200]})
        raise SpecError(500, "INTERNAL_ERROR", "Payment provider rejected the request.",
                        {"provider": "razorpay", "provider_message": str(e)[:200]})


CATALOG_SELECT = """
SELECT p.*,
       COALESCE(MIN(CASE WHEN v.stock > 0 THEN v.price_paise END), MIN(v.price_paise)) AS pmin,
       COALESCE(MAX(CASE WHEN v.stock > 0 THEN v.price_paise END), MAX(v.price_paise)) AS pmax,
       COUNT(v.variant_id) AS vcount,
       MAX(v.stock)        AS maxstock
FROM products p JOIN variants v ON v.product_id = p.product_id
"""


def row_to_catalog_product(r) -> dict:
    return {
        "product_id": r["product_id"], "title": r["title"], "description": r["description"],
        "category": r["category"], "tags": db.jl(r["tags"], []), "brand": r["brand"],
        "images": db.jl(r["images"], []),
        "price_range_paise": {"min": r["pmin"], "max": r["pmax"]},
        "in_stock": r["maxstock"] > 0,
        "variant_options": db.jl(r["variant_options"], []),
        "variant_count": r["vcount"],
        "rating_avg": r["rating_avg"], "rating_count": r["rating_count"],
        "updated_at": r["updated_at"],
    }


def bump_products_for(c, variant_ids):
    """SPEC 4 + 12: stock badla to catalog ka updated_at bhi badalna hai, aur nayi value
    poore STORE me abhi tak di gayi har value se badi honi chahiye.

    Yahan do baar galti hui, aur doosri wali pehli se gehri thi:

    1. Pehle `= now()` tha. Second-resolution timestamp: ek hi second me do change ->
       dono ki value same -> Layer ka strictly-greater delta doosre ko kabhi nahi dekhta.
    2. Phir `max(now, is product ka purana + 1s)` kiya - yaani PER-PRODUCT monotonic.
       Ye bhi toota. Baar-baar bump hone se ek product ka stamp wall clock se aage nikal
       jata hai; phir kisi DOOSRE product ka baad wala change chhota stamp leta hai.
       Layer ka watermark global hai, to wo chhota stamp watermark se peeche reh jata
       hai aur wo product hamesha ke liye gayab ho jata hai.

    Delta sync ko jo chahiye wo ye hai: "watermark T ke baad hua koi bhi change
    `updated_at > T` de". Wo sirf STORE-WIDE monotonicity se milta hai - nayi value
    poore catalog ke max se badi ho, sirf apne purane se nahi.
    """
    placeholders = ",".join("?" * len(variant_ids))
    store_max = c.execute("SELECT MAX(updated_at) m FROM products").fetchone()["m"]
    stamp = iso(now())
    if store_max and store_max >= stamp:
        stamp = iso(parse_iso(store_max) + timedelta(seconds=1))
    c.execute(
        "UPDATE products SET updated_at = ? WHERE product_id IN "
        "(SELECT product_id FROM variants WHERE variant_id IN (" + placeholders + "))",
        [stamp] + list(variant_ids))


# ---------------------------------------------------------------- 1. manifest
@app.get("/agent/manifest", response_model=m.Manifest)
def manifest(x_agent_key: str | None = Header(None)):
    require_key(x_agent_key)
    with db.connect() as c:
        row = c.execute("SELECT COUNT(*) n, MAX(updated_at) last FROM products").fetchone()
        # SPEC 4 kehta hai har product ki category manifest me honi chahiye. Hardcoded
        # list catalog se drift kar jati hai - DB se nikalo, phir drift mumkin hi nahi.
        categories = [r["category"] for r in
                      c.execute("SELECT DISTINCT category FROM products ORDER BY 1")]
    return m.Manifest(
        merchant_id=MERCHANT_ID, name=NAME, currency=CURRENCY,
        categories=categories, payment_modes=PAYMENT_MODES,
        shipping=m.Shipping(pincode_required=True, flat_paise=SHIPPING_FLAT_PAISE,
                            free_above_paise=FREE_SHIPPING_ABOVE_PAISE),
        policies=m.Policies(cancel_window_hours=CANCEL_WINDOW_HOURS,
                            max_qty_per_variant=MAX_QTY_PER_VARIANT),
        catalog=m.CatalogInfo(product_count=row["n"], last_updated_at=row["last"]),
    )


# ---------------------------------------------------------------- 2. catalog
@app.get("/agent/catalog", response_model=m.CatalogPage)
def catalog(x_agent_key: str | None = Header(None),
            updated_since: str | None = Query(None),
            cursor: str | None = Query(None),
            limit: int = Query(100, ge=1, le=500)):
    require_key(x_agent_key)
    where, args = [], []
    if updated_since:
        where.append("p.updated_at > ?")          # strictly greater - SPEC 4
        args.append(updated_since)
    if cursor:
        cua, cpid = decode_cursor(cursor)
        where.append("(p.updated_at, p.product_id) > (?, ?)")
        args += [cua, cpid]
    sql = (CATALOG_SELECT
           + ("WHERE " + " AND ".join(where) if where else "")
           + " GROUP BY p.product_id ORDER BY p.updated_at, p.product_id LIMIT ?")
    with db.connect() as c:
        rows = c.execute(sql, args + [limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    cursor_out = encode_cursor(rows[-1]["updated_at"], rows[-1]["product_id"]) if has_more else None
    return m.CatalogPage(products=[row_to_catalog_product(r) for r in rows],
                         cursor=cursor_out, has_more=has_more)


# ---------------------------------------------------------------- 3. product detail
@app.get("/agent/products/{product_id}", response_model=m.ProductDetail)
def product_detail(product_id: str,
                   x_agent_key: str | None = Header(None),
                   pincode: str | None = Query(None)):
    require_key(x_agent_key)
    with db.connect() as c:
        p = c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
        if p is None:
            raise SpecError(404, "PRODUCT_NOT_FOUND", "No product with id " + product_id + ".",
                            {"product_id": product_id})
        variants = c.execute(
            "SELECT * FROM variants WHERE product_id = ? ORDER BY position", (product_id,)
        ).fetchall()

    delivery = None
    if pincode is not None:
        if not (len(pincode) == 6 and pincode.isdigit()):
            raise SpecError(400, "MISSING_FIELD", "pincode must be 6 digits.",
                            {"pincode": pincode})
        if serviceable(pincode):
            cheapest = min(v["price_paise"] for v in variants)
            delivery = m.Delivery(serviceable=True, pincode=pincode,
                                  shipping_paise=shipping_for(cheapest), eta_days=ETA_DAYS)
        else:
            # Fact hai, failure nahi - SPEC 5: 200 lautao, error nahi
            delivery = m.Delivery(serviceable=False, pincode=pincode)

    detail = m.ProductDetail(
        product_id=p["product_id"], title=p["title"],
        description=p["description"],              # D-17: verbatim, bilkul jaisa stored hai
        category=p["category"], tags=db.jl(p["tags"], []), brand=p["brand"],
        images=db.jl(p["images"], []), attributes=db.jl(p["attributes"], {}),
        variants=[{"variant_id": v["variant_id"], "sku": v["sku"],
                   "options": db.jl(v["options"], {}), "price_paise": v["price_paise"],
                   "mrp_paise": v["mrp_paise"], "stock": v["stock"],
                   "images": db.jl(v["images"], [])} for v in variants],
        rating_avg=p["rating_avg"], rating_count=p["rating_count"],
        reviews=db.jl(p["reviews"], []), related_product_ids=db.jl(p["related_ids"], []),
        delivery=delivery, updated_at=p["updated_at"],
    )
    # SPEC 5: ye endpoint kabhi cache se serve nahi hota - paisa isi ke price pe verify hota hai
    return JSONResponse(content=detail.model_dump(), headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- 4. create order
@app.post("/agent/orders", status_code=201, response_model=m.OrderCreated)
async def create_order(request: Request,
                       x_agent_key: str | None = Header(None),
                       idempotency_key: str | None = Header(None)):
    require_key(x_agent_key)
    if not idempotency_key:
        raise SpecError(400, "MISSING_FIELD", "Idempotency-Key header is required.")

    raw = await request.body()
    body_hash = hashlib.sha256(raw).hexdigest()
    with db.connect() as c:
        prior = c.execute("SELECT * FROM idempotency WHERE key = ?", (idempotency_key,)).fetchone()
    if prior:
        if prior["body_sha256"] != body_hash:
            raise SpecError(409, "IDEMPOTENCY_CONFLICT",
                            "This Idempotency-Key was used with a different body.",
                            {"idempotency_key": idempotency_key})
        return JSONResponse(status_code=201, content=json.loads(prior["response_json"]))

    try:
        req = m.CreateOrder.model_validate_json(raw)
    except ValueError as e:
        raise SpecError(400, "MISSING_FIELD", "Request body is invalid.",
                        {"errors": str(e)[:400]})

    if req.payment_mode not in PAYMENT_MODES:
        raise SpecError(400, "UNSUPPORTED_PAYMENT_MODE",
                        req.payment_mode + " is not offered by this store.",
                        {"payment_mode": req.payment_mode, "supported": PAYMENT_MODES})
    if not (len(req.address.pincode) == 6 and req.address.pincode.isdigit()):
        raise SpecError(400, "MISSING_FIELD", "address.pincode must be 6 digits.",
                        {"pincode": req.address.pincode})
    # SPEC 6 (v1.9): shakal jaanchna maujoodgi jaanchne se alag hai. `phone: "98765"` aur
    # `country: "ZZ"` dono se order ban jata tha — ek asli, chuka hua order jo deliver ho
    # hi nahi sakta.
    if not E164.match(req.contact.phone.strip()):
        raise SpecError(400, "MISSING_FIELD",
                        "contact.phone must be an E.164 number like +919876543210.",
                        {"field": "contact.phone", "phone": req.contact.phone})
    if req.address.country.strip().upper() != "IN":
        raise SpecError(400, "MISSING_FIELD",
                        NAME + " delivers within India only; address.country must be 'IN'.",
                        {"field": "address.country", "country": req.address.country})
    if not serviceable(req.address.pincode):
        raise SpecError(409, "NOT_SERVICEABLE",
                        NAME + " does not deliver to " + req.address.pincode + ".",
                        {"pincode": req.address.pincode})

    order_id = "ord_" + uuid.uuid4().hex[:12]
    created = now()
    cancellable_until = created + timedelta(hours=CANCEL_WINDOW_HOURS)

    c = db.connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        lines, items_total = [], 0
        for item in req.items:
            v = c.execute(
                "SELECT v.*, p.title FROM variants v "
                "JOIN products p ON p.product_id = v.product_id WHERE v.variant_id = ?",
                (item.variant_id,)).fetchone()
            if v is None:
                raise SpecError(400, "INVALID_VARIANT", "Unknown variant " + item.variant_id + ".",
                                {"variant_id": item.variant_id})
            if item.qty > MAX_QTY_PER_VARIANT:
                raise SpecError(409, "QTY_LIMIT_EXCEEDED",
                                "At most %d units per variant." % MAX_QTY_PER_VARIANT,
                                {"variant_id": item.variant_id, "requested_qty": item.qty,
                                 "max_qty_per_variant": MAX_QTY_PER_VARIANT})
            if v["price_paise"] != item.expected_price_paise:
                raise SpecError(409, "PRICE_CHANGED",
                                "Price for " + item.variant_id + " changed since it was quoted.",
                                {"variant_id": item.variant_id,
                                 "expected_price_paise": item.expected_price_paise,
                                 "actual_price_paise": v["price_paise"]})
            if v["stock"] < item.qty:
                raise SpecError(409, "OUT_OF_STOCK",
                                "Only %d left of %s." % (v["stock"], item.variant_id),
                                {"variant_id": item.variant_id, "requested_qty": item.qty,
                                 "available_qty": v["stock"]})
            opts = db.jl(v["options"], {})
            suffix = (" - " + " / ".join(opts.values())) if opts else ""
            line_total = v["price_paise"] * item.qty
            items_total += line_total
            lines.append({"variant_id": item.variant_id, "title": v["title"] + suffix,
                          "qty": item.qty, "unit_price_paise": v["price_paise"],
                          "line_total_paise": line_total})

        if items_total != req.expected_items_total_paise:
            raise SpecError(409, "TOTAL_CHANGED", "Items total differs from the quoted total.",
                            {"expected_items_total_paise": req.expected_items_total_paise,
                             "actual_items_total_paise": items_total})

        discount = discount_for(req.coupon_code, items_total)
        shipping = shipping_for(items_total)
        final_total = items_total + shipping - discount

        for item in req.items:
            c.execute("UPDATE variants SET stock = stock - ? WHERE variant_id = ?",
                      (item.qty, item.variant_id))
        bump_products_for(c, [i.variant_id for i in req.items])

        payment = build_payment(req, order_id, final_total, created)
        # Delivery ka waada order ke saath JAM jata hai. Bina iske "order kab aayega" ka
        # jawab surface pe kahin hota hi nahi tha - `eta_days` sirf `get_product?pincode=`
        # pe milta hai, yaani order banne se PEHLE - aur agent ya to "paid" ko shipping
        # status samajh kar bol deta, ya product dobara padhkar AAJ ka estimate de deta,
        # jo us order ka waada hai hi nahi.
        delivery = {"eta_days": ETA_DAYS, "pincode": req.address.pincode,
                    "promised_by": iso(created + timedelta(days=ETA_DAYS))}

        c.execute(
            "INSERT INTO orders (order_id,status,items,items_total_paise,shipping_paise,"
            "discount_paise,final_total_paise,contact,address,payment,coupon_code,timeline,"
            "delivery,created_at,cancellable_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, "created", db.jd(lines), items_total, shipping, discount, final_total,
             db.jd(req.contact.model_dump()), db.jd(req.address.model_dump()),
             db.jd(payment), req.coupon_code,
             db.jd([{"status": "created", "at": iso(created)}]), db.jd(delivery),
             iso(created), iso(cancellable_until)))

        response = m.OrderCreated(
            order_id=order_id, status="created", items=lines, items_total_paise=items_total,
            shipping_paise=shipping, discount_paise=discount, final_total_paise=final_total,
            currency=CURRENCY,
            payment=m.PaymentOut(mode=req.payment_mode, link_url=payment.get("link_url"),
                                 razorpay_order_id=payment.get("razorpay_order_id"),
                                 razorpay_key_id=payment.get("razorpay_key_id"),
                                 expires_at=payment.get("expires_at")),
            delivery=m.OrderDelivery(**delivery),
            cancellable_until=iso(cancellable_until), created_at=iso(created),
        ).model_dump()

        c.execute("INSERT INTO idempotency (key,body_sha256,response_json,created_at) "
                  "VALUES (?,?,?,?)",
                  (idempotency_key, body_hash, db.jd(response), iso(created)))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    return JSONResponse(status_code=201, content=response)


def build_payment(req, order_id, final_total, created) -> dict:
    """Order pehle UNPAID banta hai (D-07). Yahan sirf instrument banta hai, paisa nahi chalta."""
    if req.payment_mode == "cod":
        return {"mode": "cod", "state": "pending"}
    if req.payment_mode == "checkout":
        rzp_order = razorpay_call(razorpay_client().order.create, {
            "amount": final_total, "currency": CURRENCY, "receipt": order_id,
            "notes": {"merchant_id": MERCHANT_ID, "order_id": order_id}})
        # SPEC v1.4: key_id ke bina checkout mode sirf sajavat hai - order id akela pay
        # nahi hota. Ye PUBLIC key hai (browser checkout bhi yahi bhejta hai); secret
        # merchant ke paas hi rehta hai aur kabhi bahar nahi jata.
        #
        # SPEC v1.5: expires_at yahan bhi. Razorpay ka checkout order khud kabhi expire
        # nahi hota, to bina apni ghadi ke chhoda hua order stock HAMESHA ke liye rok
        # leta. Expiry order ki zimmedari hai, provider ki nahi.
        return {"mode": "checkout", "state": "pending", "razorpay_order_id": rzp_order["id"],
                "razorpay_key_id": os.getenv("RAZORPAY_KEY_ID"),
                "expires_at": iso(created + timedelta(minutes=UNPAID_ORDER_TTL_MINUTES))}
    expires = created + timedelta(minutes=UNPAID_ORDER_TTL_MINUTES)
    link = razorpay_call(razorpay_client().payment_link.create, {
        "amount": final_total, "currency": CURRENCY,
        "description": NAME + " order " + order_id,
        "reference_id": order_id,
        "expire_by": int(expires.timestamp()),
        "customer": {"name": req.contact.name, "contact": req.contact.phone,
                     "email": req.contact.email or ""},
        "notify": {"sms": False, "email": False}, "reminder_enable": False,
        "notes": {"merchant_id": MERCHANT_ID, "order_id": order_id}})
    return {"mode": "payment_link", "state": "pending", "link_id": link["id"],
            "link_url": link["short_url"], "expires_at": iso(expires)}


# ---------------------------------------------------------------- 5. order status
@app.get("/agent/orders/{order_id}", response_model=m.OrderView)
def get_order(order_id: str, x_agent_key: str | None = Header(None)):
    require_key(x_agent_key)
    with db.connect() as c:
        row = c.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise SpecError(404, "ORDER_NOT_FOUND", "No order with id " + order_id + ".",
                            {"order_id": order_id})
        o = dict(row)
        refresh_payment(c, o)
        refresh_refund(c, o)

    payment = db.jl(o["payment"])
    cancellable = is_cancellable(o)
    return m.OrderView(
        order_id=o["order_id"], status=o["status"], items=db.jl(o["items"]),
        items_total_paise=o["items_total_paise"], shipping_paise=o["shipping_paise"],
        discount_paise=o["discount_paise"], final_total_paise=o["final_total_paise"],
        payment=m.PaymentStatus(mode=payment["mode"], state=payment["state"],
                                razorpay_payment_id=payment.get("razorpay_payment_id"),
                                paid_at=payment.get("paid_at")),
        # Refund ab order read pe bhi milta hai, sirf cancel ki response me nahi. Wo
        # response ek baar aati hai aur agent ke context se scroll ho jati hai; uske baad
        # refund id aur `expected_by` surface pe kahin bache hi nahi the — theek wo do
        # cheezein jo user "mera paisa kab aayega" poochhne pe maangta hai.
        refund=db.jl(o["refund"]) if o["refund"] else None,
        delivery=db.jl(o["delivery"]) if o["delivery"] else None,
        timeline=db.jl(o["timeline"]),
        cancellable=cancellable,
        # `cancellable: false` ke bagal me do din aage ki date rakhna do ulte jawab ek
        # saath dena hai. Window ka matlab tabhi hai jab wo abhi bhi khuli ho.
        cancellable_until=o["cancellable_until"] if cancellable else None,
    )


def release_stock(c, o: dict) -> None:
    """Reserve ki hui stock wapas shelf pe. Cancel aur expiry dono yahi bulate hain -
    ek hi jagah, warna dono me se ek `bump_products_for` bhoolne wali hai aur wo bhool
    chupchap Layer ke index ko jhootha kar deti hai (SPEC 4)."""
    items = db.jl(o["items"])
    for line in items:
        c.execute("UPDATE variants SET stock = stock + ? WHERE variant_id = ?",
                  (line["qty"], line["variant_id"]))
    bump_products_for(c, [line["variant_id"] for line in items])


def mark_order(c, o: dict, status: str, payment: dict, at: str, note=None) -> None:
    entry = {"status": status, "at": at}
    if note:
        entry["note"] = note
    timeline = db.jl(o["timeline"]) + [entry]
    c.execute("UPDATE orders SET status=?, payment=?, timeline=? WHERE order_id=?",
              (status, db.jd(payment), db.jd(timeline), o["order_id"]))
    o.update(status=status, payment=db.jd(payment), timeline=db.jd(timeline))


def refresh_payment(c, o: dict) -> None:
    """Webhooks nahi hain (D-13), to merchant khud Razorpay se poochta hai - tab jab koi
    order padhta hai. ponytail: read pe lazy poll; traffic bade to background job.

    Do instrument, ek hi sawaal do tareeke se:
      payment_link -> link paid hui? (aur provider khud batata hai ki link expire hui)
      checkout     -> us razorpay order pe koi captured payment hai?

    Expiry dono pe lagti hai, par uska source alag hai. Link ka status provider deta hai;
    checkout order provider ke yahan **kabhi expire nahi hota**, isliye expiry order ki
    apni ghadi se aati hai (`payment.expires_at`). Bina uske cap ke neeche wala har chhoda
    hua order stock hamesha ke liye rok leta - aur wahi failure hai jise SPEC 9 rokta hai.

    Expiry terminal hai, failure nahi. Ek fail hui koshish ke baad grahak dobara try kar
    sakta hai - us par order maar dena grahak se uska apna order chheenna hai. Par
    instrument expire hone ke baad koi kabhi pay nahi kar sakta, aur tab tak roki hui
    stock hamesha ke liye phansi rehti hai. Isliye expire pe hi order `failed` hota hai
    aur stock chhutti hai (SPEC 6, 9).
    """
    payment = db.jl(o["payment"])
    if payment.get("state") != "pending":
        return
    expires_at = payment.get("expires_at")
    past_due = bool(expires_at) and now() > parse_iso(expires_at)
    try:
        if payment.get("link_id"):
            link = razorpay_client().payment_link.fetch(payment["link_id"])
            status, payments = link.get("status"), link.get("payments") or []
            captured = payments[0] if status == "paid" and payments else None
            dead = past_due or status in ("expired", "cancelled")
        elif payment.get("razorpay_order_id"):
            found = razorpay_client().order.payments(payment["razorpay_order_id"])
            captured = next((p for p in found.get("items") or []
                             if p.get("status") == "captured"), None)
            dead = past_due
        else:
            return              # cod - poochne ko koi provider hai hi nahi
    except Exception:
        # Provider se baat nahi hui. Order ko `failed` YAHAN nahi karna - ho sakta hai wo
        # pay ho chuka ho aur humein pata na chala ho. Ghadi beet jane se order marta
        # nahi; unpaid CONFIRM hona zaroori hai. Stale pending surakshit taraf hai.
        return

    if captured:
        paid_at = iso(datetime.fromtimestamp(
            captured.get("created_at") or captured.get("paid_at"), timezone.utc))
        payment.update(state="paid",
                       razorpay_payment_id=captured.get("payment_id") or captured.get("id"),
                       paid_at=paid_at)
        mark_order(c, o, "paid", payment, paid_at)
    elif dead:
        payment["state"] = "failed"
        release_stock(c, o)
        mark_order(c, o, "failed", payment, iso(now()),
                   "payment instrument expired before it was paid")


def is_cancellable(o: dict) -> bool:
    return (o["status"] in ("created", "paid", "confirmed")
            and now() < parse_iso(o["cancellable_until"]))


# ---------------------------------------------------------------- 6. cancel
@app.post("/agent/orders/{order_id}/cancel", response_model=m.CancelResponse)
def cancel_order(order_id: str, body: m.CancelRequest,
                 x_agent_key: str | None = Header(None)):
    require_key(x_agent_key)
    c = db.connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise SpecError(404, "ORDER_NOT_FOUND", "No order with id " + order_id + ".",
                            {"order_id": order_id})
        o = dict(row)

        if o["status"] in ("cancelled", "refunded"):
            # SPEC 8: cancellation idempotent hai - dobara cancel = 200, error nahi
            c.execute("COMMIT")
            return m.CancelResponse(order_id=order_id, status=o["status"],
                                    refund=db.jl(o["refund"]), cancelled_at=o["cancelled_at"])

        refresh_payment(c, o)
        if not is_cancellable(o):
            raise SpecError(409, "ORDER_NOT_CANCELLABLE",
                            "Order " + order_id + " can no longer be cancelled.",
                            {"status": o["status"], "cancellable_until": o["cancellable_until"]})

        release_stock(c, o)

        payment = db.jl(o["payment"])
        refund = None
        # NOTE: `payment["state"]` neeche refund ki ASLI state se aata hai, "refunded"
        # hardcode nahi hota. Dekho `payment_state_for_refund`.
        if payment.get("state") == "paid":
            refund = issue_refund(payment, o["final_total_paise"])
            payment["state"] = payment_state_for_refund(refund)
        cancelled_at = iso(now())
        timeline = db.jl(o["timeline"]) + [{"status": "cancelled", "at": cancelled_at,
                                            "note": body.reason}]
        c.execute("UPDATE orders SET status=?, payment=?, refund=?, cancel_reason=?, "
                  "cancelled_at=?, timeline=? WHERE order_id=?",
                  ("cancelled", db.jd(payment), db.jd(refund) if refund else None, body.reason,
                   cancelled_at, db.jd(timeline), order_id))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    return m.CancelResponse(order_id=order_id, status="cancelled", refund=refund,
                            cancelled_at=cancelled_at)


def payment_state_for_refund(refund: dict | None) -> str:
    """Paisa WAPAS AA GAYA aur paisa WAPAS BHEJA GAYA do alag baatein hain.

    Pehle cancel karte hi `payment.state` ko seedha `"refunded"` likh diya jata tha, chahe
    Razorpay ne `pending` hi kaha ho. Nateeja ek asli audit me pakda gaya: `cancel_order`
    ne `refund.state: "pending"` aur `expected_by: 5 September` diya, aur usi order ko
    turant padhne pe `payment.state: "refunded"` mila. Agent ne user ko bola *"paisa wapas
    aa gaya hai"* — jabki paisa 5 din baad aana tha. User dekhna band kar deta hai, ya
    merchant se aise refund pe ladta hai jo late tha hi nahi.

    Refund provider ke yahan asynchronous hai: `initiated`/`pending` ka matlab hai bheja
    gaya, aaya nahi. Sirf `processed` ka matlab hai aa gaya.
    """
    if not refund:
        return "paid"
    if refund.get("state") == "processed":
        return "refunded"
    if refund.get("state") == "failed":
        return "refund_failed"
    return "refund_pending"


def refresh_refund(c, o: dict) -> None:
    """Refund ki state provider se dobara padho — wahi lazy poll jo payment pe hai.

    Bina iske refund hamesha us haalat me jama rehta jis haalat me cancel ke waqt tha,
    aur `refund_pending` kabhi `refunded` banta hi nahi — yaani order read se ye kabhi
    pata nahi chalta ki paisa sach me aa gaya.
    """
    refund = db.jl(o["refund"]) if o["refund"] else None
    if not refund or refund.get("state") in ("processed", "failed") \
            or not refund.get("razorpay_refund_id"):
        return
    try:
        live = razorpay_client().refund.fetch(refund["razorpay_refund_id"])
    except Exception:
        return          # provider tak baat nahi pahunchi - purani state hi sach rahegi
    state = live.get("status") or refund["state"]
    if state == refund["state"]:
        return
    refund["state"] = state
    payment = db.jl(o["payment"])
    payment["state"] = payment_state_for_refund(refund)
    o["refund"], o["payment"] = db.jd(refund), db.jd(payment)
    c.execute("UPDATE orders SET refund=?, payment=? WHERE order_id=?",
              (o["refund"], o["payment"], o["order_id"]))


def issue_refund(payment: dict, amount_paise: int) -> dict:
    """Refund fail ho sakta hai - test mode me paylater/wallet refundable nahi hai. Order
    phir bhi cancel hota hai (stock chhoot chuka hai); refund ki asli state response me
    dikhti hai, taaki 'kya hua' chhupe nahi."""
    try:
        r = razorpay_client().refund.create({"payment_id": payment["razorpay_payment_id"],
                                             "amount": amount_paise, "speed": "normal"})
        return {"state": r.get("status") or "initiated", "amount_paise": amount_paise,
                "razorpay_refund_id": r["id"],
                "expected_by": iso(now() + timedelta(days=5))}
    except Exception as e:
        return {"state": "failed", "amount_paise": amount_paise,
                "razorpay_refund_id": None, "expected_by": None, "error": str(e)[:200]}


# ---------------------------------------------------------------- human-facing UI
@app.get("/", response_class=HTMLResponse)
def ui_grid(request: Request, q: str | None = None, category: str | None = None):
    where, args = [], []
    if q:
        where.append("(p.title LIKE ? OR p.brand LIKE ? OR p.tags LIKE ?)")
        args += ["%" + q + "%"] * 3
    if category:
        where.append("p.category = ?")
        args.append(category)
    sql = (CATALOG_SELECT + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "GROUP BY p.product_id ORDER BY p.title")
    with db.connect() as c:
        rows = c.execute(sql, args).fetchall()
        # Categories DB se — hardcode karne par ye suchi seed se drift kar jati hai aur
        # koi error nahi aata (wahi galti manifest me ho chuki hai).
        cats = [r[0] for r in c.execute(
            "SELECT DISTINCT category FROM products ORDER BY category")]
    return templates.TemplateResponse(request, "grid.html",
                                      {"products": [dict(r) for r in rows], "q": q or "",
                                       "categories": cats, "active": category or "",
                                       "store": NAME, "policies": {
                                           "shipping": SHIPPING_FLAT_PAISE,
                                           "free_above": FREE_SHIPPING_ABOVE_PAISE,
                                           "cancel_hours": CANCEL_WINDOW_HOURS,
                                           "max_qty": MAX_QTY_PER_VARIANT}})


@app.get("/p/{product_id}", response_class=HTMLResponse)
def ui_detail(request: Request, product_id: str):
    with db.connect() as c:
        p = c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
        if p is None:
            return HTMLResponse("<h1>404</h1>", status_code=404)
        variants = c.execute("SELECT * FROM variants WHERE product_id=? ORDER BY position",
                             (product_id,)).fetchall()
    return templates.TemplateResponse(request, "detail.html", {
        "p": dict(p), "variants": [dict(v) for v in variants],
        "images": db.jl(p["images"], []), "tags": db.jl(p["tags"], []),
        "attributes": db.jl(p["attributes"], {}), "reviews": db.jl(p["reviews"], []),
        "store": NAME, "policies": {
            "shipping": SHIPPING_FLAT_PAISE, "free_above": FREE_SHIPPING_ABOVE_PAISE,
            "cancel_hours": CANCEL_WINDOW_HOURS, "max_qty": MAX_QTY_PER_VARIANT},
    })


# Northwind aksar `uvicorn merchants.northwind.main:app` se chalta hai, jo local ke liye
# theek hai. Ek hosted platform ek hi cheez chalata hai — ek command — aur port ENV se
# deta hai. Isliye ye module khud bhi chal sakta hai. Default 127.0.0.1:8001 hi rehta hai,
# taaki local bartav, docs aur saari scripts jaisi ki taisi rahen.
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app,
                host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", os.getenv("NORTHWIND_PORT", "8001"))))
