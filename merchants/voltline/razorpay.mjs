// Razorpay, seedhe REST par. Koi SDK nahi — Node ka apna `fetch` kaafi hai, aur raw
// body dekhna yahan zaroori hai: provider ka "amount bahut bada hai" aur "humara code
// toot gaya" ek jaise dikh sakte hain, aur unhe alag karna hi is file ka poora kaam hai.
//
// SPEC 11 teen matlab maangti hai, aur teenon alag hain:
//   500  humara code toota      -> agent haar jata hai
//   429  thodi der baad phir     -> agent retry karta hai
//   409  aise kabhi nahi chalega -> agent request badalta hai
// Provider ki har dikkat ko 500 bana dena ek recoverable haalat ko chhoda hua checkout
// bana deta hai.

const API = 'https://api.razorpay.com/v1';

// Keys PADHTE waqt uthayi jati hain, import waqt nahi — aur wo ek asli bug tha.
// ESM me saare `import` module ke apne code se PEHLE chalte hain, to `main.mjs` ka
// `process.loadEnvFile()` in constants ke baad chalta tha: key khaali milti thi,
// `configured` false hota tha, aur har order `RATE_LIMITED` par gir jata tha —
// theek waise jaise provider sach me neeche ho. Ek function ye poora tareeka hata deta hai.
export const keyId = () => process.env.RAZORPAY_KEY_ID || '';
const secret = () => process.env.RAZORPAY_KEY_SECRET || '';
export const configured = () => Boolean(keyId() && secret());

/** Merchant apne codes me bolta hai, provider ke nahi — call site ko sirf ye dikhta hai. */
export class ProviderError extends Error {
  constructor(code, status, message, details = {}, retryAfter = null) {
    super(message);
    this.code = code;
    this.status = status;
    this.details = details;
    this.retryAfter = retryAfter;
  }
}

/** Provider tak baat hi nahi pahunchi. Ye "unpaid" ka saboot NAHI hai — SPEC 9. */
export class ProviderUnreachable extends Error {}

function auth() {
  return 'Basic ' + Buffer.from(`${keyId()}:${secret()}`).toString('base64');
}

async function call(method, route, body, { form = false } = {}) {
  if (!configured()) throw new ProviderUnreachable('RAZORPAY_KEY_ID / _SECRET not set');

  const headers = { Authorization: auth() };
  let payload;
  if (body && form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
    payload = new URLSearchParams(body).toString();
  } else if (body) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(API + route, {
      method, headers, body: payload, signal: AbortSignal.timeout(20_000),
    });
  } catch (err) {
    // Network, DNS, timeout. Kuch pata nahi chala — call site ko yahi batana hai.
    throw new ProviderUnreachable(String(err?.message || err));
  }

  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { /* provider ne JSON nahi bheja */ }

  if (res.ok) return data;

  const description = data?.error?.description || text.slice(0, 300) || res.statusText;
  const providerMessage = String(description);

  // "Thodi der baad phir" — 500 nahi. Ek agent 500 par haar jata hai aur 429 par rukta hai.
  if (res.status === 429 || /too many requests|rate limit/i.test(providerMessage)) {
    const retryAfter = Number(res.headers.get('retry-after')) || 5;
    throw new ProviderError('RATE_LIMITED', 429,
      'Payment provider is rate limiting this store. Try again shortly.',
      { provider_message: providerMessage }, retryAfter);
  }

  // Har instrument ki apni amount ceiling hai (link, UPI, card — sabki alag). Ye
  // rejection is order ke liye PERMANENT hai aur agent use theek kar sakta hai:
  // chhota order, ya doosra mode. Isliye 409, 500 nahi.
  if (/amount/i.test(providerMessage) && res.status >= 400 && res.status < 500) {
    throw new ProviderError('AMOUNT_LIMIT_EXCEEDED', 409,
      'The payment provider refused this order because of its amount.',
      { provider_message: providerMessage });
  }

  throw new ProviderError('INTERNAL_ERROR', 500,
    'Payment provider call failed.', { provider_message: providerMessage });
}

export async function createPaymentLink({ amountPaise, description, contact, expiresAtIso }) {
  const link = await call('POST', '/payment_links', {
    amount: amountPaise,
    currency: 'INR',
    description: description.slice(0, 250),
    customer: {
      name: contact.name,
      contact: contact.phone,
      ...(contact.email ? { email: contact.email } : {}),
    },
    notify: { sms: false, email: false },
    reminder_enable: false,
    // Razorpay ka apna minimum 15 minute hai; order ki ghadi humari hai (SPEC 9),
    // ye sirf provider ki taraf ka darwaza band karta hai.
    expire_by: Math.floor(Date.parse(expiresAtIso) / 1000),
  });
  return { id: link.id, url: link.short_url };
}

export async function createCheckoutOrder({ amountPaise, receipt }) {
  const order = await call('POST', '/orders', {
    amount: amountPaise, currency: 'INR', receipt: receipt.slice(0, 40),
  });
  return { id: order.id };
}

/**
 * "Kya ye pay ho gaya?" — dono instruments ke liye ek hi jawab shape.
 * `null` ka matlab hai "abhi tak nahi", aur ye tabhi lauta ta hai jab provider ne
 * SAAF-SAAF bataya ho. Baat na ho paye to ProviderUnreachable uthta hai, kyunki
 * timeout par likha gaya "unpaid" kisi ke poore ho chuke payment ke upar likha ja
 * sakta hai (SPEC 9).
 */
export async function fetchPayment({ mode, linkId, orderId }) {
  if (mode === 'payment_link') {
    const link = await call('GET', `/payment_links/${linkId}`);
    if (link.status !== 'paid') return null;
    const paid = (link.payments || []).find((p) => p.status === 'captured')
      || (link.payments || [])[0];
    return paid
      ? { payment_id: paid.payment_id, paid_at: isoFromEpoch(paid.created_at) }
      : null;
  }

  const res = await call('GET', `/orders/${orderId}/payments`);
  const captured = (res.items || []).find((p) => p.status === 'captured');
  return captured
    ? { payment_id: captured.id, paid_at: isoFromEpoch(captured.created_at) }
    : null;
}

export async function createRefund(paymentId, amountPaise) {
  const refund = await call('POST', `/payments/${paymentId}/refund`,
    { amount: amountPaise, speed: 'normal' });
  return { id: refund.id, state: normaliseRefund(refund.status) };
}

export async function fetchRefund(refundId) {
  const refund = await call('GET', `/refunds/${refundId}`);
  return { id: refund.id, state: normaliseRefund(refund.status) };
}

/**
 * Bheja hua refund aur aaya hua refund do alag baatein hain (SPEC 7).
 * Razorpay `created` / `pending` / `processed` / `failed` bolta hai; jab tak wo
 * `processed` na kahe, paisa grahak tak pahuncha nahi hai.
 */
function normaliseRefund(status) {
  if (status === 'processed') return 'processed';
  if (status === 'failed') return 'failed';
  if (status === 'created') return 'initiated';
  return 'pending';
}

const isoFromEpoch = (epoch) =>
  epoch ? new Date(epoch * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z') : null;
