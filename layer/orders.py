"""Transaction: cart se order, order se payment, aur cancel.

Yahan Layer ka asli kaam hai — har paise wale action ke aage ek gate lagana, aur wo
gate ASLI number pe lagana. Teen baatein jo poori design tay karti hain:

1. **Order pehle UNPAID banta hai** (D-07). Isi wajah se Layer shipping aur discount ke
   baad wala sach dekh paati hai, tab cap lagati hai. Andaaze pe cap lagate to Rs 1,980
   ka cart Rs 2,030 ka charge ban jata aur cap chup-chaap toot jata.

2. **Price dono taraf verify hoti hai** (D-08). Cart quote yaad rakhta hai, wahi quote
   merchant ko `expected_price_paise` ban kar wapas jata hai, aur merchant apne live
   price se milata hai. Ek taraf ka check us akele raaste pe single point of failure hai
   jahan galti me paisa jata hai.

3. **Instrument agent chunta hai, par cap phir bhi Layer ka hai** (D-95). `create_order`
   me `instrument` ek **required parameter** hai jiska koi default nahi — `"auto"` ya
   `"link"`. COD ka naam us enum me hai hi nahi (D-05), to use refuse karne ki nautanki
   bhi nahi karni padti. Pehle instrument amount se derive hota tha, yaani agent ne wo
   faisla kabhi liya hi nahi — usne bas paisa de diya.

4. **Paisa hilne se pehle echo-back** (D-93, D-97). `create_order` ko `confirm_items`
   chahiye jo cart se bilkul mile, aur `pay_order` ko wahi `final_total_paise` jo Layer
   ne record kiya tha. Layer ye jaanch nahi sakti ki insaan se poochha gaya ya nahi —
   *"user ne pehle se ijazat di thi"* ek **dawa** hai, aur wahi vaakya humara apna
   sanitizer `forged_consent` se pakadta hai. Jo jaanchi ja sakti hai wo ye hai ki agent
   ne kya kharida aur kitne ka, dono naam lekar bola.

## Instrument aur estimate — cap kis number pe lagta hai

Razorpay ka payment link headlessly pay nahi hota (link ka order tabhi banta hai jab
koi use kholta hai — session 4 me naapa gaya). Isliye `auto` = merchant ka `checkout`
mode, `link` = `payment_link`. Par instrument order banane se PEHLE chunna padta hai,
aur asli total order banne ke BAAD milta hai:

    estimate = items_total + manifest ka shipping      (coupon ko 0 maankar)
    estimate >= asli total  hamesha,  kyunki discount sirf ghatata hai

Isliye `auto` ko estimate pe hi mana kar dena kabhi jhootha refusal nahi ho sakta — jo
estimate pe cap ke upar hai wo asli total pe bhi upar hi hoga — aur us soorat me merchant
ko chhua hi nahi jata, koi order banta hi nahi. Ulti soorat mumkin hai: estimate neeche
tha aur merchant ka asli shipping apne declare kiye flat rate se zyada nikla. Tab order
cancel hota hai (stock wapas, paisa hila hi nahi) aur agent ko `link` ke saath dobara
bulane ko kaha jata hai — Layer khud instrument nahi badalti, warna wahi cheez wapas aa
jati jise D-95 ne hataya tha. Wahi ARCHITECTURE 6.3 wala "Rs 1,980 ka cart Rs 2,030
nikla" case hai, aur wo chup-chaap paas nahi hota.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone

import httpx

import cart
import db
import policy
import registry

RZP_AJAX = "https://api.razorpay.com/v1/payments/create/ajax"
AUTONOMOUS_VPA = "success@razorpay"
PAY_POLL_SECONDS = 2            # do sawaalon ke beech ka aaram, jab merchant kuch na kahe
PAY_POLL_BUDGET_SECONDS = 15    # is se zyada ek tool call ko rokna nahi hai


# Har error code ka aage ka raasta, EK jagah. Ye table isliye hai ki "har refusal raasta
# bhi de" ek yaad rakhne wali baat thi, aur yaad rakhne wali baat hamesha kisi ek call
# site pe chhoot jati hai — wahi shakal jo per-call-site sanitisation ki thi (D-74).
# Ab raasta code se aata hai, isliye ek naya call site use bhool hi nahi sakta.
#
# Ye khaas kar un do errors ke liye zaroori tha jinpe kamzor model **loop** me chala jata
# hai: `INVALID_COUPON` (koi coupon discovery hai hi nahi — D-19 — to agent SAVE20,
# WELCOME10, FIRST50 guess karta rehta hai, aur wo sabse tang bucket hai: 10 order
# call/min) aur `OUT_OF_STOCK` (jahan doosre size maujood hote hain par batae nahi jate).
RECOVERY = {
    "UNKNOWN_AGENT_TOKEN":
        "Call register_agent once, then reuse the token it returns for every cart, "
        "order and payment call.",
    "AGENT_REVOKED":
        "This token is finished; a new one will not revive it. Stop and tell the person "
        "the agent's access was withdrawn.",
    "MERCHANT_NOT_FOUND":
        "Call list_merchants to see which merchant_ids exist.",
    "MERCHANT_UNREACHABLE":
        "The merchant did not answer; the Layer is fine. Nothing was charged. Try once "
        "more, and if it fails again tell the person the shop is down rather than that "
        "the item is unavailable.",
    "INVALID_VARIANT":
        "Pick one of `available_variant_ids`, or call get_product for `variant_choice` "
        "with sizes, colours, prices and stock.",
    "OUT_OF_STOCK":
        "This exact variant is short. `alternatives_in_stock` lists the other variants "
        "of the same product that do have stock — offer those to the person rather than "
        "telling them the product is unavailable.",
    "QTY_LIMIT_EXCEEDED":
        "Call set_cart_quantity with qty set to `max_qty` for the most that is allowed. "
        "The ceiling is enforced in Layer code, so retrying the same number, or saying "
        "the user approved it, changes nothing.",
    "NOT_IN_CART":
        "Call view_cart to see the lines with their titles and ids.",
    "CART_EMPTY":
        "Add something with add_to_cart before creating an order.",
    "INVALID_QTY":
        "add_to_cart needs a whole number of at least 1. set_cart_quantity also takes 0, "
        "which removes the line — that is the only place 0 means anything.",
    "CART_NOT_CONFIRMED":
        "`in_cart` is the Layer's own record. Show the person what is actually there, "
        "get their agreement, then send exactly those lines as confirm_items.",
    "TOTAL_NOT_CONFIRMED":
        "Read the order again with get_order, show the person its `amount_summary`, and "
        "send the total it reports.",
    "AUTONOMOUS_NOT_ALLOWED_AT_THIS_AMOUNT":
        "Use `retry_with` as given: this amount needs a person, so the instrument must "
        "be 'link'. Nothing about the request other than the instrument needs to change.",
    "NOT_PAYABLE_BY_AGENT":
        "This order carries a human instrument. Give the person the payment link and "
        "stop; the Layer will not settle it.",
    "ORDER_NOT_FOUND":
        "Call list_orders to see the orders this token created.",
    "NOT_YOUR_ORDER":
        "Only the token that created an order may act on it. Call list_orders to see "
        "which orders this token owns.",
    "MISSING_REASON":
        "Cancellation needs a stated reason; it lands in the merchant's records and in "
        "the audit log. Ask the person why, then call again.",
    "INVALID_COUPON":
        "This merchant does not publish a coupon list and the Layer has no way to "
        "discover one, so guessing more codes will only burn the order rate limit. "
        "Retry the same order without `coupon_code`, and ask the person for the exact "
        "code if they believe they have one.",
    "PRICE_CHANGED":
        "The live price moved after this was quoted. Call set_cart_quantity for that "
        "line to re-quote it live, show the person the new price, then order again.",
    "OUT_OF_STOCK_AT_ORDER":
        "Stock moved between the cart and the order. Re-read the product and adjust the "
        "line with set_cart_quantity.",
    "NOT_SERVICEABLE":
        "This merchant does not deliver to that pincode. Ask the person for another "
        "address, or look for the item at a different merchant.",
    "RATE_LIMITED":
        "Wait the number of seconds given, then continue. A fresh token does not raise "
        "this.",
    "INVALID_CONTACT":
        "Ask the person for the field named in `details.field`, in the shape given by "
        "`details.expected`, and call create_order again. Nothing was sent to the "
        "merchant and nothing is reserved.",
}

DEFAULT_RECOVERY = ("Nothing was charged. Read `message` and `details`, tell the person "
                    "plainly what happened, and do not retry the identical call.")


def err(code, message, next_step=None, **details):
    """Refusal hamesha batati hai ki kya galat tha **aur kya chalta** (ARCHITECTURE 8).

    Ek bare "no" pe kamzor model andha retry karta hai; ek samjhaya hua "no" pe wo sudhar
    leta hai. Isliye `message` aur `next_step` dono yahan **guarantee** hain, salah nahi:
    agent ka loop aksar seedha `error.message` render karta hai, aur ek error jisme wo
    field hi na ho wo user ko khaali "kuch error aaya" bhej deta hai.
    """
    return {"error": {"code": code, "message": message,
                      "next_step": next_step or RECOVERY.get(code, DEFAULT_RECOVERY),
                      **details}}


def require_agent(conn, agent_token):
    row = conn.execute("SELECT * FROM agents WHERE token=?", (agent_token or "",)).fetchone()
    if row is None:
        return err("UNKNOWN_AGENT_TOKEN",
                   "This token was not issued by the Layer. Call register_agent first.")
    if row["revoked_at"]:
        return err("AGENT_REVOKED",
                   "This token was revoked at " + row["revoked_at"] + ". No money "
                   "operation will be accepted with it.")
    return None


def alternatives_in_stock(product, exclude_variant_id):
    """Usi product ke wo variants jinme sach me stock hai.

    `OUT_OF_STOCK` pehle sirf ye batata tha ki *is* variant ka stock 0 hai. Naapa hua
    nateeja: size 9 out of stock tha jabki 7, 8 aur 10 usi waqt maujood the, aur error
    unme se ek ka bhi naam nahi leta tha — to agent user se keh deta *"ye out of stock
    hai"* aur baat wahin khatam. Theek bagal wala `INVALID_VARIANT` shuru se
    `available_variant_ids` deta hai; wahi shakal yahan honi chahiye thi.
    """
    return [{"variant_id": v["variant_id"],
             "options": v.get("options", {}),
             "price_paise": v["price_paise"],
             "stock": v["stock"]}
            for v in product.get("variants", [])
            if v["variant_id"] != exclude_variant_id and v.get("stock", 0) > 0]


def merchant_row(conn, merchant_id):
    return conn.execute("SELECT * FROM merchants WHERE merchant_id=?",
                        (merchant_id,)).fetchone()


def merchant_json(conn, response, merchant_id, source):
    """Merchant ki har **kaamyaab** response yahin se guzarti hai, aur yahin saaf hoti hai.

    Pehle har call site apne aap `policy.clean_payload` bulata tha, aur theek wahi hua jo
    aisi jagah hamesha hota hai: ek site bhool gayi. `pay_order` merchant ka `payment`
    aur `timeline` seedha agent tak laut a raha tha - aur `timeline` ke har entry me ek
    free-form `note` hota hai (SPEC 7), yaani ek poora rasta jise koi dekhta hi nahi.
    Ab safai ek helper me hai aur `test_no_merchant_json_reaches_an_agent_unsanitised`
    source padhkar ye check karta hai ki koi naya `.json()` iske bahar na likha jaye.
    """
    return policy.clean_payload(conn, merchant_id, response.json(), source)


def merchant_error(conn, response, merchant_id):
    """Merchant ka spec error envelope agent tak jaise ka taisa pahunchta hai. Layer use
    dobara likhne ki koshish nahi karti — `details` me wahi cheez hoti hai jisse agent
    recover karta hai (SPEC 11), aur use apne shabdon me badalna sirf usse chheenna hai.

    "Jaise ka taisa" ka matlab "bina safai ke" nahi hai. `message` bhi merchant ka likha
    hua text hai, yaani wahi channel jispe description wala hamla hota hai — sirf chhota
    aur aisi jagah jahan koi dekhta nahi. Code, status aur `details` chhue bina jate
    hain; unhi pe agent recover karta hai.
    """
    try:
        envelope = merchant_json(conn, response, merchant_id, "error")
    except ValueError:
        envelope = policy.clean_payload(
            conn, merchant_id,
            {"error": {"code": "INTERNAL_ERROR", "message": response.text[:200]}}, "error")
    body = envelope.get("error", {})
    if "content_flags" in envelope:
        body = {**body, "content_flags": envelope["content_flags"]}
    # Merchant ka code aur `details` chhue bina jate hain — un par agent recover karta
    # hai. Jo merchant nahi de sakta wo hai **humare surface pe aage ka raasta**: ek
    # merchant ko pata hi nahi ki agent ke paas `set_cart_quantity` naam ki koi cheez
    # hai. `INVALID_COUPON` isi ka sabse mehnga udaharan tha: bare "no", aur agent
    # SAVE20/WELCOME10 guess karta hua order bucket kha jata tha.
    return {"error": {"message": "The merchant refused this request.",
                      "next_step": RECOVERY.get(body.get("code"), DEFAULT_RECOVERY),
                      **body, "merchant_id": merchant_id,
                      "http_status": response.status_code}}


# ------------------------------------------------------------------ cart
def view_cart(conn, agent_token, merchant_id):
    """Cart padhne ka BAHARI darwaza — token yahan jaancha jata hai.

    `cart_view` andar se bhi bulaya jata hai (add/set/remove ke baad), jahan token pehle
    hi jaancha ja chuka hota hai. Wahi function seedha tool pe bhi laga hua tha, aur
    isliye `view_cart` **ikloti cart call thi jisme token check tha hi nahi**: ek galat
    ya gadha hua token khaali cart ke saath "kaamyaab" lauta ta tha, jabki usi token pe
    `add_to_cart` `UNKNOWN_AGENT_TOKEN` deta tha. Agent ke liye iska matlab hota hai
    apni hi baat badalna — pehle "cart khali hai", phir "token galat hai" — aur wahi
    cheez use paise ke liye bharosemand hone se rokti hai.
    """
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    return cart_view(conn, agent_token, merchant_id)


def cart_view(conn, agent_token, merchant_id):
    lines = cart.get(agent_token, merchant_id)
    elsewhere = cart.other_carts(agent_token, merchant_id)
    items_total = cart.items_total_paise(lines)
    row = merchant_row(conn, merchant_id)
    estimate = items_total + shipping_estimate(row, items_total) if lines else 0
    decision = policy.decide_payment(estimate) if lines else None
    shipping = estimate - items_total
    return {
        "merchant_id": merchant_id, "items": lines, "count": len(lines),
        # Khaali cart do bilkul alag baatein ho sakti hain, aur agent ke paas unme farq
        # karne ka koi raasta nahi tha: usne kuch daala hi nahi, ya jo daala tha wo 30
        # minute me expire ho gaya. Dono par jawab byte-for-byte ek jaisa tha, to agent
        # grahak se kehta *"aapki basket khaali hai"* — jabki sach ye tha ki *"basket ka
        # waqt nikal gaya, dobara bana dete hain"*.
        "expired": bool(not lines and cart.recently_expired(agent_token, merchant_id)),
        "items_total_paise": items_total,
        "estimated_shipping_paise": shipping,
        "estimated_total_paise": estimate,
        # Cart pe bhi ready lines — Q-30. Agent ne inhe khud jodkar ek ASLI run me
        # Rs 2,049 bola tha jab Layer ka apna number Rs 2,000 tha.
        "amount_summary": (money_lines(lines, items_total, shipping, 0, estimate,
                                       label="ESTIMATED") if lines else []),
        "amount_summary_note": (
            "Show these lines to the person as they are. " + DO_NOT_ADD_SHIPPING +
            " Shipping here is the Layer's estimate from the merchant's manifest; the "
            "merchant's own figure arrives with the order and is the one that is charged."
            if lines else
            "This cart had items and they timed out. Nothing was lost and nothing was "
            "charged — add them again. Tell the person it expired rather than that "
            "their basket was empty."
            if cart.recently_expired(agent_token, merchant_id) else
            "Nothing in the cart yet."),
        # Q-33: ye CART ki umar hai, order ki nahi. Ek asli run me agent ne is number ko
        # "order 30 minute me expire hoga" bolkar user ko de diya tha. Order ki apni
        # expiry `payment.expires_at` hoti hai aur wo alag cheez hai (SPEC 9).
        # Naam me "cart" isliye hai ki purana naam `expires_in_seconds` tha aur ek asli
        # run me agent ne use uthakar user ko bola *"order 30 minutes mein expire hoga"*.
        # Do field ek hi value dene se wo ambiguity bachi rehti, aur ambiguity hi bug thi.
        # Doosri dukaanon ke khule hue cart. Ek merchant ke saath ye hamesha khaali hai;
        # do ke saath ye wo ek cheez hai jo agent ko apne aap yaad nahi rehti.
        "other_carts": elsewhere,
        "other_carts_note": (
            "A cart belongs to ONE merchant, and so does an order — one order can never "
            "span two shops, because its payment, its shipping and its cancellation "
            "would each have to split in half. You also have items waiting at: " +
            ", ".join("%s (%d)" % (c["merchant_id"], c["count"]) for c in elsewhere) +
            ". That is fine and nothing is lost — it simply becomes a second order. "
            "Tell the person both baskets and let them decide, and never present one "
            "cart's total as everything they are buying."
            if elsewhere else
            "This token has nothing waiting at any other merchant."),
        "cart_expires_in_seconds": cart.expires_in_seconds(agent_token, merchant_id),
        "expiry_note": ("This is how long the CART lives, not an order. An order gets "
                        "its own deadline in payment.expires_at once it exists, and the "
                        "two are unrelated — do not quote one as the other."),
        "quoted_price_note": ("Cart prices are the quotes captured when each item was "
                              "added. They are re-verified live by the merchant at order "
                              "creation, which rejects the order if anything moved."),
        "payment_preview": (None if not lines else {
            "instrument_auto_available": decision["autonomous"],
            "note": "Estimate only — the real decision is made on the merchant's final "
                    "total, after shipping and any discount. instrument='link' works at "
                    "any amount; instrument='auto' only below the ceiling."}),
        # D-105: jo user ne nahi kaha, wo agent ka default nahi banta. *"Sirf add kar do"*
        # pe cart pe rukna sahi bartav hai, adhoora kaam nahi.
        "next_step": ("Add something before ordering." if not lines else
                      "A cart is not an order and nothing has been charged. If the person "
                      "only asked you to add this, stop here and tell them what is in it. "
                      "Going further needs two things they have not said: which payment "
                      "instrument ('auto' — the Layer settles it below the ceiling with "
                      "no human in the path, or 'link' — a person opens a link and pays), "
                      "and their agreement to what is being bought. create_order requires "
                      "both, and has no default for the instrument."),
    }


def add_to_cart(conn, agent_token, merchant_id, product_id, variant_id, qty=1):
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    if qty < 1:
        return err("INVALID_QTY", "qty must be at least 1.")
    row = merchant_row(conn, merchant_id)
    if row is None:
        return err("MERCHANT_NOT_FOUND", "No merchant registered as " + merchant_id + ".")

    # Ceiling cart ko CHHUNE SE PEHLE lagta hai, aur wo hamesha "cart me jitna pehle se
    # hai + jitna ab maanga" pe lagta hai.
    #
    # Pehle ye do hisson me tha: pehle akele `qty` pe check, phir cart me daalne ke BAAD
    # jodkar dobara check, aur zyada nikalne pe poori line `cart.remove()` se uda dena.
    # Wo refusal apne hi buyer ko saza deti thi - cart me 3 jaayaz units pade hon aur
    # agent 3 aur maange, to ceiling to sahi rukti thi par rokne ki keemat me **pehle
    # wale teen bhi mit jate the**. Ceiling ka kaam zyada lene se rokna hai, jo pehle se
    # theek tha use chheenna nahi.
    already = next((line["qty"] for line in cart.get(agent_token, merchant_id)
                    if line["variant_id"] == variant_id), 0)
    verdict = policy.check_qty(qty + already,
                               db.jl(row["policies"], {}).get("max_qty_per_variant"))
    if not verdict["allowed"]:
        return err("QTY_LIMIT_EXCEEDED", verdict["reason"], max_qty=verdict["max_qty"],
                   requested_qty=qty, already_in_cart=already)

    # LIVE, index se nahi (D-10). Cart me jo price baithega wahi baad me merchant ko
    # dikhaya jayega - use stale index se bharna matlab har order ko PRICE_CHANGED pe
    # marwana.
    try:
        with registry.client(row) as api:
            response = api.get("/agent/products/" + product_id)
    except Exception as e:
        return err("MERCHANT_UNREACHABLE", str(e)[:200], merchant_id=merchant_id)
    if response.status_code != 200:
        return merchant_error(conn, response, merchant_id)

    # Title cart line me baithta hai aur wahin se order line, receipt aur audit tak
    # jata hai — yaani ye bhi ek exit hai, aur har exit pe safai ek hi jagah se hoti hai.
    product = merchant_json(conn, response, merchant_id, product_id)
    variant = next((v for v in product["variants"] if v["variant_id"] == variant_id), None)
    if variant is None:
        return err("INVALID_VARIANT",
                   variant_id + " is not a variant of " + product_id + ".",
                   available_variant_ids=[v["variant_id"] for v in product["variants"]])

    if variant["stock"] < qty + already:
        return err("OUT_OF_STOCK",
                   "Only %d left of %s." % (variant["stock"], variant_id),
                   variant_id=variant_id, available_qty=variant["stock"],
                   already_in_cart=already, requested_qty=qty,
                   alternatives_in_stock=alternatives_in_stock(product, variant_id))

    options = " / ".join(variant.get("options", {}).values())
    cart.add(agent_token, merchant_id, {
        "variant_id": variant_id, "product_id": product_id,
        "title": product["title"] + (" - " + options if options else ""),
        "qty": qty, "quoted_price_paise": variant["price_paise"],
        "quoted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    return cart_view(conn, agent_token, merchant_id)


def set_cart_quantity(conn, agent_token, merchant_id, variant_id, qty):
    """Ek line ki qty **exact** set karo. `qty=0` line hata deta hai.

    **Ye gap ek asli baat-cheet se mila.** User ne kaha *"1 chahiye thi sirf"* aur agent
    ne jawab diya *"cart se item remove karne ka option nahi hai"* — do galtiyan ek saath:
    `remove_from_cart` maujood tha (wo humari guide ki kami thi), **aur** qty ghatane ka
    koi seedha raasta sach me tha hi nahi.

    Wajah `add_to_cart` ke semantics me hai aur wo jaan-boojhkar aisa hai: wahi variant
    dobara add karne pe qty **judti** hai (2 + 1 = 3), replace nahi hoti — warna ek retry
    chup-chaap qty ghata deta. Par uska matlab ye tha ki 3 se 1 pe aane ke liye pehle
    poori line hatao, phir dobara add karo. Do call, aur beech me cart galat.

    `set` aur `add` alag rehne chahiye, ek parameter ke do matlab nahi hone chahiye.

    Ceiling yahan **absolute** qty pe lagta hai, `already + qty` pe nahi — kyunki ye set
    hai, jod nahi. Aur price yahan bhi **live** dobara padha jata hai, isliye ye
    `PRICE_CHANGED` ke baad dobara quote lene ka bhi seedha raasta hai.
    """
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    if not isinstance(qty, int) or qty < 0:
        return err("INVALID_QTY", "qty must be 0 or more. Use 0 to remove the line.")
    row = merchant_row(conn, merchant_id)
    if row is None:
        return err("MERCHANT_NOT_FOUND", "No merchant registered as " + merchant_id + ".")

    line = next((l for l in cart.get(agent_token, merchant_id)
                 if l["variant_id"] == variant_id), None)
    if line is None:
        return err("NOT_IN_CART",
                   variant_id + " is not in this cart, so there is no quantity to "
                   "change. Use add_to_cart to put it there — that call needs the "
                   "product_id as well, which this one does not.",
                   in_cart=[l["variant_id"] for l in cart.get(agent_token, merchant_id)])

    if qty == 0:
        cart.remove(agent_token, merchant_id, variant_id)
        return cart_view(conn, agent_token, merchant_id)

    verdict = policy.check_qty(qty, db.jl(row["policies"], {}).get("max_qty_per_variant"))
    if not verdict["allowed"]:
        return err("QTY_LIMIT_EXCEEDED", verdict["reason"], max_qty=verdict["max_qty"],
                   requested_qty=qty, currently_in_cart=line["qty"])

    try:
        with registry.client(row) as api:
            response = api.get("/agent/products/" + line["product_id"])
    except Exception as e:
        return err("MERCHANT_UNREACHABLE", str(e)[:200], merchant_id=merchant_id)
    if response.status_code != 200:
        return merchant_error(conn, response, merchant_id)

    product = merchant_json(conn, response, merchant_id, line["product_id"])
    variant = next((v for v in product["variants"] if v["variant_id"] == variant_id), None)
    if variant is None:
        return err("INVALID_VARIANT",
                   variant_id + " no longer exists at this merchant.",
                   available_variant_ids=[v["variant_id"] for v in product["variants"]])
    if variant["stock"] < qty:
        return err("OUT_OF_STOCK", "Only %d left of %s." % (variant["stock"], variant_id),
                   variant_id=variant_id, available_qty=variant["stock"],
                   currently_in_cart=line["qty"], requested_qty=qty,
                   alternatives_in_stock=alternatives_in_stock(product, variant_id))

    # `cart.add` jodta hai, isliye pehle line hata kar poori nayi rakhi jaati hai. Quote
    # bhi naya hai — agar price hila hai to agent ko yahi par pata chal jayega.
    cart.remove(agent_token, merchant_id, variant_id)
    options = " / ".join(variant.get("options", {}).values())
    cart.add(agent_token, merchant_id, {
        "variant_id": variant_id, "product_id": line["product_id"],
        "title": product["title"] + (" - " + options if options else ""),
        "qty": qty, "quoted_price_paise": variant["price_paise"],
        "quoted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    view = cart_view(conn, agent_token, merchant_id)
    view["quantity_changed"] = {
        "variant_id": variant_id, "from": line["qty"], "to": qty,
        "price_was_paise": line["quoted_price_paise"],
        "price_now_paise": variant["price_paise"],
        "note": ("The line was re-quoted live while changing it."
                 + (" The price moved since it was added — show the person."
                    if variant["price_paise"] != line["quoted_price_paise"] else "")),
    }
    return view


def remove_from_cart(conn, agent_token, merchant_id, variant_id):
    """Line hatao — aur agar wo line thi hi nahi, to **saaf mana karo**.

    Pehle ye chup-chaap kaamyaab hota tha: `cart.remove()` ek na-maujood variant par kuch
    nahi karta, aur jawab me wahi cart chala jata tha, bilkul ek kaamyaab removal jaisa
    dikhta hua. Poore tool surface me ye akela silent failure tha, aur uska nuksaan
    do kadam baad dikhta tha: agent user se kehta *"hata diya"*, cheez cart me padi
    rehti, phir `confirm_items` cart se mel nahi khata aur `CART_NOT_CONFIRMED` aata —
    ek uljhan bhari doosri galti jiski asli wajah teen call peeche thi.

    Ek product ke aath-aath milte-julte variant id hote hain (`nw-87-s-maroon`,
    `nw-87-m-maroon`, ...), yaani agent ka galat id chunna anhoni nahi, **aam** hai.
    `set_cart_quantity` ye galti pehle se pakadta hai; ye uska sagaa bhai hai aur isme
    wo check rah gaya tha.
    """
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    in_cart = [l["variant_id"] for l in cart.get(agent_token, merchant_id)]
    if variant_id not in in_cart:
        return err("NOT_IN_CART",
                   variant_id + " is not in this cart, so nothing was removed. The cart "
                   "is unchanged.",
                   in_cart=in_cart,
                   next_step=("Pick the id you actually meant from `in_cart` and call "
                              "again, or call view_cart to see the lines with their "
                              "titles. If the cart is already empty there is nothing "
                              "to remove."))
    cart.remove(agent_token, merchant_id, variant_id)
    return cart_view(conn, agent_token, merchant_id)


# ------------------------------------------------------------------ order creation
def shipping_estimate(merchant, items_total_paise):
    """Manifest se andaaza. Layer ye number kabhi assert nahi karti — shipping merchant
    ka authority hai (D-18). Ye sirf instrument chunne ke kaam aata hai."""
    shipping = db.jl(merchant["shipping"], {}) if merchant else {}
    free_above = shipping.get("free_above_paise")
    if free_above is not None and items_total_paise >= free_above:
        return 0
    return shipping.get("flat_paise") or 0


def idempotency_key(agent_token, merchant_id, lines, address, suffix="",
                    contact=None, coupon_code=None):
    """Key body se BANTI hai, random nahi — aur **poore** body se.

    Random key ka matlab: network blip pe agent dobara bhejta hai aur merchant do alag
    order bana deta hai — theek wahi cheez jise `Idempotency-Key` rokne ke liye hai
    (D-16). Body se derive karne pe wahi cart dobara bhejne pe wahi key banti hai, aur
    merchant original order lauta deta hai.

    **`contact` aur `coupon_code` pehle isme the hi nahi, aur wo ek asli bug tha.**
    Merchant SPEC 6 ke hisaab se **poore body** ka hisaab rakhta hai: wahi key + alag
    body = `409 IDEMPOTENCY_CONFLICT`. Yaani key jitni cheezein chhodegi, wo utne raaste
    band kar degi. Naapa gaya: wahi cart, wahi address, sirf **phone badal do** — aur
    order permanently mana ho jata tha, bina kisi nikalne ke raaste ke. Wahi ek galat
    number theek karne par, aur wahi coupon lagane par.

    Aur error khud aur ulajhata tha: uska `next_step` kehta tha *"do not retry the
    identical call"* — jabki call identical thi hi nahi, wahi to poori shikayat thi.

    Niyam ek line ka hai: **key me wo sab kuch hona chahiye jo merchant ke body me
    jata hai.** Kam rakhoge to jayaz sudhaar block honge; zyada rakh hi nahi sakte,
    kyunki body me itna hi hai.
    """
    material = json.dumps([agent_token, merchant_id, suffix,
                           sorted((line["variant_id"], line["qty"],
                                   line["quoted_price_paise"]) for line in lines),
                           address, contact, coupon_code], sort_keys=True)
    return "acp_" + hashlib.sha256(material.encode()).hexdigest()[:40]


def post_order(row, lines, contact, address, coupon_code, mode, key):
    body = {
        "items": [{"variant_id": line["variant_id"], "qty": line["qty"],
                   "expected_price_paise": line["quoted_price_paise"]} for line in lines],
        "expected_items_total_paise": cart.items_total_paise(lines),
        "contact": contact, "address": address, "payment_mode": mode,
    }
    if coupon_code:
        body["coupon_code"] = coupon_code
    with registry.client(row) as api:
        return api.post("/agent/orders", json=body, headers={"Idempotency-Key": key})


def confirm_items_mismatch(lines, confirm_items):
    """Agent ne jo kharidne ka daawa kiya, wahi cart me hai ya nahi (D-93 ka echo-back).

    Ye "poochhna" nahi hai — poochhna client ka kaam hai aur wo enforce nahi hota (D-97).
    Ye wo hissa hai jo **enforce hota hai**: agent ko wahi variant aur wahi qty dobara
    likh kar deni padti hai jo wo kharid raha hai, aur wo Layer ke apne record se milni
    chahiye. Do cheezein isse milti hain:

      * Agent ko wo item **naam lekar** batana padta hai, yaani chupchap ek variant chun
        kar aage nikal jana ab ek call hi nahi ban sakti (D-98, D-105)
      * Ek purani padi hui cart line — pichhle turn ki, ya kisi aur cheez ki — chup-chaap
        order me nahi ghus sakti. Aaj wo bina kisi awaaz ke ghus jati hai

    D-08 wali soch hi hai (dono taraf verify), consent pe lagayi hui.
    """
    if not isinstance(confirm_items, list) or not confirm_items:
        return {"why": "confirm_items must be a non-empty list of "
                       "{variant_id, qty} matching the cart exactly."}
    try:
        claimed = sorted((str(i["variant_id"]), int(i["qty"])) for i in confirm_items)
    except (TypeError, KeyError, ValueError):
        return {"why": "every entry in confirm_items needs a variant_id and an integer qty."}
    actual = sorted((line["variant_id"], line["qty"]) for line in lines)
    if claimed != actual:
        return {"why": "confirm_items does not match the cart.",
                "in_cart": [{"variant_id": v, "qty": q} for v, q in actual],
                "you_confirmed": [{"variant_id": v, "qty": q} for v, q in claimed]}
    return None


# E.164: `+`, phir ek non-zero country digit, phir kul 8 se 15 ank. Ye ITU ka apna aakar
# hai, koi humara andaaza nahi.
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
SIX_DIGITS = re.compile(r"^\d{6}$")


def contact_problem(contact, address):
    """Order jaane se PEHLE: kya ye order deliver ho bhi sakta hai?

    Ye check yahan isliye hai, aur teeno wajahen alag hain:

    **Pehli — surface ne mechanism se zyada daawa kar rakha tha.** `Contact.phone` ka
    schema agent se kehta hai *"MANDATORY — a merchant rejects an order without it"*, aur
    `Address.country` kehta hai *"'IN' for v1"*. Naapa gaya: Northwind ne `phone: "98765"`
    liya, Voltline ne `phone: "1"` + `country: "ZZ"` liya — dono ne order bana diya, aur
    cap ke neeche wo asli paise se settle bhi ho jata. Yaani agent ko bataya gaya tha ki
    jaanch ho chuki hai, aur hui nahi thi. Ya to daawa hatana tha ya jaanch lagani thi;
    yahan jaanch lagana sasta bhi hai aur sach bhi.

    **Doosri — ek jagah jaanchna hi is poore project ki dalil hai.** Merchant sirf
    *maujoodgi* dekhta hai (SPEC 6 use isse zyada karne ko nahi kehti thi). Ek hi jagah
    lag jane se har merchant ko apni-apni nahi likhni padti — theek wahi baat jo N x M ko
    N + M banati hai (ARCH 7.3).

    **Teesri — `pincode` par do merchants do alag jawab dete the.** Northwind malformed
    pincode par `400 MISSING_FIELD` deta hai, Voltline `409 NOT_SERVICEABLE` — yaani ek
    kehta hai *"request theek karo"* aur doosra *"doosri dukaan dekho"*. SPEC v1.8 ye farq
    `get_product` ke liye tay kar chuki thi; order ke raaste par wo likha hi nahi tha.
    v1.9 use wahan bhi le jati hai, aur tab tak Layer ka apna check dono ko ek jaisa
    banata hai.

    Email ka koi check jaan-boojhkar nahi hai: uska schema koi shakal ka daawa karta hi
    nahi (`"Optional."`), aur jis field par kuch kaha hi na gaya ho uspe jhooth bhi nahi
    bola ja sakta. Jaanch daawe ke barabar rakhi gayi hai, us se badi nahi.
    """
    contact = contact or {}
    address = address or {}
    problems = []

    if not str(contact.get("name") or "").strip():
        problems.append({"field": "contact.name", "expected": "the person's full name",
                         "why": "contact.name is required."})
    phone = str(contact.get("phone") or "").strip()
    if not E164.match(phone):
        problems.append({"field": "contact.phone",
                         "expected": "E.164, like +919876543210", "received": phone,
                         "why": "contact.phone is not a phone number a courier can call: "
                                + (repr(phone) if phone else "it is empty") + "."})
    pincode = str(address.get("pincode") or "").strip()
    if not SIX_DIGITS.match(pincode):
        problems.append({"field": "address.pincode", "expected": "exactly 6 digits",
                         "received": pincode,
                         "why": "address.pincode is not a pincode: "
                                + (repr(pincode) if pincode else "it is empty") + "."})
    country = str(address.get("country") or "IN").strip().upper()
    if country != "IN":
        problems.append({"field": "address.country", "expected": "IN",
                         "received": country,
                         "why": "address.country is " + repr(country) +
                                "; this Layer serves India only in v1."})
    if not problems:
        return None

    # **Saari shikayatein ek saath.** Pehle ye ek-ek karke aati thin, aur uski keemat
    # naapi gayi: ek galat phone + galat pincode + galat country ne TEEN alag
    # `create_order` call kharch karvaye — aur order calls surface ka sabse tang bucket
    # hain (10 per minute). Har round-trip ek nakaami hai jo grahak ke saamne hoti hai,
    # aur teenon baar wo theek usi tarah theek ki ja sakti thi.
    first = problems[0]
    return err("INVALID_CONTACT",
               (first["why"] if len(problems) == 1 else
                "%d fields cannot be used to deliver an order: %s"
                % (len(problems), ", ".join(p["field"] for p in problems))),
               field=first["field"], expected=first["expected"],
               received=first.get("received"), problems=problems)


def create_order(conn, agent_token, merchant_id, contact, address, instrument,
                 confirm_items, coupon_code=None):
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    row = merchant_row(conn, merchant_id)
    if row is None:
        return err("MERCHANT_NOT_FOUND", "No merchant registered as " + merchant_id + ".")
    lines = cart.get(agent_token, merchant_id)
    if not lines:
        return err("CART_EMPTY", "Nothing in the cart for " + merchant_id +
                   ". Carts expire after 30 minutes of inactivity; add items again.")

    mismatch = confirm_items_mismatch(lines, confirm_items)
    if mismatch:
        return err("CART_NOT_CONFIRMED", mismatch.pop("why"), **mismatch)

    bad_contact = contact_problem(contact, address)
    if bad_contact:
        return bad_contact

    items_total = cart.items_total_paise(lines)
    estimate = items_total + shipping_estimate(row, items_total)

    # Instrument agent ka chunav hai (D-95), par cap phir bhi amount ka rule hai. Estimate
    # `>= asli total` hamesha hota hai (coupon 0 maana jata hai), to yahan refuse karna
    # kabhi jhootha refusal nahi ho sakta — jo estimate pe cap ke upar hai wo asli total
    # pe bhi upar hi hoga. Merchant ko chhuye bina mana kar dena sabse saaf hai: koi order
    # banta hi nahi, koi stock reserve hoti hi nahi.
    verdict = policy.check_instrument(instrument, estimate)
    if not verdict["allowed"]:
        return err(verdict["code"], verdict["reason"],
                   estimated_total_paise=estimate,
                   cap_paise=policy.MAX_AUTONOMOUS_PAISE,
                   **{k: v for k, v in verdict.items()
                      if k in ("retry_with",)})
    mode = verdict["payment_mode"]

    try:
        response = post_order(row, lines, contact, address, coupon_code, mode,
                              idempotency_key(agent_token, merchant_id, lines, address,
                                              suffix=instrument, contact=contact,
                                              coupon_code=coupon_code))
    except Exception as e:
        return err("MERCHANT_UNREACHABLE", str(e)[:200], merchant_id=merchant_id)
    if response.status_code != 201:
        return merchant_error(conn, response, merchant_id)

    order = merchant_json(conn, response, merchant_id, "order")

    # **Merchant ka 201 ek REPLAY bhi ho sakta hai, aur uska matlab "abhi bana" nahi hai.**
    # SPEC 6 merchant se maangti hai ki wahi key + wahi body par wo *original* 201
    # verbatim lauta de. Wo bilkul theek karta hai — par wo response us order ke JANM ka
    # record hai, uski aaj ki haalat ka nahi. Layer use aage bhej deti thi, to ek order
    # jo paid ho kar cancel ho chuka hai wo `status: "created"`, `money_moved: false` aur
    # *"call pay_order"* ban kar agent tak pahunchta tha — aur wahan se seedha `pay_order`
    # ke us raaste me girta tha jo apni state nahi dekhta. Dono milkar ek grahak se kehte
    # the ki uska refund ho chuka order abhi kharida ja sakta hai.
    #
    # Pehchan saaf hai: agar ye order_id humare apne `layer_orders` me pehle se hai, to
    # ye call ne kuch naya banaya hi nahi. Aisi soorat me merchant se uska ASLI aaj ka
    # roop poochho aur wahi lauta o.
    if conn.execute("SELECT 1 FROM layer_orders WHERE order_id=?",
                    (order["order_id"],)).fetchone():
        return _replayed_order(conn, agent_token, merchant_id, order["order_id"])

    decision = policy.decide_payment(order["final_total_paise"])

    if instrument == "auto" and not decision["autonomous"]:
        # Estimate cap ke neeche tha, merchant ka asli total upar nikla (uska shipping
        # apne declare kiye flat rate se zyada tha). Pehle Layer yahan khud link wala
        # order bana deti thi — par wo phir se Layer ka instrument chunna hai, theek wahi
        # cheez jo D-95 hataata hai. Ab order cancel hota hai (stock chhoot jati hai,
        # paisa hila hi nahi kyunki order UNPAID banta hai) aur faisla agent ke paas
        # wapas jata hai.
        return _refuse_auto_above_cap(conn, row, merchant_id, order, decision, estimate)

    return _record(conn, agent_token, merchant_id, order, decision, instrument)


def _refuse_auto_above_cap(conn, row, merchant_id, order, decision, estimate):
    reason = ("Layer policy: final total Rs %.2f is at or above the Rs %.2f autonomous "
              "ceiling; 'auto' was requested, so this order was cancelled unpaid."
              % (order["final_total_paise"] / 100, policy.MAX_AUTONOMOUS_PAISE / 100))
    released = True
    try:
        with registry.client(row) as api:
            cancelled = api.post("/agent/orders/" + order["order_id"] + "/cancel",
                                 json={"reason": reason})
        released = cancelled.status_code == 200
    except Exception:
        released = False
    return err("AUTONOMOUS_NOT_ALLOWED_AT_THIS_AMOUNT",
               decision["reason"] + " You asked for instrument='auto'. Create the order "
               "again with instrument='link' and a person will pay it.",
               retry_with={"instrument": "link"},
               estimated_total_paise=estimate,
               actual_total_paise=order["final_total_paise"],
               cap_paise=policy.MAX_AUTONOMOUS_PAISE,
               cancelled_order_id=order["order_id"],
               stock_released=released,
               money_moved=False,
               merchant_id=merchant_id)


def rupees(paise):
    return "Rs %s" % format(paise / 100, ",.2f")


def money_lines(items, items_total, shipping, discount, total, label="TOTAL"):
    """Paisa ka hisaab, ek hi jagah se banaya hua, taaki agent use **dobara na likhe**.

    Ye do baar naapa hua problem hai, kalpana nahi.

    **Pehli baar (5.6):** model ne user ko likha *"Total Cost: Rs 849.00, Shipping Cost:
    Rs 49.00, Final Amount: Rs 849.00"* — teen number, aapas me mel nahi khate. Us par
    `create_order` ko `amount_summary` diya gaya.

    **Doosri baar (5.8), aur ye zyada gehri thi:** agent ne CART ka total khud jodkar
    banaya — do product ke `delivery.shipping_paise` (0 aur 4900) jodkar user ko
    **Rs 2,049** bola, jabki Layer ka apna `estimated_total_paise` **Rs 2,000** tha
    (cart Rs 999 se upar tha, to shipping muft). SPEC 5 saaf kehta hai ki wo number
    *"for this product alone"* hai. Yaani maine 5.7 me aadha kaam kiya tha: order pe
    ready lines di, aur **cart pe nahi** — jabki baat-cheet cart par hoti hai.

    Isliye ab ye ek hi function hai aur dono jagah se bulaya jata hai. Jahan bhi Layer
    paisa dikhati hai, wahan ready lines hongi — jodne ka kaam agent ke paas bachega hi
    nahi.
    """
    lines = []
    for item in items:
        # Order ki line me `line_total_paise` hota hai; cart ki line me sirf quote.
        amount = item.get("line_total_paise")
        if amount is None:
            amount = item["quoted_price_paise"] * item["qty"]
        lines.append("%s x %d  %s" % (item["title"], item["qty"], rupees(amount)))
    lines.append("Items      %s" % rupees(items_total))
    if shipping:
        lines.append("Shipping   %s" % rupees(shipping))
    if discount:
        lines.append("Discount  -%s" % rupees(discount))
    lines.append("%-9s %s" % (label, rupees(total)))
    return lines


def amount_summary(order):
    return money_lines(order["items"], order["items_total_paise"],
                       order["shipping_paise"], order["discount_paise"],
                       order["final_total_paise"])


DO_NOT_ADD_SHIPPING = (
    "Do not add these lines up yourself, and never sum the `delivery.shipping_paise` "
    "values from get_product: that number is the cost of shipping that ONE product "
    "(SPEC 5), not a per-item charge. Cart-level shipping is a single figure the "
    "merchant settles once, at order creation.")


def _replayed_order(conn, agent_token, merchant_id, order_id):
    """Ye order pehle se maujood hai — uski AAJ ki haalat lauta o, janm ka record nahi.

    `get_order` merchant se live padhta hai aur `layer_orders` ka status bhi wahin se
    theek kar deta hai, to yahan doosra raasta banane ki zarurat nahi.
    """
    live = get_order(conn, agent_token, order_id)
    if live.get("error"):
        return live
    status = live.get("status")
    return {**live,
            "replayed": True,
            "created_now": False,
            "money_moved": status in ("paid", "confirmed", "shipped", "delivered"),
            "why": ("This cart, address and instrument were already sent as an order, so "
                    "the merchant returned the existing one (" + order_id + ") instead of "
                    "creating a second. Nothing new was created by this call. What you "
                    "see here is that order's CURRENT state, read live — not the response "
                    "it gave when it was first placed."),
            "next_step": (
                "Order " + order_id + " is currently `" + str(status) + "`. Read "
                "`payment.state` and tell the person that, rather than describing this as "
                "a new order. If they want another one of the same thing, change the cart "
                "(quantity, or add a line) so it is a genuinely different order — an "
                "identical cart will keep returning this same one, which is the point of "
                "the idempotency rule.")}


def _record(conn, agent_token, merchant_id, order, decision, instrument):
    payment = order["payment"]                 # `order` merchant_json() se saaf aa chuka hai
    payable = decision["autonomous"] and payment["mode"] == "checkout"
    # `autonomous` cap ka faisla hai — ek **amount** ke baare me. `layer_will_settle`
    # us par agent ke chunav ka asar hai: cap ke neeche bhi agar usne `link` maanga to
    # Layer settle nahi karti. Dono alag likhe jate hain warna record jhootha ho jata:
    # ek link wale order pe `autonomous: true` padha hua dikhta aur uska matlab ulta
    # samjha jata.
    decision = {**decision, "instrument": instrument, "layer_will_settle": payable}
    conn.execute(
        "INSERT INTO layer_orders (order_id, merchant_id, agent_token, final_total_paise,"
        " payment, decision, status, created_at) VALUES (?,?,?,?,?,?,?,?)"
        # `status` JAAN-BOOJHKAR conflict par nahi likha jata. Row ka pehle se hona ka
        # matlab hi ye hai ki hum is order ko already jaante hain — aur jo `order` yahan
        # aaya hai wo merchant ka JANM-record hai (`created`). Use likh dene se ek
        # cancelled ya paid order humare apne record me wapas `created` ho jata tha, aur
        # `list_orders` `created` kehta jabki `get_order` `cancelled` kehta — ek hi order,
        # ek hi lamha, do jawab. Isse bura ye hai ki `pay_order` ka state guard usi record
        # ko padhta hai: is line ke rehte wo guard chup-chaap bekaar ho jata.
        " ON CONFLICT(order_id) DO UPDATE SET payment=excluded.payment,"
        " decision=excluded.decision",
        (order["order_id"], merchant_id, agent_token, order["final_total_paise"],
         db.jd(payment), db.jd(decision), order["status"], order["created_at"]))
    cart.clear(agent_token, merchant_id)
    # Is merchant ka cart khali ho gaya — doosri dukaanon ka nahi. Bina is line ke ek
    # order banne ke baad baaki cart chup-chaap pade rehte hain aur agent unhe bhool
    # jata hai: user ne do cheezein maangi thin aur ek hi kharidi gayi.
    still_waiting = cart.other_carts(agent_token, merchant_id)
    return {
        "order_id": order["order_id"], "merchant_id": merchant_id,
        "other_carts": still_waiting,
        "other_carts_note": (
            "This order covers " + merchant_id + " only. Still in a cart elsewhere: " +
            ", ".join("%s (%d)" % (c["merchant_id"], c["count"]) for c in still_waiting) +
            ". Those need their own create_order — one order never spans two merchants."
            if still_waiting else
            "Nothing of this token's is left in a cart at any other merchant."),
        "status": order["status"], "items": order["items"],
        "items_total_paise": order["items_total_paise"],
        "shipping_paise": order["shipping_paise"],
        "discount_paise": order["discount_paise"],
        "final_total_paise": order["final_total_paise"],
        "money_moved": False,
        "instrument": instrument,
        "policy_decision": decision,
        "payment": payment,
        "amount_summary": amount_summary(order),
        "amount_summary_note": ("Show these lines to the person as they are. They are the "
                                "merchant's own numbers, already settled; rewriting them "
                                "from memory is how a total, a shipping charge and a "
                                "final amount end up disagreeing with each other. "
                                + DO_NOT_ADD_SHIPPING),
        "payment_expires_at": order["payment"].get("expires_at"),
        "expiry_note": ("payment.expires_at is this ORDER's deadline. If it passes "
                        "unpaid the merchant fails the order and releases its stock "
                        "(SPEC 9). It has nothing to do with how long a cart lives."),
        # `next_step` ab agla SAWAAL batata hai, agla paisa wala kadam nahi. Purana text
        # seedha *"Call pay_order — this total is below the ceiling and the Layer can
        # settle it without a human"* likhta tha: yaani Layer khud agent ko paisa dene ko
        # keh rahi thi, aur phir hum shikayat karte the ki agent bina poochhe pay kar deta
        # hai. Wo asli mujrim yahi line thi.
        "next_step": (
            "Nothing has been charged. Put amount_summary in front of the person, say "
            "that the Layer will settle %s itself with no human in the path, and wait for "
            "them to agree. Then call pay_order with confirm_total_paise=%d — that exact "
            "number, echoed back. A different number is refused."
            % (rupees(order["final_total_paise"]), order["final_total_paise"])
            if payable else
            "Nothing has been charged. Give the person payment.link_url to open and pay; "
            "the Layer will not settle this order, and pay_order refuses it."),
    }


# ------------------------------------------------------------------ payment
def poll_until_paid(conn, merchant, order_id, sleep=time.sleep, clock=time.monotonic):
    """Merchant se poochte raho ki paisa aaya ya nahi — ek waqt ki seema ke andar.

    Do baatein jaan-boojhkar aisi hain:

    **Ginti nahi, budget.** Pehle ye "6 koshish" tha. Jab merchant `429` deta hai to har
    koshish ke andar transport apna backoff bhi lagata tha, yaani koshishein jaldi khatam
    ho jatin aur beeta hua waqt kai guna ho jata. Naapkar dekha: 12 second ka socha hua
    budget **70 second** nikla, aur us merchant ko **18** HTTP call gayin jisne abhi-abhi
    "slow down" kaha tha. Budget me sochne se dono cheezein bandh jati hain.

    **`Retry-After` ka hisaab yahin hai, transport me nahi** (`registry.no_retry`). Layer
    wo header maanti hai (SPEC 2.7, D-78) — par ek hi baar. Ek retry loop ke andar doosra
    retry loop rakhna backoff ko guna karta hai, kam nahi.

    Timeout hone pe hum `paid` nahi likhte: `pay_order` `PAYMENT_NOT_CONFIRMED` lauta ta
    hai aur order jaisa hai waisa rehta hai. Ek adhoora jawab yahan surakshit hai; jhootha
    jawab nahi (D-20).
    """
    deadline = clock() + PAY_POLL_BUDGET_SECONDS
    view = {}
    while True:
        with registry.client(merchant,
                             transport=registry.no_retry(merchant["merchant_id"])) as api:
            fetched = api.get("/agent/orders/" + order_id)
        if fetched.status_code == 200:
            view = merchant_json(conn, fetched, merchant["merchant_id"], "order:" + order_id)
            if view.get("status") == "paid":
                return view
        nap = (registry.HonourRetryAfter.wait_for(fetched)
               if fetched.status_code == 429 else PAY_POLL_SECONDS)
        if clock() + nap >= deadline:
            return view
        sleep(nap)


def pay_order(conn, agent_token, order_id, confirm_total_paise):
    """Cap ke NEECHE hi chalta hai, aur asli paisa chalata hai (Razorpay test mode).

    **`confirm_total_paise` ka echo-back hi wo gate hai jo enforce hota hai (D-93/D-97).**
    Layer ye jaanch nahi sakti ki agent ne insaan se poochha ya nahi — *"user ne pehle se
    ijazat di thi"* theek wahi vaakya hai jise humara apna sanitizer `forged_consent` se
    pakadta hai, aur us dawe pe paisa chala dena wahi darwaza kholna hai jise band karne
    ke liye ye project bana hai (D-09). Jo Layer jaanch **sakti** hai wo ye hai ki paisa
    hilne se pehle agent ne wo amount naam lekar bola tha, aur wo amount Layer ke apne
    record se milta tha. Elicitation (poochhna) iske upar sirf behtar UX hai — headless
    client uska jawab `cancel` deta hai (D-100), to gate uspe kabhi nahi rakha ja sakta.

    Ye browser automation nahi hai — koi page render nahi hota, koi selector nahi, koi
    click nahi. Ek deterministic HTTP call hai, wahi jo checkout khud andar se karta hai,
    aur usme sirf merchant ki **public** key_id jati hai. Layer kisi merchant ka secret
    kabhi nahi rakhti.

    ponytail: ye endpoint Razorpay documented nahi karta, isliye ye demo ka instrument
    hai, production ka nahi. Production me theek yahin UPI Reserve Pay / autopay mandate
    baithta hai: insaan ek baar boundary deta hai, uske baad debits server-side hote hain
    — yaani wahi cap jo yahan Rs 2,000 hai, wahan tokenised roop me hota hai. Endpoint
    marta hai to yahan saaf error milta hai aur order cancel karke link wala raasta
    khula rehta hai; chupchap 'paid' kabhi nahi likha jata.
    """
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    row = conn.execute("SELECT * FROM layer_orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        return err("ORDER_NOT_FOUND", "The Layer has no record of " + order_id + ".")
    if row["agent_token"] != agent_token:
        return err("NOT_YOUR_ORDER", "This order was created by a different agent token.")

    # **State guard — echo-back se bhi PEHLE, kyunki jo order pay ho hi nahi sakta uske
    # liye "tumne galat total bola" ek jhootha jawab hai.**
    #
    # Ye poori tarah gayab tha, aur uska nateeja naapa gaya: ek paid order par dobara
    # `pay_order` bulane par Layer seedha provider ko debit bhejti thi, provider `400`
    # deta tha, aur Layer ek hardcoded line chhapti thi — *"The order is still unpaid and
    # its stock is still reserved. Cancel it and create it again."* Ek hi message me chaar
    # jhooth, aur salah aisi jisse ek agent **paid order cancel karke dobara kharid leta**.
    # Timeout ke baad payment retry karna sabse aam cheez hai jo ek agent karta hai.
    #
    # `cancel_order` me ye pattern shuru se sahi hai (dobara cancel karo to wahi refund
    # wapas milta hai, doosra refund nahi banta). Yahan wo laga hi nahi tha.
    #
    # Record par bharosa sirf itna kiya jata hai ki *kab* live padhna hai. `created` ke
    # alawa kuch bhi ho to merchant se aaj ka sach poochha jata hai — record purana ho
    # sakta hai (kisi ne link se pay kar diya ho), aur is raaste par galat hona mehnga hai.
    if row["status"] != "created":
        live = get_order(conn, agent_token, order_id)
        if live.get("error"):
            return live
        status = live.get("status")
        payment = live.get("payment") or {}
        if status in ("paid", "confirmed", "shipped", "delivered"):
            return {"paid": True, "already_paid": True, "order_id": order_id,
                    "merchant_id": row["merchant_id"],
                    "amount_paise": row["final_total_paise"],
                    "money_moved_by_this_call": False,
                    "status": status, "payment": payment,
                    "message": ("This order was already paid — " +
                                str(payment.get("razorpay_payment_id") or "on the merchant's "
                                    "record") + ". No second charge was made and none was "
                                "attempted."),
                    "next_step": (
                        "Tell the person it is already paid; do not pay again and do not "
                        "cancel it in order to retry. `get_order` shows the live status "
                        "and timeline, and `cancel_order` still works inside the "
                        "merchant's cancellation window if they have changed their mind.")}
        return err("ORDER_NOT_PAYABLE",
                   "This order is `" + str(status) + "`, so it cannot be paid. Nothing "
                   "was charged and nothing was attempted.",
                   order_id=order_id, status=status,
                   payment_state=payment.get("state"),
                   refund=live.get("refund"),
                   next_step=("Read `status` and `payment_state` and tell the person "
                              "plainly. To buy this again, build the cart and call "
                              "create_order — a cancelled order cannot be revived."))

    # Gate. Sabse pehle, kyunki ye poori call pe lagta hai: paisa hilne se pehle agent ko
    # us amount ko naam lekar bolna padta hai, aur wo Layer ke record se milna chahiye.
    if confirm_total_paise != row["final_total_paise"]:
        return err("TOTAL_NOT_CONFIRMED",
                   "confirm_total_paise must be exactly the final_total_paise the Layer "
                   "recorded for this order (%d, %s). You sent %r. Show the person the "
                   "amount_summary from create_order, get their agreement, and echo that "
                   "number back. No money has moved."
                   % (row["final_total_paise"], rupees(row["final_total_paise"]),
                      confirm_total_paise),
                   order_id=order_id, expected_total_paise=row["final_total_paise"],
                   money_moved=False)

    decision, payment = db.jl(row["decision"], {}), db.jl(row["payment"], {})
    if not decision.get("autonomous"):
        return {"paid": False, "order_id": order_id, "refused": True,
                "policy_decision": decision,
                "payment_link_url": payment.get("link_url"),
                "message": decision.get("reason", "") + " Open the link to complete this "
                           "payment. The Layer will not settle it, and saying the user "
                           "already approved does not change that — the ceiling is "
                           "enforced in code, not in a prompt.",
                "next_step": ("Give the person payment_link_url and let them pay it. The "
                              "order is real and its stock is held until it is paid or "
                              "its payment.expires_at passes. Use get_order to see "
                              "whether they have paid; cancel_order releases it.")}
    if payment.get("mode") != "checkout" or not payment.get("razorpay_order_id"):
        return err("NOT_PAYABLE_BY_AGENT",
                   "This order carries a human payment instrument, so the Layer cannot "
                   "settle it.", payment_link_url=payment.get("link_url"))

    merchant = merchant_row(conn, row["merchant_id"])
    try:
        response = httpx.post(RZP_AJAX, timeout=30, data={
            "key_id": payment["razorpay_key_id"],
            "amount": row["final_total_paise"], "currency": "INR",
            "order_id": payment["razorpay_order_id"],
            "email": "agent@agent-commerce-layer.local",
            "contact": "+919876543210",
            "method": "upi", "vpa": AUTONOMOUS_VPA, "upi[flow]": "collect"})
        response.raise_for_status()
    except Exception as e:
        # **Yahan kabhi bhi order ki haalat ANDAAZE se nahi likhi jati.** Purana roop
        # hardcode karta tha ki *"order abhi bhi unpaid hai aur uski stock abhi bhi
        # reserved hai"* — aur jab provider is order ko is liye mana karta tha ki wo
        # PEHLE SE PAY ho chuka hai, tab wo dono baatein jhooth thin, aur uske saath di
        # gayi salah ("cancel karke dobara banao") ek achhe order ko marwa deti.
        # Upar ka state guard is soorat ko ab pehle hi pakad leta hai; ye doosri parat
        # hai, un races ke liye jo guard ke baad ho sakti hain — aur ye state batati
        # nahi, POOCHHTI hai.
        live = get_order(conn, agent_token, order_id)
        status = live.get("status") if not live.get("error") else None
        if status in ("paid", "confirmed", "shipped", "delivered"):
            return err("AUTONOMOUS_PAYMENT_FAILED",
                       "The debit was refused, and the reason is that this order is "
                       "already `" + status + "` on the merchant's record. No second "
                       "charge was made. Do not cancel it and do not pay again.",
                       order_id=order_id, status=status,
                       payment=live.get("payment"),
                       next_step=("Tell the person the order is already paid. Use "
                                  "get_order for the live status and timeline."))
        return err("AUTONOMOUS_PAYMENT_FAILED",
                   "The mandate debit did not go through: " + str(e)[:200] +
                   (". The merchant reports this order as `" + status + "`."
                    if status else ". The merchant could not be reached to confirm the "
                                   "order's current state, so do not assume it is unpaid.")
                   + " No money moved on this call.",
                   order_id=order_id, status=status,
                   next_step=("Call get_order first and read `status` and "
                              "`payment.state`. Only if it is still `created` should you "
                              "consider cancelling it and creating it again to receive a "
                              "human payment link."))

    # Merchant se hi poochte hain ki paisa aaya - Razorpay se nahi. Sach wahi hai jo
    # merchant apne order pe likhta hai; agar wo 'paid' nahi kehta to hum 'paid' nahi keh
    # sakte, chahe provider kuch bhi bole.
    view = poll_until_paid(conn, merchant, order_id)

    conn.execute("UPDATE layer_orders SET status=? WHERE order_id=?",
                 (view.get("status", "created"), order_id))
    if view.get("status") != "paid":
        return err("PAYMENT_NOT_CONFIRMED",
                   "The debit was accepted but the merchant has not reported the order as "
                   "paid yet. Call get_order again in a few seconds; do not pay twice.",
                   order_id=order_id, merchant_status=view.get("status"))
    return {"paid": True, "order_id": order_id, "merchant_id": row["merchant_id"],
            "amount_paise": row["final_total_paise"],
            "next_step": ("Money has moved. Tell the person what was paid and give them "
                          "the order id. cancel_order still works inside the merchant's "
                          "cancellation window and starts a refund; get_order shows the "
                          "live status and timeline."),
            "policy_decision": db.jl(row["decision"], {}),
            "payment": view.get("payment"), "timeline": view.get("timeline"),
            "mechanism": {
                "settled_by": "layer_autonomous_mandate",
                "instrument": "razorpay_test_upi",
                "human_in_the_loop": False,
                "production_equivalent": "UPI Reserve Pay / autopay mandate — a human "
                                         "authorises the ceiling once, debits under it "
                                         "are server-side."}}


# ------------------------------------------------------------------ read + cancel
def _owned(conn, agent_token, order_id):
    row = conn.execute("SELECT * FROM layer_orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        return None, err("ORDER_NOT_FOUND", "The Layer has no record of " + order_id + ".")
    if row["agent_token"] != agent_token:
        return None, err("NOT_YOUR_ORDER", "This order was created by a different agent token.")
    return row, None


def get_order(conn, agent_token, order_id):
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    row, bad = _owned(conn, agent_token, order_id)
    if bad:
        return bad
    merchant = merchant_row(conn, row["merchant_id"])
    try:
        with registry.client(merchant) as api:
            response = api.get("/agent/orders/" + order_id)
    except Exception as e:
        return err("MERCHANT_UNREACHABLE", str(e)[:200], merchant_id=row["merchant_id"])
    if response.status_code != 200:
        return merchant_error(conn, response, row["merchant_id"])
    view = merchant_json(conn, response, row["merchant_id"], "order:" + order_id)
    conn.execute("UPDATE layer_orders SET status=? WHERE order_id=?",
                 (view["status"], order_id))
    return {**view, "merchant_id": row["merchant_id"],
            "policy_decision": db.jl(row["decision"], {}),
            "amount_summary": amount_summary(view) if view.get("items") else [],
            "next_step": (
                "This is the merchant's own record and it is the truth about this order. "
                "`payment.state` says where the money is, and read it exactly: `paid` "
                "means the person has been charged, `refund_pending` means a refund has "
                "been SENT but has NOT arrived — never tell them the money is back on "
                "`refund_pending` — `refunded` means it has arrived, `refund_failed` "
                "means it did not go through and needs a human. When a refund exists, "
                "`refund.expected_by` is the date to quote and `refund.razorpay_refund_id` "
                "is what the person's bank will ask for. `cancellable` says whether "
                "cancel_order would work right now; `payment.expires_at` is this order's "
                "own deadline, not a cart's.")}


def list_orders(conn, agent_token, merchant_id=None, limit=20):
    """Is agent ke apne orders — Layer ke record se, merchant ko chhue bina.

    **Ye bhi ek asli gap tha.** `get_order` ek `order_id` maangta hai, aur agar agent ke
    paas wo id na ho — nayi baat-cheet, ya user ne bas poochh liya *"mere pichhle order
    kya the"* — to use dhoondhne ka koi raasta hi nahi tha. `layer_orders` me `agent_token`
    shuru se pada hai (D-63); sirf koi use padh nahi raha tha.

    Ye jaan-boojhkar **live nahi** hai: ye Layer ka apna record hai, ek suchi banane ke
    liye. Kisi ek order ka sach hamesha `get_order` se aata hai, jo merchant tak jaati
    hai — aur wahi ek order ki asli haalat batati hai (D-10 ka wahi usool: suchi purani
    ho sakti hai, paisa kabhi nahi).
    """
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    sql = "SELECT * FROM layer_orders WHERE agent_token=?"
    args = [agent_token]
    if merchant_id:
        sql += " AND merchant_id=?"
        args.append(merchant_id)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(min(max(int(limit), 1), 100))
    rows = conn.execute(sql, args).fetchall()
    orders = []
    for row in rows:
        decision = db.jl(row["decision"], {})
        payment = db.jl(row["payment"], {})
        orders.append({
            "order_id": row["order_id"], "merchant_id": row["merchant_id"],
            "status": row["status"], "created_at": row["created_at"],
            "final_total_paise": row["final_total_paise"],
            "total": rupees(row["final_total_paise"]),
            "instrument": decision.get("instrument"),
            "layer_will_settle": decision.get("layer_will_settle"),
            "payment_link_url": payment.get("link_url"),
            # **"Order kitne ka tha" aur "paisa kitna gaya" do alag baatein hain**, aur
            # pehle yahan sirf pehli thi. Ek cancel ya kabhi na paid hua order bhi apna
            # poora total dikhata tha, aur us suchi se kharcha jodne wala agent us amount
            # ko bhi gin leta. Naapa gaya: ek audit ne is suchi se Rs 2,049 zyada bataya.
            "amount_charged_paise": (row["final_total_paise"]
                                     if payment.get("state") == "paid" else 0),
        })
    charged = sum(o["amount_charged_paise"] for o in orders)
    return {
        "orders": orders, "count": len(orders),
        # Ye system kehta hai ki har paise wala action explainable hai. Ab tak wo har
        # action ke liye alag-alag sach tha aur JODA hua kabhi nahi — ek agent ko "aaj
        # kitna kharch hua" haath se jodna padta tha, aur ek galat jod chup-chaap
        # nikal jata.
        "spend_summary": {
            "orders_listed": len(orders),
            "orders_paid": sum(1 for o in orders if o["amount_charged_paise"]),
            "charged_paise": charged,
            "charged": rupees(charged),
            "note": ("Only orders whose payment actually settled are counted here. An "
                     "order that was created and never paid, or was cancelled, carries "
                     "its total but charged nothing — those are different numbers and "
                     "adding them together overstates the spend. This covers the orders "
                     "in this list only, and it is not a budget: the Layer has no "
                     "cumulative ceiling, so a sequence of orders each below the "
                     "autonomous cap is allowed however long it gets."),
        },
        "merchant_id": merchant_id,
        "source": "layer_record",
        "note": ("These are the Layer's own records of orders this token created. The "
                 "status here is as of the last time the Layer looked; it is a list, not "
                 "a live truth."),
        "next_step": ("Call get_order with one of these order_ids for its live state — "
                      "that reads the merchant and is the only answer to use when it "
                      "matters whether money has moved."
                      if orders else
                      "This token has not created any orders yet."),
    }


def cancel_order(conn, agent_token, order_id, reason):
    bad = require_agent(conn, agent_token)
    if bad:
        return bad
    if not (reason or "").strip():
        return err("MISSING_REASON",
                   "A cancellation needs a stated reason — it lands in the merchant's "
                   "records and in the Layer's audit log.")
    row, bad = _owned(conn, agent_token, order_id)
    if bad:
        return bad
    merchant = merchant_row(conn, row["merchant_id"])
    try:
        with registry.client(merchant) as api:
            response = api.post("/agent/orders/" + order_id + "/cancel",
                                json={"reason": reason[:200]})
    except Exception as e:
        return err("MERCHANT_UNREACHABLE", str(e)[:200], merchant_id=row["merchant_id"])
    if response.status_code != 200:
        return merchant_error(conn, response, row["merchant_id"])
    result = merchant_json(conn, response, row["merchant_id"], "order:" + order_id)
    conn.execute("UPDATE layer_orders SET status=? WHERE order_id=?",
                 (result["status"], order_id))
    refund = result.get("refund")
    return {**result, "merchant_id": row["merchant_id"],
            "next_step": (
                "Cancelled, and a refund has been started — tell the person the amount "
                "and that it takes time to land. refund.expected_by is the merchant's "
                "own estimate."
                if refund else
                "Cancelled. No money had moved, so there is nothing to refund and the "
                "stock has been released. Cancelling again is safe and returns this "
                "same answer.")}
