"""Session 4 ka de-risk: kya ek AGENT (bina browser ke) test mode me pay kar sakta hai?

    .venv/Scripts/python.exe scripts/derisk_s2s.py

Sawaal ye hai: cap ke neeche wala autonomous payment ASLI ho sakta hai ya simulated
hi rakhna padega. Teen raaste hain jinme browser nahi aata — teenon try karke uska
exact jawab nikalte hain, guess karke nahi:

  A. S2S UPI collect  — POST /v1/payments/create/upi, vpa=success@razorpay
  B. Checkout ajax    — POST /v1/payments/create/ajax  (checkout page andar yahi call karta hai)
  C. UPI Autopay      — recurring token wala order (= Reserve Pay ka test roop)

Raw httpx isliye, razorpay SDK isliye nahi: session 0 me SDK ne har cheez ko
"invalid request sent" bana diya tha aur usse kuch pata nahi chalta. Yahan asli
provider body chahiye — "not enabled for this account" aur "bad payload" me farq
karna hi is script ka poora kaam hai.
"""
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
KEY_ID = os.environ["RAZORPAY_KEY_ID"]
SECRET = os.environ["RAZORPAY_KEY_SECRET"]
assert KEY_ID.startswith("rzp_test_"), "refusing non-test key: " + KEY_ID[:12]

API = "https://api.razorpay.com/v1"
AUTH = (KEY_ID, SECRET)
AMOUNT = 10000          # Rs 100 — sasta, sirf capability check hai
CONTACT = "+919876543210"   # session 0: recurring digits wale number reject hote hain
EMAIL = "buyer@example.com"


def show(label, response):
    body = response.text[:500]
    try:
        body = json.dumps(response.json(), indent=2)[:700]
    except ValueError:
        pass
    print("  HTTP %d\n%s" % (response.status_code, body))
    ok = response.status_code < 400
    print("  => %s %s\n" % ("CHALA" if ok else "NAHI CHALA", label))
    return ok


def make_order(extra=None):
    payload = {"amount": AMOUNT, "currency": "INR", "receipt": "derisk_s2s"}
    payload.update(extra or {})
    r = httpx.post(API + "/orders", auth=AUTH, json=payload, timeout=30)
    if r.status_code >= 400:
        show("order banana", r)
        return None
    return r.json()["id"]


def route_a():
    """S2S UPI collect. Documented API, par account pe enable hona chahiye."""
    print("--- A. S2S UPI collect (payments/create/upi) ---")
    order_id = make_order()
    if not order_id:
        return False
    r = httpx.post(API + "/payments/create/upi", auth=AUTH, timeout=30, json={
        "amount": AMOUNT, "currency": "INR", "order_id": order_id,
        "email": EMAIL, "contact": CONTACT, "method": "upi",
        "upi": {"flow": "collect", "vpa": "success@razorpay"}})
    return show("S2S UPI", r)


def route_b():
    """Checkout page khud yahi endpoint maarta hai. Undocumented — agar ye chala bhi,
    to iski keemat ye hai ki kisi bhi din bina notice ke badal sakta hai."""
    print("--- B. Checkout internal ajax (payments/create/ajax) ---")
    order_id = make_order()
    if not order_id:
        return False
    r = httpx.post(API + "/payments/create/ajax", timeout=30, data={
        "key_id": KEY_ID, "amount": AMOUNT, "currency": "INR", "order_id": order_id,
        "email": EMAIL, "contact": CONTACT, "method": "upi", "vpa": "success@razorpay",
        "upi[flow]": "collect"})
    return show("checkout ajax", r)


def route_c():
    """UPI Autopay / e-mandate — yahi asli "Reserve Pay" jaisa model hai: insaan EK BAAR
    mandate deta hai, uske baad debits server-side hote hain. Yahan sirf ye dekh rahe hain
    ki account recurring order banane deta hai ya nahi."""
    print("--- C. UPI Autopay mandate order (recurring) ---")
    r = httpx.post(API + "/customers", auth=AUTH, timeout=30,
                   json={"name": "Agent Buyer", "contact": CONTACT, "email": EMAIL,
                         "fail_existing": "0"})
    if r.status_code >= 400:
        return show("customer banana", r)
    customer_id = r.json()["id"]
    r = httpx.post(API + "/orders", auth=AUTH, timeout=30, json={
        "amount": AMOUNT, "currency": "INR", "receipt": "derisk_mandate",
        "method": "upi", "customer_id": customer_id, "payment_capture": True,
        "token": {"max_amount": 200000, "expire_at": 2145916800,
                  "frequency": "as_presented"}})
    return show("autopay mandate order", r)


if __name__ == "__main__":
    print("key:", KEY_ID[:14] + "...\n")
    results = {"A s2s_upi": route_a(), "B checkout_ajax": route_b(), "C upi_autopay": route_c()}
    print("=" * 60)
    for name, ok in results.items():
        print("  %-18s %s" % (name, "CHALA" if ok else "nahi"))
    print("\nFaisla:", "cap ke neeche ASLI payment mumkin hai"
          if any(results.values()) else
          "koi bhi browser-free raasta enabled nahi -> simulated mandate")
    sys.exit(0 if any(results.values()) else 1)
