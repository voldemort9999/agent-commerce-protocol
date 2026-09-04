"""SQLite access for Northwind Apparel. Schema lives here; seed.py aur main.py dono isse lete hain."""
import json
import pathlib
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "northwind.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  product_id      TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL,
  category        TEXT NOT NULL,
  brand           TEXT,
  tags            TEXT NOT NULL DEFAULT '[]',
  images          TEXT NOT NULL DEFAULT '[]',
  attributes      TEXT NOT NULL DEFAULT '{}',
  reviews         TEXT NOT NULL DEFAULT '[]',
  related_ids     TEXT NOT NULL DEFAULT '[]',
  variant_options TEXT NOT NULL DEFAULT '[]',
  rating_avg      REAL,
  rating_count    INTEGER,
  updated_at      TEXT NOT NULL
);
-- catalog pagination isi order pe chalti hai: updated_at, phir product_id
CREATE INDEX IF NOT EXISTS idx_products_sync ON products(updated_at, product_id);

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
  coupon_code       TEXT,
  timeline          TEXT NOT NULL,
  delivery          TEXT,
  refund            TEXT,
  cancel_reason     TEXT,
  created_at        TEXT NOT NULL,
  cancellable_until TEXT NOT NULL,
  cancelled_at      TEXT
);

CREATE TABLE IF NOT EXISTS idempotency (
  key          TEXT PRIMARY KEY,
  body_sha256  TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    add_missing_columns(conn)
    return conn


# Naya column jodne par `CREATE TABLE IF NOT EXISTS` PURANE database ko chup-chaap waise
# hi chhod deta hai — koi error nahi, bas `no such column` tab aata hai jab koi us column
# ko chhuta hai. Ye ek line ka guard usi shakal ka hai jo Layer ke FTS rebuild ka hai.
NEW_COLUMNS = {"orders": {"delivery": "TEXT"}}


def add_missing_columns(conn):
    for table, columns in NEW_COLUMNS.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}
        if not have:
            continue                    # table abhi bana hi nahi - SCHEMA usse banayega
        for name, kind in columns.items():
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))


def jl(value, default=None):
    """JSON column -> Python. NULL ko default deta hai."""
    return default if value is None else json.loads(value)


def jd(value):
    return json.dumps(value, ensure_ascii=False)
