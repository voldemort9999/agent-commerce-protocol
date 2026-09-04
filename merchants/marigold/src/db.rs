//! Marigold ka store: schema, connection, aur wo ek rule jispe poori delta sync tiki hai.

use rusqlite::Connection;
use std::path::PathBuf;

pub fn path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("marigold.db")
}

pub const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS products (
  product_id     TEXT PRIMARY KEY,
  title          TEXT NOT NULL,
  description    TEXT NOT NULL,
  category       TEXT NOT NULL,
  brand          TEXT,
  tags           TEXT NOT NULL DEFAULT '[]',
  images         TEXT NOT NULL DEFAULT '[]',
  attributes     TEXT NOT NULL DEFAULT '{}',
  reviews        TEXT NOT NULL DEFAULT '[]',
  related        TEXT NOT NULL DEFAULT '[]',
  rating_avg     REAL,
  rating_count   INTEGER,
  updated_epoch  INTEGER NOT NULL,
  updated_at     TEXT NOT NULL,
  position       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_products_updated ON products(updated_at, product_id);

CREATE TABLE IF NOT EXISTS variants (
  variant_id  TEXT PRIMARY KEY,
  product_id  TEXT NOT NULL REFERENCES products(product_id),
  sku         TEXT,
  options     TEXT NOT NULL DEFAULT '{}',
  price_paise INTEGER NOT NULL,
  mrp_paise   INTEGER,
  stock       INTEGER NOT NULL,
  images      TEXT NOT NULL DEFAULT '[]',
  position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_variants_product ON variants(product_id, position);

CREATE TABLE IF NOT EXISTS orders (
  order_id          TEXT PRIMARY KEY,
  status            TEXT NOT NULL,
  items             TEXT NOT NULL,
  items_total_paise INTEGER NOT NULL,
  shipping_paise    INTEGER NOT NULL,
  discount_paise    INTEGER NOT NULL,
  final_total_paise INTEGER NOT NULL,
  contact           TEXT NOT NULL,
  address           TEXT NOT NULL,
  payment           TEXT NOT NULL,
  delivery          TEXT NOT NULL,
  refund            TEXT,
  timeline          TEXT NOT NULL,
  coupon_code       TEXT,
  created_epoch     INTEGER NOT NULL,
  created_at        TEXT NOT NULL,
  cancellable_until TEXT,
  expires_epoch     INTEGER
);

-- Idempotency: body ka HASH nahi, poora canonical body rakha jata hai.
-- Hash se do alag body ek jaisi dikh sakti hain, aur uska nateeja "kisi aur ka order
-- lauta diya" hota hai — ek order-create ke raaste par wo bardasht ke bahar hai. 64-bit
-- collision ki sambhavna chhoti hai, par yahan use lene ki koi wajah hi nahi: chand
-- kilobyte text rakhna sasta hai aur tulna theek hai (SPEC 6).
CREATE TABLE IF NOT EXISTS idempotency (
  key           TEXT PRIMARY KEY,
  body          TEXT NOT NULL,
  order_id      TEXT NOT NULL,
  created_epoch INTEGER NOT NULL
);
"#;

pub fn open() -> Connection {
    let conn = Connection::open(path()).expect("marigold.db khul nahi paayi");
    conn.pragma_update(None, "journal_mode", "WAL").ok();
    conn.pragma_update(None, "foreign_keys", "ON").ok();
    conn.execute_batch(SCHEMA).expect("schema");
    conn
}

/// Agli `updated_at`, **poore catalog** ke max se badi (SPEC 4).
///
/// Yahi wo ek rule hai jo delta sync ko sach banata hai, aur jise pehli baar galat likha
/// gaya to koi error nahi aata — kuch products chup-chaap index me aana band ho jate hain.
/// Do kamzor roop dono fail hote hain:
///
///   * saada `now()` — ek hi second me do change, doosra hamesha ke liye invisible
///   * per-product `max(now, apna purana + 1s)` — ek vyast product apna stamp wall clock
///     se AAGE le jata hai, aur phir kisi doosre product ka pehla change watermark se
///     PEECHE aa girta hai. Gap ek second ka nahi, utna hota hai jitna wo vyast product
///     bhag chuka hai
///
/// Isliye base poore store ka max hai, is product ka apna nahi.
pub fn next_updated(conn: &Connection) -> (i64, String) {
    let peak: i64 = conn
        .query_row(
            "SELECT COALESCE(MAX(updated_epoch), 0) FROM products",
            [],
            |r| r.get(0),
        )
        .unwrap_or(0);
    let epoch = std::cmp::max(crate::util::now(), peak + 1);
    (epoch, crate::util::iso(epoch))
}

/// JSON column padhna, aur na padh paane par ek surakshit default.
pub fn jl(text: &str, fallback: serde_json::Value) -> serde_json::Value {
    serde_json::from_str(text).unwrap_or(fallback)
}
