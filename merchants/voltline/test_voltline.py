"""Merchant B ke apne test — sirf wo rules jo kisi ARBITRARY merchant pe generically
test nahi ho sakte.

    .venv/Scripts/python.exe -m pytest merchants/voltline

`spec/conformance/` hamesha pehli pasand hai: wo har merchant pe chalti hai aur spec ko
chalne wali cheez banati hai. Voltline uske 52 test **bina ek line badle** pass karta
hai. Par kuch rules ke liye us merchant ki apni andar ki baat chahiye:

  * **Expiry** — anjaan merchant pe iska matlab hai uski TTL jitni der baithna (yahan 30
    minute). Apni dukaan me expiry peeche khiska di jati hai.
  * **Provider ki amount ceiling** — iske liye ceiling se upar ka product chahiye.
  * **Refund ka sach** — refund tabhi hota hai jab order **paid** ho, aur conformance
    jaan-boojhkar COD pe chalti hai (D-33). To paid+refund wala order DB me rakha jata hai.
  * **Purane order ka waada** — abhi bane order pe `now + eta` aur `created_at + eta`
    ek hi second me girte hain, to farq dikhta hi nahi. Purana order sirf apni DB me
    rakha ja sakta hai.

Ye D-68 ka wahi usool hai jo Northwind pe laga tha, doosri dukaan par.

**Ek baat is file ke bare me jo Northwind wali file me nahi hai:** Voltline JavaScript
me likha hai, par uski database SQLite hai — to Python se wahi fixtures rakhe ja sakte
hain. Isliye poore repo ke liye ek hi test runner bacha rehta hai (`pytest` root se),
aur do zubaanon ke liye do CI raaste nahi banane padte.

Server ka chalna zaroori hai (port 8002), kyunki asli rule HTTP ke us paar hai.
"""
import json
import os
import pathlib
import re
import sqlite3
import uuid

import httpx
import pytest
from dotenv import load_dotenv

HERE = pathlib.Path(__file__).parent
DB_PATH = HERE / "voltline.db"
BASE_URL = "http://127.0.0.1:8002"

load_dotenv(HERE.parents[1] / ".env")

CONTACT = {"name": "Voltline Test", "phone": "+919876543210", "email": "t@example.com"}
ADDRESS = {"line1": "1 Test Road", "city": "Nagpur", "state": "Maharashtra",
           "pincode": "440001", "country": "IN"}


