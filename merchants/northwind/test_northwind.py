"""Merchant A ke apne test — sirf wo rules jo kisi ARBITRARY merchant pe generically
test nahi ho sakte.

    .venv/Scripts/python.exe -m pytest merchants/northwind

`spec/conformance/` hamesha pehli pasand hai: wo teenon merchants pe chalti hai aur spec
ko chalne wali cheez banati hai. Par kuch rules aise hain jinke liye us merchant ki apni
andar ki baat jaanni padti hai:

- **Instrument ki expiry.** Rule ye hai ki bina pay hua order expire hone pe `failed` ho
  aur stock chhode. Kisi anjaan merchant pe iska test likhne ka matlab hai uski TTL jitni
  der baithna — 30 minute. Yahan hum uski database me expiry ko peeche khiska dete hain,
  jo sirf isliye mumkin hai ki ye humara apna merchant hai.
- **Provider ki amount ceiling.** Iske liye aisa product chahiye jo ceiling se upar ho;
  har merchant ke paas nahi hota.

Ye SPEC 0.7 ("bina test ke rule sirf sujhaav hai") ka imaandar jawab hai: jo generically
nahi ho sakta wo yahan hota hai, aur `UPDATED_SITUATION.md` D-68 me likha hai ki kaun sa
rule kahan test hota hai.

Server ka chalna zaroori hai (port 8001), kyunki asli rule HTTP ke us paar hai.
"""
import importlib.util
import json
import pathlib
import re
import uuid

import httpx
import pytest
from dotenv import load_dotenv


