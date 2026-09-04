# Agent Commerce Protocol

**A specification that makes any merchant transactable by any AI buyer, and a layer that
speaks it.**

Built for the Razorpay AI Buildathon, Track 01 — *"an agent that makes a merchant
transactable by an AI buyer end to end."*

## Demo video

**▶ _(link to be added)_** — five minutes, recorded against the system running locally:
a cross-merchant search, a purchase settled below the ceiling with real Razorpay
test-mode money, one refused above it, a planted prompt injection stripped and logged,
and the audit trail of the order that was just placed.

---

## The problem

An AI assistant can already research a purchase. It cannot complete one.

Ask any frontier model to buy a black t-shirt under ₹2,000 and it will find three
candidates, compare them, and stop — because between *"here are your options"* and *"it is
ordered"* sits a wall no model can climb. Storefronts are built for eyes, not machines.
Checkout is built to stop bots, and every defence that keeps fraud out keeps legitimate
agents out too. And nobody knows who is liable: merchants have no mechanism to bound an
agent's authority, so they bound it to zero.

A new class of customer exists, has money, and cannot spend it.

## The claim

> Any merchant that implements **six HTTP endpoints** becomes transactable end to end —
> discovery through payment through cancellation — by **any AI buyer**, with every money
> action **bounded, gated and auditable**.

Two things are built, and it matters that they are two:

| | What | Where |
|---|---|---|
| **A specification** | Six endpoints on a merchant's existing backend. Framework-, language- and database-agnostic | [`docs/SPEC.md`](docs/SPEC.md) |
| **A layer that speaks it** | One MCP server that indexes every registered merchant, gives agents a single tool surface, and enforces every safety rule in one place | [`layer/`](layer/) |

An agent connects once — every merchant behind it becomes reachable. A merchant
implements once — every agent becomes a customer. **N × M trust relationships collapse to
N + M**, which is the Layer's actual value to a merchant.

```
   ChatGPT / Claude / custom bot          ← buyer agents (not ours)
              │  MCP
   ╔══════════▼═══════════════════════════════╗
   ║        AGENT COMMERCE LAYER               ║
   ║  Registry · FTS index · Cart sessions     ║
   ║  POLICY: identity · ceilings · sanitizer  ║
   ║          price re-verify · audit log      ║
   ╚═══╤═══════════╤═══════════════╤═══════════╝
       │ X-Agent-Key               │
   Merchant A   Merchant B    Merchant C        ← implement SPEC.md
       └───────────┼───────────────┘
              Razorpay (test mode)
```

**Note where the LLM is: only at the top, and it is not ours.** Everything inside the
Layer and below it is deterministic code. That is a decision, not an omission — an LLM
placed in front of attacker-written product text would recreate the exact vulnerability
this project exists to close.

---

## What makes a money action safe here

**Bounded.** The quantity ceiling (5 per line) and the autonomous-payment ceiling
(₹2,000) live in Layer code, not in a prompt. An agent convinced by a product description
to order fifty units is refused by an `if` statement that never read the description. An
agent asserting *"the user already approved this"* changes nothing — that sentence is one
the sanitizer itself matches as forged consent.

**Gated.** Orders are created **unpaid**, so the Layer sees the real total — shipping and
discount included — before deciding whether an agent may settle it. The payment
instrument is a required parameter with no default, so the agent has to make that choice
rather than have it derived. And before money moves, the agent must echo back what it is
buying and what it is about to pay; a mismatch refuses and nothing moves.

**Auditable.** Every tool call, policy decision, refusal, stripped payload and revocation
is appended to a log that SQLite triggers make impossible to edit. `scripts/audit.py trail
<order_id>` prints an order's whole path, including the cart calls that explain its total.

**Honest about its own limits.** The echo-back proves what the agent *stated*, not that a
human was asked — a consulted human and a skipped one produce byte-identical calls, and
the tool text says so. The per-order cap does not bound a *sequence* of legal orders;
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §7.4 states that gap plainly rather than
letting a reviewer find it.

---

## See the shops