def connect():
    """Merchant ki apni DB. Server ki connection alag hai; SQLite WAL me ye theek hai."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def api():
    key = os.getenv("VOLTLINE_AGENT_KEY", "vl_agentkey_local_dev_only")
    client = httpx.Client(base_url=BASE_URL, headers={"X-Agent-Key": key}, timeout=30)
    try:
        if client.get("/agent/manifest").status_code != 200:
            pytest.skip("Voltline 8002 pe nahi chal raha")
    except httpx.HTTPError:
        pytest.skip("Voltline 8002 pe nahi chal raha")
    yield client
    client.close()


def cheapest_in_stock(api, min_stock=1):
    page = api.get("/agent/catalog", params={"limit": 500}).json()
    best = None
    for product in page["products"]:
        if not product["in_stock"]:
            continue
        detail = api.get("/agent/products/" + product["product_id"]).json()
        for variant in detail["variants"]:
            if variant["stock"] >= min_stock and (
                    best is None or variant["price_paise"] < best[1]["price_paise"]):
                best = (detail, variant)
    if best is None:
        pytest.skip("koi in-stock variant nahi mila")
    return best


def stock_of(api, product_id, variant_id):
    detail = api.get("/agent/products/" + product_id).json()
    return next(v["stock"] for v in detail["variants"] if v["variant_id"] == variant_id)


def plant_order(**columns):
    """Ek poora order seedhe merchant ki DB me. Test ke baad khud hi hat jata hai."""
    conn = connect()
    try:
        keys = ", ".join(columns)
        marks = ", ".join("?" * len(columns))
        conn.execute(f"INSERT INTO orders ({keys}) VALUES ({marks})",
                     tuple(columns.values()))
        conn.commit()
    finally:
        conn.close()


def drop_order(order_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
        conn.commit()
    finally:
        conn.close()


def base_order(order_id, **overrides):
    now = "2026-09-02T10:00:00Z"
    row = {
        "order_id": order_id, "status": "created",
        "items": json.dumps([{"variant_id": "vl-test-std", "product_id": "vl-test",
                              "title": "Fixture", "qty": 1,
                              "unit_price_paise": 112000, "line_total_paise": 112000}]),
        "items_total_paise": 112000, "shipping_paise": 0, "discount_paise": 0,
        "final_total_paise": 112000,
        "contact": json.dumps(CONTACT), "address": json.dumps(ADDRESS),
        "payment_mode": "checkout", "coupon_code": None,
        "payment": json.dumps({}), "refund": None,
        "delivery": json.dumps({"eta_days": 2, "promised_by": now, "pincode": "440001"}),
        "timeline": json.dumps([{"status": "created", "at": now}]),
        "created_at": now, "cancellable_until": None, "expires_at": None,
        "cancelled_at": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------- SPEC 1.5: expiry

def test_an_expired_unpaid_order_fails_and_gives_its_stock_back(api):
    """SPEC 9: bina pay hua order expire hone pe `failed` ho aur apni stock chhode.

    Ye wo failure hai jo kabhi error nahi deti. Order banta hai, stock kam hoti hai, aur
    agar koi kabhi pay na kare to wo stock **hamesha ke liye** phansi rehti hai — dikhta
    ye hai ki warehouse bhara hai par item out-of-stock ja raha hai.

    `checkout` mode isliye chuna gaya ki Razorpay ka checkout order provider ke yahan
    **kabhi expire nahi hota** — yaani yahan ghadi poori tarah merchant ki apni hai, aur
    theek yahi hissa SPEC v1.4 me chhoot gaya tha.
    """
    detail, variant = cheapest_in_stock(api)
    before = stock_of(api, detail["product_id"], variant["variant_id"])

    body = {"items": [{"variant_id": variant["variant_id"], "qty": 1,
                       "expected_price_paise": variant["price_paise"]}],
            "expected_items_total_paise": variant["price_paise"],
            "contact": CONTACT, "address": ADDRESS, "payment_mode": "checkout"}
    created = api.post("/agent/orders", json=body,
                       headers={"Idempotency-Key": uuid.uuid4().hex})
    if created.status_code != 201:
        pytest.skip("provider abhi order nahi bana pa raha: " + created.text[:120])
    order = created.json()
    assert order["payment"]["expires_at"], "SPEC v1.5: unpaid order pe expires_at chahiye"

    reserved = stock_of(api, detail["product_id"], variant["variant_id"])
    assert reserved == before - 1, "order banne pe stock reserve honi chahiye thi"

    # Ghadi aage badhane ke bajaye expiry peeche khiska do
    conn = connect()
    try:
        conn.execute("UPDATE orders SET expires_at=? WHERE order_id=?",
                     ("2020-01-01T00:00:00Z", order["order_id"]))
        conn.commit()
    finally:
        conn.close()

    # Webhooks nahi hain — merchant ko ye padhne par hi pata chalta hai
    view = api.get("/agent/orders/" + order["order_id"]).json()
    assert view["status"] == "failed", view
    assert view["payment"]["state"] == "failed"
    assert view["timeline"][-1]["status"] == "failed"
    assert stock_of(api, detail["product_id"], variant["variant_id"]) == before, (
        "expire hone ke baad stock wapas aani chahiye thi")


def test_an_order_is_never_expired_on_an_unreachable_provider(api):
    """SPEC 9: `failed` sirf CONFIRM unpaid order par. Provider chup ho to order pending.

    Timeout par likha gaya `failed` kisi ke poore ho chuke payment ke **upar** likha ja
    sakta hai. Yahan wo nakli provider order id se test hota hai: expiry beet chuki hai,
    par Razorpay us id ko jaanta hi nahi, to "unpaid hai" confirm hota hi nahi — aur
    order ko chhoo bhi nahi jana chahiye.
    """
    order_id = "ord_unreach_" + uuid.uuid4().hex[:8]
    row = base_order(order_id, expires_at="2020-01-01T00:00:00Z",
                     payment=json.dumps({"razorpay_order_id": "order_doesnotexist_xyz",
                                         "razorpay_key_id": "rzp_test_fake"}))
    plant_order(**row)
    try:
        view = api.get("/agent/orders/" + order_id).json()
        assert view["status"] == "created", (
            "provider se confirm nahi hua ki unpaid hai — order pending rehna chahiye, "
            "kyunki timeout par likha `failed` kisi ke payment ke upar likha ja sakta hai")
        assert view["payment"]["state"] == "pending"
    finally:
        drop_order(order_id)


# ---------------------------------------------------------------- SPEC 1.4: amount ceiling

def test_a_provider_amount_ceiling_is_a_spec_code_not_a_500(api):
    """SPEC 11: provider ka amount refuse karna `409 AMOUNT_LIMIT_EXCEEDED` hai, `500` nahi.

    Teen alag matlab chahiye: `500` = humara code toota (agent haar jata hai), `429` =
    thodi der baad, `409` = ye request aise kabhi nahi chalegi, badlo. Voltline ke paas
    Rs 8 lakh se upar ki ghadiyan hain, isliye ye yahan test ho sakta hai.
    """
    page = api.get("/agent/catalog", params={"limit": 500}).json()
    costly = None
    for product in page["products"]:
        if product["in_stock"] and product["price_range_paise"]["max"] >= 5_000_000:
            detail = api.get("/agent/products/" + product["product_id"]).json()
            costly = next(((detail, v) for v in detail["variants"]
                           if v["stock"] >= 1 and v["price_paise"] >= 5_000_000), None)
            if costly:
                break
    if costly is None:
        pytest.skip("provider ki ceiling se upar ka koi in-stock variant nahi hai")
    detail, variant = costly

    body = {"items": [{"variant_id": variant["variant_id"], "qty": 1,
                       "expected_price_paise": variant["price_paise"]}],
            "expected_items_total_paise": variant["price_paise"],
            "contact": CONTACT, "address": ADDRESS, "payment_mode": "payment_link"}
    r = api.post("/agent/orders", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
    error = r.json().get("error", {})
    if error.get("code") == "RATE_LIMITED":
        # Provider ne amount dekhne se pehle hi rate limit laga diya (D-33). Merchant ne
        # yahan bhi theek kiya (429 + Retry-After); bas ye test apni baat nahi keh sakta.
        assert r.status_code == 429 and r.headers.get("retry-after")
        pytest.skip("provider abhi rate limit kar raha hai - amount ceiling tak pahuncha hi nahi")
    assert r.status_code == 409, r.text
    assert error["code"] == "AMOUNT_LIMIT_EXCEEDED"
    assert error["details"]["provider_message"], "provider ki asli wajah chhupni nahi chahiye"


# ---------------------------------------------------------------- SPEC 1.6: refund ka sach

@pytest.mark.parametrize("refund_state,expected", [
    ("initiated", "refund_pending"),
    ("pending", "refund_pending"),
    ("processed", "refunded"),
    ("failed", "refund_failed"),
])
def test_a_sent_refund_is_not_reported_as_a_returned_one(api, refund_state, expected):
    """SPEC 7: `payment.state` `refunded` sirf tab jab paisa SACH ME pahunch gaya ho.

    Northwind me ye ek shuddh function par test hota hai; yahan wo function JavaScript me
    hai, to test **taar ke us paar** se hota hai — jo behtar bhi hai: rule wahan test ho
    raha hai jahan agent use padhta hai, na ki jahan wo likha gaya hai.

    Asli naap jisse ye galti pehle pakdi gayi thi: cancel ne `state: "pending"` aur
    `expected_by` paanch din aage diya, aur usi order ko **teen second baad** padhne par
    `payment.state: "refunded"` mila. Agent order padhta hai, cancel ki purani response
    nahi — aur grahak ko bata deta hai ki paisa aa gaya.
    """
    order_id = "ord_ref_%s_%s" % (refund_state, uuid.uuid4().hex[:6])
    now = "2026-09-02T10:00:00Z"
    row = base_order(
        order_id, status="cancelled", cancelled_at=now,
        payment=json.dumps({"razorpay_order_id": "order_fixture",
                            "payment_id": "pay_fixture", "paid_at": now}),
        # razorpay_refund_id jaan-boojhkar nahi diya: `refreshOrder` provider se poochhne
        # ki koshish hi nahi karega, to yahan sirf mapping test hoti hai.
        refund=json.dumps({"state": refund_state, "amount_paise": 112000,
                           "razorpay_refund_id": None,
                           "expected_by": "2026-09-07T10:00:00Z"}),
        timeline=json.dumps([{"status": "created", "at": now},
                             {"status": "paid", "at": now},
                             {"status": "cancelled", "at": now, "note": "fixture"}]))
    plant_order(**row)
    try:
        view = api.get("/agent/orders/" + order_id).json()
        assert view["payment"]["state"] == expected, view["payment"]
        if expected == "refund_pending":
            assert view["payment"]["state"] != "refunded", (
                "provider ne abhi paisa nahi bheja — `refunded` bolna grahak se jhooth hai")
    finally:
        drop_order(order_id)


def test_a_pending_refund_reaches_the_agent_with_its_id_and_its_date(api):
    """SPEC 7: `refund` object order READ pe milta hai, sirf cancel ki response me nahi.

    Cancel ki response ek baar aati hai aur agent ke context se scroll ho jati hai. Uske
    baad `expected_by` aur `razorpay_refund_id` — theek wo do cheezein jo insaan maangta
    hai (*"mera paisa kab aayega"*, *"reference kya hai"*) — surface pe bachti hi nahi.
    """
    order_id = "ord_refdetail_" + uuid.uuid4().hex[:8]
    now = "2026-09-02T10:00:00Z"
    row = base_order(
        order_id, status="cancelled", cancelled_at=now,
        payment=json.dumps({"payment_id": "pay_fixture", "paid_at": now}),
        refund=json.dumps({"state": "pending", "amount_paise": 112000,
                           "razorpay_refund_id": "rfnd_fake_for_test",
                           "expected_by": "2026-09-07T10:00:00Z"}))
    plant_order(**row)
    try:
        view = api.get("/agent/orders/" + order_id).json()
        assert view["refund"] is not None
        assert view["refund"]["razorpay_refund_id"] == "rfnd_fake_for_test"
        assert view["refund"]["expected_by"] == "2026-09-07T10:00:00Z"
        assert view["payment"]["state"] == "refund_pending"
    finally:
        drop_order(order_id)


def test_refund_state_is_never_written_by_hand():
    """Source guard: `payment.state` ek hi jagah DERIVE hoti hai, kahin store nahi hoti.

    Behavioural test us darwaze ko dekhte hain jise kisi ne kholne ka socha; source guard
    us darwaze ko dekhta hai jise abhi tak kisi ne khola hi nahi. Northwind me theek yahi
    guard isliye likhna pada tha ki `= "refunded"` hardcode kar dene par **koi test red
    nahi hua** — refund ka call site sirf ek asli paid order par chalta hai.
    """
    source = (HERE / "agent.mjs").read_text(encoding="utf-8")
    offenders = [
        "agent.mjs:%d %s" % (n, line.strip())
        for n, line in enumerate(source.splitlines(), 1)
        if re.search(r"""\bstate\s*[:=]\s*['"](refunded|refund_pending|refund_failed)['"]""",
                     line.split("//")[0])
        and "refund.state ===" not in line
    ]
    assert not offenders, (
        "refund ke baad ki payment state haath se likhi ja rahi hai; wo `orderPayment()` "
        "me refund.state se derive honi chahiye:\n  " + "\n  ".join(offenders))
    # Guard apni PAHUNCH bhi assert kare, warna wo chup-chaap kam dekhne lagta hai.
    assert "refund.state === 'processed' ? 'refunded'" in source, (
        "guard kuch dekh hi nahi raha — derive karne wali jagah hi gayab hai")


