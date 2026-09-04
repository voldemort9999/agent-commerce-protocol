// SPEC.md ke chhe endpoints. Yahi poora merchant contract hai — is file ke bahar Layer
// ke liye kuch nahi hai.
//
// Ek baat jo har handler par lagti hai: **jo verify kiya jata hai wo live DB se aata
// hai**, kisi cache se nahi. Order banate waqt daam, stock aur serviceability teenon
// dobara padhe jate hain, chahe agent ne abhi-abhi detail padhi ho.

import express from 'express';
import crypto from 'node:crypto';
import * as store from './store.mjs';
import { jd, jl, nowIso, plusHours, plusDays, bumpedUpdatedAt } from './db.mjs';
import * as rzp from './razorpay.mjs';

const SPEC_VERSION = '1.0';

// ---------------------------------------------------------------- error envelope

// SPEC 11. `details` 409 par hamesha bharta hai: Layer ek samjhi hui rejection se
// nikal sakti hai, ek bare "no" se nahi.
export function fail(res, status, code, message, details) {
  const error = { code, message };
  if (details) error.details = details;
  return res.status(status).json({ error });
}

class SpecError extends Error {
  constructor(status, code, message, details) {
    super(message);
    Object.assign(this, { status, code, details });
  }
}
const bad = (status, code, message, details) => new SpecError(status, code, message, details);

// ---------------------------------------------------------------- helpers

const isInt = (v) => Number.isInteger(v);
const orderId = () => 'ord_' + crypto.randomBytes(6).toString('hex');

/** Idempotency: wahi key + wahi body = wahi order. Key stable hone ke liye body ka
 *  hash bhi stable hona chahiye, isliye keys sort karke serialise hoti hain. */
function bodyHash(value) {
  const stable = (v) => {
    if (Array.isArray(v)) return v.map(stable);
    if (v && typeof v === 'object') {
      return Object.fromEntries(Object.keys(v).sort().map((k) => [k, stable(v[k])]));
    }
    return v;
  };
  return crypto.createHash('sha256').update(JSON.stringify(stable(value))).digest('hex');
}

function requireString(obj, field, path) {
  const v = obj?.[field];
  if (typeof v !== 'string' || !v.trim()) {
    throw bad(400, 'MISSING_FIELD', `${path} is required and must be a non-empty string.`,
      { field: path });
  }
  return v.trim();
}

// ---------------------------------------------------------------- order shaping

function orderPayment(order) {
  const payment = jl(order.payment) || {};
  const refund = jl(order.refund);
  let state = 'pending';
  if (order.status === 'failed') state = 'failed';
  if (payment.payment_id) state = 'paid';
  if (refund) {
    // SPEC 7: bheja hua refund aur AAYA hua refund do alag facts hain. Jab tak provider
    // `processed` na kahe, grahak ke paas paisa nahi hai — aur agent yahi padh kar
    // grahak ko batata hai.
    state = refund.state === 'processed' ? 'refunded'
      : refund.state === 'failed' ? 'refund_failed'
        : 'refund_pending';
  }
  return {
    mode: order.payment_mode,
    state,
    razorpay_payment_id: payment.payment_id ?? null,
    paid_at: payment.paid_at ?? null,
  };
}

function cancellability(order, now = nowIso()) {
  const open = ['created', 'paid', 'confirmed'].includes(order.status);
  const inWindow = Boolean(order.cancellable_until) && now < order.cancellable_until;
  const cancellable = open && inWindow;
  // SPEC 7: `cancellable: false` ke bagal me ek future deadline do ulte jawab hain, aur
  // padhne wale ke paas unme se chunne ka koi rule nahi. Isliye band hote hi `null`.
  return { cancellable, cancellable_until: cancellable ? order.cancellable_until : null };
}

function orderView(order) {
  const refund = jl(order.refund);
  return {
    spec_version: SPEC_VERSION,
    order_id: order.order_id,
    status: order.status,
    items: jl(order.items),
    items_total_paise: order.items_total_paise,
    shipping_paise: order.shipping_paise,
    discount_paise: order.discount_paise,
    final_total_paise: order.final_total_paise,
    currency: store.CURRENCY,
    payment: orderPayment(order),
    refund: refund ?? null,
    delivery: jl(order.delivery),
    timeline: jl(order.timeline),
    ...cancellability(order),
  };
}

