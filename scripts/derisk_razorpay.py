"""Session 0 gate: Razorpay test-mode round trip.

    python scripts/derisk_razorpay.py create        -> payment link banao, URL print karo
    python scripts/derisk_razorpay.py verify <id>   -> paid hone ka wait, fetch, refund

Paanch gates: link bane . URL mile . test card se pay ho . status fetch ho . refund ho.
"""
import os, sys, time
import razorpay
from dotenv import load_dotenv

load_dotenv()
KEY_ID = os.environ["RAZORPAY_KEY_ID"]
SECRET = os.environ["RAZORPAY_KEY_SECRET"]
assert KEY_ID.startswith("rzp_test_"), f"refusing non-test key: {KEY_ID[:12]}"

client = razorpay.Client(auth=(KEY_ID, SECRET))
AMOUNT_PAISE = 49900  # Rs 499 - ek asli order jaisa


def create():
    link = client.payment_link.create({
        "amount": AMOUNT_PAISE,
        "currency": "INR",
        "description": "ACP de-risk - Northwind Apparel test order",
        "customer": {"name": "Test Buyer", "contact": "+919876543210",
                     "email": "buyer@example.com"},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
    })
    print(f"GATE 1 link banaya   : {link['id']}")
    print(f"GATE 2 URL           : {link['short_url']}")
    print(f"       amount        : {link['amount']} paise  status: {link['status']}")
    print(f"\nab isse browser me kholo, card 4111 1111 1111 1111 / koi bhi future expiry / cvv 123")
    print(f"phir chalao: python scripts/derisk_razorpay.py verify {link['id']}")
    return link["id"]


def verify(link_id, timeout_s=600):
    deadline = time.time() + timeout_s
    while True:
        link = client.payment_link.fetch(link_id)
        if link["status"] == "paid":
            break
        if time.time() > deadline:
            sys.exit(f"FAIL: {timeout_s}s me pay nahi hua (status={link['status']})")
        time.sleep(5)

    payment_id = link["payments"][0]["payment_id"]
    print(f"GATE 3 pay hua       : {payment_id}")

    pay = client.payment.fetch(payment_id)
    print(f"GATE 4 status fetch  : status={pay['status']} amount={pay['amount']} "
          f"method={pay.get('method')} captured={pay.get('captured')}")
    assert pay["status"] == "captured", f"captured nahi hua: {pay['status']}"

    refund = client.refund.create({"payment_id": payment_id,
                                   "amount": pay["amount"], "speed": "normal"})
    print(f"GATE 5 refund        : {refund['id']} status={refund['status']} "
          f"amount={refund['amount']}")
    print("\nSAARE 5 GATES PASS - Razorpay path clear hai")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "create"
    if cmd == "create":
        create()
    elif cmd == "verify":
        verify(sys.argv[2])
    else:
        sys.exit(__doc__)
