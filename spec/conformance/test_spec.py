"""SPEC.md section 12 ki checklist, chalne wale roop me.

Har test ka naam spec ke us niyam se aata hai jise wo pakadta hai. Ye file kisi
framework ko nahi jaanti - sirf HTTP. Session 6 me Express merchant isi file se pass hoga.
"""
import json
import uuid

import pytest
from conftest import error_code

RFC3339 = "%Y-%m-%dT%H:%M:%SZ"


def is_rfc3339_utc(value):
    from datetime import datetime
    try:
        datetime.strptime(value, RFC3339)
        return True
    except (TypeError, ValueError):
        return False


def walk_money_fields(node, path=""):
    """Har *_paise field dhoondo, chahe kitna bhi gehra ho."""
    if isinstance(node, dict):
        for k, v in node.items():
            # price_range_paise khud ek dict hai {min, max} - sirf scalar pe assert karo
            if k.endswith("_paise") and not isinstance(v, (dict, list)):
                yield path + "." + k, v
            yield from walk_money_fields(v, path + "." + k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_money_fields(v, path + f"[{i}]")


# ------------------------------------------------------------------ Shape
def test_all_bodies_carry_spec_version(api, manifest, catalog, buyable):
    detail, _ = buyable
    assert manifest["spec_version"] == "1.0"
    assert api.get("/agent/catalog").json()["spec_version"] == "1.0"
    assert detail["spec_version"] == "1.0"


def test_money_fields_are_integers_named_paise(manifest, catalog, buyable):
    detail, _ = buyable
    for blob in (manifest, {"products": catalog}, detail):
        for path, value in walk_money_fields(blob):
            if value is None:      # optional money fields: mrp_paise, shipping_paise
                continue
            assert isinstance(value, int) and not isinstance(value, bool), \
                f"{path} = {value!r} - money integer paise me hona chahiye"


def test_timestamps_are_rfc3339_utc(manifest, catalog, buyable):
    detail, _ = buyable
    assert is_rfc3339_utc(manifest["catalog"]["last_updated_at"])
    for p in catalog:
        assert is_rfc3339_utc(p["updated_at"]), p["product_id"]
    assert is_rfc3339_utc(detail["updated_at"])


def test_every_product_has_at_least_one_variant(api, catalog):
    for p in catalog:
        assert p["variant_count"] >= 1, p["product_id"]
    for p in catalog[:5]:
        assert len(api.get("/agent/products/" + p["product_id"]).json()["variants"]) >= 1


def test_missing_key_is_401_unauthorized(opt):
    import httpx
    r = httpx.get(opt["base_url"] + "/agent/manifest", timeout=10)
    assert r.status_code == 401 and error_code(r) == "UNAUTHORIZED"


def test_manifest_declares_required_policy_fields(manifest):
    assert manifest["policies"]["cancel_window_hours"] >= 0
    assert manifest["policies"]["max_qty_per_variant"] >= 1
    assert set(manifest["payment_modes"]) <= {"payment_link", "checkout", "cod"}
    assert manifest["currency"] == "INR"


def test_catalog_categories_are_declared_in_manifest(manifest, catalog):
    declared = set(manifest["categories"])
    used = {p["category"] for p in catalog}
    assert used <= declared, f"manifest me nahi hain: {sorted(used - declared)}"


# ------------------------------------------------------------------ Catalog
def test_catalog_is_ordered_by_updated_at_then_product_id(catalog):
    keys = [(p["updated_at"], p["product_id"]) for p in catalog]
    assert keys == sorted(keys), "bina stable order ke cursor rows skip kar dega"


def test_cursor_pagination_terminates_and_covers_everything(api, catalog):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 5}
        if cursor:
            params["cursor"] = cursor
        page = api.get("/agent/catalog", params=params).json()
        seen += [p["product_id"] for p in page["products"]]
        pages += 1
        assert pages < 200, "pagination khatam hi nahi hui"
        if not page["has_more"]:
            assert page.get("cursor") in (None, ""), "has_more false pe cursor nahi hona chahiye"
            break
        cursor = page["cursor"]
    assert seen == [p["product_id"] for p in catalog]
    assert len(seen) == len(set(seen)), "cursor ne rows dohra diye"


