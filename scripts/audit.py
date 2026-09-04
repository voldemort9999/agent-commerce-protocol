"""Audit log padhne aur token revoke karne ka raasta — operator ke liye, agent ke liye nahi.

    .venv/Scripts/python.exe scripts/audit.py tail [n]
    .venv/Scripts/python.exe scripts/audit.py trail <order_id>
    .venv/Scripts/python.exe scripts/audit.py agent <agent_token>
    .venv/Scripts/python.exe scripts/audit.py agents
    .venv/Scripts/python.exe scripts/audit.py revoke <agent_token> <reason...>
    .venv/Scripts/python.exe scripts/audit.py metrics

**Revoke yahan hai, MCP tool me nahi.** Token hi agent ki poori pehchaan hai; agar koi
bhi agent kisi bhi token ko revoke kar sake to ek galat agent baaki sabko band kar dega
— yaani ek denial-of-service jo humne khud banaya. Aur jis agent ko rokna hai, wahi khud
ko rokne wala nahi ho sakta. Rokna Layer chalane wale ka kaam hai.

`trail <order_id>` wahi cheez hai jo session 5 ka gate maangta hai: ek order ka poora
raasta — kisne maanga, kya faisla hua, kitna paisa hila.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "layer"))
import db          # noqa: E402
import policy      # noqa: E402
import sync        # noqa: E402

MARK = {"allow": "ALLOW", "block": "BLOCK"}


def rupees(paise):
    return "" if paise is None else "Rs %s" % format(paise / 100, ",.2f")


def show(rows, with_args=True):
    if not rows:
        print("   (audit log me kuch nahi)")
        return
    for r in rows:
        print("%-20s %-5s %-16s %-14s %s" % (
            r["at"], MARK.get(r["decision"], r["decision"]), r["tool"],
            rupees(r["amount_paise"]), r["order_id"] or ""))
        if r["reason"]:
            print("       reason : " + r["reason"][:150])
        # Instrument aur echo-back wali values alag line pe, args ke truncation se
        # pehle. Yahi wo do cheezein hain jo session 5.7 ke gate banati hain — agar wo
        # 150 characters ke baad kat jayen to audit log unhe rakhta to hai par dikhata
        # nahi, aur "explainable" ka matlab dikhna bhi hai.
        args = json.loads(r["arguments"]) if r["arguments"] else {}
        stated = {k: args[k] for k in ("instrument", "confirm_items",
                                       "confirm_total_paise") if k in args}
        if stated:
            print("       stated : " + json.dumps(stated)[:150])
        if with_args and r["arguments"] not in ("{}", None):
            print("       args   : " + r["arguments"][:150])


def cmd_tail(conn, n="30"):
    rows = policy.trail(conn, limit=int(n))
    print("--- last %d audit rows (newest first) ---" % int(n))
    show(rows)


def cmd_agent(conn, token):
    """Attribution: revoke se pehle is agent ne kya-kya kiya."""
    row = conn.execute("SELECT * FROM agents WHERE token=?", (token,)).fetchone()
    if row is None:
        sys.exit("aisa koi token nahi: " + token)
    print("agent      : %s" % token)
    print("label      : %s" % (row["label"] or "-"))
    print("created    : %s" % row["created_at"])
    print("revoked    : %s" % (row["revoked_at"] or "no"))
    orders = conn.execute("SELECT order_id, merchant_id, final_total_paise, status"
                          " FROM layer_orders WHERE agent_token=?", (token,)).fetchall()
    print("orders     : %d" % len(orders))
    for o in orders:
        print("   %s  %-22s %-12s %s" % (o["order_id"], o["merchant_id"],
                                         rupees(o["final_total_paise"]), o["status"]))
    rows = policy.trail(conn, agent_token=token)
    blocked = sum(1 for r in rows if r["decision"] == "block")
    print("tool calls : %d  (%d blocked)" % (len(rows), blocked))
    show(rows)


def cmd_agents(conn):
    rows = conn.execute(
        "SELECT a.token, a.label, a.created_at, a.revoked_at,"
        " (SELECT COUNT(*) FROM audit x WHERE x.agent_token = a.token) AS calls"
        " FROM agents a ORDER BY a.created_at DESC LIMIT 40").fetchall()
    for r in rows:
        print("%-40s %-16s calls=%-4s %s" % (
            r["token"], (r["label"] or "-")[:16], r["calls"],
            "REVOKED " + r["revoked_at"] if r["revoked_at"] else ""))


def cmd_trail(conn, order_id):
    """Ek order ka poora raasta — cart se faisle se paise tak."""
    row = conn.execute("SELECT * FROM layer_orders WHERE order_id=?",
                       (order_id,)).fetchone()
    if row is None:
        sys.exit("Layer ke paas is order ka record nahi: " + order_id)
    decision = db.jl(row["decision"], {})
    print("order      : %s" % order_id)
    print("merchant   : %s" % row["merchant_id"])
    print("agent      : %s" % row["agent_token"])
    print("total      : %s" % rupees(row["final_total_paise"]))
    print("status     : %s" % row["status"])
    print("decision   : autonomous=%s  instrument=%s  layer_will_settle=%s"
          % (decision.get("autonomous"), decision.get("instrument", "-"),
             decision.get("layer_will_settle", "-")))
    print("             %s" % decision.get("reason", ""))
    print("             (autonomous = cap ka faisla, ek AMOUNT ke baare me."
          " layer_will_settle = uspe agent ke chune hue instrument ka asar)")
    print("payment    : %s" % json.dumps(db.jl(row["payment"], {}))[:200])
    print("--- every tool call on the way to this order ---")
    show(policy.trail(conn, order_id=order_id))


def cmd_revoke(conn, token, *reason):
    out = policy.revoke(conn, token, " ".join(reason) or "no reason given")
    print(json.dumps(out, indent=2))
    if out.get("revoked"):
        print("\nAb is token se koi money operation nahi chalega. Isne pehle kya kiya:")
        print("   scripts/audit.py agent " + token)


def cmd_metrics(conn):
    """§15 ke metrics — naapkar, dawe se nahi. Jo naapa nahi ja saka wo 'not measured'.

    Sab audit log aur index se aate hain, yaani inhe banane ke liye kuch alag se record
    nahi karna padta. Pehle `demo/attacks/run_attacks.py` chalao, warna denominator
    khali rehte hain.
    """
    def count(where, args=()):
        return conn.execute("SELECT COUNT(*) c FROM audit WHERE " + where,
                            args).fetchone()["c"]

    stripped = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(json_extract(arguments,"
                            " '$.removed_lines')), 0) lines FROM audit"
                            " WHERE tool='sanitizer'").fetchone()
    qty_blocks = count("decision='block' AND reason LIKE 'QTY_LIMIT_EXCEEDED%'")
    pay_total = count("tool='pay_order'")
    pay_done = count("tool='pay_order' AND decision='allow'")
    revoked_blocks = count("reason LIKE 'AGENT_REVOKED%'")
    # Session 5.7 se `pay_order` ka block do bilkul alag wajah se ho sakta hai: cap
    # (amount insaan ke paas jana chahiye) aur confirmation gate (agent ne galat amount
    # bola). Pehle ye metric saare pay_order blocks ko "cap enforcement" gin raha tha —
    # ab wo jhooth hota, aur wo jhooth chup-chaap badhta jaata jaise-jaise echo-back
    # refusals jama hote. Do alag ginti, kyunki ye do alag rules hain.
    cap_blocks = count("decision='block' AND (reason LIKE '%autonomous ceiling%'"
                       " OR reason LIKE 'AUTONOMOUS_NOT_ALLOWED_AT_THIS_AMOUNT%')")
    confirm_blocks = count("decision='block' AND (reason LIKE 'CART_NOT_CONFIRMED%'"
                           " OR reason LIKE 'TOTAL_NOT_CONFIRMED%')")

    print("injection payloads stripped and logged : %d  (%d lines)"
          % (stripped["c"], stripped["lines"]))
    print("   detected = stripped: an outbound payload is sanitised before the response")
    print("   is built, so anything detected was already removed. Two guards keep that")
    print("   true rather than merely claimed - a source-level test that no merchant JSON")
    print("   is read outside merchant_json()/merchant_error(), and a behavioural test for")
    print("   the one exit whose text comes from our own DB (the manifest).")
    print("quantity ceiling refusals              : %d" % qty_blocks)
    print("pay_order calls                        : %d" % pay_total)
    print("   autonomous completion rate          : %s"
          % ("%.2f (%d/%d)" % (pay_done / pay_total, pay_done, pay_total)
             if pay_total else "not measured — no pay_order call in the log"))
    print("cap enforcement refusals               : %d" % cap_blocks)
    print("   an amount at or above the ceiling, refused to settle autonomously —")
    print("   at create_order when 'auto' was asked for, or at pay_order for an order")
    print("   already carrying a human instrument")
    print("confirmation gate refusals             : %d" % confirm_blocks)
    print("   the agent stated items or a total that did not match the Layer's own")
    print("   record, so nothing was ordered and no money moved. Asking a person is")
    print("   the agent's job and is not enforceable; echoing the number back is ours")
    rate_blocks = count("decision='block' AND reason LIKE 'RATE_LIMITED%'")
    print("rate-limit refusals                    : %d" % rate_blocks)
    print("   the Layer keeps its own traffic to any one merchant inside a budget, and")
    print("   budgets each agent token separately. ARCHITECTURE 7.3 promises a merchant")
    print("   it is dealing with a single caller; this is that promise in code rather")
    print("   than in prose")
    print("revoked-token refusals                 : %d" % revoked_blocks)

    # Revocation latency: revoke likhne se pehle refuse hone tak.
    latency = conn.execute(
        "SELECT a.agent_token t, a.at revoked_at,"
        " (SELECT MIN(b.at) FROM audit b WHERE b.agent_token = a.agent_token"
        "   AND b.reason LIKE 'AGENT_REVOKED%' AND b.id > a.id) first_refusal"
        " FROM audit a WHERE a.tool='revoke_agent' ORDER BY a.id DESC LIMIT 1").fetchone()
    if latency and latency["first_refusal"]:
        seconds = (sync.datetime.strptime(latency["first_refusal"], sync.ISO)
                   - sync.datetime.strptime(latency["revoked_at"], sync.ISO)).total_seconds()
        # Audit ke timestamps second-resolution hain (baaki poore project ki tarah,
        # SPEC 2.2). Turant hui revocation "0s" dikhti hai, jo "naapa nahi" jaisa padha
        # jata hai - isliye wo saaf likha jata hai.
        print("revocation latency                     : %s "
              "(revoked %s, first refusal %s)"
              % ("%.0fs" % seconds if seconds else "<1s (second-resolution stamps)",
                 latency["revoked_at"], latency["first_refusal"]))
    else:
        print("revocation latency                     : not measured — no revoked token "
              "has been used since")

    age = sync.index_age_seconds(conn)
    print("sync freshness (median row age)        : %.0fs" % (age or 0))
    hits = db.search(conn, query="shirt", limit=50)
    healthy = conn.execute("SELECT COUNT(*) c FROM merchants WHERE healthy=1").fetchone()["c"]
    print("cross-merchant coverage ('shirt')      : %d of %d healthy merchants"
          % (len({h["merchant_id"] for h in hits}), healthy))


COMMANDS = {"tail": cmd_tail, "trail": cmd_trail, "agent": cmd_agent,
            "agents": cmd_agents, "revoke": cmd_revoke, "metrics": cmd_metrics}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    with db.connect() as connection:
        COMMANDS[sys.argv[1]](connection, *sys.argv[2:])
