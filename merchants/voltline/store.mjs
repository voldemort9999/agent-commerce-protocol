// Voltline ki dukaan ka sach — ek jagah. `/agent/*` aur `/` dono yahin se padhte hain.
//
// Ye file jaan-boojhkar Express ko nahi jaanti. Jo bhi cheez "is dukaan ke bare me ek
// fact" hai — daam, stock, shipping, delivery, coupon — wo yahan hai, taaki insaan ko
// dikhne wala page aur agent ko milne wala JSON kabhi alag jawab na de saken. Wahi "ek
// database, do darwaze" wali baat, code me.

import { jl, jd, nowIso, plusDays } from './db.mjs';

export const MERCHANT_ID = 'voltline-electronics';
export const MERCHANT_NAME = 'Voltline Electronics';
export const CURRENCY = 'INR';
export const PAYMENT_MODES = ['payment_link', 'checkout', 'cod'];

// Voltline ki apni policies. Har number Northwind se alag hai — do dukaanein alag
// policies declare karti hain, aur Layer ko dono ke saath sahi chalna chahiye.
export const SHIPPING_FLAT_PAISE = 9900;      // insured electronics courier
export const FREE_SHIPPING_ABOVE_PAISE = 499900;
export const CANCEL_WINDOW_HOURS = 24;
export const MAX_QTY_PER_VARIANT = 3;         // Layer ka apna ceiling 5 hai; sakht wala jeetega
export const UNPAID_ORDER_TTL_MINUTES = 30;

// Demo convention, poore project me ek jaisi: 7 se shuru hone wala pincode serve nahi
// hota. Ye ek line ek asli NOT_SERVICEABLE raasta deti hai bina kisi courier API ke.
export const isServiceable = (pincode) => /^\d{6}$/.test(pincode) && !pincode.startsWith('7');

/**
 * Voltline ka warehouse Bengaluru me hai, Northwind ka nahi — isliye ETA alag nikalta
 * hai. Zone pehle digit se: 4/5/6 (west, south, telugu belt) do din, baaki chaar.
 */
export function deliveryFor(pincode) {
  if (!isServiceable(pincode)) return { serviceable: false, pincode };
  return {
    serviceable: true,
    pincode,
    shipping_paise: SHIPPING_FLAT_PAISE,
    eta_days: '456'.includes(pincode[0]) ? 2 : 4,
  };
}

export const etaDaysFor = (pincode) => ('456'.includes(pincode[0]) ? 2 : 4);

/** Free-shipping ka faisla saamaan ke daam par hota hai, discount lagne se pehle. */
export const shippingFor = (itemsTotalPaise) =>
  (itemsTotalPaise >= FREE_SHIPPING_ABOVE_PAISE ? 0 : SHIPPING_FLAT_PAISE);

/**
 * Coupons. SPEC 19 ke hisaab se koi listing endpoint nahi hai — merchant khud validate
 * karta hai aur na chale to INVALID_COUPON phenkta hai.
 * `DEAD10` jaan-boojhkar hamesha invalid hai: expired-coupon ka raasta bhi chalna chahiye.
 */
export function discountFor(code, itemsTotalPaise) {
  if (!code) return { discount: 0 };
  const key = String(code).trim().toUpperCase();
  if (key === 'VOLT15') return { discount: Math.round(itemsTotalPaise * 0.15) };
  if (key === 'NEWYEAR500') {
    return itemsTotalPaise >= 500000
      ? { discount: 50000 }
      : { reason: 'NEWYEAR500 applies to orders of Rs 5,000 or more.', min_items_total_paise: 500000 };
  }
  if (key === 'DEAD10') return { reason: 'DEAD10 expired on 2026-01-31.' };
  return { reason: `Unknown coupon code ${key}.` };
}

// ---------------------------------------------------------------- catalog reads

const parseProduct = (row) => ({
  ...row,
  tags: jl(row.tags),
  images: jl(row.images),
  attributes: jl(row.attributes),
  reviews: jl(row.reviews),
  related_ids: jl(row.related_ids),
  variant_options: jl(row.variant_options),
});

const parseVariant = (row) => ({
  ...row,
  options: jl(row.options),
  images: jl(row.images),
});

export function variantsOf(db, productId) {
  return db.prepare('SELECT * FROM variants WHERE product_id = ? ORDER BY position, variant_id')
    .all(productId).map(parseVariant);
}

/** SPEC 4: min/max saare IN-STOCK variants par. Kuch bhi stock me na ho to poore set par. */
export function priceRange(variants) {
  const live = variants.filter((v) => v.stock > 0);
  const pool = (live.length ? live : variants).map((v) => v.price_paise);
  return { min: Math.min(...pool), max: Math.max(...pool) };
}

