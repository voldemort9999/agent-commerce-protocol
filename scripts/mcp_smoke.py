"""Layer se ek ASLI MCP client ki tarah baat karo — stdio pe, subprocess spawn karke.

    .venv/Scripts/python.exe scripts/mcp_smoke.py

pytest layer ke plain functions test karta hai. Ye file wo cheez test karti hai jo
pytest nahi kar sakti: kya server sach me MCP protocol bolta hai, kya tools list hote
hain, kya call_tool ka jawab wapas aata hai. Claude Desktop bhi bilkul yahi karta hai.

Ye ek ASLI Razorpay test-mode payment karta hai aur uska refund bhi. Har run pe ek
payment link bhi banta hai (cap ke upar wala hissa) - Razorpay link creation pe rate
limit lagata hai, to isse loop me mat chalao (D-33).

Session 5.7 se ye teen aur cheezein bhi dikhata hai, kyunki teenon sirf asli protocol pe
dikhti hain: `get_product` ke jawab me ek **image block** aata hai (D-96), `create_order`
bina `instrument` ke chalti hi nahi (D-95), aur `pay_order` galat `confirm_total_paise`
pe **mana kar deta hai** (D-93).
"""
import asyncio
import json
import pathlib
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

SERVER = pathlib.Path(__file__).parents[1] / "layer" / "server.py"

CONTACT = {"name": "Agent Buyer", "phone": "+919876543210", "email": "buyer@example.com"}
ADDRESS = {"line1": "Flat 402, Sunrise Residency", "city": "Nagpur",
           "state": "Maharashtra", "pincode": "440001", "country": "IN"}