| Shop | Built with | Storefront |
|---|---|---|
| Northwind Apparel | FastAPI · Python · SQLite · Jinja2 | **[northwind-apparel.vercel.app](https://northwind-apparel.vercel.app)** |
| Voltline Electronics | Express · Node · `node:sqlite` · template literals | **[voltline-electronics.vercel.app](https://voltline-electronics.vercel.app)** |
| Marigold Bazaar | Rust · `tiny_http` · `rusqlite` · no framework, no async runtime | **[marigold-bazaar.vercel.app](https://marigold-bazaar.vercel.app)** |

**Those three links are static snapshots of the shop fronts, and every page says so at the
top.** They exist so you can see what the shops look like without cloning anything. They
are not the running system and nothing there can be bought.

That distinction is deliberate rather than a shortcut, and the reason is worth stating.
Every merchant's money path writes to its own SQLite database — orders, stock reservation,
idempotency records. On a serverless host those writes do not survive: an order would be
created and the next read of it would 404, stock would never actually decrement, a retried
request would never be recognised as a duplicate. **The deployed merchants would fail their
own conformance suite**, which is precisely the thing this project asks to be judged on.
Publishing a storefront and saying so is honest; publishing something that looks like a
live merchant and is not would undo the one claim here that is proven rather than argued.

The live system — three servers, three databases, real Razorpay test-mode payments, the MCP
tool surface and the audit log — runs locally, in one command. That is the next section.

A couple of pages worth opening: [a product whose customer review carries a planted
prompt-injection payload](https://marigold-bazaar.vercel.app/p-mb-48.html), [the same
attack written as a plain instruction at another
shop](https://northwind-apparel.vercel.app/p-nw-86.html), and [one shop's grocery
aisle](https://marigold-bazaar.vercel.app/category-groceries.html). What an AI buyer
actually receives for those products — with the instruction-shaped lines removed, counted
and logged — is what `demo/attacks/run_attacks.py` shows.

---

## Running it

Everything below runs on your own machine. There is no hosted instance to trust and
nothing to sign up for.

### Prerequisites

Python 3.11+, Node 22.5+, and a Rust toolchain (the three merchants are deliberately
written in three different languages — that is the point, not an accident).

### From a clean clone

```bash
git clone <this repo> && cd agent-commerce-protocol

python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows
# .venv/bin/pip install -r requirements.txt                     # macOS / Linux

npm install --prefix merchants/voltline                         # one dependency: express
cp .env.example .env                                            # see the note below

# seed the three catalogues — the .db files are deliberately not committed
.venv/Scripts/python.exe merchants/northwind/seed.py
node merchants/voltline/seed.mjs
cargo run --release --manifest-path merchants/marigold/Cargo.toml -- seed

# start all three shops, each in its own visible window, and prove each answered
powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
#    Northwind Apparel    — FastAPI + Python    http://127.0.0.1:8001/
#    Voltline Electronics — Express  + Node     http://127.0.0.1:8002/
#    Marigold Bazaar      — Rust, no framework  http://127.0.0.1:8003/

.venv/Scripts/python.exe layer/sync.py          # fill the Layer's search index
.venv/Scripts/python.exe scripts/readiness.py   # is this ready to be driven right now?
```

`serve.ps1` builds the Rust merchant, then waits for each shop to return a bare `401`
without a key. An open port is not proof — it has lied twice in this project's history.

### On macOS or Linux

This was built on Windows, so the commands above name `.venv/Scripts/python.exe` and a
PowerShell script. Two substitutions cover everything: use **`.venv/bin/python`**
throughout, and since `serve.ps1` is the only Windows-only file here, start the three
shops yourself — one terminal each, so a shop that dies is visible rather than inferred:

```bash
.venv/bin/python -m uvicorn merchants.northwind.main:app --port 8001
node merchants/voltline/main.mjs
cargo run --release --manifest-path merchants/marigold/Cargo.toml
```

Then check each one the way `serve.ps1` does — the proof is the answer, not the port:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/agent/manifest   # expect 401
```

Nothing else is platform-specific. The test suites, the conformance runner, the Layer and
`readiness.py` all run unchanged.

### You do not need a Razorpay account

**Measured, with every Razorpay variable left empty: `55 passed, 3 skipped` of the 58
conformance tests, per merchant.** The three that skip say so by name — one is structural
(all three merchants offer all three payment modes, so "an undeclared payment mode" has no
case left to test) and two are the payment-instrument tests, which report
`RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET set nahi hain` rather than passing quietly.

Everything else runs without credentials: all three shops, cross-merchant search, live
product detail, carts, both money ceilings, the prompt-injection sanitizer, the audit log,
and order creation in `cod` mode. What needs keys is the part where money actually moves.

There are six variables, and `.env.example` carries this table with the reasoning:

| Variable | What it buys | Without it |
|---|---|---|
| `NORTHWIND_AGENT_KEY` | The shared secret Northwind authenticates the Layer with (`SPEC.md` §2.5) | That shop answers `401` to everything; search and orders skip it |
| `VOLTLINE_AGENT_KEY` | The same, for Voltline | Same, for Voltline |
| `MARIGOLD_AGENT_KEY` | The same, for Marigold | Same, for Marigold |
| `RAZORPAY_KEY_ID` | Test-mode payment links, checkout orders and refunds | Orders still create; only the steps where money moves are skipped, by name |
| `RAZORPAY_KEY_SECRET` | The other half of the same credential | Same |
| `OPENROUTER_API_KEY` | `demo/buyer.py` — a real LLM driving the tool surface | Everything else runs; only that one script needs it |

The three merchant keys are **not secrets in any real sense** — they are shared between
your Layer and your own merchants, so any string works locally as long as the merchant and
the Layer are started with the same one. `.env.example` ships with working placeholder
values for exactly that reason. The Razorpay pair is a real credential and is the only
thing here that must be kept; it is test-mode only, and a merchant returns the publishable
key id and never the secret.

### The things worth running

```bash
# the whole suite — 246 tests across the Layer, the spec and three merchants
.venv/Scripts/python.exe -m pytest

# the conformance suite: ONE suite, every registered merchant, unedited
.venv/Scripts/python.exe scripts/conformance_all.py

# three attacks, each blocked and logged
.venv/Scripts/python.exe demo/attacks/run_attacks.py

# a real MCP client driving the Layer over stdio, end to end
.venv/Scripts/python.exe scripts/mcp_smoke.py

# a real LLM buying through the MCP tools          (needs OPENROUTER_API_KEY)
.venv/Scripts/python.exe demo/buyer.py --scenario decided     # all chosen -> full purchase
.venv/Scripts/python.exe demo/buyer.py --scenario bulk        # 10 units -> both ceilings fire
.venv/Scripts/python.exe demo/buyer.py --scenario injected    # merchant A's payload
.venv/Scripts/python.exe demo/buyer.py --scenario injected_b  # merchant B's subtler one

# read the audit trail of any order, and the metrics
.venv/Scripts/python.exe scripts/audit.py trail <order_id>
.venv/Scripts/python.exe scripts/audit.py metrics
```

### Driving it from a real AI client

Claude Code picks the Layer up from the repository's own `.mcp.json` — check with
`claude mcp list`. Claude Desktop reads its own config file and needs absolute paths.
This surface has been driven from cold by two outside AI buyers with no access to this
repository — one running an adversarial audit, one shopping ordinarily. What they found,
and what was rejected as wrong, is in [`docs/WHAT_BROKE.md`](docs/WHAT_BROKE.md).

Keys never appear in the repository. `layer/merchants.json` stores the **name** of the
environment variable that carries a merchant key, never the value.

---

## Where the numbers come from

Every number in this repository was produced by running something. Measured on
2026-09-04:

| | |
|---|---|
| Test suite | **246 tests** — 58 conformance · 158 layer · 6 + 11 + 13 merchant. A run has no failures; the passed/skipped split moves with Razorpay, and two runs an hour apart today gave `243 passed, 3 skipped` and `244 passed, 2 skipped` — which is exactly why the count is not the thing to read. One skip is structural (every merchant offers all three payment modes, so "undeclared mode" has no case left); the rest appear when Razorpay rate-limits payment-link creation, which it does after a day of demo runs. Read skips by name (`-rs`), not by count |
| Conformance across stacks | The same suite, **unedited**, against all three merchants: 3 of 3 conformant, `57 passed, 1 skipped` each while Razorpay is answering and `56, 2` once it starts rate-limiting link creation — both were seen within an hour today. The third merchant needed **no change to the specification at all** — the second one had moved it from 1.7 to 1.8 |
| MCP tools | **13** |
| Merchants | **3**, sharing no line of code — Python, JavaScript and **Rust** — 225 products, 612 variants, three planted injection payloads of different kinds, one of them in a customer review rather than a description |
| Cross-merchant prices | Ten products are in stock at **all three** shops. `nw-94` / `vl-94` / `mb-94`, one Longines Master Collection, is ₹60,000, ₹77,999 and ₹39,000. The difference runs in every direction on purpose. The ids are named because an auditor once compared a *different* Rolex, found other numbers, and reported this row as stale |
| Search quality | 20 of 20 natural-phrase queries answered (16 of 20 before that work) |
| Typo correction | Three signals: similarity ≥ 0.80, at most 2 unmatched characters, and the correction may only *add* letters. Each was forced by a new merchant arriving — the cutoff alone stopped separating at two shops (`tractor`→`traction` scores exactly 0.800, as does the real typo `shrit`), and at three, `pink` began correcting to `pin` because one shop sells a rolling pin. Measured after the fix: 10 of 10 real typos corrected, 0 of 7 absent words |
| Correction dictionary | 490 terms across three shops, built from titles, brands, categories, tags and variant options — never from `description`, which is attacker-controlled |
| Sanitizer false positives | **8 lines removed out of 8,273** scanned across all three catalogs — every string field of every product-detail payload, descriptions, attributes and reviews included. All eight were on the three planted products; **zero** honest lines were touched |
| Break verification | Every new rule is verified by breaking it and reading the result *by name*, not by count — a green suite that has never been seen to fail is not evidence. On three separate occasions a break appeared *uncaught* and the fault turned out to be the harness rather than the test; [`docs/WHAT_BROKE.md`](docs/WHAT_BROKE.md) records those, because they are the ones worth knowing about |
| Autonomous purchase | 5 tool calls on `gpt-4o-mini` — $0.0019 without the product photograph, $0.0116 with it, since the image is re-sent with the whole history each turn. The same purchase completes on `llama-3.1-8b`, which cannot take images at all |

Real Razorpay test-mode money moves: an order below the ceiling is captured and refunded
by the Layer with no human and no browser anywhere in the path — one deterministic HTTP
call carrying only a publishable key.

**The framework-agnostic claim is the one thing here that is proven rather than argued.**
Merchant A is FastAPI, Python, `sqlite3`, httpx, Pydantic and Jinja2. Merchant B is
Express, Node, `node:sqlite` and `fetch`, with `express` as its only runtime dependency.
Merchant C is **Rust** — `tiny_http`, `rusqlite`, `ureq` — with no web framework, no async
runtime, no ORM, no template engine and no date library; its router is a `match` statement
and its RFC 3339 formatting is sixty hand-written lines with their own test.

They share no line of code — not a database driver, an HTTP client, a model layer or a
template engine. They share `docs/SPEC.md`, and the same Python conformance suite passes
against all three with no edit at all.

The third one is the one that tests the claim rather than repeating it. A and B are both
dynamically typed, so "two languages" was really one kind of language: a spec loose enough
to be ambiguous can still be papered over by an object that flows through untyped. Rust has
to name every field it reads and writes. Nothing had to change — including, for the first
time, the specification itself.

---

## Documents

| File | For |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | A third-party merchant developer. **Self-contained** — implement from this alone |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | A reviewer: every design choice with the mechanism behind it, and the failures that produced it |
| [`docs/WHAT_BROKE.md`](docs/WHAT_BROKE.md) | Anyone asking whether this was actually built or merely assembled: the failures that shaped it, each with its mechanism |

The specification has been through nine revisions, and **every one of them came from
something breaking**: `updated_at` monotonicity (a change went permanently invisible to
delta sync), an advancing cursor (a catalog silently indexed half), a payable checkout
instrument, an order expiry that leaked stock, a refund reported as complete while it was
still pending, a delivery date answered two different ways in one response, one photograph
claiming to be two different variants, and — found by an outside AI buyer — a phone number
that was present, unusable, and paid for anyway. The changelog in `SPEC.md` §13 names the
failure behind each.

## Deliberately out of scope

Discovery registries, platform adapters, webhooks, multi-currency, post-delivery returns,
coupon discovery, and a single cart spanning two merchants — each with its reason and what
would change, in `ARCHITECTURE.md` §11. Two shops means two orders, and that is stated to
the agent rather than left for it to discover.

## Status

All three merchants — A (FastAPI + Python), B (Express + Node) and C (Rust, no
framework) — are complete and conformant. **The same conformance suite, unchanged, is the
gate, and all three pass it — 3 of 3.** That is where the
framework-agnostic claim was either proven or exposed, and it is the one claim here that is
proven rather than argued.

Merchant C also settled something the first two could not. Both of them refuse deliveries
to the same region, so "one shop said no, the buyer went to another" was written down as a
demo and **could not actually happen** — and no conformance test could have revealed it,
because conformance examines one merchant at a time. Marigold delivers where its neighbours
do not.

Two outside AI buyers have since driven the whole surface adversarially, with no access
to this repository — an adversarial audit and a cold first-time buyer. Every finding they
produced has been reproduced, then fixed or rejected with its reason — including the one
that turned out to be wrong, and what ambiguity of ours caused it.
