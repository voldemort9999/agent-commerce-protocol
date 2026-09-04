# What broke, and how we got out

This project was built over roughly two weeks, and almost everything it does correctly
today it does because of a specific failure. This document is the short version of that
record: the failures worth knowing about, each with the mechanism behind it.

They are collected here for one reason. **Nearly every failure in this list was silent.**
Nothing errored, no test went red, and the system kept answering — which is the only
failure mode that actually matters on a path where money moves.

---

## The ones that were invisible by design

**A change to a product became permanently invisible to the search index.**
Delta sync asks a merchant for everything changed since a watermark, using a *strictly
greater* comparison. The first rule we wrote said each product's timestamp must advance
past its own previous value. That is not enough: a product bumped repeatedly drifts ahead
of the wall clock, the watermark follows it, and then some *other* product changes for the
first time in months, honestly stamps the current time, and lands **behind** the watermark
— so it is never indexed again. Measured on a deliberately weakened merchant: `11:18:17Z`
against a store maximum of `11:18:19Z`, two seconds behind and gone forever. The rule is
now store-wide monotonicity (`SPEC.md` §4), and the gap it closes is as large as the
busiest product's drift, not one second.

**A merchant could publish half its catalog with no error anywhere.**
The specification never actually required a cursor alongside `has_more: true`. A merchant
returning `true` with no cursor leaves the Layer either looping on one page forever or
stopping early and indexing a partial catalog. Neither raises anything. `SPEC.md` v1.2.

**Every abandoned checkout permanently removed stock from a catalog.**
Stock is reserved when an order is created, and we had written a rule that expired unpaid
orders — resting it on whatever expiry the payment provider offered. A hosted payment link
has one; a plain provider order id has none at all. So the one mode an agent actually pays
through held its stock forever. The rule that was written to prevent inventory leaking did
not cover the path where it leaked. The expiry now belongs to the *order*, and the provider
is asked only whether it was paid before that clock ran out (`SPEC.md` v1.5).

**A refund that had been sent was reported as a refund that had arrived.**
`payment.state` had a single word for both. Our own merchant wrote `refunded` at
cancellation while Razorpay still reported `pending` with an expected date five days out.
An AI buyer reads the order, not the cancellation response that has scrolled past, and
tells the customer their money is back. Observed once, three seconds apart. `SPEC.md` v1.6
adds `refund_pending`.

**A phone number that was present, unusable, and paid for anyway.**
Found by an outside AI buyer driving the order path. One reference merchant accepted
`phone: "98765"`; the other accepted `phone: "1"` together with `country: "ZZ"`. Both
created the order, and below the autonomous ceiling that order is then settled with real
money. Both merchants were checking presence, which is exactly what the spec had asked
for. The failure surfaces days later at a warehouse, on a parcel nobody can dispatch and a
customer nobody can call. `SPEC.md` v1.9: present is not the same as valid.

---

## The ones where our own tests were lying

**A test had been silently skipping for weeks, and skips look green.**
The payment-instrument test began with "if the provider returns 5xx, skip". The merchant
was mapping every provider problem to `500`, so that test had never once run. The moment
the provider's amount ceiling was mapped correctly to `409`, it failed immediately — and
took a second bug down with it.

**A guard whose regex could never match.**
A source-level test was supposed to fail if merchant JSON was parsed outside one function.
A patch script wrote a literal backspace character (`0x08`) into the pattern where `\b`
was intended, so the regex required a backspace before every match and never fired. It was
green while the code it guarded was broken. It was caught only because a break-verification
run printed `1 failed, 1 passed` and someone asked *which one passed*.

**A break that was never applied, reported as "the test did not catch it".**
Three separate times, across three sessions, a break appeared uncaught and the fault was
the harness, not the test: once a server restart raced ahead of the next case, once a
shell-quoting bug meant `pytest -k` selected nothing, once the conformance runner was not
passing the merchant's key and every test errored on a `401`. Corrected by hand, every one
of them was caught. *A break that was never applied is not evidence.*

**A measurement that went stale without anyone touching it.**
The sanitizer's false-positive scan counted regex hits per line. Then redaction was widened
from the line to the *sentence*, so a line could be removed with no pattern on it at all —
and the old scan could not see those. Its reassuring "zero false positives" had quietly
stopped measuring the thing that happens. Re-measured on what is actually removed:
**8,273 lines scanned across three catalogs, 8 removed, all eight on the three planted
products, zero honest lines touched.**

**`pkill` returns success on Windows without killing anything.**
An entire experiment was invalidated by this. Merchant code was deliberately broken, the
server was "restarted", the test passed five times out of five, and a finding was reported
— while the old, correct code was still running the whole time. The kill is now verified
(`netstat` → `taskkill`), and the proof that a server is up is a real `401`, never an open
port.

