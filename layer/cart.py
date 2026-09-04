"""Cart sessions — Layer ke andar, merchant ke paas kabhi nahi.

Merchant cart nahi banata (SPEC 1). Wajah sirf "kaam kam ho" nahi hai: cart ek aisi
jagah hai jahan quote kiya gaya price yaad rakhna padta hai, aur wahi quote baad me
`expected_price_paise` banta hai jise merchant strictly verify karta hai (SPEC 6). Agar
cart merchant ke paas hota to merchant apne hi quote ko verify kar raha hota — dono
taraf check hone ka poora matlab hi khatam (D-08).

Cart ek dict me rehta hai, DB me nahi. Wo jaan-boojhkar hai: cart ephemeral hai, 30
minute ka hai, aur uska khona ek recoverable ghatna hai (dobara add kar lo). Jis cheez
ka khona recoverable nahi — order aur uska policy faisla — wo DB me jati hai (orders.py).
ponytail: process-local dict; ek se zyada Layer process chale to Redis (ARCHITECTURE 12
me production path pehle se yahi kehta hai).

Cart per (agent_token, merchant_id) hai — ek agent ke do merchants pe do alag cart. Ek
cart me do merchants ke items nahi mila sakte: uska matlab payment, shipping aur
cancellation teenon ko do hisson me todna hai (ARCHITECTURE 11 me scope se bahar).
"""
import time

TTL_SECONDS = 30 * 60

_CARTS = {}

# Wo carts jo expire ho gaye. Sirf "kab" aur "kitni lines" — content nahi, warna ye khud
# ek doosra cart ban jata aur cart ka 30 minute me marna bemaani ho jata.
_EXPIRED = {}
EXPIRY_MEMORY_SECONDS = 3 * 60 * 60      # (agent_token, merchant_id) -> {"lines": {variant_id: line}, "touched": float}


def _live(key):
    """Expiry padhte waqt lagti hai, background sweep se nahi.

    Sweeper ek thread hai jise chalana, rokna aur test karna padta hai; wo sirf memory
    jaldi khali karta hai. Yahan galat jawab dono me ek jaisa hai — expired cart kabhi
    dikhta nahi — aur lazy version me chalane ko kuch hai hi nahi. ponytail: rows lakhon
    ho jayen to sweep, abhi ek agent ke paas ek cart hai.
    """
    entry = _CARTS.get(key)
    if entry is None:
        return None
    if time.time() - entry["touched"] > TTL_SECONDS:
        _CARTS.pop(key, None)
        # Yaad rakho ki YAHAN kuch tha. Bina iske ek expire hua cart aur ek kabhi na
        # bhara gaya cart bilkul ek jaise dikhte hain, aur agent grahak se kehta hai ki
        # unki basket khaali thi — jabki wo bani thi aur uska waqt nikal gaya. Sirf ginti
        # aur waqt rakha jata hai, lines nahi: wo cart ko zinda rakhna hota, aur cart ka
        # 30 minute me marna jaan-boojhkar hai.
        if entry["lines"]:
            _EXPIRED[key] = {"at": time.time(), "count": len(entry["lines"])}
        return None
    return entry


def recently_expired(agent_token, merchant_id, within=EXPIRY_MEMORY_SECONDS):
    """Is (token, merchant) ka cart abhi-abhi expire hua tha?"""
    seen = _EXPIRED.get((agent_token, merchant_id))
    return bool(seen and time.time() - seen["at"] <= within)


def get(agent_token, merchant_id):
    entry = _live((agent_token, merchant_id))
    return list(entry["lines"].values()) if entry else []


def add(agent_token, merchant_id, line):
    """`line` me quote kiya gaya price hota hai — wahi baad me merchant ko wapas
    dikhaya jata hai. Wahi variant dobara add karne pe qty judti hai, replace nahi hoti."""
    key = (agent_token, merchant_id)
    entry = _live(key) or {"lines": {}, "touched": time.time()}
    existing = entry["lines"].get(line["variant_id"])
    if existing:
        line = {**line, "qty": existing["qty"] + line["qty"]}
    entry["lines"][line["variant_id"]] = line
    entry["touched"] = time.time()
    _CARTS[key] = entry
    return list(entry["lines"].values())


def remove(agent_token, merchant_id, variant_id):
    entry = _live((agent_token, merchant_id))
    if not entry:
        return []
    entry["lines"].pop(variant_id, None)
    entry["touched"] = time.time()
    return list(entry["lines"].values())


def clear(agent_token, merchant_id):
    _EXPIRED.pop((agent_token, merchant_id), None)
    _CARTS.pop((agent_token, merchant_id), None)


def other_carts(agent_token, merchant_id):
    """Is token ke wo cart jo KISI AUR merchant pe khule hain.

    Ye tab tak dikhta hi nahi jab tak ek se zyada merchant na ho, aur theek isi wajah se
    ise pehle se banana zaroori hai. Do dukaan hote hi ye ek asli galti ban jati hai:
    agent ek dukaan se shirt cart me daalta hai, doosri se kuch aur, phir `view_cart`
    karta hai aur use **aadha** cart dikhta hai — kyunki cart per merchant hai. Wahan se
    wo ya to user ko adhoora total batata hai, ya `create_order` aisa banata hai jisme
    doosri cheez hai hi nahi, aur `confirm_items` us galti ko pakadta zaroor hai par
    uski **wajah** kahin nahi likhi hoti.

    Isliye jawab prose me nahi, ginti me jata hai: har cart response ke saath ye suchi
    jati hai, aur agent ko kuch yaad rakhna nahi padta.

    **Aur sirf ginti kaafi nahi thi.** `{"merchant_id": ..., "count": 2}` ek adhoora
    jawab hai jo **poora jawab jaisa padha jata hai**: *"mere basket me kya hai?"* poochhne
    par agent ke paas do hi raaste bachte the — har merchant par ek aur `view_cart` (jo
    use yaad rakhna padta) ya bare number ko jawab bana kar de dena. Note kehta tha ki
    doosra raasta galat hai, par ek imaandaar ginti ke bagal me likha hua note ginti se
    kamzor hota hai. Isliye ab title aur us cart ka apna items total bhi saath jata hai —
    tab wo aadha jawab dene ki naubat hi nahi aati.

    Yahan shipping jaan-boojhkar NAHI hai: shipping merchant ka authority hai (D-18) aur
    us dukaan ke apne cart response me pehle se hai. Do jagah do number likhne ka natija
    is repo me pehle bhi wahi raha hai — do jawab, aur padhne wale ke paas chunne ka koi
    rule nahi (D-142).
    """
    out = []
    for (token, mid), entry in sorted(_CARTS.items()):
        if token != agent_token or mid == merchant_id or not _live((token, mid)):
            continue
        lines = list(entry["lines"].values())
        if not lines:
            continue
        out.append({
            "merchant_id": mid,
            "count": len(lines),
            "items_total_paise": items_total_paise(lines),
            "items": [{"variant_id": line["variant_id"], "qty": line["qty"],
                       "title": line.get("title")} for line in lines],
        })
    return out


def items_total_paise(lines):
    return sum(line["quoted_price_paise"] * line["qty"] for line in lines)


def expires_in_seconds(agent_token, merchant_id):
    entry = _live((agent_token, merchant_id))
    return None if entry is None else int(TTL_SECONDS - (time.time() - entry["touched"]))