function creationView(order) {
  const payment = jl(order.payment) || {};
  return {
    spec_version: SPEC_VERSION,
    order_id: order.order_id,
    status: order.status,
    items: jl(order.items),
    items_total_paise: order.items_total_paise,
    shipping_paise: order.shipping_paise,
    discount_paise: order.discount_paise,
    final_total_paise: order.final_total_paise,
    currency: store.CURRENCY,
    payment: {
      mode: order.payment_mode,
      link_url: payment.link_url ?? null,
      razorpay_order_id: payment.razorpay_order_id ?? null,
      // SPEC 6: order id akela kisi se pay nahi hota. Har checkout client ko wo public
      // key chahiye jo account pehchanti hai. Secret kabhi kisi response me nahi jata.
      razorpay_key_id: payment.razorpay_key_id ?? null,
      expires_at: order.expires_at ?? null,
    },
    delivery: jl(order.delivery),
    cancellable_until: order.cancellable_until,
    created_at: order.created_at,
  };
}

const timelineAdd = (order, entry) => {
  const timeline = jl(order.timeline) || [];
  timeline.push(entry);
  return jd(timeline);
};

// ---------------------------------------------------------------- lazy provider refresh

/**
 * Merchant ke paas webhook nahi hai (poore project ka faisla), to "pay hua ya nahi" wahin
 * poochha jata hai jahan waise bhi provider se baat hoti hai — order read par.
 *
 * Do rules yahan load-bearing hain, dono SPEC 9 se:
 *  1. Expiry ORDER ki hai, provider ke instrument ki nahi. Razorpay ka checkout order
 *     kabhi expire nahi hota, to agar ghadi provider par chhod dein to cap ke neeche
 *     wala har chhoda hua order stock HAMESHA ke liye rok lega.
 *  2. Order sirf tab `failed` hota hai jab uska unpaid hona CONFIRM ho. Provider tak
 *     baat na pahunche to order pending rehta hai — timeout par likha `failed` kisi ke
 *     poore ho chuke payment ke upar likha ja sakta hai.
 */
async function refreshOrder(db, order) {
  if (order.payment_mode === 'cod') return order;

  const payment = jl(order.payment) || {};
  const refund = jl(order.refund);

  // --- refund pehle: bheja hua refund poora hua ya nahi, ye bhi provider hi jaanta hai
  if (refund?.razorpay_refund_id && !['processed', 'failed'].includes(refund.state)) {
    try {
      const live = await rzp.fetchRefund(refund.razorpay_refund_id);
      if (live.state !== refund.state) {
        refund.state = live.state;
        const patch = { refund: jd(refund) };
        if (live.state === 'processed') {
          patch.status = 'refunded';
          patch.timeline = timelineAdd(order, { status: 'refunded', at: nowIso() });
        }
        db.prepare(`UPDATE orders SET refund = ?${patch.status ? ', status = ?, timeline = ?' : ''}
                    WHERE order_id = ?`)
          .run(...(patch.status
            ? [patch.refund, patch.status, patch.timeline, order.order_id]
            : [patch.refund, order.order_id]));
        return db.prepare('SELECT * FROM orders WHERE order_id = ?').get(order.order_id);
      }
    } catch (err) {
      if (!(err instanceof rzp.ProviderUnreachable) && !(err instanceof rzp.ProviderError)) throw err;
      // Provider chup hai — refund ki purani state rehne do, agli read par phir poochenge.
    }
  }

  if (order.status !== 'created' || payment.payment_id) return order;

  let paid = null;
  try {
    paid = await rzp.fetchPayment({
      mode: order.payment_mode,
      linkId: payment.razorpay_link_id,
      orderId: payment.razorpay_order_id,
    });
  } catch (err) {
    if (err instanceof rzp.ProviderUnreachable || err instanceof rzp.ProviderError) {
      return order;   // confirm nahi hua ki unpaid hai -> chhedo mat (rule 2 upar)
    }
    throw err;
  }

  if (paid) {
    const at = paid.paid_at || nowIso();
    db.prepare('UPDATE orders SET status = ?, payment = ?, timeline = ? WHERE order_id = ?')
      .run('paid', jd({ ...payment, payment_id: paid.payment_id, paid_at: at }),
        timelineAdd(order, { status: 'paid', at }), order.order_id);
    return db.prepare('SELECT * FROM orders WHERE order_id = ?').get(order.order_id);
  }

  if (order.expires_at && nowIso() > order.expires_at) {
    const at = nowIso();
    db.exec('BEGIN');
    try {
      for (const line of jl(order.items)) {
        db.prepare('UPDATE variants SET stock = stock + ? WHERE variant_id = ?')
          .run(line.qty, line.variant_id);
      }
      bumpTouchedProducts(db, jl(order.items));
      db.prepare('UPDATE orders SET status = ?, timeline = ? WHERE order_id = ?')
        .run('failed', timelineAdd(order, {
          status: 'failed', at, note: 'Payment window expired; reserved stock released.',
        }), order.order_id);
      db.exec('COMMIT');
    } catch (err) { db.exec('ROLLBACK'); throw err; }
    return db.prepare('SELECT * FROM orders WHERE order_id = ?').get(order.order_id);
  }

  return order;
}