# ---------------------------------------------------------------- SPEC 1.7: waada

def test_an_old_orders_promise_does_not_move_with_the_clock(api):
    """SPEC 6/7: order ka delivery waada banate waqt JAM jata hai, read pe dobara nahi banta.

    Conformance suite ise pakad hi nahi sakti: wahan order abhi-abhi banta hai, to
    `now + eta` aur `created_at + eta` ek hi second me girte hain. Farq sirf ek PURANE
    order par dikhta hai, aur purana order sirf apni DB me rakha ja sakta hai.

    Kyun mayne rakhta hai: waada har read pe naya bane to jawab hamesha *"aaj se do
    din"* aata hai — waada kabhi kareeb nahi aata, aur late order kabhi late nahi dikhta.
    """
    order_id = "ord_ageta_" + uuid.uuid4().hex[:8]
    created = "2026-08-20T09:00:00Z"       # das din purana
    promised = "2026-08-22T09:00:00Z"      # created + 2 din (Voltline ka metro ETA)
    row = base_order(
        order_id, created_at=created, cancellable_until=created,
        payment_mode="cod", payment=json.dumps({}), expires_at=None,
        delivery=json.dumps({"eta_days": 2, "promised_by": promised, "pincode": "440001"}),
        timeline=json.dumps([{"status": "created", "at": created}]))
    plant_order(**row)
    try:
        view = api.get("/agent/orders/" + order_id).json()
        assert view["delivery"]["promised_by"] == promised, (
            "waada wahi rehna chahiye jo order banate waqt kiya gaya tha")
        assert view["delivery"]["eta_days"] == 2
        # Aur wahi doosri baat jo isi parivaar ki hai: band window par koi deadline nahi.
        assert view["cancellable"] is False
        assert view["cancellable_until"] is None, (
            "`cancellable: false` ke bagal me future deadline do ulte jawab hain")
    finally:
        drop_order(order_id)


