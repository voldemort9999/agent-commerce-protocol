"""Ek suite, har merchant. Stack se koi matlab nahi - sirf HTTP.

    pytest spec/conformance --base-url http://localhost:8001 --agent-key <key>
"""
import os
import uuid

import httpx
import pytest


def pytest_addoption(parser):
    parser.addoption("--base-url", default="http://127.0.0.1:8001")
    parser.addoption("--agent-key", default=os.getenv("NORTHWIND_AGENT_KEY",
                                                     "nw_agentkey_local_dev_only"))
    parser.addoption("--pincode", default="440001",
                     help="Ek pincode jise ye merchant serve karta hai")
    parser.addoption("--dead-pincode", default="781001",
                     help="Ek pincode jise ye merchant serve NAHI karta")


@pytest.fixture(scope="session")
def opt(pytestconfig):
    return {
        "base_url": pytestconfig.getoption("--base-url").rstrip("/"),
        "key": pytestconfig.getoption("--agent-key"),
        "pincode": pytestconfig.getoption("--pincode"),
        "dead_pincode": pytestconfig.getoption("--dead-pincode"),
    }


@pytest.fixture(scope="session")
def api(opt):
    with httpx.Client(base_url=opt["base_url"], headers={"X-Agent-Key": opt["key"]},
                      timeout=30) as client:
        yield client


@pytest.fixture(scope="session")
def manifest(api):
    r = api.get("/agent/manifest")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def catalog(api):
    r = api.get("/agent/catalog", params={"limit": 500})
    assert r.status_code == 200, r.text
    return r.json()["products"]


@pytest.fixture(scope="session")
def buyable(api, catalog):
    """Ek (product, variant) jiska stock >= 3 ho — aur unme SABSE SASTA.

    Pehle ye "jo pehle mil jaye" leta tha, aur catalog `updated_at` ke order me aata hai.
    Yaani kaunsa variant test hoga wo is baat pe nirbhar tha ki aaj kis product ka stock
    aakhri baar hila — do alag din, do alag variants. Ek din wo ek Rs 5.6 lakh wali
    ghadi pe ja gira aur payment provider ne amount hi mana kar di.

    Sabse sasta chunna do cheezein deta hai: suite deterministic ho jati hai, aur order
    ka total har payment instrument ki apni ceiling ke neeche rehta hai (UPI, link, card
    — sabki alag hai). Contract test karne ke liye mehnga order chahiye hi nahi.
    """
    best = None
    for p in catalog:
        if not p["in_stock"]:
            continue
        detail = api.get("/agent/products/" + p["product_id"]).json()
        for v in detail["variants"]:
            if v["stock"] >= 3 and (best is None or v["price_paise"] < best[1]["price_paise"]):
                best = (detail, v)
    if best is None:
        pytest.skip("koi variant stock >= 3 ke saath nahi mila")
    return best


@pytest.fixture(scope="session")
def cheap_mode(manifest):
    """Contract test karne ke liye asli payment instrument banane ki zarurat nahi.
    COD kisi provider ko call nahi karta - aur Razorpay payment-link creation pe rate
    limit lagata hai, to 15 order banate hi suite khud 429 kha jati thi."""
    modes = manifest["payment_modes"]
    return "cod" if "cod" in modes else modes[0]


@pytest.fixture
def order_factory(api, opt, buyable, cheap_mode):
    """Order banata hai aur test ke baad cancel kar deta hai - suite dobara chalayi ja sake."""
    created = []

    def make(qty=1, price=None, total=None, pincode=None, mode=None,
             coupon=None, key=None, expect=201, phone=None, country=None):
        mode = mode or cheap_mode
        detail, v = buyable
        unit = v["price_paise"] if price is None else price
        body = {
            "items": [{"variant_id": v["variant_id"], "qty": qty,
                       "expected_price_paise": unit}],
            "expected_items_total_paise": unit * qty if total is None else total,
            "contact": {"name": "Conformance Bot", "phone": phone or "+919876543210",
                        "email": "bot@example.com"},
            "address": {"line1": "1 Test Road", "city": "Nagpur", "state": "Maharashtra",
                        "pincode": pincode or opt["pincode"],
                        "country": country or "IN"},
            "payment_mode": mode,
        }
        if coupon:
            body["coupon_code"] = coupon
        r = api.post("/agent/orders", json=body,
                     headers={"Idempotency-Key": key or uuid.uuid4().hex})
        if expect is not None:      # expect=None matlab caller khud status dekhega
            assert r.status_code == expect, f"expected {expect}, got {r.status_code}: {r.text}"
        if r.status_code == 201:
            created.append(r.json()["order_id"])
        return r, body

    yield make

    for order_id in created:
        api.post(f"/agent/orders/{order_id}/cancel", json={"reason": "conformance cleanup"})


def error_code(response):
    return response.json().get("error", {}).get("code")