/** Stock catalog-visible hai (SPEC 4 `in_stock`, aur detail ka `stock`), to har badlav
 *  ke saath us product ka `updated_at` store-wide aage badhna chahiye. */
function bumpTouchedProducts(db, lines) {
  const ids = [...new Set(lines.map((l) => l.product_id).filter(Boolean))];
  for (const pid of ids) {
    db.prepare('UPDATE products SET updated_at = ? WHERE product_id = ?')
      .run(bumpedUpdatedAt(db), pid);
  }
}

// ---------------------------------------------------------------- router

export function agentRouter(db) {
  const router = express.Router();
  const AGENT_KEY = process.env.VOLTLINE_AGENT_KEY || 'vl_agentkey_local_dev_only';

  // SPEC 2.5: ye endpoints public internet par nahi hain. Theek ek caller ke paas key hai.
  router.use((req, res, next) => {
    if (req.get('X-Agent-Key') !== AGENT_KEY) {
      return fail(res, 401, 'UNAUTHORIZED', 'Missing or invalid X-Agent-Key.');
    }
    next();
  });

  // 1 ------------------------------------------------------------ manifest
  router.get('/manifest', (req, res) => res.json(store.manifest(db)));

  // 2 ------------------------------------------------------------ catalog
  router.get('/catalog', (req, res) => {
    const rawLimit = req.query.limit;
    let limit = 100;
    if (rawLimit !== undefined) {
      limit = Number(rawLimit);
      if (!Number.isInteger(limit) || limit < 1) {
        return fail(res, 400, 'MISSING_FIELD', 'limit must be a positive integer.',
          { field: 'limit' });
      }
      limit = Math.min(limit, 500);
    }

    let cursor = null;
    if (req.query.cursor) {
      cursor = store.decodeCursor(String(req.query.cursor));
      if (!cursor) {
        return fail(res, 400, 'MISSING_FIELD', 'cursor is not a cursor this store issued.',
          { field: 'cursor' });
      }
    }

    const updatedSince = req.query.updated_since ? String(req.query.updated_since) : null;
    if (updatedSince && Number.isNaN(Date.parse(updatedSince))) {
      return fail(res, 400, 'MISSING_FIELD', 'updated_since must be RFC 3339 UTC.',
        { field: 'updated_since' });
    }

    res.json({ spec_version: SPEC_VERSION, ...store.catalogPage(db, { updatedSince, cursor, limit }) });
  });

  // 3 ------------------------------------------------------------ product detail
  router.get('/products/:product_id', (req, res) => {
    // SPEC 5: ye endpoint cache se serve nahi hota — iska daam aur stock wahi hain
    // jinke khilaf paisa verify hota hai.
    res.set('Cache-Control', 'no-store');

    const product = store.productRow(db, req.params.product_id);
    if (!product) {
      return fail(res, 404, 'PRODUCT_NOT_FOUND', `No product with id ${req.params.product_id}.`,
        { product_id: req.params.product_id });
    }

    let pincode = null;
    if (req.query.pincode !== undefined) {
      pincode = String(req.query.pincode).trim();
      if (!/^\d{6}$/.test(pincode)) {
        return fail(res, 400, 'MISSING_FIELD', 'pincode must be exactly 6 digits.',
          { field: 'pincode', received: pincode });
      }
    }

    res.json({ spec_version: SPEC_VERSION, ...store.productDetail(db, product, pincode) });
  });

  // 4 ------------------------------------------------------------ create order
  router.post('/orders', async (req, res) => {
    try {
      res.status(201).json(await createOrder(db, req));
    } catch (err) {
      if (err instanceof SpecError) return fail(res, err.status, err.code, err.message, err.details);
      if (err instanceof rzp.ProviderError) {
        if (err.retryAfter) res.set('Retry-After', String(err.retryAfter));
        return fail(res, err.status, err.code, err.message, err.details);
      }
      if (err instanceof rzp.ProviderUnreachable) {
        res.set('Retry-After', '5');
        return fail(res, 429, 'RATE_LIMITED',
          'Could not reach the payment provider to build an instrument. Try again shortly.',
          { provider_message: err.message });
      }
      console.error('[voltline] create order failed:', err);
      return fail(res, 500, 'INTERNAL_ERROR', 'Order could not be created.');
    }
  });

  // 5 ------------------------------------------------------------ order status
  router.get('/orders/:order_id', async (req, res) => {
    let order = db.prepare('SELECT * FROM orders WHERE order_id = ?').get(req.params.order_id);
    if (!order) {
      return fail(res, 404, 'ORDER_NOT_FOUND', `No order with id ${req.params.order_id}.`,
        { order_id: req.params.order_id });
    }
    try {
      order = await refreshOrder(db, order);
    } catch (err) {
      console.error('[voltline] order refresh failed:', err);
    }
    res.json(orderView(order));
  });

  // 6 ------------------------------------------------------------ cancel
  router.post('/orders/:order_id/cancel', async (req, res) => {
    try {
      res.json(await cancelOrder(db, req));
    } catch (err) {
      if (err instanceof SpecError) return fail(res, err.status, err.code, err.message, err.details);
      if (err instanceof rzp.ProviderError) {
        if (err.retryAfter) res.set('Retry-After', String(err.retryAfter));
        return fail(res, err.status, err.code, err.message, err.details);
      }
      console.error('[voltline] cancel failed:', err);
      return fail(res, 500, 'INTERNAL_ERROR', 'Cancellation could not be completed.');
    }
  });

  return router;
}