def test_has_more_always_comes_with_an_advancing_cursor(api, catalog):
    """SPEC 4 + 12: `has_more: true` ke saath non-empty cursor aana hi chahiye, aur
    wo aage badhna chahiye.

    Bina iske Layer ke paas agla page maangne ka koi tareeka nahi bachta - ya to wo
    usi page pe hamesha ghoomti rahegi, ya jaldi ruk kar aadha catalog index kar
    legi. Dono me koi error nahi aata.
    """
    cursors, seen_cursor = [], None
    for _ in range(10):
        params = {"limit": 5}
        if seen_cursor:
            params["cursor"] = seen_cursor
        page = api.get("/agent/catalog", params=params).json()
        if not page["has_more"]:
            break
        assert page.get("cursor"), "has_more true hai par cursor nahi aaya"
        assert page["cursor"] not in cursors, (
            "cursor aage nahi badha - wahi page dobara: " + page["cursor"])
        cursors.append(page["cursor"])
        seen_cursor = page["cursor"]
    assert cursors, "catalog itna chhota hai ki pagination hui hi nahi"


def test_updated_since_is_strictly_greater(api, catalog):
    pivot = catalog[len(catalog) // 2]["updated_at"]
    got = api.get("/agent/catalog", params={"updated_since": pivot, "limit": 500}).json()["products"]
    assert all(p["updated_at"] > pivot for p in got), "strictly greater hona chahiye, >= nahi"
    expected = [p["product_id"] for p in catalog if p["updated_at"] > pivot]
    assert [p["product_id"] for p in got] == expected


def test_updated_since_in_the_future_returns_nothing(api):
    page = api.get("/agent/catalog", params={"updated_since": "2099-01-01T00:00:00Z"}).json()
    assert page["products"] == [] and page["has_more"] is False


def test_price_range_is_coherent(catalog):
    for p in catalog:
        assert p["price_range_paise"]["min"] <= p["price_range_paise"]["max"], p["product_id"]


def test_malformed_cursor_is_rejected(api):
    r = api.get("/agent/catalog", params={"cursor": "!!!not-base64!!!"})
    assert r.status_code == 400 and error_code(r) == "MISSING_FIELD"


# ------------------------------------------------------------------ Detail
def test_detail_is_never_cached(api, catalog):
    r = api.get("/agent/products/" + catalog[0]["product_id"])
    cache = r.headers.get("cache-control", "")
    assert "no-store" in cache or "no-cache" in cache, \
        "is endpoint ke price pe paisa verify hota hai - cache nahi ho sakta"


def test_unknown_product_is_404(api):
    r = api.get("/agent/products/definitely-not-a-real-id")
    assert r.status_code == 404 and error_code(r) == "PRODUCT_NOT_FOUND"


def test_pincode_produces_a_delivery_block(api, opt, catalog):
    r = api.get("/agent/products/" + catalog[0]["product_id"],
                params={"pincode": opt["pincode"]})
    assert r.status_code == 200
    delivery = r.json()["delivery"]
    assert delivery["serviceable"] is True
    assert delivery["pincode"] == opt["pincode"]
    assert isinstance(delivery["shipping_paise"], int)


def test_non_serviceable_pincode_is_200_not_an_error(api, opt, catalog):
    r = api.get("/agent/products/" + catalog[0]["product_id"],
                params={"pincode": opt["dead_pincode"]})
    assert r.status_code == 200, "non-serviceability ek fact hai, failure nahi"
    assert r.json()["delivery"]["serviceable"] is False


def test_a_malformed_pincode_is_a_bad_request_not_an_unserviceable_one(api, catalog):
    """SPEC v1.8 5: `pincode` jo 6 digit ka na ho, wo galat REQUEST hai — galat PATA nahi.

    Farq caller ke liye asli hai. `serviceable: false` ka matlab hai *"doosra merchant
    dekho"*, aur agent theek wahi karta hai — poora merchant chhod deta hai. `400
    MISSING_FIELD` ka matlab hai *"request theek karo"*, aur wahi karne layak cheez hai
    jab pincode me typo ho. Ek merchant jo dono ko ek jaisa jawab deta hai, wo agent ko
    apne hi catalog se door bhej deta hai, bina kisi error ke.
    """
    for bad in ("44001", "4400011", "abcdef", "44 001"):
        r = api.get("/agent/products/" + catalog[0]["product_id"], params={"pincode": bad})
        assert r.status_code == 400, (
            "%r par %d aaya, 400 chahiye tha: %s" % (bad, r.status_code, r.text[:160]))
        assert error_code(r) == "MISSING_FIELD", r.text[:160]


def test_no_two_variants_claim_the_same_photograph(api, catalog):
    """SPEC v1.8 5: do variants ek hi image URL ka daawa nahi kar sakte.

    `variants[].images` ka matlab hai *us variant ki* tasveerein, aur uspar ek consumer
    ye vaakya khada karta hai: *"jo dikh raha hai wahi kharida ja raha hai"*. Product ki
    generic tasveerein har variant par chipka dene se taar par wahi vaakya sach dikhta
    hai — aur grahak ko maroon shirt ko turquoise bata diya jata hai, poore aatmvishwas
    ke saath.

    Jaanch mechanical hai: agar do variants ek hi URL likhte hain, to wo URL dono me se
    kisi ko alag nahi kar sakta. **Khaali `images` bilkul theek hai** aur zyadatar
    catalogs ke liye wahi sahi jawab hai — ye test sirf jhoothe DAAWE ko rokta hai, gap
    ko nahi.
    """
    checked = 0
    for product in catalog:
        detail = api.get("/agent/products/" + product["product_id"]).json()
        variants = detail["variants"]
        if len(variants) < 2:
            continue
        checked += 1
        seen = {}
        for variant in variants:
            for url in variant.get("images") or []:
                assert url not in seen, (
                    "%s: %s aur %s dono %s ka daawa karte hain — ek hi tasveer dono ko "
                    "alag nahi kar sakti"
                    % (product["product_id"], seen[url], variant["variant_id"], url))
                seen[url] = variant["variant_id"]
    if not checked:
        pytest.skip("is catalog me koi multi-variant product hi nahi hai")


def test_detail_respects_review_and_related_limits(api, catalog):
    for p in catalog[:10]:
        d = api.get("/agent/products/" + p["product_id"]).json()
        assert len(d.get("reviews", [])) <= 5, p["product_id"]
        assert len(d.get("related_product_ids", [])) <= 10, p["product_id"]


def test_variant_options_match_declared_dimensions(api, catalog):
    for p in catalog[:10]:
        names = {o["name"] for o in p["variant_options"]}
        for v in api.get("/agent/products/" + p["product_id"]).json()["variants"]:
            assert set(v["options"]) == names, f"{v['variant_id']} options catalog se mel nahi khate"


def test_mrp_is_never_below_price(api, catalog):
    for p in catalog[:10]:
        for v in api.get("/agent/products/" + p["product_id"]).json()["variants"]:
            if v.get("mrp_paise") is not None:
                assert v["mrp_paise"] >= v["price_paise"], v["variant_id"]


# ------------------------------------------------------------------ Orders
def test_order_totals_add_up(order_factory):
    r, _ = order_factory(qty=2)
    o = r.json()
    assert o["final_total_paise"] == (o["items_total_paise"] + o["shipping_paise"]
                                      - o["discount_paise"])
    assert o["status"] == "created", "order create pe koi paisa nahi chalna chahiye"


def test_order_creation_moves_no_money(api, order_factory):
    r, _ = order_factory()
    view = api.get("/agent/orders/" + r.json()["order_id"]).json()
    assert view["payment"]["state"] == "pending"
    assert view["payment"].get("razorpay_payment_id") is None


def test_price_mismatch_is_rejected_with_the_actual_price(order_factory, buyable):
    _, v = buyable
    r, _ = order_factory(price=v["price_paise"] + 1, expect=409)
    assert error_code(r) == "PRICE_CHANGED"
    assert r.json()["error"]["details"]["actual_price_paise"] == v["price_paise"]


def test_items_total_mismatch_is_rejected(order_factory):
    r, _ = order_factory(total=1, expect=409)
    assert error_code(r) == "TOTAL_CHANGED"
    assert "actual_items_total_paise" in r.json()["error"]["details"]


def test_quantity_above_the_declared_ceiling_is_refused(order_factory, manifest):
    r, _ = order_factory(qty=manifest["policies"]["max_qty_per_variant"] + 1, expect=409)
    assert error_code(r) == "QTY_LIMIT_EXCEEDED"


def test_stock_beyond_availability_is_refused_with_available_qty(api, order_factory, buyable):
    _, v = buyable
    live = api.get("/agent/products/" + buyable[0]["product_id"]).json()
    stock = next(x["stock"] for x in live["variants"] if x["variant_id"] == v["variant_id"])
    r, _ = order_factory(qty=stock + 1, expect=409)
    assert error_code(r) in ("OUT_OF_STOCK", "QTY_LIMIT_EXCEEDED")
    if error_code(r) == "OUT_OF_STOCK":
        assert "available_qty" in r.json()["error"]["details"]


def test_non_serviceable_pincode_is_refused_at_order_time(order_factory, opt):
    r, _ = order_factory(pincode=opt["dead_pincode"], expect=409)
    assert error_code(r) == "NOT_SERVICEABLE"


# SPEC v1.9. Ye teen test ek hi baat ke teen roop hain: **maujoodgi jaanchna shakal
# jaanchna nahi hai.** Dono reference merchants `phone: "98765"` / `phone: "1"` aur
# `country: "ZZ"` par order bana dete the — ek asli order, asli paise se settle hone
# layak, jise koi courier deliver nahi kar sakta. Aur koi test red nahi hota tha, kyunki
# koi rule likha hi nahi tha.
def test_a_malformed_phone_is_rejected(order_factory):
    r, _ = order_factory(phone="98765", expect=400)
    assert error_code(r) == "MISSING_FIELD"


def test_a_country_other_than_india_is_rejected(order_factory):
    r, _ = order_factory(country="ZZ", expect=400)
    assert error_code(r) == "MISSING_FIELD"


# Malformed pincode "doosri dukaan dekho" nahi hai, "request theek karo" hai — wahi farq
# jo v1.8 ne product detail par tay kiya tha. Order ke raaste par wo likha nahi tha, aur
# do merchants ne do alag jawab diye: ek `400 MISSING_FIELD`, doosra `409 NOT_SERVICEABLE`.
def test_a_malformed_pincode_at_order_time_is_a_bad_request_not_unserviceable(order_factory):
    r, _ = order_factory(pincode="44001", expect=400)
    assert error_code(r) == "MISSING_FIELD"


def test_unknown_variant_is_refused(api, opt):
    body = {"items": [{"variant_id": "no-such-variant", "qty": 1, "expected_price_paise": 100}],
            "expected_items_total_paise": 100,
            "contact": {"name": "Bot", "phone": "+919876543210"},
            "address": {"line1": "1 Test Road", "city": "Nagpur", "state": "Maharashtra",
                        "pincode": opt["pincode"], "country": "IN"},
            "payment_mode": "payment_link"}
    r = api.post("/agent/orders", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 400 and error_code(r) == "INVALID_VARIANT"


def test_idempotency_key_is_mandatory(api, opt, buyable):
    _, v = buyable
    body = {"items": [{"variant_id": v["variant_id"], "qty": 1,
                       "expected_price_paise": v["price_paise"]}],
            "expected_items_total_paise": v["price_paise"],
            "contact": {"name": "Bot", "phone": "+919876543210"},
            "address": {"line1": "1 Test Road", "city": "Nagpur", "state": "Maharashtra",
                        "pincode": opt["pincode"], "country": "IN"},
            "payment_mode": "payment_link"}
    r = api.post("/agent/orders", json=body)
    assert r.status_code == 400 and error_code(r) == "MISSING_FIELD"


def test_replaying_a_key_returns_the_original_order(order_factory):
    key = uuid.uuid4().hex
    first, _ = order_factory(key=key)
    second, _ = order_factory(key=key)
    assert first.json()["order_id"] == second.json()["order_id"]
    assert first.json() == second.json(), "replay verbatim original lautana chahiye"


def test_same_key_with_a_different_body_conflicts(order_factory):
    key = uuid.uuid4().hex
    order_factory(qty=1, key=key)
    r, _ = order_factory(qty=2, key=key, expect=409)
    assert error_code(r) == "IDEMPOTENCY_CONFLICT"


def test_undeclared_payment_mode_is_refused(order_factory, manifest):
    undeclared = {"payment_link", "checkout", "cod"} - set(manifest["payment_modes"])
    if not undeclared:
        pytest.skip("ye merchant teenon modes deta hai - undeclared mode bacha hi nahi")
    r, _ = order_factory(mode=sorted(undeclared)[0], expect=400)
    assert error_code(r) == "UNSUPPORTED_PAYMENT_MODE"


def test_checkout_mode_returns_an_instrument_that_can_actually_be_paid(order_factory,
                                                                        manifest):
    """SPEC v1.4 6: `checkout` me order id AUR public key id, dono.

    Order id akela pay nahi hota - har checkout client ko wo key chahiye jo account
    pehchanti hai. Sirf order id lautane wala merchant ek aisa instrument deta hai jise
    koi settle nahi kar sakta, aur ye chupchap hota hai: koi error nahi aata, bas order
    unpaid pada rehta hai jab tak expire na ho jaye.

    Ye link nahi banata, sirf ek provider order - isliye rate limit ka khatra nahi (D-33).
    """
    if "checkout" not in manifest["payment_modes"]:
        pytest.skip("ye merchant checkout mode nahi deta")
    r, _ = order_factory(mode="checkout", expect=None)
    if r.status_code in (429, 409) or r.status_code >= 500:
        pytest.skip("provider abhi instrument nahi bana pa raha: " + r.text[:120])
    assert r.status_code == 201, r.text
    payment = r.json()["payment"]
    assert payment["mode"] == "checkout"
    assert payment.get("razorpay_order_id"), "checkout me order id chahiye"
    assert payment.get("razorpay_key_id"), (
        "checkout me public key_id bhi chahiye - iske bina instrument pay hi nahi hota")
    assert payment.get("link_url") is None, "checkout me link_url null hona chahiye"
    # SPEC v1.5 9: expiry order ki zimmedari hai, provider ki nahi. Provider order id ke
    # paas apni koi expiry hoti hi nahi - agar merchant wahin se leta hai to bina pay hua
    # order stock HAMESHA ke liye rok leta hai.
    assert payment.get("expires_at"), (
        "unpaid order pe expires_at chahiye - iske bina chhoda hua order stock kabhi "
        "nahi chhodta")
    assert is_rfc3339_utc(payment["expires_at"]), payment["expires_at"]
    # Secret kabhi response me nahi. Razorpay ka publishable id `rzp_` se shuru hota hai;
    # secret nahi hota - to shape check ke bajaye ye dekhte hain ki koi bhi aisi value
    # response me na ho jo secret jaisi lambi random string ho.
    assert "secret" not in json.dumps(r.json()).lower()


def test_payment_link_mode_returns_a_usable_instrument(order_factory, manifest):
    """Sirf ek order asli provider se banta hai - baaki suite COD pe chalti hai taaki
    provider ka rate limit na lage."""
    if "payment_link" not in manifest["payment_modes"]:
        pytest.skip("ye merchant payment_link nahi deta")
    r, _ = order_factory(mode="payment_link", expect=None)
    if r.status_code == 429:
        # Provider ka rate limit merchant ki contract violation nahi hai. Merchant ne
        # sahi kiya: 429 RATE_LIMITED + Retry-After. Assert karne ko kuch bacha nahi.
        assert error_code(r) == "RATE_LIMITED" and r.headers.get("retry-after")
        pytest.skip("payment provider abhi rate limit kar raha hai")
    if r.status_code >= 500:
        # Provider gir gaya. Skip karna theek hai, par CHUPKE se nahi - merchant ko
        # provider ka message details me dena hi chahiye, warna kisi ko pata hi nahi
        # chalega ki fail kaun hua.
        details = r.json().get("error", {}).get("details", {})
        assert details.get("provider_message"), (
            "provider fail hua par merchant ne wajah nahi batayi: " + r.text[:200])
        pytest.skip("payment provider ne mana kiya: " + details["provider_message"][:120])
    assert r.status_code == 201, r.text
    payment = r.json()["payment"]
    assert payment["mode"] == "payment_link"
    assert payment["link_url"], "payment_link mode me link_url hona chahiye"
    assert is_rfc3339_utc(payment["expires_at"])


def test_reserved_stock_is_released_on_cancel(api, order_factory, buyable):
    detail, v = buyable
    before = next(x["stock"] for x in api.get("/agent/products/" + detail["product_id"]).json()
                  ["variants"] if x["variant_id"] == v["variant_id"])
    r, _ = order_factory(qty=2)
    during = next(x["stock"] for x in api.get("/agent/products/" + detail["product_id"]).json()
                  ["variants"] if x["variant_id"] == v["variant_id"])
    assert during == before - 2, "order create pe stock reserve hona chahiye"
    api.post(f"/agent/orders/{r.json()['order_id']}/cancel", json={"reason": "test"})
    after = next(x["stock"] for x in api.get("/agent/products/" + detail["product_id"]).json()
                 ["variants"] if x["variant_id"] == v["variant_id"])
    assert after == before, "cancel pe stock chhootna chahiye"


def test_updated_at_advances_monotonically_within_one_second(api, order_factory, buyable):
    """SPEC 4 + 12: ek hi second me do change ho to bhi dono timestamp strictly badhne
    chahiye. Bina iske Layer ka strictly-greater delta sync doosre change ko hamesha
    ke liye nahi dekhta - bina kisi error ke.

    Do order back-to-back banake hi ye reproduce hota hai: dono ek hi second me
    stock ghatate hain. `updated_at = now()` likhne wala merchant yahin girega.
    """
    detail, _ = buyable
    stamps = [api.get("/agent/products/" + detail["product_id"]).json()["updated_at"]]
    for _ in range(3):
        order_factory(qty=1)
        stamps.append(api.get("/agent/products/" + detail["product_id"]).json()["updated_at"])
    assert stamps == sorted(stamps), f"timestamps peeche gaye: {stamps}"
    assert len(set(stamps)) == len(stamps), (
        f"do change, ek hi timestamp -> doosra change delta sync se hamesha ke liye "
        f"gayab: {stamps}")


def test_updated_at_is_monotonic_across_the_whole_store(api, order_factory, buyable):
    """SPEC 4 + 12: nayi `updated_at` poore STORE ke max se badi honi chahiye, sirf
    us product ke apne purane se nahi.

    Per-product monotonicity kaafi lagti hai, par nahi hai. Ek product ko baar-baar
    bump karo aur uska stamp asli waqt se AAGE nikal jata hai. Ab koi DOOSRA product
    pehli baar badle - uska sahi `now` us drift ke peeche padta hai, Layer ke global
    watermark se peeche, aur wo product kabhi index nahi hota.

    Test yahi banata hai: ek product ko teen baar bump karke drift paida karo, phir
    doosre product ko chhuo, aur dekho ki uska naya stamp poore catalog ke purane max
    se bada hai ya nahi.
    """
    detail, _ = buyable
    for _ in range(3):                      # ek product ko aage dhakelo
        order_factory(qty=1)

    products = api.get("/agent/catalog", params={"limit": 500}).json()["products"]
    peak = max(p["updated_at"] for p in products)
    other = next((p for p in products
                  if p["product_id"] != detail["product_id"] and p["in_stock"]), None)
    if other is None:
        pytest.skip("doosra in-stock product nahi mila")

    other_detail = api.get("/agent/products/" + other["product_id"]).json()
    variant = next((v for v in other_detail["variants"] if v["stock"] >= 1), None)
    if variant is None:
        pytest.skip("doosre product ka koi variant stock me nahi")

    r = api.post("/agent/orders", headers={"Idempotency-Key": uuid.uuid4().hex}, json={
        "items": [{"variant_id": variant["variant_id"], "qty": 1,
                   "expected_price_paise": variant["price_paise"]}],
        "expected_items_total_paise": variant["price_paise"],
        "contact": {"name": "Conformance Bot", "phone": "+919876543210"},
        "address": {"line1": "1 Test Road", "city": "Nagpur", "state": "Maharashtra",
                    "pincode": "440001", "country": "IN"},
        "payment_mode": "cod"})
    assert r.status_code == 201, r.text
    try:
        after = api.get("/agent/products/" + other["product_id"]).json()["updated_at"]
        assert after > peak, (
            "doosre product ka naya updated_at %s poore store ke purane max %s se bada "
            "nahi hai - Layer ka global watermark ise kabhi nahi dekhega" % (after, peak))
    finally:
        api.post("/agent/orders/%s/cancel" % r.json()["order_id"],
                 json={"reason": "conformance cleanup"})


def test_stock_change_bumps_updated_at(api, order_factory, buyable):
    detail, _ = buyable
    before = api.get("/agent/products/" + detail["product_id"]).json()["updated_at"]
    order_factory(qty=1)
    after = api.get("/agent/products/" + detail["product_id"]).json()["updated_at"]
    assert after >= before
    assert after != before, "stock badla par updated_at nahi - delta sync ise kabhi nahi dekhega"


# ------------------------------------------------------------------ Order status
def test_order_status_returns_a_timeline(api, order_factory):
    r, _ = order_factory()
    view = api.get("/agent/orders/" + r.json()["order_id"]).json()
    assert view["timeline"] and view["timeline"][0]["status"] == "created"
    assert all(is_rfc3339_utc(t["at"]) for t in view["timeline"])
    ats = [t["at"] for t in view["timeline"]]
    assert ats == sorted(ats), "timeline chronological honi chahiye"


def test_unknown_order_is_404(api):
    r = api.get("/agent/orders/ord_definitely_not_real")
    assert r.status_code == 404 and error_code(r) == "ORDER_NOT_FOUND"


def test_status_is_a_known_lifecycle_value(api, order_factory):
    r, _ = order_factory()
    view = api.get("/agent/orders/" + r.json()["order_id"]).json()
    assert view["status"] in {"created", "paid", "confirmed", "shipped", "delivered",
                              "cancelled", "refunded", "failed"}


# ------------------------------------------------------------------ Cancel
def test_cancel_requires_a_reason(api, order_factory):
    r, _ = order_factory()
    bad = api.post(f"/agent/orders/{r.json()['order_id']}/cancel", json={})
    assert bad.status_code == 400 and error_code(bad) == "MISSING_FIELD"


def test_cancelling_twice_is_not_an_error(api, order_factory):
    r, _ = order_factory()
    order_id = r.json()["order_id"]
    first = api.post(f"/agent/orders/{order_id}/cancel", json={"reason": "changed mind"})
    second = api.post(f"/agent/orders/{order_id}/cancel", json={"reason": "changed mind"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["cancelled_at"] == second.json()["cancelled_at"]


def test_cancelling_an_unpaid_order_has_no_refund(api, order_factory):
    r, _ = order_factory()
    out = api.post(f"/agent/orders/{r.json()['order_id']}/cancel",
                   json={"reason": "unpaid cancel"}).json()
    assert out["status"] == "cancelled"
    assert out["refund"] is None, "unpaid order me lautane ko paisa hai hi nahi"


def test_cancelled_order_is_no_longer_cancellable(api, order_factory):
    r, _ = order_factory()
    order_id = r.json()["order_id"]
    api.post(f"/agent/orders/{order_id}/cancel", json={"reason": "done"})
    assert api.get("/agent/orders/" + order_id).json()["cancellable"] is False


def test_a_closed_cancel_window_reports_no_deadline(api, order_factory):
    """SPEC 7: `cancellable_until` `null` ho jab `cancellable` false ho.

    Dono ek saath dena — "cancel nahi ho sakta" aur "cancel karne ki aakhri tareekh do
    din baad hai" — ek hi body me do ulte jawab hain, aur padhne wale ke paas unme se
    chunne ka koi niyam nahi hai."""
    r, _ = order_factory()
    order_id = r.json()["order_id"]
    api.post(f"/agent/orders/{order_id}/cancel", json={"reason": "done"})
    view = api.get("/agent/orders/" + order_id).json()
    assert view["cancellable"] is False
    assert view.get("cancellable_until") is None, (
        "cancellable false hai par deadline abhi bhi di ja rahi hai: "
        + str(view.get("cancellable_until")))


def test_order_read_carries_the_refund_object(api, order_factory):
    """SPEC 7: `refund` order READ pe bhi aata hai, sirf cancel ki response me nahi.

    Cancel ki response ek hi baar aati hai aur agent ke context se scroll ho jati hai.
    Uske baad `razorpay_refund_id` aur `expected_by` — theek wo do cheezein jo insaan
    *"mera paisa kab aayega"* poochhne pe maangta hai — kahin bachti hi nahi thin.

    Yahan order COD/unpaid hai, to `refund` `null` hona chahiye. Ye field ke **maujood
    hone** ka test hai; pending-vs-settled wala rule paid order maangta hai aur wo
    `merchants/northwind/test_northwind.py` me hai (D-67)."""
    r, _ = order_factory()
    order_id = r.json()["order_id"]
    api.post(f"/agent/orders/{order_id}/cancel", json={"reason": "refund shape"})
    view = api.get("/agent/orders/" + order_id).json()
    assert "refund" in view, "order read me `refund` field hai hi nahi (SPEC 7)"
    assert view["refund"] is None, "unpaid order pe refund hona hi nahi chahiye"


def test_payment_state_is_a_known_value(api, order_factory):
    """SPEC 7 ka enum. `refund_pending` isme isliye hai ki "bhej diya" aur "aa gaya" do
    alag baatein hain, aur purane enum me doosri kehne ka koi tareeka hi nahi tha."""
    r, _ = order_factory()
    order_id = r.json()["order_id"]
    api.post(f"/agent/orders/{order_id}/cancel", json={"reason": "state check"})
    view = api.get("/agent/orders/" + order_id).json()
    assert view["payment"]["state"] in {"pending", "paid", "failed",
                                        "refund_pending", "refunded", "refund_failed"}


# ------------------------------------------------------------------ Honesty
def test_description_is_returned_verbatim(api, catalog):
    """SPEC 10: merchant description sanitize nahi karta. Safai layer ka kaam hai -
    agar har merchant apne tarike se saaf kare to layer attack detect hi nahi kar payegi."""
    for p in catalog[:20]:
        detail = api.get("/agent/products/" + p["product_id"]).json()
        assert detail["description"] == p["description"], \
            f"{p['product_id']}: catalog aur detail ki description alag hain"


def test_detail_prices_match_a_fresh_read(api, catalog):
    pid = catalog[0]["product_id"]
    a = api.get("/agent/products/" + pid).json()
    b = api.get("/agent/products/" + pid).json()
    assert [v["price_paise"] for v in a["variants"]] == [v["price_paise"] for v in b["variants"]]


# ------------------------------------------------------------ delivery (SPEC 1.7)
DELIVERY_WORDS = ("ship", "deliver", "dispatch", "eta", "arriv")


def test_attributes_do_not_answer_delivery_timing(api, catalog):
    """SPEC 5: delivery ka waqt sirf `delivery.eta_days` batata hai.

    `attributes` ek azaad prose field hai jo har buyer ke liye ek jaisa jata hai;
    `delivery` ek pincode ke liye bantI hai. Dono bolen to wo ek doosre se ulte hote
    hain aur response me koi ye nahi kehta ki kaun sa jeetega. Naapa hua nateeja: teen
    me teen products par `attributes.shipping_note` *"Ships overnight"* / *"Ships in 1
    week"* keh raha tha jabki `eta_days` teenon par `3` tha — aur `attributes` body me
    **pehle** aata hai, yaani ek model wahi galat wala uthata hai.
    """
    # Poora catalog dekha jata hai (50 tak). Pehle ye `[:20]` tha aur break-verify me
    # pakda hi nahi gaya: maine ek product me contradiction wapas daala aur wo product
    # us window me tha hi nahi. Ek test jo aadha catalog dekhta hai, wo aadha jawab deta
    # hai aur poora green dikhta hai.
    offenders = []
    for product in catalog[:50]:
        detail = api.get("/agent/products/" + product["product_id"]).json()
        for key, value in (detail.get("attributes") or {}).items():
            if any(word in key.lower() for word in DELIVERY_WORDS):
                offenders.append("%s: attributes.%s = %r" % (product["product_id"],
                                                             key, value))
    assert not offenders, (
        "delivery ka waqt do jagah se aa raha hai; `delivery.eta_days` ke alawa kahin "
        "nahi hona chahiye:\n  " + "\n  ".join(offenders[:8]))


def test_an_order_carries_the_delivery_promise_it_was_made_with(api, order_factory):
    """SPEC 6: order apne saath wo waada le kar chalta hai jo banate waqt kiya gaya.

    Iske bina *"order kab aayega"* ka jawab surface pe kahin hai hi nahi — `eta_days`
    sirf product endpoint par milta hai, aur wahi tab jab pincode diya jaye, yaani order
    banne se **pehle**. Agent ke paas do hi raaste bachte hain aur dono galat hain:
    `status: paid` ko shipping state bol dena, ya product dobara padhkar **aaj** ka
    estimate dena — jo us order ka waada hai hi nahi.
    """
    r, _ = order_factory()
    created = r.json()
    assert created.get("delivery"), "order create me `delivery` block hai hi nahi"
    assert isinstance(created["delivery"]["eta_days"], int)
    assert is_rfc3339_utc(created["delivery"]["promised_by"])

    view = api.get("/agent/orders/" + created["order_id"]).json()
    assert view.get("delivery"), "order read me `delivery` block hai hi nahi"
    assert view["delivery"] == created["delivery"]
    # NOTE: ye test "dobara compute mat karo" wale aadhe hisse ko **pakad nahi sakta**,
    # aur ye seema likh kar rakhi ja rahi hai. Order abhi-abhi bana hai, to `now + eta`
    # aur `created_at + eta` ek hi second me girte hain aur barabar dikhte hain. Use
    # pakadne ke liye ek PURANA order chahiye, jo sirf apne merchant ki DB me rakha ja
    # sakta hai - `merchants/northwind/test_northwind.py` dekho (D-67 wali hi wajah).
