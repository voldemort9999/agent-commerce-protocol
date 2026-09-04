"""Layer ka store: merchant registry, search index, agent tokens.

Yahan sirf wo rehta hai jise Layer *dhoondh aur dikha* sakti hai. Jis cheez pe paisa
verify hota hai — live price, live stock — wo yahan kabhi nahi aati (D-10). Index galat
ho sakta hai; money path nahi.
"""
import difflib
import json
import pathlib
import re
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "layer.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
  merchant_id     TEXT PRIMARY KEY,
  name            TEXT,
  base_url        TEXT NOT NULL,
  key_env         TEXT NOT NULL,      -- ENV ka naam, key khud nahi
  currency        TEXT,
  categories      TEXT NOT NULL DEFAULT '[]',
  payment_modes   TEXT NOT NULL DEFAULT '[]',
  shipping        TEXT NOT NULL DEFAULT '{}',
  policies        TEXT NOT NULL DEFAULT '{}',
  product_count   INTEGER,
  healthy         INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  last_checked_at TEXT,
  watermark       TEXT                -- aakhri kaamyaab sync ka max(updated_at)
);

CREATE TABLE IF NOT EXISTS products (
  id              INTEGER PRIMARY KEY,   -- products_fts.rowid isi se bandha hai
  merchant_id     TEXT NOT NULL REFERENCES merchants(merchant_id),
  product_id      TEXT NOT NULL,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL,
  category        TEXT,
  brand           TEXT,
  tags            TEXT NOT NULL DEFAULT '[]',
  images          TEXT NOT NULL DEFAULT '[]',
  price_min_paise INTEGER NOT NULL,
  price_max_paise INTEGER NOT NULL,
  in_stock        INTEGER NOT NULL,
  variant_options TEXT NOT NULL DEFAULT '[]',
  variant_count   INTEGER NOT NULL,
  rating_avg      REAL,
  rating_count    INTEGER,
  updated_at      TEXT NOT NULL,
  synced_at       TEXT NOT NULL,
  UNIQUE (merchant_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_products_price ON products(price_min_paise);
CREATE INDEX IF NOT EXISTS idx_products_merchant ON products(merchant_id);

-- Saada FTS5 table, external-content NAHI. External content me ya to triggers chahiye
-- ya 'delete' command me purani saari values dobara deni padti hain. 49-150 rows pe
-- text ki doosri copy kuch bhi nahi hai, aur delete seedha rowid se ho jata hai.
--
-- `options` column me har variant ka har option value jata hai (Maroon, Cream, XL, 9...).
-- Ye 5.10 me juda: pehle colour aur size index me the hi nahi, sirf `products` ki JSON
-- column me pade the — yaani `maroon shirt` ya `navy corset` search **ho hi nahi sakte
-- the**, aur `red shirt` chaar aise shirt lauta ta tha jinme se ek bhi red nahi tha.
CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
  title, description, tags, brand, category, options
);

-- Index ke apne shabdon ki suchi. `fts5vocab` FTS5 ke andar ka term list padhta hai, to
-- ye poore catalog ka scan nahi hai. Do kaam: (1) ye batana ki koi shabd index me hai bhi
-- ya nahi, (2) typo pe sabse kareebi ASLI shabd dhoondhna — isliye yahan stemmer nahi
-- lagaya gaya, warna `did_you_mean` "sunglas" bolta, "sunglasses" nahi.
CREATE VIRTUAL TABLE IF NOT EXISTS products_vocab USING fts5vocab(products_fts, row);

-- Wahi shabd, par column ke saath. Ye isliye chahiye ki **"shabd index me hai kya"** aur
-- **"typo kis shabd me sudhre"** do alag sawaal hain, aur inka jawab ek nahi ho sakta.
-- Poori kahani `correction_vocabulary()` me likhi hai.
CREATE VIRTUAL TABLE IF NOT EXISTS products_vocab_col USING fts5vocab(products_fts, col);

-- Layer ke apne order records. Ye cart nahi hai - cart ka khona recoverable hai, ye nahi.
-- Yahan wo cheez rehti hai jo merchant ke paas hai hi nahi: kis agent ne order banaya
-- aur us par kya policy faisla hua. Session 5 ka audit log isi ke upar khada hoga.
CREATE TABLE IF NOT EXISTS layer_orders (
  order_id          TEXT PRIMARY KEY,
  merchant_id       TEXT NOT NULL REFERENCES merchants(merchant_id),
  agent_token       TEXT NOT NULL,
  final_total_paise INTEGER NOT NULL,
  payment           TEXT NOT NULL DEFAULT '{}',
  decision          TEXT NOT NULL DEFAULT '{}',
  status            TEXT NOT NULL,
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  token      TEXT PRIMARY KEY,
  label      TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);

-- Har tool call, har policy faisla, har stripped payload. Isi se "every money action
-- explainable" ek dawa se system ki property banti hai.
CREATE TABLE IF NOT EXISTS audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at           TEXT NOT NULL,
  agent_token  TEXT,
  merchant_id  TEXT,
  tool         TEXT NOT NULL,
  arguments    TEXT NOT NULL DEFAULT '{}',
  decision     TEXT NOT NULL,          -- allow | block
  reason       TEXT,
  amount_paise INTEGER,
  order_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit(agent_token);
CREATE INDEX IF NOT EXISTS idx_audit_order ON audit(order_id);

-- Append-only ek dawa nahi, ek constraint hai. Bina inke "append-only" ka matlab sirf
-- itna hota ki abhi tak kisi ne UPDATE nahi likha - aur audit log ki poori keemat isi
-- baat me hai ki jo ho chuka usse badla na ja sake. SQLite yahan hi mana kar deta hai,
-- to Layer ka koi bhi naya code galti se bhi record nahi badal sakta.
CREATE TRIGGER IF NOT EXISTS audit_is_append_only_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_is_append_only_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""

# title sabse bhaari, description sabse halki. Description attacker-controlled hai
# (SPEC 10) - use ranking me zyada vazan dena matlab hamlavar ko ranking ka control dena.
# `options` title ke baad sabse bhaari hai: rang aur size wahi do cheezein hain jinpe
# insaan sach me chunta hai, aur wo merchant ke structured data se aati hain, prose se nahi.
FTS_COLUMNS = ["title", "description", "tags", "brand", "category", "options"]
BM25_WEIGHTS = (10.0, 1.0, 5.0, 3.0, 2.0, 6.0)


def connect(path=DB_PATH):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    rebuild_fts_if_stale(conn)
    return conn


def rebuild_fts_if_stale(conn):
    """FTS ka aakar badla ho to use dobara banao.

    `CREATE VIRTUAL TABLE IF NOT EXISTS` purane table ko chup-chaap rehne deta hai — yaani
    ek naya column jodne pe koi error nahi aata, index bas ek column kam se chalta rehta
    hai aur us column pe koi search kabhi kuch nahi lauta ti. Ye theek wahi shakal hai jo
    is project me baar-baar mili hai: **kuch toota nahi, sirf chup-chaap kam kaam hua.**

    Rebuild surakshit hai kyunki har byte `products` me maujood hai — FTS yahan doosri
    copy hai, sach nahi. 49 rows pe ye milliseconds ka kaam hai.
    """
    have = [r[1] for r in conn.execute("PRAGMA table_info(products_fts)")]
    if have == FTS_COLUMNS:
        return 0
    conn.execute("DROP TABLE IF EXISTS products_vocab")
    conn.execute("DROP TABLE IF EXISTS products_vocab_col")
    conn.execute("DROP TABLE IF EXISTS products_fts")
    conn.executescript(SCHEMA)
    return reindex_all(conn)


def reindex_all(conn):
    """Har product ki FTS row `products` se dobara likho.

    Rebuild ke alawa iska ek aur kaam hai: ise bulakar koi bhi test **is session ke
    code se** index banwa sakta hai. Bina uske indexing ka test purane pade hue index
    par khada rehta hai — maine ye khud dekha, break-verify me: `index_product_text` se
    variant options hata dene par bhi colour-search ka test **green raha**, kyunki
    index me options pehle se likhe hue the. Wo test code ko nahi, kal ke index ko naap
    raha tha.
    """
    rows = conn.execute("SELECT id, title, description, tags, brand, category,"
                        " variant_options FROM products").fetchall()
    for r in rows:
        index_product_text(conn, r["id"], r["title"], r["description"], jl(r["tags"], []),
                           r["brand"], r["category"], jl(r["variant_options"], []))
    return len(rows)


def option_text(variant_options):
    """Har variant option ki saari values ek line me. Option ke NAAM (`Size`, `Color`)
    jaan-boojhkar nahi jate: `color` shabd har product pe match karta aur ek bhi cheez
    alag nahi karta — wo signal nahi, shor hai."""
    return " ".join(str(v) for opt in (variant_options or [])
                    for v in (opt.get("values") or []))


def index_product_text(conn, row_id, title, description, tags, brand, category, options):
    """FTS row ek hi jagah se likhti hai — upsert aur rebuild dono yahin se guzarte hain,
    warna dono me se ek `options` bhoolne wala hai aur wo bhool chup-chaap hoti hai."""
    conn.execute("DELETE FROM products_fts WHERE rowid = ?", (row_id,))
    conn.execute(
        "INSERT INTO products_fts (rowid, title, description, tags, brand, category,"
        " options) VALUES (?,?,?,?,?,?,?)",
        (row_id, title, description, " ".join(tags or []), brand or "", category or "",
         option_text(options)))


def jl(value, default=None):
    return default if value is None else json.loads(value)


def jd(value):
    """`default=str` ek backstop hai, sajawat nahi.

    Naapa hua bug (5.8): `create_order` ke `contact` ko typed Pydantic model banaya, aur
    audit writer ne us model ko `json.dumps` karne ki koshish ki — `TypeError`, aur us
    exception ne **poori tool call gira di**. Yaani ek logging ki dikkat ne ek paise wale
    raaste ko maar diya, aur agent ko `Error executing tool create_order` mila jiska
    asli wajah se koi lena-dena nahi tha.

    Audit ka kaam record rakhna hai, kaam rokna nahi. Jo cheez seedha JSON na bane, wo
    `str()` bankar log me jaye — adhoora record poore blackout se behtar hai, aur galat
    blackout se to bahut behtar.
    """
    return json.dumps(value, ensure_ascii=False, default=str)


# Wo shabd jo ya to har product me milte hain ya kisi cheez ko doosri se alag nahi karte.
# Inhe hatana do wajah se zaroori hai, aur doosri wajah pehli se badi hai:
#   1. `for`, `and`, `with` sach me index me maujood hain (description me), yaani wo
#      SAB kuch match karte hain aur ranking ko patla kar dete hain.
#   2. Coverage ki ginti me har shabd ek "concept" hai. `mujhe ek shirt chahiye` me agar
#      `mujhe` aur `chahiye` bhi concept gine jayen to asli concept (`shirt`) ka vazan
#      teen guna gir jata hai.
# Yahan sirf wahi shabd hain jo kisi catalog me kabhi khareedne layak cheez nahi hote.
# `cheap`, `best`, `latest` jaan-boojhkar NAHI hain — wo match na hon to `unmatched_terms`
# me jaate hain, aur agent ko wo batana ki "ye shabd search nahi kar sakta, `sort_by`
# istemaal karo" chup-chaap gira dene se kahin behtar hai.
STOPWORDS = frozenset("""
a an the and or of for with to in on at as by from is are was were be been being
it its this that these those there here i me my mine we our us you your he she they
them their who what which when where how
mujhe mera meri mere hume hamein humein ek koi kuch chahiye chaiye chahie dikhao dikha
dedo karo kar ko ka ki ke wala wali wale hai ho hain hun tha thi bhi to na nahi haan
please plz show find get give want need looking search buy purchase order
under below above over between around near about upto within than less more only just
thing things stuff item items something anything no not such
""".split())

# `products_vocab` har search pe padha jata hai. 407 terms pe ye kuch bhi nahi.
# ponytail: lakhon rows pe ise cache karo, invalidate max(synced_at) pe.
MIN_CORRECTION_LENGTH = 4      # 3 akshar ke shabd me har typo doosra asli shabd hota hai
# Naapa gaya, chuna nahi — aur DO baar naapa gaya, kyunki ek hi merchant par liya gaya
# naap doosra merchant aate hi purana ho gaya.
#
# 5.10 me, Northwind ke akele 407 terms par: asli typo 0.80-0.95, aur gair-maujood
# shabd 0.75 ya neeche. 0.80 dono ko saaf alag karta tha.
#
# Voltline ke baad vocabulary **750 terms** ki hai, aur ye ab SACH NAHI hai:
#   tractor -> traction   0.800    <- Voltline ke aane se pehle wo shabd tha hi nahi
#   shrit   -> short      0.800    <- asli typo, theek usi score par
# Yaani ratio akela ab kaam nahi karta, aur ye ittefaq nahi hai: `ratio` lambai se
# normalise hota hai, to LAMBA shabd usi ratio par ZYADA asli galtiyan sambhal leta
# hai. Catalog badhne par lambe shabd badhte hain, aur har naya lamba shabd kisi
# gair-maujood cheez ko sanyog se pass kara sakta hai.
#
# Isliye doosra signal, aur wo jaan-boojhkar **absolute** hai: kitne akshar mile hi
# nahi. Ek typo ungli ki ek-do phisalan hai — lambai badalne se wo baat nahi badalti.
#   asli typo (10/10)        unmatched 1 ya 2
#   tractor -> traction      unmatched 3   -> ruk gaya
#   saree, kids, pink        ratio 0.75 se neeche -> pehle hi ruk jate the
# Dono signal load-bearing hain: ratio chhote shabdon ke sanyog pakadta hai,
# unmatched lambe shabdon ke.
CORRECTION_CUTOFF = 0.80
CORRECTION_MAX_UNMATCHED = 2
CORRECTION_CANDIDATES = 5


def _unmatched_chars(a, b):
    """Kitne akshar dono taraf mile hi nahi — `ratio` ka bina-normalise kiya hua roop.

    `SequenceMatcher` ka ratio 2M/T hai, to T-2M wahi ginti hai bina lambai se bhaag
    diye. Iske liye koi nayi dependency nahi chahiye, aur na hi apna Levenshtein.
    """
    matcher = difflib.SequenceMatcher(None, a, b)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return len(a) + len(b) - 2 * matched


def _words(text):
    """Query ko saade tokens me todta hai. Yahan koi natural-language samajh NAHI hai —
    ye sirf shabd alag karta hai. Layer ke andar LLM kabhi nahi jata (D-04)."""
    return re.findall(r"[0-9a-z]+", (text or "").lower())


def vocabulary(conn):
    """Index ka har shabd, uske saath kitne products me hai.

    **Kram sthir hona zaroori hai.** Pehla roop `set` lauta ta tha, aur `shrit` ke liye
    `short` aur `shirt` dono ka score theek 0.800 nikla — yaani do barabar ke jawab, aur
    kaunsa milega wo set ke iteration order pe tha. Ek search engine jo ek hi query ka
    do alag jawab de sakta ho, wo debug bhi nahi kiya ja sakta.
    """
    return {r["term"]: r["doc"] for r in
            conn.execute("SELECT term, doc FROM products_vocab ORDER BY term")}


# Typo correction sirf in columns ke shabdon me hoti hai. `description` yahan JAAN-BOOJHKAR
# nahi hai.
CORRECTION_COLUMNS = ("title", "brand", "category", "tags", "options")


def correction_vocabulary(conn):
    """Wo shabd jinme ek typo sudhara ja sakta hai — pehchan wale columns se, description se nahi.

    **Correction user ki query ko BADAL deta hai.** Matching sirf ye poochhti hai ki shabd
    index me hai ya nahi; correction ek naya shabd chun kar user ke shabd ki jagah rakh
    deta hai, aur phir agent user se kehta hai *"maine `X` samjha"*. Yaani jo shabd
    dictionary me hai, wo query ka **target** ban sakta hai.

    `description` attacker-controlled hai (SPEC 10), aur D-133 isi wajah se use coverage
    ranking se bahar rakhta hai. Dictionary me uska rehna usse **zyada** khatarnak hai:
    ek merchant apni description me koi bhi shabd likh kar use ek correction target bana
    sakta hai, aur us shabd ke aas-paas ki har query apne aap uski taraf mud jayegi.

    Do merchants aane se pehle ye sirf ek dalil thi. Voltline ke baad ye chalne laga:
    `thing` (ek bilkul aam shabd) `hinge` me sudhar raha tha — kyunki `hinge` kisi ek
    laptop ki description me pada hai. Ek generic English shabd ek product term me badal
    gaya aur search ne ek natija de diya. D-135 ka usool yahin lagta hai: **khaali jawab
    agent ko rok deta hai, galat jawab insaan tak pahunch jata hai.**

    Keemat naapi gayi hai aur wo asli hai: dictionary 750 se **244** terms ki reh jati
    hai, aur das me se ek asli typo (`tablte`) ab sudharta nahi — wo `unmatched_terms`
    me chala jata hai, jahan agent use dekh kar bata sakta hai. Ek recall ka nuksaan
    jise surface bol deti hai, ek chup-chaap galat jawab se behtar hai.
    """
    placeholders = ",".join("?" * len(CORRECTION_COLUMNS))
    return {r["term"]: r["doc"] for r in conn.execute(
        f"SELECT term, SUM(doc) AS doc FROM products_vocab_col WHERE col IN ({placeholders})"
        " GROUP BY term ORDER BY term", CORRECTION_COLUMNS)}


def _squeeze(word):
    """Dohraye gaye akshar ek kar do: `shirtt` -> `shirt`, `bootle` -> `botle`."""
    out = []
    for ch in word:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def _addition_only(word, candidate):
    """Correction shabd me akshar JODKAR banti hai, ghatakar nahi — ek chhoot ke saath.

    Ye teesra signal hai, aur wo teesre merchant ne pakadwaya. Do signal (ratio 0.80,
    aur "kitne akshar mile hi nahi" <= 2) do dukaanon par kaafi the; teesri dukaan par
    `pink` -> `pin` **dono** paar kar gaya (ratio 0.857, unmatched 1) — kyunki Marigold
    ek *Rolling Pin* bechta hai, aur `pin` ek bilkul jayaz identity shabd hai.

    `pink` ek aam angrezi shabd hai jo in dukaanon me bikta nahi. Use `pin` bana dena
    theek wahi galti hai jo `thing` -> `hinge` thi: correction user ki query **badal**
    deta hai, aur phir agent kehta hai *"maine pink ko pin samjha"*.

    Mechanism jo dono ko alag karta hai: **ek typo aksar akshar chhod deta hai, ya ek
    akshar badal deta hai — wo shabd ko CHHOTA karke kisi doosre asli shabd par nahi le
    jata.** Naapa gaya: das me se das asli typo aise candidate par jate hain jo utna hi
    lamba ya lamba hai (`wach`->`watch`, `sunglases`->`sunglasses`, `shrit`->`shirt`).
    `pink`->`pin` aur `tablte`->`table` dono chhote candidate par jaate hain.

    Ek chhoot zaroori hai: dohra tha hua akshar (`shirtt` -> `shirt`) ek asli typo class
    hai aur wahan candidate chhota hota hi hai. Use `_squeeze` se alag pehchan liya jata
    hai — us soorat me chhota candidate manzoor hai, aur kisi aur soorat me nahi.
    """
    if len(candidate) >= len(word):
        return True
    return _squeeze(word) == candidate


def nearest_word(word, vocab):
    """Sabse kareebi ASLI shabd, aur barabari pe wo jo zyada products me hai.

    `shrit` par `short` aur `shirt` dono 0.800 pe hain. Doc-count se todna sirf
    deterministic nahi hai, wo sahi bhi hai: jo shabd catalog me zyada jagah hai wahi
    zyada sambhavit typo target hai.

    Teen signal lagte hain, aur teenon alag-alag naye merchant ne zaroori banaye:
    ratio (>= 0.80), kitne akshar mile hi nahi (<= 2), aur correction ka jodne wala
    hona (`_addition_only`).
    """
    close = [t for t in difflib.get_close_matches(word, vocab, n=CORRECTION_CANDIDATES,
                                                  cutoff=CORRECTION_CUTOFF)
             if _unmatched_chars(word, t) <= CORRECTION_MAX_UNMATCHED
             and _addition_only(word, t)]
    if not close:
        return None
    best = max(close, key=lambda t: (difflib.SequenceMatcher(None, word, t).ratio(),
                                     vocab[t], t))
    return best


def word_forms(word):
    """Ek shabd ke wo roop jo index me ho sakte hain — dono taraf singular/plural.

    FTS5 ka `porter` stemmer jaan-boojhkar nahi liya. Wo index me stem likhta hai, aur
    phir `products_vocab` asli shabd ki jagah stem deta — yaani typo pe `did_you_mean`
    "sunglas" bolta, "sunglasses" nahi. Ye chhota rule dono taraf chalta hai
    (`earrings`->`earring`, `shoe`->`shoes`) aur dictionary asli shabdon ki rehti hai.
    Jo roop index me nahi hai wo apne aap gir jata hai, isliye zyada roop banana
    khatarnak nahi hai.
    """
    forms = [word]
    if len(word) > 4 and word.endswith("ies"):
        forms.append(word[:-3] + "y")
    if len(word) > 3 and word.endswith("es"):
        forms.append(word[:-2])
    if len(word) > 2 and word.endswith("s"):
        forms.append(word[:-1])
    else:
        forms += [word + "s", word + "es"]
    return forms


def plan_query(conn, text):
    """Ek insaani vaakya ko FTS expression **aur ek imaandaar report** me badalta hai.

    Report hi is function ka asli maqsad hai. Pehle ye sirf tokens ko OR se jodta tha,
    aur nateeja ye tha ki `red shirt` chaar shirt lauta ta tha jinme se ek bhi red nahi
    tha — aur response me kahin nahi likha hota tha ki `red` kisi cheez se match hua hi
    nahi. Agent wo chaar user ko "red shirt" bolkar de deta tha. **Galti search ki nahi
    thi, chup rehne ki thi.**

    Chaar cheezein hoti hain, is kram me:
      * bekaar shabd hataye jate hain (STOPWORDS) — `ignored` me naam ke saath
      * har bache hue shabd ke roop dhoondhe jate hain (`shoes` <-> `shoe`)
      * jo shabd kisi roop me na mile, uska sabse kareebi ASLI shabd dhoondha jata hai
        (`shrit` -> `shirt`) — aur wo `corrected` me batakar kiya jata hai, chupke se nahi
      * do lagataar shabd jo judkar ek asli shabd bante hain (`sun glasses` ->
        `sunglasses`, `t shirt` -> `tshirt`) ek alag term ban jate hain
    """
    words = _words(text)
    if not words:
        return None
    vocab = vocabulary(conn)
    corrections = correction_vocabulary(conn)
    terms, ignored = [], []
    for word in words:
        if word in STOPWORDS or len(word) < 2:
            ignored.append(word)
            continue
        hits = [f for f in word_forms(word) if f in vocab]
        near = (nearest_word(word, corrections)
                if not hits and len(word) >= MIN_CORRECTION_LENGTH else None)
        if hits:
            terms.append({"word": word, "status": "matched", "matched_as": hits})
        elif near:
            terms.append({"word": word, "status": "corrected", "matched_as": [near],
                          "did_you_mean": near})
        else:
            terms.append({"word": word, "status": "unmatched", "matched_as": []})

    # Judne wale jode `words` par chalte hain, `terms` par nahi — `t-shirt` ka `t`
    # ek akshar ka hai aur upar gir chuka hota hai, par `t`+`shirt` = `tshirt` index me
    # sach me maujood hai (`Gigabyte Aorus Men Tshirt`).
    #
    # **Jo do shabd judkar ek asli shabd bante hain, wo coverage ke liye ek hi concept
    # hain — do nahi.** Ye chhoot gaya tha aur naapne pe iska nateeja ulta nikla:
    # `smart phone` teen concept banata tha (`smart`, `phone`, `smart phone`), aur koi
    # row bare `smart` aur `phone` ko alag-alag nahi rakhti, to
    # `results_matching_every_word` hamesha **0** aata tha — yaani surface ek asli
    # smartphone ke baare me agent se kehta tha *"no product here carries all of `smart`,
    # `phone`, `smart phone` together... Do not describe these as if they had the
    # attribute they did not match."* Aur nuksaan sirf us vaakya ka nahi tha: coverage
    # flat ho jane se **ranking bhi mar jati thi**, to top result ek phone *case* nikla.
    #
    # `subsumed` — hataya nahi — isliye ki `matched_as` ke forms MATCH expression me jaate
    # hain, aur wahan dono aadhe zaroori hain: ek product jiska title *"Smart Phone X"* ho
    # wo `smartphone` token rakhta hi nahi. Coverage se nikalna hai, recall se nahi.
    for first, second in zip(words, words[1:]):
        joined = first + second
        if joined in vocab and not any(t["word"] == joined for t in terms):
            # `word_forms` yahan bhi lagti hai, aur ye chhoot gaya tha. `smartphone` vocab
            # me sirf kisi description ki wajah se tha; catalog ka asli pehchan wala shabd
            # `smartphones` hai (category). Bina forms ke joined term coverage me kuch
            # match hi nahi karta tha — yaani join hone ke BAAWJOOD `every_word` 0 rehta.
            terms.append({"word": first + " " + second, "status": "matched",
                          "matched_as": [f for f in word_forms(joined) if f in vocab]})
            for half in (first, second):
                for t in terms:
                    if t["word"] == half:
                        t["subsumed"] = True

    forms = sorted({f for t in terms for f in t["matched_as"]})
    return {
        "terms": terms,
        "ignored_words": ignored,
        # FTS5 ka MATCH ek query language hai — `"`, `*`, `(`, `:`, `NEAR`, `OR` sabke
        # matlab hain, aur agent ka bheja `laptop (2024)` seedhe daal do to `fts5: syntax
        # error` aata hai aur search KHUD mar jati hai. Quote karne se har token data
        # ban jata hai, operator nahi.
        "match": " OR ".join('"' + f + '"' for f in forms) or None,
    }


def fts_query(text, conn=None):
    """Purana naam, ab planner ke upar. Sirf MATCH expression chahiye to yahi kaafi hai."""
    if conn is None:
        tokens = [t for t in _words(text) if len(t) > 1 and t not in STOPWORDS]
        return " OR ".join('"' + t + '"' for t in tokens) or None
    plan = plan_query(conn, text)
    return plan["match"] if plan else None


def upsert_product(conn, merchant_id, product, synced_at):
    """Ek catalog row index me daalta ya update karta hai. products.id sthir rehti hai,
    isliye products_fts.rowid bhi usi row pe bandhi rehti hai.

    Lauta ta hai `(row_id, changed)`. `changed` isliye chahiye ki Layer boundary second
    ki rows jaan-boojhkar dobara fetch karti hai (dekho sync.py) - to "kitni rows aayin"
    aur "kitni sach me badlin" do alag cheezein hain. Doosri wali hi asli sync signal hai.
    """
    previous = conn.execute(
        "SELECT updated_at FROM products WHERE merchant_id=? AND product_id=?",
        (merchant_id, product["product_id"])).fetchone()
    changed = previous is None or previous["updated_at"] != product["updated_at"]
    row = conn.execute(
        "INSERT INTO products (merchant_id, product_id, title, description, category,"
        " brand, tags, images, price_min_paise, price_max_paise, in_stock,"
        " variant_options, variant_count, rating_avg, rating_count, updated_at, synced_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(merchant_id, product_id) DO UPDATE SET"
        "   title=excluded.title, description=excluded.description,"
        "   category=excluded.category, brand=excluded.brand, tags=excluded.tags,"
        "   images=excluded.images, price_min_paise=excluded.price_min_paise,"
        "   price_max_paise=excluded.price_max_paise, in_stock=excluded.in_stock,"
        "   variant_options=excluded.variant_options,"
        "   variant_count=excluded.variant_count, rating_avg=excluded.rating_avg,"
        "   rating_count=excluded.rating_count, updated_at=excluded.updated_at,"
        "   synced_at=excluded.synced_at"
        " RETURNING id",
        (merchant_id, product["product_id"], product["title"], product["description"],
         product.get("category"), product.get("brand"), jd(product.get("tags") or []),
         jd(product.get("images") or []),
         product["price_range_paise"]["min"], product["price_range_paise"]["max"],
         1 if product["in_stock"] else 0, jd(product.get("variant_options") or []),
         product["variant_count"], product.get("rating_avg"), product.get("rating_count"),
         product["updated_at"], synced_at)).fetchone()
    row_id = row["id"]
    index_product_text(conn, row_id, product["title"], product["description"],
                       product.get("tags") or [], product.get("brand"),
                       product.get("category"), product.get("variant_options") or [])
    return row_id, changed


PRODUCT_COLUMNS = """
  p.merchant_id, m.name AS merchant_name, p.product_id, p.title, p.description,
  p.category, p.brand, p.tags, p.images, p.price_min_paise, p.price_max_paise,
  p.in_stock, p.variant_options, p.variant_count, p.rating_avg, p.rating_count,
  p.updated_at, p.synced_at
"""


def row_to_product(r):
    return {
        "merchant_id": r["merchant_id"], "merchant_name": r["merchant_name"],
        "product_id": r["product_id"], "title": r["title"],
        "description": r["description"], "category": r["category"], "brand": r["brand"],
        "tags": jl(r["tags"], []), "images": jl(r["images"], []),
        "price_range_paise": {"min": r["price_min_paise"], "max": r["price_max_paise"]},
        "in_stock": bool(r["in_stock"]), "variant_options": jl(r["variant_options"], []),
        "variant_count": r["variant_count"], "rating_avg": r["rating_avg"],
        "rating_count": r["rating_count"], "updated_at": r["updated_at"],
        "indexed_at": r["synced_at"],
    }


SORTS = {
    "relevance": None,                                  # query ho to bm25+coverage, warna rating
    "price_asc": "p.price_min_paise ASC, p.rating_avg DESC",
    "price_desc": "p.price_min_paise DESC, p.rating_avg DESC",
    "rating_desc": "p.rating_avg DESC, p.rating_count DESC",
    "newest": "p.updated_at DESC",
}

# Coverage ke liye kitne candidate uthayen. bm25 ranking ke baad hum inhe dobara
# chhaanto hain, to window limit se bada hona chahiye — warna wo row jo saare shabd
# match karti hai par bm25 me 21ve number pe hai, kabhi upar aa hi nahi sakti.
CANDIDATE_FACTOR = 6
CANDIDATE_CAP = 300


def identity_text(product):
    """Wo text jispe "kitne concept mile" gina jata hai.

    **`description` jaan-boojhkar isme NAHI hai.** Description attacker-controlled hai
    (SPEC 10). Agar coverage description padhti, to koi bhi merchant apni description me
    `red blue green shirt shoes watch` likh kar har query me top pe aa jata — yaani
    ranking ka control wahi likh deta jise ye poora project untrusted maanta hai.
    bm25 description ko dekhta hai par sabse halke vazan (1.0) pe, aur wo sirf
    tie-break karta hai. Coverage sirf un fields pe chalti hai jo cheez ki **pehchan**
    hain: naam, brand, category, tags, aur variant ke apne options.
    """
    return " ".join(_words(" ".join([
        product["title"] or "",
        " ".join(product.get("tags") or []),
        product.get("brand") or "",
        product.get("category") or "",
        option_text(product.get("variant_options") or []),
    ])))


def coverage_terms(terms):
    """Wo terms jinpe "saare shabd mile ya nahi" gina jata hai.

    Do cheezein bahar rehti hain. Jo shabd kahin match hi nahi hua (`matched_as` khaali)
    — use ginne se har row ka coverage barabar ghat jata aur ginti kuch batati hi nahi.
    Aur jo shabd apne padosi ke saath judkar ek asli shabd ban gaya (`subsumed`) — wo
    aadha hai, alag concept nahi.
    """
    return [t for t in terms if t["matched_as"] and not t.get("subsumed")]


def concepts_matched(product, terms):
    """Is product ne user ke kitne alag shabd sach me pooray kiye.

    Yahi wo ek cheez hai jo pehle missing thi. Sirf bm25 pe `red shirt` ka pehla natija
    ek **sneaker** tha (`Sports Sneakers Off White Red`) — usne ek shabd bahut mazbooti se
    match kiya tha. Insaan ke liye do me se do shabd match karna hamesha ek shabd ko
    zor se match karne se behtar hai, aur bm25 ye farq karta hi nahi: wo score jodta hai,
    concept ginta nahi.
    """
    haystack = " " + identity_text(product) + " "
    return sum(1 for t in terms
               if any((" " + f + " ") in haystack for f in t["matched_as"]))


def search(conn, query=None, min_price_paise=None, max_price_paise=None, category=None,
           merchant_id=None, in_stock_only=True, limit=20, sort_by="relevance"):
    """Saare healthy merchants pe cross-merchant search. Sirf results."""
    return search_with_explain(
        conn, query=query, min_price_paise=min_price_paise,
        max_price_paise=max_price_paise, category=category, merchant_id=merchant_id,
        in_stock_only=in_stock_only, limit=limit, sort_by=sort_by)[0]


def search_with_explain(conn, query=None, min_price_paise=None, max_price_paise=None,
                        category=None, merchant_id=None, in_stock_only=True, limit=20,
                        sort_by="relevance"):
    """Results **aur** ye ki wo results kaise bane. Do cheezein lauta ta hai.

    `explain` koi debug output nahi hai — wo response ka hissa hai, aur wahi F-1/F-2 ka
    asli ilaaj hai. Ek agent ko ye pata hona chahiye ki `red` kisi cheez se match nahi
    hua, ki `shrit` ko `shirt` padha gaya, aur ki `chahiye` gira diya gaya. In teeno me
    se ek bhi na batao to agent user se poore vishwas ke saath galat baat keh deta hai.

    Yahan bhi koi natural language "samjhi" nahi jaati: `under 2000` ko Layer nahi todti,
    wo `max_price_paise` parameter hai. Ye sirf shabdon ka milaan hai (D-04).
    """
    where = ["m.healthy = 1"]
    args = []
    plan = plan_query(conn, query)
    match = plan["match"] if plan else None
    sort_by = sort_by if sort_by in SORTS else "relevance"
    explicit_sort = SORTS.get(sort_by)

    if plan and not match:
        # Query di gayi thi aur uska EK BHI shabd index me nahi hai. Browse mode me girna
        # yahan sabse bura jawab hai: `iphone` par ek Rolex lauta na khaali lauta ne se
        # bura hai, kyunki agent use natija samajh kar user ko de deta hai. Khaali lauta o
        # aur `explain` me batao ki kyun.
        return [], explain(conn, query, plan, [], sort_by, {
            "merchant_id": merchant_id, "category": category,
            "min_price_paise": min_price_paise, "max_price_paise": max_price_paise,
            "in_stock_only": in_stock_only})

    if match:
        source = ("products_fts JOIN products p ON p.id = products_fts.rowid"
                  " JOIN merchants m ON m.merchant_id = p.merchant_id")
        where.append("products_fts MATCH ?")
        args.append(match)
        order = explicit_sort or ("bm25(products_fts, "
                                  + ", ".join(["?"] * len(BM25_WEIGHTS)) + ")")
        order_args = [] if explicit_sort else list(BM25_WEIGHTS)
    else:
        # Browse mode - koi query di hi nahi gayi. Relevance ka koi signal nahi, to rating.
        source = "products p JOIN merchants m ON m.merchant_id = p.merchant_id"
        order = explicit_sort or SORTS["rating_desc"]
        order_args = []

    if in_stock_only:
        where.append("p.in_stock = 1")
    if min_price_paise is not None:
        where.append("p.price_max_paise >= ?")
        args.append(min_price_paise)
    if max_price_paise is not None:
        # min pe filter karte hain: agar koi bhi variant budget me hai, product dikhna chahiye
        where.append("p.price_min_paise <= ?")
        args.append(max_price_paise)
    if category:
        where.append("p.category = ?")
        args.append(category)
    if merchant_id:
        where.append("p.merchant_id = ?")
        args.append(merchant_id)

    # Coverage se dobara chhaantna hai, to window se zyada candidate uthao.
    window = min(max(limit * CANDIDATE_FACTOR, limit), CANDIDATE_CAP) \
        if (match and sort_by == "relevance") else limit
    sql = ("SELECT " + PRODUCT_COLUMNS + " FROM " + source
           + " WHERE " + " AND ".join(where) + " ORDER BY " + order + " LIMIT ?")
    rows = [row_to_product(r) for r in
            conn.execute(sql, args + order_args + [window]).fetchall()]

    if match and sort_by == "relevance":
        useful = coverage_terms(plan["terms"])
        # Python ka sort **stable** hai, to barabar coverage wali rows apne bm25 kram me
        # hi rehti hain — yaani bm25 apne aap tie-break ban jata hai, bina kuch likhe.
        rows.sort(key=lambda r: -concepts_matched(r, useful))
        rows = rows[:limit]

    return rows, explain(conn, query, plan, rows, sort_by, {
        "merchant_id": merchant_id, "category": category,
        "min_price_paise": min_price_paise, "max_price_paise": max_price_paise,
        "in_stock_only": in_stock_only})


def id_shaped(query):
    """Query ek id jaisi dikhti hai ya nahi — ek hi token, aur usme dash/underscore/dot
    ya koi ank ho.

    Ye **maujoodgi** se alag sawaal hai, aur dono chahiye. `nw-86` query me daalna ek
    aam galti hai; par `zzzz-no-such-thing` bhi utni hi id jaisi dikhti hai aur wo
    catalog me hai hi nahi. Dono soorton me agent ko wahi ek baat batani hai — **id
    `query` me nahi jati, `get_product` me jati hai** — sirf doosri soorat me ye bhi
    kehna hai ki aisi koi id hai bhi nahi.
    """
    text = (query or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{2,64}", text)
                and re.search(r"[-_.]|\d", text))


def looks_like_a_product_id(conn, query):
    """Id jaisi dikhti hai **aur** sach me kisi product ki id hai."""
    if not id_shaped(query):
        return None
    row = conn.execute("SELECT merchant_id, product_id FROM products WHERE product_id=?",
                       ((query or "").strip(),)).fetchone()
    return dict(row) if row else None


BUDGET_WORDS = ("under", "below", "less", "cheaper", "max", "maximum", "upto", "up",
                "within", "budget", "rs", "rupees", "inr", "cheap")


def price_intent(query):
    """Query me ek aisa number hai jo budget jaisa lagta hai?

    Ye query ko **parse nahi karta aur filter khud nahi lagata** — *"filters are
    parameters, not prose"* waisa ka waisa hai, aur uski wajah bhi: prose se filter
    nikalna ek doosra interpreter hai, aur ek interpreter wo cheez hai jiske liye hamlavar
    input likh sakta hai.

    Jo ye karta hai wo alag hai: **batata hai ki wo number dikha aur lagaya nahi gaya.**
    Naapa gaya farq — `"a watch under 2000 rupees please"` par pehle jawab me sirf
    `unmatched_terms: ["2000", "rupees"]` aata tha, jo *"ye shabd catalog me nahi mile"*
    kehta hai. Wo sach hai aur galat baat hai: asli baat ye thi ki **budget gira diya
    gaya**, aur pehle number par Rs 5,60,000 ki ghadi aa gayi. Ek agent us line ko padh
    kar bhi grahak ko wo ghadi de deta.
    """
    words = _words(query or "")
    numbers = [w for w in words if w.isdigit() and 2 <= len(w) <= 8]
    if not numbers:
        return None
    if not any(w in BUDGET_WORDS for w in words):
        return None
    return {"value_in_query": int(numbers[0]),
            "as_paise": int(numbers[0]) * 100,
            "applied": False}


def explain(conn, query, plan, rows, sort_by, filters):
    """Search ne kya kiya, shabdon me. Khaali natije pe ye batata hai ki **kaunsi** khaali.

    Pehle paanch bilkul alag failures ka jawab byte-for-byte ek jaisa tha: ek typo, ek
    aisi cheez jo dukaan me hai hi nahi, query me daali hui product id, ek anjaan
    merchant, aur ek aisa filter jo kabhi kuch match kar hi nahi sakta. Agent ke paas
    unme farq karne ka koi raasta nahi tha, isliye wo sabse aasan jhooth bol deta tha:
    *"is dukaan me shirts nahi hain."*
    """
    terms = plan["terms"] if plan else []
    out = {
        "sorted_by": sort_by,
        # Ek **corrected** shabd bhi match hua shabd hai — bas doosri hijje me. Pehle wo
        # teenon list se bahar gir jata tha (`matched` khaali, `unmatched` khaali,
        # `corrected` bhara), aur ek agent jo `matched_terms` padhta hai wo teen achhe
        # results ke upar "kuch match hi nahi hua" likh deta tha. `corrected_terms` ab bhi
        # alag se batata hai ki kya-kya badla gaya.
        "matched_terms": [t["did_you_mean"] if t["status"] == "corrected" else t["word"]
                          for t in terms if t["status"] in ("matched", "corrected")],
        "unmatched_terms": [t["word"] for t in terms if t["status"] == "unmatched"],
        "corrected_terms": {t["word"]: t["did_you_mean"]
                            for t in terms if t["status"] == "corrected"},
        "ignored_words": plan["ignored_words"] if plan else [],
        "empty_reason": None,
        "next_step": None,
    }
    # Budget jo shabdon me likha tha aur parameter me nahi aaya.
    intent = price_intent(query) if not (filters or {}).get("max_price_paise") else None
    if intent:
        out["price_intent_in_query"] = intent

    useful = coverage_terms(terms)
    if rows:
        # Kitne results ne user ke SAARE shabd poore kiye. Ye `unmatched_terms` se alag
        # cheez hai aur utni hi zaroori: `red` is catalog me maujood hai (`Red Shoes`),
        # yaani wo "unmatched" nahi hai — par kisi SHIRT par nahi hai. Sirf `unmatched`
        # batane se `red shirt` ka jawab phir bhi chup rehta, aur agent chaar non-red
        # shirt "red shirt" bolkar de deta. Yahi F-1 ki asli jad thi.
        full = sum(1 for r in rows if concepts_matched(r, useful) == len(useful))
        out["results_matching_every_word"] = full
        gaps = []
        if out["unmatched_terms"]:
            gaps.append(", ".join("`%s`" % w for w in out["unmatched_terms"]) +
                        " matched nothing anywhere in this catalogue")
        if useful and not full:
            gaps.append("no product here carries all of " +
                        ", ".join("`%s`" % t["word"] for t in useful) +
                        " together — every result below matched only some of them")
        told = ""
        if intent:
            told = ("You wrote a budget in the query (`%d`) and it was NOT applied — "
                    "these results are unfiltered by price. Say so, or call again with "
                    "max_price_paise=%d. " % (intent["value_in_query"], intent["as_paise"]))
        if gaps:
            told += ("Tell the person this before you show them anything: "
                     + "; ".join(gaps) + ". Do not describe these as if they had the "
                     "attribute they did not match. ")
        elif out["corrected_terms"]:
            told = ("Say that you read " + ", ".join("`%s` as `%s`" % kv for kv in
                                                     out["corrected_terms"].items())
                    + ". ")
        out["next_step"] = (
            told + "Nothing here is live and nothing here has photographs. Call "
            "get_product on the candidate you want — that gives the live price, the "
            "product's photo, and `variant_choice`, which is what the person needs in "
            "order to pick.")
        return out

    # ---- khaali. Ab batao KYUN khaali. -----------------------------------------
    merchant_id = filters["merchant_id"]
    known = looks_like_a_product_id(conn, query)
    narrowed_by = [name for name in ("max_price_paise", "min_price_paise", "category")
                   if filters[name] is not None]
    if filters["in_stock_only"]:
        narrowed_by.append("in_stock_only")

    if merchant_id and not conn.execute(
            "SELECT 1 FROM merchants WHERE merchant_id=?", (merchant_id,)).fetchone():
        out["empty_reason"] = "unknown_merchant"
        out["next_step"] = ("No merchant is registered as `%s`. Call list_merchants to "
                            "see the ones that are, or drop `merchant_id` to search all "
                            "of them." % merchant_id)
    elif filters["category"] is not None and not conn.execute(
            "SELECT 1 FROM products WHERE category=?"
            + (" AND merchant_id=?" if merchant_id else ""),
            (filters["category"],) + ((merchant_id,) if merchant_id else ())).fetchone():
        # `unknown_merchant` ko theek jawab milta tha aur `category` ko nahi — jabki dono
        # ek hi kism ki galti hain: ek aisi cheez maangi gayi jo maujood hi nahi. Purana
        # roop ise `filters_too_narrow` bata kar kehta tha *"The words matched — relax a
        # filter"*, chahe us call me koi query bheji hi na gayi ho. Koi bhi dheela karna
        # kabhi kaam nahi kar sakta tha, to agent har koshish par ek call jalata tha.
        out["empty_reason"] = "unknown_category"
        out["next_step"] = ("No product is filed under category `%s`%s. `category` is an "
                            "exact match on the merchant's own list — call "
                            "list_merchants and use one of the `categories` it names, or "
                            "drop `category` and let the words do the work."
                            % (filters["category"],
                               " at " + merchant_id if merchant_id else ""))
    elif known:
        out["empty_reason"] = "looks_like_a_product_id"
        out["next_step"] = ('`%s` is a product id, not a search term. Call '
                            'get_product(merchant_id="%s", product_id="%s") directly.'
                            % (known["product_id"], known["merchant_id"],
                               known["product_id"]))
    elif plan and not any(t["matched_as"] for t in terms):
        out["empty_reason"] = "no_word_matched"
        # Id jaisi dikhne wali query par wahi baat pehle aani chahiye jo `nw-86` par
        # aati hai — id `query` me nahi jati. Farq sirf itna ki aisi koi id hai bhi nahi.
        id_note = ("`%s` also looks like a product id rather than search words, and an "
                   "id never belongs in `query` — call get_product directly when you "
                   "have one. No product carries this id either, so check it with the "
                   "person. " % (query or "").strip()) if id_shaped(query) else ""
        out["next_step"] = (
            id_note + "None of " + ", ".join("`%s`" % t["word"] for t in terms) +
            " appears anywhere in this catalogue — not as a spelling this could correct, "
            "and not in any product's name, brand, category, tags or variant options. "
            "This is not something to retry with different wording. Tell the person this "
            "shop does not appear to carry that, and offer to browse instead: "
            'search_products with no `query` and a `category` from list_merchants, or '
            'with sort_by="price_asc".')
    # Wahi query, bina filters ke. Agar ab kuch milta hai to galti shabdon ki nahi thi —
    # aur "plainer words use karo" wali salah theek ulta bhej rahi hoti hai.
    elif narrowed_by and search(conn, query=query, limit=1, in_stock_only=False,
                                merchant_id=merchant_id):
        out["empty_reason"] = "filters_too_narrow"
        out["next_step"] = (
            "The words matched — the filters removed everything. Relax one of " +
            ", ".join("`%s`" % f for f in narrowed_by) + " and call again. Do not "
            "reword the query; that part worked.")
    elif not plan:
        out["empty_reason"] = "empty_catalogue"
        out["next_step"] = ("Nothing is indexed for this filter yet. Call list_merchants "
                            "to see what is registered and how fresh the index is.")
    else:
        out["empty_reason"] = "no_match"
        out["next_step"] = (
            "Each word exists in this catalogue but no product carries them together. "
            "Search the most important noun on its own, then narrow with `category` or "
            "`max_price_paise`.")
    return out
