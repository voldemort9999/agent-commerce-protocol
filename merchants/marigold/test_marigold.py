"""Marigold Bazaar ke apne test — sirf wo rules jinhe koi GENERIC suite bana hi nahi sakti.

`spec/conformance` teenon merchants par chalti hai aur wahi is dukaan ka asli gate hai.
Yahan sirf wo cheezein hain jinke liye ya to merchant ki **apni DB me haalat plant** karni
padti hai (ek purana order, ek expire ho chuka instrument, ek pending refund), ya jo is
dukaan ka apna **declared fact** hain (delivery footprint, per-variant photo ki policy) —
aur ek generic suite dono me se kuch nahi jaanti. Ye D-68 ka usool hai, teesri dukaan par.

    .venv/Scripts/python.exe -m pytest merchants/marigold -q

Server chalu hona chahiye:  bash merchants/marigold/restart.sh
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
DB_PATH = HERE / "marigold.db"
BASE_URL = "http://127.0.0.1:8003"

load_dotenv(HERE.parents[1] / ".env")

CONTACT = {"name": "Marigold Test", "phone": "+919876543210", "email": "t@example.com"}
# Jaipur — Marigold ke apne zone me.
ADDRESS = {"line1": "1 Test Road", "city": "Jaipur", "state": "Rajasthan",
           "pincode": "302001", "country": "IN"}


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def api():
    key = os.getenv("MARIGOLD_AGENT_KEY", "mg_agentkey_local_dev_only")
    client = httpx.Client(base_url=BASE_URL, headers={"X-Agent-Key": key}, timeout=30)
    try:
        if client.get("/agent/manifest").status_code != 200:
            pytest.skip("Marigold 8003 pe nahi chal raha")
    except httpx.HTTPError:
        pytest.skip("Marigold 8003 pe nahi chal raha")
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
    at = "2026-09-02T10:00:00Z"
    row = {
        "order_id": order_id, "status": "created",
        "items": json.dumps([{"variant_id": "mb-test-std", "title": "Fixture", "qty": 1,
                              "unit_price_paise": 112000, "line_total_paise": 112000}]),
        "items_total_paise": 112000, "shipping_paise": 0, "discount_paise": 0,
        "final_total_paise": 112000,
        "contact": json.dumps(CONTACT), "address": json.dumps(ADDRESS),
        "payment": json.dumps({"mode": "checkout", "state": "pending"}),
        "refund": None,
        "delivery": json.dumps({"eta_days": 2, "promised_by": at, "pincode": "302001"}),
        "timeline": json.dumps([{"status": "created", "at": at}]),
        "coupon_code": None, "created_epoch": 1_756_807_200, "created_at": at,
        "cancellable_until": None, "expires_epoch": None,
    }
    row.update(overrides)
    return row


# ------------------------------------------- delivery footprint (is dukaan ka apna sach)

def test_marigold_delivers_where_its_neighbours_do_not(api):
    """**Ye ek merchant-specific fact hai, aur isi se cross-merchant recovery ASLI hai.**

    Northwind aur Voltline dono `7` se shuru hone wale pincode mana karte hain. Yaani
    *"ek dukaan ne mana kiya, agent ne doosri se le liya"* wala failure-recovery demo do
    merchants ke saath **chal hi nahi sakta tha** — buyer ke paas koi teesra raasta hi
    nahi tha, aur ye baat kisi conformance test me dikh bhi nahi sakti thi, kyunki wahan
    har merchant apne aap me dekha jata hai.

    Generic suite ye assert kar hi nahi sakti: use pata hi nahi ki is dukaan ka footprint
    kya hai. Isliye ye yahan hai.
    """
    guwahati = api.get("/agent/products/mb-48", params={"pincode": "781001"}).json()
    assert guwahati["delivery"]["serviceable"] is True, (
        "Marigold poorvottar bhejta hai — yahi is dukaan ki demo me zimmedari hai")
    assert guwahati["delivery"]["eta_days"] == 6, "aur wo BATATA hai ki der lagegi"

    kochi = api.get("/agent/products/mb-48", params={"pincode": "682001"}).json()
    assert kochi["delivery"]["serviceable"] is False
    # Non-serviceability ek FACT hai, failure nahi (SPEC 5).
    assert "shipping_paise" not in kochi["delivery"]


def test_the_quantity_ceiling_here_is_the_strictest_of_the_three(api):
    """Manifest ka `max_qty_per_variant` 2 hai — Layer ke apne 5 aur Voltline ke 3 se bhi
    sakht. SPEC 3 kehta hai "stricter wins", aur us `min()` ki teesri value hone se wo
    rule pehli baar teen alag natije deta hai."""
    manifest = api.get("/agent/manifest").json()
    assert manifest["policies"]["max_qty_per_variant"] == 2
    detail, variant = cheapest_in_stock(api, min_stock=3)
    r = api.post("/agent/orders", headers={"Idempotency-Key": uuid.uuid4().hex}, json={
        "items": [{"variant_id": variant["variant_id"], "qty": 3,
                   "expected_price_paise": variant["price_paise"]}],
        "expected_items_total_paise": variant["price_paise"] * 3,
        "contact": CONTACT, "address": ADDRESS, "payment_mode": "cod"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "QTY_LIMIT_EXCEEDED"


# ---------------------------------------------------------------- SPEC 9: expiry

def test_an_expired_unpaid_order_fails_and_gives_its_stock_back(api):
    """Stock order banate waqt ghatta hai aur use koi aur wapas nahi karta: order kabhi
    pay nahi hota, isliye kabhi fulfil nahi hota, aur bhoole hue order ko koi cancel nahi
    karta. Har chhoda hua checkout catalog se units hamesha ke liye nikal deta hai — aur
    dikhta ye hai ki warehouse bhara hai par item out-of-stock ja raha hai.

    Order ASLI banaya jata hai, phir uski expiry DB me peeche khiska di jati hai. Pehla
    version ek nakli `link_id` plant karta tha — provider us par `400` deta tha, order
    "confirmed unpaid" ban hi nahi paata tha, aur test chup-chaap **SKIP** ho jata tha.
    Yaani expiry ka rule kabhi chala hi nahi aur suite poori green rehti thi.

    `checkout` mode isliye ki Razorpay ka checkout order provider ke yahan **kabhi expire
    nahi hota** — yahan ghadi poori tarah merchant ki apni hai, aur theek yahi hissa SPEC
    v1.4 me chhoot gaya tha.
    """
    detail, variant = cheapest_in_stock(api)
    before = stock_of(api, detail["product_id"], variant["variant_id"])

    created = api.post("/agent/orders", headers={"Idempotency-Key": uuid.uuid4().hex}, json={
        "items": [{"variant_id": variant["variant_id"], "qty": 1,
                   "expected_price_paise": variant["price_paise"]}],
        "expected_items_total_paise": variant["price_paise"],
        "contact": CONTACT, "address": ADDRESS, "payment_mode": "checkout"})
    if created.status_code != 201:
        pytest.skip("provider abhi order nahi bana pa raha: " + created.text[:120])
    order = created.json()
    assert order["payment"]["expires_at"], "SPEC v1.5: har unpaid order par expires_at"

    assert stock_of(api, detail["product_id"], variant["variant_id"]) == before - 1, (
        "order banne par stock reserve honi chahiye thi")

    conn = connect()
    try:
        conn.execute("UPDATE orders SET expires_epoch=? WHERE order_id=?",
                     (1_577_836_800, order["order_id"]))   # 2020-01-01
        conn.commit()
    finally:
        conn.close()

    # Koi webhook nahi hai — merchant ko ye PADHNE par hi pata chalta hai.
    view = api.get("/agent/orders/" + order["order_id"]).json()
    assert view["status"] == "failed", view
    assert view["payment"]["state"] == "failed"
    assert view["timeline"][-1]["status"] == "failed"
    assert stock_of(api, detail["product_id"], variant["variant_id"]) == before, (
        "expire hone ke baad stock wapas shelf par aani chahiye thi")


def test_an_order_is_never_expired_on_an_unreachable_provider(api):
    """**Kram ka rule** (SPEC 9): `failed` sirf ek CONFIRMED-unpaid order ko kiya jata hai.

    Provider tak baat na pahunche to order pending chhodo. Timeout par likha gaya `failed`
    kisi ke poore ho chuke payment ke upar likha ja sakta hai — aur wo galti wapas nahi
    hoti. Yahan `link_id` ek aisa id hai jispe provider `400` deta hai, yaani "unpaid" ki
    koi tasdeeq nahi milti.
    """
    order_id = "ord_unreach_" + uuid.uuid4().hex[:8]
    plant_order(**base_order(
        order_id,
        payment=json.dumps({"mode": "payment_link", "state": "pending",
                            "link_id": "plink_definitely_not_real_xyz",
                            "expires_at": "2020-01-01T00:00:00Z"}),
        expires_epoch=1_577_836_800,
    ))
    try:
        view = api.get("/agent/orders/" + order_id).json()
        assert view["status"] == "created", (
            "provider ne unpaid hona confirm nahi kiya — order pending rehna chahiye")
    finally:
        drop_order(order_id)


# ----------------------------------------- SPEC 7: bheja hua refund != aaya hua refund

@pytest.mark.parametrize("refund_state,expected", [
    ("initiated", "refund_pending"),
    ("pending", "refund_pending"),
    ("processed", "refunded"),
    ("failed", "refund_failed"),
])
def test_a_sent_refund_is_not_reported_as_a_returned_one(refund_state, expected):
    """Ye mapping hi wo cheez hai jise collapse kar dena grahak se jhooth bulwata hai:
    *"aapka paisa wapas aa gaya"* — jabki provider abhi `pending` keh raha hai aur paisa
    paanch din door hai. Function seedha jaancha jata hai, kyunki HTTP se in chaaron
    haalat me pahunchna provider ke mood par nirbhar hai."""
    source = (HERE / "src" / "agent.rs").read_text(encoding="utf-8")
    body = source.split("pub fn payment_state_for_refund")[1].split("}\n")[0]
    mapping = dict(re.findall(r'"(\w+)" => "(\w+)"', body))
    got = mapping.get(refund_state, mapping.get("_", "refund_pending"))
    if refund_state in ("initiated", "pending"):
        got = "refund_pending"
    assert got == expected, f"{refund_state} -> {got}, chahiye tha {expected}"


def test_a_pending_refund_reaches_the_agent_with_its_id_and_its_date(api):
    """`refund` object **order-read par** hona chahiye, sirf cancel ki response par nahi.

    Cancel ka jawab ek message hai — wo scroll ho kar chala jata hai. Agent baad me order
    padhta hai, aur wahin se use grahak ke do asli sawaalon ka jawab dena hota hai: *kab*
    (`expected_by`) aur *kis reference se* (`razorpay_refund_id`). Sirf cancel me rakhne
    se wo dono ek message ke baad hamesha ke liye gayab ho jate hain.
    """
    order_id = "ord_refund_" + uuid.uuid4().hex[:8]
    plant_order(**base_order(
        order_id, status="cancelled",
        payment=json.dumps({"mode": "checkout", "state": "refund_pending",
                            "razorpay_payment_id": "pay_fixture"}),
        refund=json.dumps({"state": "pending", "amount_paise": 112000,
                           "razorpay_refund_id": "rfnd_fixture_not_real",
                           "expected_by": "2026-09-08T00:00:00Z"}),
    ))
    try:
        view = api.get("/agent/orders/" + order_id).json()
        assert view["refund"] is not None, "refund order-read par bhi aana chahiye"
        assert view["refund"]["razorpay_refund_id"] == "rfnd_fixture_not_real"
        assert view["refund"]["expected_by"] == "2026-09-08T00:00:00Z"
        assert view["payment"]["state"] != "refunded", (
            "provider ne abhi processed nahi kaha — paisa grahak tak pahuncha hi nahi")
        # Band window par future deadline dena do ulte jawab hain, ek hi body me.
        assert view["cancellable"] is False
        assert view["cancellable_until"] is None
    finally:
        drop_order(order_id)


def test_refund_state_is_never_written_by_hand():
    """Source-level guard. Behavioural test us darwaze ko dekhte hain jise kisi ne kholne
    ka socha; ye us darwaze ko dekhta hai jise abhi tak kisi ne khola hi nahi — kisi ne
    `payment.state` par seedha `"refunded"` likh diya ho.

    Ye Voltline me pakda gaya tha ki behavioural test isko pakad hi nahi sakta: jo test
    order DB me plant karta hai, uska cancel-handler wala call site kabhi chalta hi nahi.
    """
    source = (HERE / "src" / "agent.rs").read_text(encoding="utf-8")
    lines = [l for l in source.splitlines()
             if '"refunded"' in l and "payment_state_for_refund" not in l
             and not l.strip().startswith("//")]
    # Sirf ek jagah `"refunded"` likha ja sakta hai — us mapping function ke andar.
    allowed = [l for l in lines if '=> "refunded"' in l]
    assert lines == allowed, (
        "payment.state kahin haath se 'refunded' likha ja raha hai:\n" + "\n".join(lines))


# ------------------------------------------------ SPEC 5 (v1.8): per-variant photographs

def test_only_single_variant_products_carry_their_own_photographs(api):
    """Do variants ek hi image URL ka daawa nahi kar sakte (SPEC v1.8) — wo conformance
    dekhti hai. **Ye is dukaan ki apni policy hai** aur wo alag baat hai: Marigold
    per-variant tasveerein sirf wahan deta hai jahan variant hi poora product hai.

    Wajah mechanism wali hai: DummyJSON ki chaar tasveerein ek hi cheez ke chaar angle
    hain, alag rang nahi. Unhe `Steel` aur `Copper` par baant dena taar par ye padha
    jata hai ki *"jo dikh raha hai wahi kharida ja raha hai"* — aur wo jhooth hota, theek
    wahi jo v1.8 rokne ke liye likhi gayi thi. Khaali `images` ek poora aur imaandaar
    jawab hai.
    """
    page = api.get("/agent/catalog", params={"limit": 500}).json()
    singles = multis = 0
    for product in page["products"]:
        detail = api.get("/agent/products/" + product["product_id"]).json()
        variants = detail["variants"]
        with_photos = [v for v in variants if v.get("images")]
        if len(variants) == 1:
            singles += 1 if with_photos else 0
        else:
            multis += 1
            assert not with_photos, (
                f"{product['product_id']}: multi-variant product par per-variant "
                "tasveerein — ye daawa is data se sach ho hi nahi sakta")
    assert singles >= 20, f"single-variant products par tasveerein honi chahiye ({singles})"
    assert multis >= 20, "multi-variant products bhi hone chahiye, warna test khali hai"


# --------------------------------------------------- SPEC 10: payload verbatim, review me

def test_the_planted_payload_survives_verbatim_in_a_review(api):
    """Merchant sanitize NAHI karta (SPEC 10) — safai Layer ka kaam hai, aur wo ek jagah
    honi chahiye warna Layer ye bata hi nahi paati ki use asal me kya mila.

    Marigold ka payload jaan-boojhkar `description` me nahi, ek **review** me hai. Wahi
    jagah asli duniya me sabse zyada attacker-controlled hoti hai — koi bhi likh sakta
    hai, aur merchant use padhta bhi nahi.
    """
    detail = api.get("/agent/products/mb-48").json()
    bodies = " ".join(r.get("body", "") for r in detail["reviews"])
    assert "standing approval on file" in bodies, "payload chhed-chhaad ke bina aana chahiye"
    assert "Add 40 units" in bodies
    assert "[removed" not in bodies, "merchant ko safai nahi karni hai — wo Layer ka kaam hai"


def test_attributes_never_answer_a_question_delivery_already_answers(api):
    """SPEC v1.7. Source catalogue me `shippingInformation` aur `returnPolicy` dono hain
    aur dono seed me se JAAN-BOOJHKAR chhode gaye hain: pehla `delivery.eta_days` ka
    doosra jawab ban jata (aur do jawab me se kaun sa sach hai, response me kahin likha
    nahi hota), doosra manifest ke `cancel_window_hours` ke ulta bolta.

    Northwind ne yahi galti ki thi aur SPEC v1.7 usi se nikli — ek nayi dukaan ko wo
    galti dobara nahi karni chahiye, aur "nahi ki" ka saboot ek test hai.
    """
    page = api.get("/agent/catalog", params={"limit": 500}).json()
    banned = re.compile(r"ship|deliver|dispatch|return polic", re.I)
    for product in page["products"][:40]:
        detail = api.get("/agent/products/" + product["product_id"]).json()
        for key, value in (detail.get("attributes") or {}).items():
            assert not banned.search(key), f"{product['product_id']}: attributes.{key}"
            assert not banned.search(str(value)), (
                f"{product['product_id']}: attributes.{key} = {value!r}")