// ---------------------------------------------------------------- create order

async function createOrder(db, req) {
  const idempotencyKey = req.get('Idempotency-Key');
  if (!idempotencyKey || idempotencyKey.length > 64) {
    throw bad(400, 'MISSING_FIELD',
      'Idempotency-Key header is required and must be at most 64 characters.',
      { field: 'Idempotency-Key' });
  }

  const body = req.body ?? {};

  // --- shape
  if (!Array.isArray(body.items) || body.items.length === 0) {
    throw bad(400, 'MISSING_FIELD', 'items must be a non-empty array.', { field: 'items' });
  }
  for (const [i, line] of body.items.entries()) {
    requireString(line, 'variant_id', `items[${i}].variant_id`);
    if (!isInt(line.qty) || line.qty < 1) {
      throw bad(400, 'MISSING_FIELD', `items[${i}].qty must be an integer of at least 1.`,
        { field: `items[${i}].qty` });
    }
    if (!isInt(line.expected_price_paise) || line.expected_price_paise < 0) {
      throw bad(400, 'MISSING_FIELD',
        `items[${i}].expected_price_paise must be an integer number of paise.`,
        { field: `items[${i}].expected_price_paise` });
    }
  }
  if (!isInt(body.expected_items_total_paise)) {
    throw bad(400, 'MISSING_FIELD',
      'expected_items_total_paise must be an integer number of paise.',
      { field: 'expected_items_total_paise' });
  }
  const contact = {
    name: requireString(body.contact, 'name', 'contact.name'),
    // SPEC 6: phone MANDATORY hai — bina uske delivery hoti hi nahi.
    phone: requireString(body.contact, 'phone', 'contact.phone'),
    email: body.contact?.email ? String(body.contact.email) : null,
  };
  const address = {
    line1: requireString(body.address, 'line1', 'address.line1'),
    line2: body.address?.line2 ? String(body.address.line2) : null,
    city: requireString(body.address, 'city', 'address.city'),
    state: requireString(body.address, 'state', 'address.state'),
    pincode: requireString(body.address, 'pincode', 'address.pincode'),
    country: requireString(body.address, 'country', 'address.country'),
  };
  // SPEC 6 (v1.9): maujoodgi jaanchna shakal jaanchna nahi hai. `phone: "1"` aur
  // `country: "ZZ"` dono se yahan order ban jata tha — ek asli, chuka hua order jise koi
  // deliver nahi kar sakta. Aur pincode ki shakal galat ho to wo `NOT_SERVICEABLE` nahi
  // hai: SPEC v1.8 ka farq — "request theek karo" (400) vs "doosri dukaan dekho" (409) —
  // is raaste par bhi utna hi laagu hai jitna product detail par.
  if (!/^\+[1-9]\d{7,14}$/.test(contact.phone)) {
    throw bad(400, 'MISSING_FIELD',
      'contact.phone must be an E.164 number like +919876543210.',
      { field: 'contact.phone', phone: contact.phone });
  }
  if (!/^\d{6}$/.test(address.pincode)) {
    throw bad(400, 'MISSING_FIELD', 'address.pincode must be exactly 6 digits.',
      { field: 'address.pincode', pincode: address.pincode });
  }
  if (address.country.toUpperCase() !== 'IN') {
    throw bad(400, 'MISSING_FIELD',
      "Voltline delivers within India only; address.country must be 'IN'.",
      { field: 'address.country', country: address.country });
  }

  const mode = requireString(body, 'payment_mode', 'payment_mode');
  if (!store.PAYMENT_MODES.includes(mode)) {
    throw bad(400, 'UNSUPPORTED_PAYMENT_MODE',
      `payment_mode ${mode} is not offered by this store.`,
      { payment_mode: mode, supported: store.PAYMENT_MODES });
  }

  // --- idempotency: retry karta agent kabhi do order na banaye (SPEC 6)
  const hash = bodyHash(body);
  const seen = db.prepare('SELECT * FROM idempotency WHERE key = ?').get(idempotencyKey);
  if (seen) {
    if (seen.body_hash !== hash) {
      throw bad(409, 'IDEMPOTENCY_CONFLICT',
        'This Idempotency-Key was already used with a different request body.',
        { idempotency_key: idempotencyKey });
    }
    const original = db.prepare('SELECT * FROM orders WHERE order_id = ?').get(seen.order_id);
    if (original) return creationView(original);
  }

  // --- live verification. Sab kuch DB se dobara padha jata hai.
  const lines = [];
  let itemsTotal = 0;
  for (const line of body.items) {
    const variant = db.prepare('SELECT * FROM variants WHERE variant_id = ?').get(line.variant_id);
    if (!variant) {
      throw bad(400, 'INVALID_VARIANT', `Unknown variant_id ${line.variant_id}.`,
        { variant_id: line.variant_id });
    }
    // Ceiling request ki property hai, inventory ki nahi — isliye stock se PEHLE.
    if (line.qty > store.MAX_QTY_PER_VARIANT) {
      throw bad(409, 'QTY_LIMIT_EXCEEDED',
        `This store allows at most ${store.MAX_QTY_PER_VARIANT} units of a variant per order.`,
        { variant_id: line.variant_id, requested: line.qty,
          max_qty_per_variant: store.MAX_QTY_PER_VARIANT });
    }
    if (variant.price_paise !== line.expected_price_paise) {
      throw bad(409, 'PRICE_CHANGED',
        `Price for ${line.variant_id} changed since it was quoted.`,
        { variant_id: line.variant_id,
          expected_price_paise: line.expected_price_paise,
          actual_price_paise: variant.price_paise });
    }
    if (variant.stock < line.qty) {
      throw bad(409, 'OUT_OF_STOCK', `Only ${variant.stock} left of ${line.variant_id}.`,
        { variant_id: line.variant_id, requested: line.qty, available: variant.stock });
    }
    const product = db.prepare('SELECT title FROM products WHERE product_id = ?')
      .get(variant.product_id);
    const opts = Object.values(jl(variant.options) || {}).join(' / ');
    lines.push({
      variant_id: variant.variant_id,
      product_id: variant.product_id,
      title: opts ? `${product.title} — ${opts}` : product.title,
      qty: line.qty,
      unit_price_paise: variant.price_paise,
      line_total_paise: variant.price_paise * line.qty,
    });
    itemsTotal += variant.price_paise * line.qty;
  }

  if (itemsTotal !== body.expected_items_total_paise) {
    throw bad(409, 'TOTAL_CHANGED', 'Items total does not match what was expected.',
      { expected_items_total_paise: body.expected_items_total_paise,
        actual_items_total_paise: itemsTotal });
  }

  if (!store.isServiceable(address.pincode)) {
    throw bad(409, 'NOT_SERVICEABLE', `Voltline does not deliver to ${address.pincode}.`,
      { pincode: address.pincode });
  }

  const coupon = body.coupon_code ? String(body.coupon_code) : null;
  const applied = store.discountFor(coupon, itemsTotal);
  if (coupon && applied.discount === undefined) {
    throw bad(409, 'INVALID_COUPON', applied.reason, { coupon_code: coupon, ...applied });
  }
  const discount = applied.discount ?? 0;
  const shipping = store.shippingFor(itemsTotal);
  const finalTotal = itemsTotal + shipping - discount;

  const createdAt = nowIso();
  const id = orderId();
  const expiresAt = mode === 'cod'
    ? null
    : plusHours(createdAt, store.UNPAID_ORDER_TTL_MINUTES / 60);

  // --- instrument. Paisa yahan bhi nahi hilta (SPEC 6) — sirf ek payable cheez banti hai.
  let payment = {};
  if (mode === 'payment_link') {
    const link = await rzp.createPaymentLink({
      amountPaise: finalTotal,
      description: `${store.MERCHANT_NAME} order ${id}`,
      contact,
      expiresAtIso: expiresAt,
    });
    payment = { razorpay_link_id: link.id, link_url: link.url };
  } else if (mode === 'checkout') {
    const providerOrder = await rzp.createCheckoutOrder({ amountPaise: finalTotal, receipt: id });
    payment = { razorpay_order_id: providerOrder.id, razorpay_key_id: rzp.keyId() };
  }

  // SPEC 6: waada AB banta hai, aur baad me kabhi dobara compute nahi hota.
  const etaDays = store.etaDaysFor(address.pincode);
  const delivery = {
    eta_days: etaDays,
    promised_by: plusDays(createdAt, etaDays),
    pincode: address.pincode,
  };

  db.exec('BEGIN');
  try {
    for (const line of lines) {
      db.prepare('UPDATE variants SET stock = stock - ? WHERE variant_id = ?')
        .run(line.qty, line.variant_id);
    }
    bumpTouchedProducts(db, lines);
    db.prepare(`INSERT INTO orders (order_id,status,items,items_total_paise,shipping_paise,
        discount_paise,final_total_paise,contact,address,payment_mode,coupon_code,payment,
        refund,delivery,timeline,created_at,cancellable_until,expires_at,cancelled_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
      .run(id, 'created', jd(lines), itemsTotal, shipping, discount, finalTotal,
        jd(contact), jd(address), mode, coupon, jd(payment), null, jd(delivery),
        jd([{ status: 'created', at: createdAt }]), createdAt,
        plusHours(createdAt, store.CANCEL_WINDOW_HOURS), expiresAt, null);
    db.prepare('INSERT INTO idempotency (key,body_hash,order_id,created_at) VALUES (?,?,?,?)')
      .run(idempotencyKey, hash, id, createdAt);
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }

  return creationView(db.prepare('SELECT * FROM orders WHERE order_id = ?').get(id));
}

// ---------------------------------------------------------------- cancel

async function cancelOrder(db, req) {
  const reason = req.body?.reason;
  // SPEC 8: bina wajah ki cancellation auditable nahi hai.
  if (typeof reason !== 'string' || !reason.trim()) {
    throw bad(400, 'MISSING_FIELD', 'reason is required.', { field: 'reason' });
  }
  if (reason.length > 200) {
    throw bad(400, 'MISSING_FIELD', 'reason must be at most 200 characters.', { field: 'reason' });
  }

  let order = db.prepare('SELECT * FROM orders WHERE order_id = ?').get(req.params.order_id);
  if (!order) {
    throw bad(404, 'ORDER_NOT_FOUND', `No order with id ${req.params.order_id}.`,
      { order_id: req.params.order_id });
  }

  // Cancel karne se pehle sach maloom hona chahiye: ho sakta hai beech me pay ho gaya ho,
  // ya expire ho chuka ho.
  try { order = await refreshOrder(db, order); } catch { /* provider chup — aage badho */ }

  // SPEC 8: cancellation idempotent hai. Cancelled order dobara cancel karna error nahi.
  if (['cancelled', 'refunded'].includes(order.status)) {
    return cancelView(order);
  }

  const { cancellable } = cancellability(order);
  if (!cancellable) {
    throw bad(409, 'ORDER_NOT_CANCELLABLE',
      `An order in status ${order.status} can no longer be cancelled.`,
      { order_id: order.order_id, status: order.status,
        cancel_window_hours: store.CANCEL_WINDOW_HOURS,
        cancellable_until: order.cancellable_until });
  }

  const payment = jl(order.payment) || {};
  const at = nowIso();
  let refund = null;

  if (payment.payment_id) {
    try {
      const created = await rzp.createRefund(payment.payment_id, order.final_total_paise);
      refund = {
        state: created.state,
        amount_paise: order.final_total_paise,
        razorpay_refund_id: created.id,
        expected_by: plusDays(at, 5),
      };
    } catch (err) {
      if (err instanceof rzp.ProviderError || err instanceof rzp.ProviderUnreachable) {
        // Refund ka fail hona chhupaya nahi jata: order phir bhi cancel hota hai aur
        // stock wapas aata hai, par state wahi kehti hai jo sach me hua.
        refund = {
          state: 'failed',
          amount_paise: order.final_total_paise,
          razorpay_refund_id: null,
          expected_by: null,
          provider_message: err.message,
        };
      } else throw err;
    }
  }

  db.exec('BEGIN');
  try {
    for (const line of jl(order.items)) {
      db.prepare('UPDATE variants SET stock = stock + ? WHERE variant_id = ?')
        .run(line.qty, line.variant_id);
    }
    bumpTouchedProducts(db, jl(order.items));
    db.prepare(`UPDATE orders SET status = ?, refund = ?, timeline = ?, cancelled_at = ?
                WHERE order_id = ?`)
      .run('cancelled', refund ? jd(refund) : null,
        timelineAdd(order, { status: 'cancelled', at, note: reason.trim() }),
        at, order.order_id);
    db.exec('COMMIT');
  } catch (err) { db.exec('ROLLBACK'); throw err; }

  return cancelView(db.prepare('SELECT * FROM orders WHERE order_id = ?').get(order.order_id));
}

function cancelView(order) {
  return {
    spec_version: SPEC_VERSION,
    order_id: order.order_id,
    status: order.status,
    refund: jl(order.refund),
    cancelled_at: order.cancelled_at,
  };
}
