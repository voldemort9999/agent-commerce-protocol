// Voltline ka storage. node:sqlite — Node 22.5+ me built-in hai, koi native module nahi.
//
// Ek hi database do darwazon ko serve karta hai: `/agent/*` (machine) aur `/`, `/p/:id`
// (insaan). Dono ke beech koi cache, koi copy, koi sync nahi — isliye dukaan me jo daam
// dikhta hai wahi daam agent ko milta hai, aur wahi verify hota hai.

import { DatabaseSync } from 'node:sqlite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const DB_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), 'voltline.db');

export const SCHEMA = `
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

CREATE TABLE IF NOT EXISTS variants (
  variant_id  TEXT PRIMARY KEY,
  product_id  TEXT NOT NULL REFERENCES products(product_id),
  sku         TEXT,
  options     TEXT NOT NULL DEFAULT '{}',
  price_paise INTEGER NOT NULL,
  mrp_paise   INTEGER,
  stock       INTEGER NOT NULL DEFAULT 0,
  images      TEXT NOT NULL DEFAULT '[]',
  position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS variants_by_product ON variants(product_id, position);

CREATE TABLE IF NOT EXISTS orders (
  order_id           TEXT PRIMARY KEY,
  status             TEXT NOT NULL,
  items              TEXT NOT NULL,
  items_total_paise  INTEGER NOT NULL,
  shipping_paise     INTEGER NOT NULL,
  discount_paise     INTEGER NOT NULL,
  final_total_paise  INTEGER NOT NULL,
  contact            TEXT NOT NULL,
  address            TEXT NOT NULL,
  payment_mode       TEXT NOT NULL,
  coupon_code        TEXT,
  payment            TEXT NOT NULL,
  refund             TEXT,
  delivery           TEXT NOT NULL,
  timeline           TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  cancellable_until  TEXT,
  expires_at         TEXT,
  cancelled_at       TEXT
);

CREATE TABLE IF NOT EXISTS idempotency (
  key        TEXT PRIMARY KEY,
  body_hash  TEXT NOT NULL,
  order_id   TEXT NOT NULL,
  created_at TEXT NOT NULL
);
`;

export function connect() {
  const db = new DatabaseSync(DB_PATH);
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA foreign_keys = ON');
  return db;
}

export const jd = (value) => JSON.stringify(value ?? null);
export const jl = (text) => (text == null ? null : JSON.parse(text));

/** RFC 3339, UTC, `Z` — SPEC 2.2. Second resolution: the wire never carries millis. */
export function nowIso(date = new Date()) {
  return date.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export function plusHours(iso, hours) {
  return nowIso(new Date(Date.parse(iso) + hours * 3600_000));
}

export function plusDays(iso, days) {
  return nowIso(new Date(Date.parse(iso) + days * 86_400_000));
}

/**
 * SPEC 4: a catalog-visible change must produce an `updated_at` strictly greater than
 * every `updated_at` this store has ever issued — **store-wide**, not per product.
 *
 * Per-product monotonicity looks sufficient and is not. Bump one product repeatedly and
 * its stamp drifts ahead of the wall clock; a different product changing for the first
 * time then stamps an honest `now` that lands *behind* the Layer's global watermark, and
 * that product is never indexed again. Nothing errors. This one line is the whole rule.
 *
 * Must be called inside the same transaction as the change it stamps.
 */
export function bumpedUpdatedAt(db, now = new Date()) {
  const row = db.prepare('SELECT MAX(updated_at) AS peak FROM products').get();
  const wall = nowIso(now);
  if (!row || !row.peak) return wall;
  const next = nowIso(new Date(Date.parse(row.peak) + 1000));
  return next > wall ? next : wall;
}