export function catalogEntry(db, product) {
  const variants = variantsOf(db, product.product_id);
  return {
    product_id: product.product_id,
    title: product.title,
    description: product.description,          // SPEC 10: verbatim, jaisa stored hai
    category: product.category,
    tags: product.tags,
    brand: product.brand ?? undefined,
    images: product.images,
    price_range_paise: priceRange(variants),
    in_stock: variants.some((v) => v.stock > 0),
    variant_options: product.variant_options,
    variant_count: variants.length,
    rating_avg: product.rating_avg ?? undefined,
    rating_count: product.rating_count ?? undefined,
    updated_at: product.updated_at,
  };
}

export function productRow(db, productId) {
  const row = db.prepare('SELECT * FROM products WHERE product_id = ?').get(productId);
  return row ? parseProduct(row) : null;
}

export function allProducts(db) {
  return db.prepare('SELECT * FROM products ORDER BY updated_at DESC, product_id')
    .all().map(parseProduct);
}

export function productDetail(db, product, pincode) {
  const variants = variantsOf(db, product.product_id);
  const detail = {
    product_id: product.product_id,
    title: product.title,
    description: product.description,
    category: product.category,
    tags: product.tags,
    brand: product.brand ?? undefined,
    images: product.images,
    attributes: product.attributes,
    variants: variants.map((v) => ({
      variant_id: v.variant_id,
      sku: v.sku ?? undefined,
      options: v.options,
      price_paise: v.price_paise,
      mrp_paise: v.mrp_paise ?? undefined,
      stock: v.stock,
      images: v.images,
    })),
    rating_avg: product.rating_avg ?? undefined,
    rating_count: product.rating_count ?? undefined,
    reviews: (product.reviews || []).slice(0, 5),        // SPEC 5: max 5
    related_product_ids: (product.related_ids || []).slice(0, 10),  // SPEC 5: max 10
    updated_at: product.updated_at,
  };
  if (pincode) detail.delivery = deliveryFor(pincode);
  return detail;
}

// ---------------------------------------------------------------- cursor

/**
 * Cursor ko chatur hone ki zarurat nahi — sirf opaque aur sthir hone ki. Aakhri row ka
 * (updated_at, product_id) hi kaafi hai, aur wo SPEC 4 ke ordering rule ke saath khud
 * jud jata hai.
 */
export const encodeCursor = (row) =>
  Buffer.from(jd({ u: row.updated_at, p: row.product_id })).toString('base64url');

export function decodeCursor(cursor) {
  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'));
    if (typeof parsed?.u === 'string' && typeof parsed?.p === 'string') return parsed;
  } catch { /* malformed cursor neeche handle hota hai */ }
  return null;
}

export function catalogPage(db, { updatedSince, cursor, limit }) {
  const where = [];
  const args = [];
  if (updatedSince) { where.push('updated_at > ?'); args.push(updatedSince); }
  if (cursor) { where.push('(updated_at, product_id) > (?, ?)'); args.push(cursor.u, cursor.p); }
  const sql = `SELECT * FROM products ${where.length ? 'WHERE ' + where.join(' AND ') : ''}
               ORDER BY updated_at ASC, product_id ASC LIMIT ?`;
  const rows = db.prepare(sql).all(...args, limit + 1).map(parseProduct);

  const hasMore = rows.length > limit;
  const page = hasMore ? rows.slice(0, limit) : rows;
  return {
    products: page.map((p) => catalogEntry(db, p)),
    // SPEC 4: has_more true ho to cursor non-empty aur AAGE badhta hua hona hi chahiye,
    // warna Layer ya usi page par ghoomti rahegi ya aadha catalog index karke chup ho
    // jayegi — dono me koi error nahi aata.
    cursor: hasMore && page.length ? encodeCursor(page[page.length - 1]) : null,
    has_more: hasMore,
  };
}

export function catalogStats(db) {
  const row = db.prepare(
    'SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM products').get();
  return { product_count: row.n, last_updated_at: row.last };
}

/** SPEC 4: har product ki category manifest me honi chahiye. DB se nikaalo, likho mat —
 *  hardcoded list catalog se drift kar jati hai aur drift chup-chaap hoti hai. */
export function categories(db) {
  return db.prepare('SELECT DISTINCT category FROM products ORDER BY category')
    .all().map((r) => r.category);
}

export function manifest(db) {
  const stats = catalogStats(db);
  return {
    spec_version: '1.0',
    merchant_id: MERCHANT_ID,
    name: MERCHANT_NAME,
    currency: CURRENCY,
    categories: categories(db),
    payment_modes: PAYMENT_MODES,
    shipping: {
      pincode_required: true,
      flat_paise: SHIPPING_FLAT_PAISE,
      free_above_paise: FREE_SHIPPING_ABOVE_PAISE,
    },
    policies: {
      cancel_window_hours: CANCEL_WINDOW_HOURS,
      max_qty_per_variant: MAX_QTY_PER_VARIANT,
    },
    catalog: stats,
  };
}

export { nowIso, plusDays };