def unwrap(result):
    """CallToolResult se dict nikalo."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


def kinds(result):
    """Jawab me kis-kis kism ke content block aaye. Ye sirf protocol pe dikhta hai —
    `pytest layer` plain function bulata hai, jahan ye layer hoti hi nahi."""
    return [type(block).__name__ for block in result.content]


def echo(cart_view):
    """`confirm_items` — wahi jo ek asli agent ko khud likhna padta hai (D-93)."""
    return [{"variant_id": line["variant_id"], "qty": line["qty"]}
            for line in cart_view["items"]]


async def pick_variant(session, below_paise=None, above_paise=None, token=None):
    """Ek in-stock variant is price band me — search index se dhoondho, par price LIVE
    detail se lo. Index minutes purani ho sakti hai; jis number pe order jayega wo nahi.

    `token` read tools pe **optional** hai (ARCH 7.2 unhe khula rakhta hai). Dena faayda
    hai: bada rate-limit budget, aur reads audit trail me naam ke saath chadhti hain.
    """
    found = unwrap(await session.call_tool("search_products", {
        "min_price_paise": above_paise, "max_price_paise": below_paise, "limit": 10,
        "agent_token": token}))
    for candidate in found["results"]:
        live = unwrap(await session.call_tool("get_product", {
            "merchant_id": candidate["merchant_id"],
            "product_id": candidate["product_id"], "agent_token": token}))
        for variant in live.get("variants", []):
            if variant["stock"] < 1:
                continue
            if below_paise and variant["price_paise"] > below_paise:
                continue
            if above_paise and variant["price_paise"] < above_paise:
                continue
            return candidate["merchant_id"], candidate["product_id"], variant
    return None


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print("connected to :", info.server_info.name, info.server_info.version)

            tools = (await session.list_tools()).tools
            print("tools        :", ", ".join(t.name for t in tools))

            merchants = unwrap(await session.call_tool("list_merchants", {}))
            print("\nlist_merchants ->", merchants["count"], "healthy,",
                  "index median age", merchants["index_median_age_seconds"], "s")
            for m in merchants["merchants"]:
                print(f"   {m['merchant_id']:22} {m['name']:20} "
                      f"cancel<{m['policies']['cancel_window_hours']}h "
                      f"maxqty={m['policies']['max_qty_per_variant']}")

            agent = unwrap(await session.call_tool(
                "register_agent", {"label": "smoke"}))["agent_token"]
            print("\nregister_agent ->", agent[:16] + "...")
            print("   (token read tools pe bhi ja raha hai: bada budget + audit me naam)")

            found = unwrap(await session.call_tool("search_products", {
                "query": "black t-shirt", "max_price_paise": 200000, "limit": 5,
                "agent_token": agent}))
            print(f"\nsearch_products('black t-shirt', <=Rs2000) -> {found['count']} "
                  f"from {found['source']}")
            for p in found["results"]:
                print(f"   Rs{p['price_range_paise']['min'] // 100:>6,}  "
                      f"{p['merchant_id']:20} {p['product_id']:8} {p['title'][:36]}")
            print("   note:", found["price_note"][:78], "...")

            if not found["results"]:
                sys.exit("search ne kuch nahi diya. Index khali ho to "
                         "`python layer/sync.py` chalao; index bhari ho to koi "
                         "merchant unhealthy hai — `list_merchants` me last_error dekho")
            pick = found["results"][0]

            live = unwrap(await session.call_tool("get_product", {
                "merchant_id": pick["merchant_id"], "product_id": pick["product_id"],
                "pincode": "440001", "agent_token": agent}))
            in_stock = [v for v in live["variants"] if v["stock"] > 0]
            print(f"\nget_product({pick['product_id']}) -> {live['source']}")
            print(f"   {len(live['variants'])} variants, {len(in_stock)} in stock")
            for v in live["variants"][:3]:
                opts = " / ".join(v["options"].values()) or "Standard"
                print(f"   Rs{v['price_paise'] // 100:>6,}  stock {v['stock']:>3}  {opts}")
            print(f"   delivery to 440001: serviceable={live['delivery']['serviceable']} "
                  f"shipping={live['delivery'].get('shipping_paise')} paise")
            print(f"   photo     : {live['photo']['panels']} panels, one labelled "
                  f"collage, attached={live['photo']['attached']}")
            print(f"   human link: {live['product_page_url']}")
            shown = await session.call_tool("get_product", {
                "merchant_id": pick["merchant_id"], "product_id": pick["product_id"],
                "agent_token": agent})
            print("   blocks    :", ", ".join(kinds(shown)),
                  "  <- ek URL kabhi kisi ne nahi khola; ye model sach me dekh leta hai")
            print(f"   variants  : Layer koi nahi chunti — "
                  f"{live['variant_choice']['count']} in stock, sab agent ke saamne")

            dead = unwrap(await session.call_tool("get_product", {
                "merchant_id": pick["merchant_id"], "product_id": pick["product_id"],
                "pincode": "781001", "agent_token": agent}))
            print(f"   delivery to 781001: serviceable={dead['delivery']['serviceable']}"
                  "   <- agent yahan doosra merchant dhoondega")

            index_price = pick["price_range_paise"]["min"]
            live_price = min(v["price_paise"] for v in live["variants"])
            print(f"\nindex says Rs{index_price // 100:,} | live says Rs{live_price // 100:,}"
                  f" | same={index_price == live_price}")
            print("   (paisa hamesha live wale pe chalega — index kabhi charge nahi hoti)")

            # ---------------------------------------------------------- transaction
            print("\n" + "=" * 68)
            print("TRANSACTION — cap ke NEECHE: agent khud pay karta hai")
            print("=" * 68)

            cheap = await pick_variant(session, below_paise=150_000, token=agent)
            if cheap is None:
                sys.exit("Rs 1,500 ke neeche koi in-stock variant nahi mila")
            merchant_id, product_id, variant = cheap
            print(f"pick         : {variant['variant_id']} @ Rs{variant['price_paise'] // 100:,}")

            cart = unwrap(await session.call_tool("add_to_cart", {
                "agent_token": agent, "merchant_id": merchant_id,
                "product_id": product_id, "variant_id": variant["variant_id"], "qty": 1}))
            print(f"add_to_cart  : {cart['count']} line, quoted "
                  f"Rs{cart['items_total_paise'] // 100:,}, expires in "
                  f"{cart['cart_expires_in_seconds']}s")

            wrong = unwrap(await session.call_tool("create_order", {
                "agent_token": agent, "merchant_id": merchant_id,
                "contact": CONTACT, "address": ADDRESS, "instrument": "auto",
                "confirm_items": [{"variant_id": variant["variant_id"], "qty": 99}]}))
            print(f"create_order : REFUSED {wrong['error']['code']}  "
                  "<- agent ne wo confirm kiya jo cart me hai hi nahi")

            order = unwrap(await session.call_tool("create_order", {
                "agent_token": agent, "merchant_id": merchant_id,
                "contact": CONTACT, "address": ADDRESS, "instrument": "auto",
                "confirm_items": echo(cart)}))
            if "error" in order:
                sys.exit("create_order: " + json.dumps(order["error"]))
            print(f"create_order : {order['order_id']}  status={order['status']}  "
                  f"money_moved={order['money_moved']}")
            print(f"   items Rs{order['items_total_paise'] // 100:,} + shipping "
                  f"Rs{order['shipping_paise'] // 100:,} - discount "
                  f"Rs{order['discount_paise'] // 100:,} = FINAL "
                  f"Rs{order['final_total_paise'] // 100:,}")
            print(f"   policy    : autonomous={order['policy_decision']['autonomous']} "
                  f"({order['policy_decision']['reason']})")
            print(f"   instrument: {order['instrument']} -> {order['payment']['mode']}"
                  "   (agent ne chuna, amount se derive nahi hua)")
            for line in order["amount_summary"]:
                print("     ", line)
            print("   next_step :", order["next_step"][:110], "...")

            refused_echo = unwrap(await session.call_tool("pay_order", {
                "agent_token": agent, "order_id": order["order_id"],
                "confirm_total_paise": order["final_total_paise"] - 1}))
            print(f"pay_order    : REFUSED {refused_echo['error']['code']}  "
                  f"money_moved={refused_echo['error']['money_moved']}  "
                  "<- ek paisa kam bola, gate ruk gaya")

            paid = unwrap(await session.call_tool("pay_order", {
                "agent_token": agent, "order_id": order["order_id"],
                "confirm_total_paise": order["final_total_paise"]}))
            if not paid.get("paid"):
                sys.exit("pay_order: " + json.dumps(paid)[:300])
            print(f"pay_order    : PAID  {paid['payment']['razorpay_payment_id']}  "
                  f"human_in_the_loop={paid['mechanism']['human_in_the_loop']}")

            view = unwrap(await session.call_tool("get_order", {
                "agent_token": agent, "order_id": order["order_id"]}))
            print(f"get_order    : {view['status']}  timeline="
                  f"{[e['status'] for e in view['timeline']]}")

            cancelled = unwrap(await session.call_tool("cancel_order", {
                "agent_token": agent, "order_id": order["order_id"],
                "reason": "smoke test cleanup"}))
            print(f"cancel_order : {cancelled['status']}  refund="
                  f"{cancelled['refund']['state']} {cancelled['refund']['razorpay_refund_id']}")

            print("\n" + "=" * 68)
            print("TRANSACTION — cap ke UPAR: Layer khud ko mana karti hai")
            print("=" * 68)

            refusal_shown = False
            costly = await pick_variant(session, above_paise=200_000,
                                        below_paise=450_000, token=agent)
            if costly is None:
                print("cap ke upar koi variant nahi mila — ye hissa chhoda")
            else:
                merchant_id, product_id, variant = costly
                print(f"pick         : {variant['variant_id']} @ "
                      f"Rs{variant['price_paise'] // 100:,}")
                costly_cart = unwrap(await session.call_tool("add_to_cart", {
                    "agent_token": agent, "merchant_id": merchant_id,
                    "product_id": product_id, "variant_id": variant["variant_id"],
                    "qty": 1}))
                auto = unwrap(await session.call_tool("create_order", {
                    "agent_token": agent, "merchant_id": merchant_id,
                    "contact": CONTACT, "address": ADDRESS, "instrument": "auto",
                    "confirm_items": echo(costly_cart)}))
                print(f"create_order : REFUSED {auto['error']['code']}  "
                      f"retry_with={auto['error']['retry_with']}")
                print("   -> merchant ko call hi nahi gayi: koi order nahi, koi stock "
                      "reserve nahi, Razorpay pe ek bhi request nahi")

                big = unwrap(await session.call_tool("create_order", {
                    "agent_token": agent, "merchant_id": merchant_id,
                    "contact": CONTACT, "address": ADDRESS, "instrument": "link",
                    "confirm_items": echo(costly_cart)}))
                if "error" in big:
                    # Razorpay link creation pe rate limit lagata hai (D-33). Dhyan dene
                    # wali baat ye hai ki code bacha rehta hai: agent ko `RATE_LIMITED`
                    # milta hai, `500` nahi - yaani wo retry kar sakta hai.
                    print("create_order:", json.dumps(big["error"])[:200])
                else:
                    print(f"create_order : {big['order_id']}  FINAL "
                          f"Rs{big['final_total_paise'] // 100:,}  "
                          f"instrument={big['instrument']} -> {big['payment']['mode']}")
                    refused = unwrap(await session.call_tool("pay_order", {
                        "agent_token": agent, "order_id": big["order_id"],
                        "confirm_total_paise": big["final_total_paise"]}))
                    print(f"pay_order    : paid={refused['paid']} refused="
                          f"{refused.get('refused')}")
                    print("   ->", refused["message"][:150])
                    print("   link for a human:", refused["payment_link_url"])
                    refusal_shown = True
                    await session.call_tool("cancel_order", {
                        "agent_token": agent, "order_id": big["order_id"],
                        "reason": "smoke test cleanup"})

            print("\nOK — ek hi MCP session me: search, live detail + tasveer, cart, "
                  "confirm, unpaid order, autonomous payment aur refund.")
            print("   cap se upar wala refusal: "
                  + ("dikha" if refusal_shown else
                     "NAHI chala — upar ki wajah padho (aksar provider ka rate limit). "
                     "Uska pakka test `pytest layer -k above_the_cap` hai."))


if __name__ == "__main__":
    asyncio.run(main())