def _load(name, filename):
    """Merchant ka module seedhe uske path se lo, `sys.path` chhede bina.

    `layer/db.py` aur `merchants/northwind/db.py` — dono ka module naam `db` hai. Alag
    process me chalein to koi dikkat nahi, par ek hi pytest process me dono suites chalen
    (`pytest layer merchants/northwind`, ya seedha `pytest`) to jo pehle import ho jata
    hai wahi `sys.modules["db"]` me baith jata hai. Phir ye test **Layer ki** DB kholta
    hai aur `no such table: orders` deta hai — bug test me hai, code me nahi.

    Alag-alag command chalane se ye chhupa rehta tha. Naam se import karne ke bajaye
    file se import karna usko poori tarah khatam kar deta hai.
    """
    spec = importlib.util.spec_from_file_location(
        name, pathlib.Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


db = _load("northwind_db", "db.py")

load_dotenv(pathlib.Path(__file__).parents[2] / ".env")

BASE_URL = "http://127.0.0.1:8001"
CONTACT = {"name": "Northwind Test", "phone": "+919876543210", "email": "t@example.com"}
ADDRESS = {"line1": "1 Test Road", "city": "Nagpur", "state": "Maharashtra",
           "pincode": "440001", "country": "IN"}


@pytest.fixture(scope="module")
def api():
    import os
    key = os.getenv("NORTHWIND_AGENT_KEY", "nw_agentkey_local_dev_only")
    client = httpx.Client(base_url=BASE_URL, headers={"X-Agent-Key": key}, timeout=30)
    try:
        if client.get("/agent/manifest").status_code != 200:
            pytest.skip("Northwind 8001 pe nahi chal raha")
    except httpx.HTTPError:
        pytest.skip("Northwind 8001 pe nahi chal raha")
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


def backdate_expiry(order_id):
    """Order ke instrument ko abhi expire karwa do — ghadi aage badhane ke bajaye expiry
    peeche khiska do. Server ki apni connection alag hai; SQLite WAL me ye theek hai."""
    connection = db.connect()
    try:
        row = connection.execute("SELECT payment FROM orders WHERE order_id=?",
                                 (order_id,)).fetchone()
        payment = json.loads(row["payment"])
        payment["expires_at"] = "2020-01-01T00:00:00Z"
        connection.execute("UPDATE orders SET payment=? WHERE order_id=?",
                           (json.dumps(payment), order_id))
    finally:
        connection.close()


def test_an_expired_unpaid_order_fails_and_gives_its_stock_back(api):
    """SPEC v1.5 9. Ye wo failure hai jo kabhi error nahi deti: order bana, stock kam
    hui, aur agar koi kabhi pay na kare to wo stock **hamesha ke liye** phansi rehti hai.
    Dikhta ye hai ki warehouse bhara hai par item out-of-stock ja raha hai.

    Checkout mode isliye chuna: Razorpay ka checkout order provider ke yahan kabhi expire
    nahi hota, to yahan expiry poori tarah merchant ki apni ghadi se aati hai — aur wahi
    hissa pehle gayab tha.
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

    backdate_expiry(order["order_id"])

    # Padhne par hi merchant ko pata chalta hai (webhooks nahi hain, D-13)
    view = api.get("/agent/orders/" + order["order_id"]).json()
    assert view["status"] == "failed", view
    assert view["payment"]["state"] == "failed"
    assert view["timeline"][-1]["status"] == "failed"
    assert stock_of(api, detail["product_id"], variant["variant_id"]) == before, (
        "expire hone ke baad stock wapas aani chahiye thi")


def test_a_provider_amount_ceiling_is_a_spec_code_not_a_500(api):
    """SPEC v1.4 11. Generic suite me nahi ho sakta: iske liye provider ki ceiling se
    upar ka product chahiye, jo har merchant ke paas nahi hota. Northwind ke paas ek
    Rs 5.6 lakh ki ghadi hai."""
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
        # Provider ne amount dekhne se pehle hi rate limit laga diya. Merchant ne yahan bhi
        # theek kiya (429 + Retry-After), bas ye test apni baat nahi keh sakta (D-33).
        assert r.status_code == 429 and r.headers.get("retry-after")
        pytest.skip("provider abhi rate limit kar raha hai - amount ceiling tak pahuncha hi nahi")
    assert r.status_code == 409, r.text
    assert error["code"] == "AMOUNT_LIMIT_EXCEEDED"
    assert error["details"]["provider_message"], "provider ki asli wajah chhupni nahi chahiye"


# --------------------------------------------------------------- refund honesty (SPEC 1.6)
main = _load("northwind_main", "main.py")


def test_a_sent_refund_is_not_reported_as_a_returned_one():
    """SPEC 7: `payment.state` `refunded` sirf tab jab paisa SACH ME aa gaya ho.

    Ye generically test nahi ho sakta: iske liye ek **paid** order chahiye, yaani asli
    provider paisa — aur conformance suite jaan-boojhkar COD pe chalti hai (D-33). Par
    rule ka poora dil ek shuddh function hai, aur usi function ne galti ki thi: pehle
    cancel karte hi seedha `payment["state"] = "refunded"` likh diya jata tha, chahe
    Razorpay ne `pending` kaha ho.

    Asli naap jisse ye pakda gaya: `cancel_order` ne `state: "pending"`,
    `expected_by: 2026-09-05` diya, aur usi order ko turant padhne pe
    `payment.state: "refunded"` mila — do jawab, teen second ke andar, ek doosre ke ulte.
    """
    pending = {"state": "pending", "amount_paise": 112000,
               "razorpay_refund_id": "rfnd_x", "expected_by": "2026-09-05T08:16:53Z"}
    assert main.payment_state_for_refund(pending) == "refund_pending", (
        "provider ne pending kaha hai — paisa abhi user tak pahuncha nahi")
    assert main.payment_state_for_refund({**pending, "state": "initiated"}) \
        == "refund_pending"
    assert main.payment_state_for_refund({**pending, "state": "processed"}) == "refunded"
    assert main.payment_state_for_refund({**pending, "state": "failed"}) == "refund_failed"
    assert main.payment_state_for_refund(None) == "paid", (
        "bina refund ke payment ki state chhoo bhi nahi honi chahiye")


def test_a_pending_refund_reaches_the_agent_with_its_id_and_its_date(api):
    """SPEC 7: `refund` order READ pe milta hai, aur `payment.state` uska sach bolta hai.

    Ye generically test nahi ho sakta aur wajah ginne layak hai: conformance suite COD
    par chalti hai (D-33), aur bina paid order ke refund hota hi nahi — to wahan `refund`
    hamesha `null` hota hai. Maine pehle ek conformance test likha jo sirf field ki
    **maujoodgi** dekhta tha, aur break-verify me wo pakda hi nahi gaya: `refund=None`
    hardcode karke bhi wo green raha. Ek test jo apne bug ke saath bhi pass ho jaye, wo
    test nahi hai.

    Yahan wo order **seedhe merchant ki apni DB me** rakha jata hai — wahi tareeka jo
    expiry wale test me pehle se hai, aur wo sirf isliye jayaz hai ki ye merchant humara
    apna hai. Refund id jaan-boojhkar nakli hai: `refresh_refund` provider se poochhne ki
    koshish karega, nahi pahunch payega, aur state ko **chhodkar** aage badh jayega —
    yaani wo raasta bhi yahin test ho jata hai (*"provider tak baat na pahunche to purani
    state hi sach hai"*).
    """
    order_id = "ord_reftest_" + uuid.uuid4().hex[:10]
    now = "2026-09-02T10:00:00Z"
    payment = {"mode": "checkout", "state": "refund_pending",
               "razorpay_payment_id": "pay_fake_for_test", "paid_at": now}
    refund = {"state": "pending", "amount_paise": 112000,
              "razorpay_refund_id": "rfnd_fake_for_test",
              "expected_by": "2026-09-07T10:00:00Z"}
    items = [{"variant_id": "nw-test-std", "title": "Refund fixture", "qty": 1,
              "unit_price_paise": 112000, "line_total_paise": 112000}]
    connection = db.connect()
    try:
        connection.execute(
            "INSERT INTO orders (order_id,status,items,items_total_paise,shipping_paise,"
            "discount_paise,final_total_paise,contact,address,payment,timeline,refund,"
            "cancel_reason,created_at,cancellable_until,cancelled_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, "cancelled", db.jd(items), 112000, 0, 0, 112000,
             db.jd(CONTACT), db.jd(ADDRESS), db.jd(payment),
             db.jd([{"status": "created", "at": now},
                    {"status": "paid", "at": now},
                    {"status": "cancelled", "at": now, "note": "refund fixture"}]),
             db.jd(refund), "refund fixture", now, now, now))
        connection.commit()

        view = api.get("/agent/orders/" + order_id).json()

        assert view["refund"] is not None, (
            "refund order read pe aana chahiye — cancel ki response ek baar aati hai aur "
            "agent ke context se scroll ho jati hai")
        assert view["refund"]["razorpay_refund_id"] == "rfnd_fake_for_test", (
            "wahi reference jo insaan ka bank maangta hai")
        assert view["refund"]["expected_by"] == "2026-09-07T10:00:00Z", (
            "wahi tareekh jo *'mera paisa kab aayega'* ka jawab hai")
        assert view["payment"]["state"] == "refund_pending", (
            "provider ne pending kaha hai — `refunded` bolna user se jhooth hai")
        assert view["payment"]["state"] != "refunded"
    finally:
        connection.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
        connection.commit()
        connection.close()


def test_refund_state_is_never_written_by_hand():
    """Source-level guard: `payment["state"]` refund ke liye SIRF
    `payment_state_for_refund()` se aaye.

    Ye guard isliye hai ki uska behavioural test ban hi nahi sakta bina asli paise ke.
    Cancel handler ka wo call site tabhi chalta hai jab ek **paid** order cancel ho, aur
    conformance suite jaan-boojhkar COD par chalti hai (D-33) — to maine order DB me
    plant karke test kiya, jisse wo call site chhut gaya. Break-verify me ye saaf dikha:
    `payment["state"] = payment_state_for_refund(refund)` ko `= "refunded"` se badal dene
    par **koi test red nahi hua**.

    Yahi wo shakal hai jiske liye is project me pehle se source guard hai
    (`test_no_merchant_json_reaches_an_agent_unsanitised`): behavioural test us darwaze ko
    dekhte hain jise kisi ne kholne ka socha; source guard us darwaze ko dekhta hai jise
    abhi tak kisi ne khola hi nahi.
    """
    source = (pathlib.Path(__file__).parent / "main.py").read_text(encoding="utf-8")
    offenders = []
    for number, line in enumerate(source.splitlines(), 1):
        code = line.split("#")[0]
        if re.search(r'payment\[\s*["\']state["\']\s*\]\s*=', code) and \
                "payment_state_for_refund" not in code:
            # `refresh_payment` ke apne do assignment jayaz hain: wo PAYMENT ki state
            # hai (paid / failed), refund ki nahi. Unhe naam se chhoot di gayi hai,
            # marker comment se nahi - warna koi bhi naya assignment comment likh kar
            # nikal jata.
            if code.strip() in ('payment["state"] = "failed"',):
                continue
            offenders.append("main.py:%d %s" % (number, line.strip()))
    assert not offenders, (
        "refund ke baad ki payment state haath se likhi ja rahi hai; wo "
        "payment_state_for_refund() se aani chahiye:\n  " + "\n  ".join(offenders))
    # Aur guard apni PAHUNCH bhi check kare: wo function sach me use hota hai ya nahi.
    assert re.search(r'payment\[\s*["\']state["\']\s*\]\s*=\s*payment_state_for_refund',
                     source), "guard kuch dekh hi nahi raha - call site hi gayab hai"


def test_an_old_orders_promise_does_not_move_with_the_clock(api):
    """SPEC 6: order ka delivery waada banate waqt JAM jata hai, read pe dobara nahi banta.

    Ye conformance suite pakad hi nahi sakti, aur wajah ginne layak hai: wahan order
    abhi-abhi banta hai, to `now + eta` aur `created_at + eta` ek hi second me girte hain
    aur barabar dikhte hain. Break-verify me theek yahi hua — maine waada har read pe
    dobara compute karwaya aur conformance **green** rahi.

    Farq sirf ek PURANE order par dikhta hai, aur purana order sirf apne merchant ki DB
    me rakha ja sakta hai. Yahi wo shakal hai jiske liye ye file bani hai (D-67).

    Kyun mayne rakhta hai: agar waada har read pe naya bane, to har baar jawab *"aaj se
    teen din"* aata hai — waada kabhi kareeb nahi aata, aur ek late order kabhi late
    nahi dikhta.
    """
    order_id = "ord_agedeta_" + uuid.uuid4().hex[:8]
    created = "2026-08-20T09:00:00Z"          # das din purana
    promised = "2026-08-23T09:00:00Z"         # created + 3 din
    payment = {"mode": "checkout", "state": "pending", "expires_at": created}
    items = [{"variant_id": "nw-test-std", "title": "ETA fixture", "qty": 1,
              "unit_price_paise": 50000, "line_total_paise": 50000}]
    connection = db.connect()
    try:
        connection.execute(
            "INSERT INTO orders (order_id,status,items,items_total_paise,shipping_paise,"
            "discount_paise,final_total_paise,contact,address,payment,timeline,delivery,"
            "created_at,cancellable_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, "created", db.jd(items), 50000, 0, 0, 50000,
             db.jd(CONTACT), db.jd(ADDRESS), db.jd(payment),
             db.jd([{"status": "created", "at": created}]),
             db.jd({"eta_days": 3, "promised_by": promised,
                    "pincode": ADDRESS["pincode"]}),
             created, created))
        connection.commit()

        view = api.get("/agent/orders/" + order_id).json()
        assert view["delivery"]["promised_by"] == promised, (
            "waada ghadi ke saath khisak gaya: order 20 August ka hai, uska waada "
            "23 August tha, aur ab jawab " + str(view["delivery"]["promised_by"]) +
            " aa raha hai")
        assert view["delivery"]["eta_days"] == 3
        assert view["delivery"]["promised_by"] > created, (
            "waada order banne ke BAAD ka hona chahiye")
    finally:
        connection.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
        connection.commit()
        connection.close()