# ---------------------------------------------------------------- Voltline ka apna data

def test_a_single_variant_product_carries_its_own_photographs(api):
    """Voltline single-variant products par `variants[].images` bharta hai — aur ye ek
    DAAWA hai jise sach rakhna is merchant ka kaam hai.

    Jahan product ka ek hi variant hai, wahan product ki tasveerein US variant ki
    tasveerein **hain** — variant hi poora product hai, to koi daawa jhootha nahi hota.
    Jahan kai variants hain wahan `images` khaali hai, kyunki Voltline har rang ki alag
    photo khinchwati nahi, aur "ye Midnight wali hai" likh dena wahi jhooth hai jisse
    Layer ka photo-note bachne ke liye bana hai.

    Layer isi field par `variant_photos_available` khadi karti hai, isliye is data ke
    jhooth hote hi ek galat baat seedha grahak tak pahunchti hai.
    """
    page = api.get("/agent/catalog", params={"limit": 500}).json()
    singles = withphotos = multis = liars = 0
    for product in page["products"]:
        detail = api.get("/agent/products/" + product["product_id"]).json()
        variants = detail["variants"]
        if len(variants) == 1:
            singles += 1
            if variants[0]["images"]:
                withphotos += 1
                assert variants[0]["images"] == detail["images"], (
                    "ek hi variant hai, to uski tasveerein product ki tasveerein hi hain")
        else:
            multis += 1
            liars += sum(1 for v in variants if v["images"])
    assert singles and multis, "dono kism ke products hone chahiye"
    assert withphotos == singles, (
        "har single-variant product ko apni tasveerein deni chahiye: %d/%d"
        % (withphotos, singles))
    assert liars == 0, (
        "multi-variant product par per-variant photo ka daawa jhootha hai — Voltline ke "
        "paas har rang ki alag tasveer hai hi nahi")
