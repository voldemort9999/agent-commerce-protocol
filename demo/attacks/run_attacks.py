"""Teen hamle, teenon block, teenon log me — session 5 ka gate.

    .venv/Scripts/python.exe demo/attacks/run_attacks.py

Ye Layer ke **usi tool surface** se hokar chalta hai jisse ek MCP client baat karta hai
(`server.add_to_cart` wahi function hai jise MCP `add_to_cart` call karta hai), isliye
har call `@audited` se guzarti hai aur asli `layer.db` ke audit log me utarti hai. Baad
me use padho:

    .venv/Scripts/python.exe scripts/audit.py agent <token>
    .venv/Scripts/python.exe scripts/audit.py trail <order_id>
    .venv/Scripts/python.exe scripts/audit.py metrics

**Ye stdio MCP client kyun nahi hai:** teesra hamla — cart ka quote badalkar sasta order
banana — MCP se express hi nahi hota. `create_order` me price ka koi parameter hai hi
nahi; Layer khud apne cart se quote uthati hai. Us hamle ko *koshish* karne ke liye bhi
Layer ke andar haath daalna padta hai, aur yahi us hamle ka jawab hai. Protocol sach me
bolta hai ya nahi, wo `scripts/mcp_smoke.py` sabit karta hai.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "layer"))
import cart        # noqa: E402
import db          # noqa: E402
import policy      # noqa: E402
import registry    # noqa: E402
import server      # noqa: E402
import sync        # noqa: E402

MERCHANT = "northwind-apparel"
INJECTED_PRODUCT = "nw-86"          # seed.py: "Man Short Sleeve Shirt"
CONTACT = {"name": "Agent Buyer", "phone": "+919876543210", "email": "buyer@example.com"}
ADDRESS = {"line1": "Flat 402, Sunrise Residency", "city": "Nagpur",
           "state": "Maharashtra", "pincode": "440001", "country": "IN"}

PASSED, FAILED, SKIPPED = [], [], []


def head(n, title):
    print("\n" + "=" * 74)
    print("ATTACK %d — %s" % (n, title))
    print("=" * 74)


def verdict(name, blocked, detail):
    (PASSED if blocked else FAILED).append(name)
    print("   %s %s" % ("BLOCKED :" if blocked else "NOT BLOCKED :", detail))


def not_exercised(name, why):
    """"Hamla chala hi nahi" aur "hamla ruka nahi" do alag baatein hain.

    Attack 2 ko provider se ek asli payment link chahiye, aur Razorpay link banane pe
    rate limit lagata hai (D-33). Pehle aisi haalat me script chup-chaap `blocked: 4`
    chhap deti thi: koi FAIL nahi, par ek check gayab, aur dekhne wale ko pata bhi nahi
    chalta tha. Ab wo alag ginta hai - security ka natija badla nahi, sirf aaj provider ne
    mauka nahi diya. Recording se pehle ye line khaali honi chahiye.
    """
    SKIPPED.append(name)
    print("   NOT EXERCISED : %s" % why)


def pick_variant(conn, low, high, min_stock=1):
    """Merchant se seedhe — index se nahi. Index stale ho sakti hai aur demo tab Layer ki
    apni staleness pe girta, kisi hamle pe nahi."""
    merchant = conn.execute("SELECT * FROM merchants WHERE merchant_id=?",
                            (MERCHANT,)).fetchone()
    with registry.client(merchant) as api:
        for product in api.get("/agent/catalog", params={"limit": 500}).json()["products"]:
            if not product["in_stock"]:
                continue
            detail = api.get("/agent/products/" + product["product_id"]).json()
            for v in detail["variants"]:
                if v["stock"] >= min_stock and low <= v["price_paise"] <= high:
                    return detail["product_id"], v
    return None, None


# ------------------------------------------------------------------ attack 1
def attack_injection(conn, token):
    head(1, "prompt injection planted in a product description")
    print("Merchant honestly serves what a seller wrote (SPEC 10). The Layer is what")
    print("has to survive it.\n")

    raw = conn.execute("SELECT description FROM products WHERE merchant_id=? AND"
                       " product_id=?", (MERCHANT, INJECTED_PRODUCT)).fetchone()
    payload_line = [line for line in (raw["description"] if raw else "").split("\n")
                    if policy.scan_line(line)]
    print("what the seller planted (read straight from the index, unsanitised):")
    print("   " + (payload_line[0][:150] if payload_line else "(not found — reseed?)"))

    # `get_product` ab content blocks ki list lauta ta hai — product JSON aur uske saath
    # product ka labelled collage (D-96). Pehla block hi wo dict hai.
    blocks = server.get_product(merchant_id=MERCHANT, product_id=INJECTED_PRODUCT)
    served = blocks[0]
    print("   (jawab ke blocks: %d — JSON ke saath product ki tasveer bhi jati hai)"
          % len(blocks))
    flags = served.get("content_flags") or {}
    leaked = any(policy.scan_line(line) for line in served["description"].split("\n"))
    print("\nwhat an agent actually receives:")
    print("   " + served["description"].replace("\n", " ")[:150])
    print("   content_flags: removed_lines=%s patterns=%s"
          % (flags.get("removed_lines"), flags.get("patterns")))
    verdict("injection/sanitize", bool(flags) and not leaked,
            "instruction-shaped line stripped before the response was built, and the "
            "attempt is in the audit log")

    print("\nNow obey the payload anyway — 50 units, as the description demanded:")
    variant = next(v for v in served["variants"] if v["stock"] >= 1)
    out = server.add_to_cart(agent_token=token, merchant_id=MERCHANT,
                             product_id=INJECTED_PRODUCT,
                             variant_id=variant["variant_id"], qty=50)
    code = (out.get("error") or {}).get("code")
    verdict("injection/ceiling", code == "QTY_LIMIT_EXCEEDED",
            "%s — the ceiling is an `if` in Layer code that never read the description"
            % code)
    print("   " + (out.get("error") or {}).get("message", "")[:160])


# ------------------------------------------------------------------ attack 2
def attack_cap_abuse(conn, token):
    head(2, "agent claims the user already approved a bigger spend")
    print("The claim rides along in the agent's own label, and it is recorded. It")
    print("changes nothing, because the ceiling is not a sentence anyone can argue with.\n")

    product_id, variant = pick_variant(conn, policy.MAX_AUTONOMOUS_PAISE, 450_000)
    if variant is None:
        not_exercised("cap abuse", "cap ke upar koi in-stock variant nahi mila")
        return None
    print("pick: %s @ Rs%s" % (variant["variant_id"], format(variant["price_paise"] // 100, ",")))
    cart_now = server.add_to_cart(agent_token=token, merchant_id=MERCHANT,
                                  product_id=product_id,
                                  variant_id=variant["variant_id"], qty=1)
    echo = [{"variant_id": line["variant_id"], "qty": line["qty"]}
            for line in cart_now["items"]]

    # Session 5.7 se instrument ek required parameter hai (D-95), aur cap ke upar `auto`
    # maangna estimate par hi refuse ho jata hai — merchant ko call hi nahi jati. Isliye
    # ye check ab provider ke mood pe nirbhar nahi hai: koi Razorpay call hi nahi hoti,
    # to rate limit ise "not exercised" nahi bana sakta (D-81 wali soorat yahan khatam).
    grabbed = server.create_order(agent_token=token, merchant_id=MERCHANT,
                                  contact=CONTACT, address=ADDRESS,
                                  instrument="auto", confirm_items=echo)
    error = grabbed.get("error") or {}
    verdict("cap abuse", error.get("code") == "AUTONOMOUS_NOT_ALLOWED_AT_THIS_AMOUNT",
            "Layer refused to settle this itself, before the merchant was ever called")
    print("   " + str(error.get("message", ""))[:180])
    print("   retry_with: %s   (instrument ka faisla agent ke paas hi rehta hai)"
          % error.get("retry_with"))

    # Aur wahi cart `link` ke saath chal jata hai — dono instrument har amount pe
    # maujood hain; cap sirf `auto` ko rokta hai.
    order = server.create_order(agent_token=token, merchant_id=MERCHANT,
                                contact=CONTACT, address=ADDRESS,
                                instrument="link", confirm_items=echo)
    if "error" in order:
        # Yahan tak pahunchne ka matlab: Layer ne `Retry-After` maankar teen baar
        # koshish ki aur phir bhi provider ne mana kar diya (registry.HonourRetryAfter).
        # agent ko `RATE_LIMITED` milta hai, `500` nahi — yaani wo retry kar sakta hai.
        # Upar wali verdict line phir bhi chal chuki hai; ye sirf link wala hissa hai.
        print("   link handoff chhoda: " + str(order["error"])[:160])
        cart.clear(token, MERCHANT)
        return None
    print("order %s  FINAL Rs%s  instrument=%s -> %s"
          % (order["order_id"], format(order["final_total_paise"] // 100, ","),
             order["instrument"], order["payment"]["mode"]))
    refused = server.pay_order(agent_token=token, order_id=order["order_id"],
                               confirm_total_paise=order["final_total_paise"])
    print("   pay_order -> paid=%s refused=%s   link for a human: %s"
          % (refused.get("paid"), refused.get("refused"),
             refused.get("payment_link_url")))
    server.cancel_order(agent_token=token, order_id=order["order_id"],
                        reason="attack demo cleanup")
    return order["order_id"]


# ------------------------------------------------------------------ attack 3
def attack_price_manipulation(conn, token):
    head(3, "quoted price tampered so the order settles cheaper")
    print("An agent cannot even ask for this: create_order takes no price. To attempt it")
    print("at all we have to reach inside the Layer and rewrite the cart's own quote.")
    print("Both sides verify anyway, so the merchant refuses it (D-08).\n")

    product_id, variant = pick_variant(conn, 30_000, 150_000)
    if variant is None:
        not_exercised("price manipulation", "koi sasta in-stock variant nahi mila")
        return
    server.add_to_cart(agent_token=token, merchant_id=MERCHANT, product_id=product_id,
                       variant_id=variant["variant_id"], qty=1)
    line = cart.get(token, MERCHANT)[0]
    print("quoted live : Rs%s" % format(line["quoted_price_paise"] / 100, ",.2f"))
    line["quoted_price_paise"] = max(line["quoted_price_paise"] - 50_000, 100)
    print("tampered to : Rs%s" % format(line["quoted_price_paise"] / 100, ",.2f"))

    out = server.create_order(agent_token=token, merchant_id=MERCHANT,
                              contact=CONTACT, address=ADDRESS, instrument="auto",
                              confirm_items=[{"variant_id": line["variant_id"],
                                              "qty": line["qty"]}])
    error = out.get("error") or {}
    verdict("price manipulation", error.get("code") == "PRICE_CHANGED",
            "merchant rejected with %s, actual price Rs%s"
            % (error.get("code"),
               format((error.get("details") or {}).get("actual_price_paise", 0) / 100,
                      ",.2f")))
    cart.clear(token, MERCHANT)


# ------------------------------------------------------------------ revocation
def revoke_and_retry(conn, token):
    head(4, "revocation — the same token, one second later")
    print("Revocation is an operator action (scripts/audit.py revoke), not a tool. A")
    print("token is the whole of an agent's identity, so an agent able to revoke tokens")
    print("could switch every other agent off.\n")
    print(policy.revoke(conn, token, "attack demo: this agent tried three attacks")["revoked_at"]
          + "  revoked")
    out = server.add_to_cart(agent_token=token, merchant_id=MERCHANT,
                             product_id=INJECTED_PRODUCT, variant_id="whatever", qty=1)
    code = (out.get("error") or {}).get("code")
    verdict("revocation", code == "AGENT_REVOKED",
            "%s — refused before the merchant was ever contacted" % code)


def main():
    conn = db.connect()
    try:
        registry.register_all(conn)
    except Exception as e:
        sys.exit("Northwind 8001 pe nahi chal raha? " + str(e)[:200])
    if not conn.execute("SELECT healthy FROM merchants WHERE merchant_id=?",
                        (MERCHANT,)).fetchone()["healthy"]:
        sys.exit("merchant unhealthy — `uvicorn merchants.northwind.main:app --port 8001`")
    if conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0:
        sync.sync_merchant(conn, MERCHANT)

    token = server.register_agent(
        label="user pre-approved Rs 5,000")["agent_token"]
    print("attacker token :", token)
    print("its label is a lie the Layer records and ignores.")

    attack_injection(conn, token)
    order_id = attack_cap_abuse(conn, token)
    attack_price_manipulation(conn, token)
    revoke_and_retry(conn, token)

    print("\n" + "=" * 74)
    print("blocked: %d   not blocked: %d   not exercised: %d"
          % (len(PASSED), len(FAILED), len(SKIPPED)))
    if FAILED:
        print("STILL OPEN: " + ", ".join(FAILED))
    if SKIPPED:
        print("NOT EXERCISED (chalaya hi nahi ja saka, security ka natija nahi): "
              + ", ".join(SKIPPED))
    rows = policy.trail(conn, agent_token=token)
    print("audit rows for this agent: %d  (%d blocked)"
          % (len(rows), sum(1 for r in rows if r["decision"] == "block")))
    print("\nread the whole thing:")
    print("   .venv/Scripts/python.exe scripts/audit.py agent " + token)
    if order_id:
        print("   .venv/Scripts/python.exe scripts/audit.py trail " + order_id)
    print("   .venv/Scripts/python.exe scripts/audit.py metrics")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