---

## The ones that were nobody's bug

**The Layer was instructing the agent to spend.**
Someone asked it to find a shirt and it completed a payment without asking anything. Every
ceiling held, the sanitizer ran, the audit log recorded it correctly — no safety code
failed. The cause was one line of our own text: `create_order` answered with *"Call
pay_order — this total is below the ceiling and the Layer can settle it without a human."*
Removing that sentence changed the behaviour completely. **A default and a hint are the
same kind of thing: both decide on the agent's behalf.** No test could have caught this,
because every call was correct.

**A promise written in a design document that no code kept.**
Three times. The specification told every merchant *"you may return 429 with Retry-After;
the Layer honours it"* — and nothing in the Layer read that header, so a merchant obeying
the contract exactly got a failed order and the fault was ours. The architecture document
described a periodic health check that no running code ever called, so a merchant that
blinked once stayed invisible forever. And it promised merchants they would deal with a
single caller, while nothing bounded how often the Layer called them. Writing it down is
not building it.

**Retrying a payment destroyed a good order.**
Calling `pay_order` a second time on an order that was already paid fired a doomed debit,
and the provider's refusal was turned into a hardcoded sentence: *"the order is still
unpaid and its stock is still reserved; cancel it and create it again."* Four false claims
in one message, and advice that makes an agent cancel a paid order and buy a second one.
Retrying after a timeout is the most ordinary thing an agent does.

**A third shop broke the search's spelling correction, exactly as predicted.**
Typo correction ran at a similarity cutoff measured against one catalog. At two shops
`tractor`→`traction` scored **0.800** — identical to the real typo `shrit`→`shirt` —
because the score is length-normalised and bigger catalogs have longer words. A second,
absolute signal fixed it. Then a third shop arrived selling a rolling pin, and `pink`
began correcting to `pin` at ratio 0.857 with one unmatched character, clearing both
signals. Correction *rewrites what the customer asked for*, so a wrong one travels all the
way to a person. **A number measured against one catalog is stale the moment a new merchant
arrives** — this happened three times, and the open question had named it in advance.

**A word in a product description became a search target.**
`thing` — an ordinary English word — started correcting to `hinge`, which appears in one
laptop's description. Descriptions are attacker-controlled, so any merchant could have
written any word into its copy and bent nearby queries toward its own products. The
correction dictionary is now built only from titles, brands, categories, tags and variant
options. The cost was measured rather than waved away: 750 dictionary terms down to 244,
and one real typo in ten stops being corrected and is reported instead.

**Re-seeding a catalog silently defeated delta sync.**
Seed scripts stamp timestamps from a base thirty days in the past on purpose. After a
reseed every timestamp therefore sits *behind* the Layer's watermark, the next sync
truthfully reports zero rows fetched, and the index keeps serving pre-reseed prices and
stock. Nothing errors. There is now an explicit `sync.py --full`, because guessing would
put a rewind path into the one mechanism whose correctness depends on never going
backwards.

---

## Two that cannot be undone, and are recorded for that reason

**A probe polluted the audit log permanently.** Testing whether two Layer processes could
share one database, 5,321 rows were written to the real audit table. That table has SQLite
triggers that abort every `UPDATE` and `DELETE` — which is a guarantee this project
deliberately built, and it applies to our own mistakes too. Those rows are there forever.
Every reported metric filters by tool and decision, so none of them became false; the
lesson cost nothing else and is worth stating: *never probe an append-only store on the
real database.*

**A demo was nearly lost to a currency conversion.** Converting the source catalog at the
real exchange rate put only 6 of 49 products below the ₹2,000 autonomous ceiling — meaning
the entire below-the-cap flow would have been demonstrated on six items. The multiplier is
a demo calibration constant, not a currency rate, and it is commented as such. Measured at
the chosen value: 22 of 49.

---

## The pattern underneath all of it

Every failure above is the same shape. **The system kept answering.** No exception was
raised, no test went red, and the wrong behaviour was indistinguishable from the right one
from the outside — a skipped test looks like a passing test, a regex that never matches
looks like a regex that finds nothing, a merchant with no cursor looks like a merchant with
a short catalog, and an agent that was never asked to confirm looks exactly like one that
was.

That is why the ceilings in this project are `if` statements that read no text, why the
audit log is append-only at the database level rather than by convention, why every new
rule is verified by breaking it and reading the result *by name* rather than by count, and
why the specification says *why* a rule is not optional instead of only stating it.

A green test suite that has never been observed to fail is not evidence of anything.
