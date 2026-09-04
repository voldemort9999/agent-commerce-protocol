// Voltline ka insaani darwaza — do pages, server-rendered, koi build step nahi, koi JS nahi.
//
// Ye page wahi `store.mjs` padhta hai jo `/agent/*` padhta hai. Isliye demo ka split
// screen dikhawa nahi hai: baayen browser me jo daam, stock aur variant dikhta hai wo
// theek wahi row hai jo daayen taraf agent ke JSON me hai.
//
// Design ek jaan-boojhkar faisla hai: ye page ek DATASHEET ki tarah padha jata hai —
// part number, tabulated specs, aur har variant apni line par apne daam aur stock ke
// saath. Wajah demo hai: dekhne wale ko do taraf ke numbers line-by-line milane hote
// hain, aur ek boutique-style page wo kaam mushkil banata hai. Northwind halka aur
// kapdon wala hai; Voltline gehra aur upkaran wala. Ek nazar me alag dukaan.

import express from 'express';
import * as store from './store.mjs';

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

const rupees = (paise) => '₹' + (paise / 100).toLocaleString('en-IN',
  { minimumFractionDigits: 0, maximumFractionDigits: 0 });

const CSS = `
:root{
  --ink:#0E1A2B; --ink-2:#16273D; --line:#24384F;
  --text:#E6EDF5; --muted:#8FA6C0; --signal:#FFB020; --good:#4ED2A0; --low:#FF7A6B;
  --tile:#F2F5F8;
  /* Page jaan-boojhkar ek hi roop me hai — ye rack-mount upkaran ka faceplate hai,
     aur wahi Northwind ke halke, kapdon wale page se ise ek nazar me alag karta hai.
     color-scheme isliye ki browser ke apne controls (scrollbar, input ka caret,
     autofill) bhi usi taraf rahen; bina iske wo OS ke light theme se aate hain aur
     poore page par ek safed dhabba dikhta hai.
     (Aur haan — is CSS block ke andar backtick nahi likh sakte, wo template literal
      hi khatam kar deta hai. Ye galti abhi ek baar ho chuki hai.) */
  color-scheme:dark;
}
*{box-sizing:border-box}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0 0 0 0);white-space:nowrap;border:0}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--ink); color:var(--text);
  font-family:Archivo,"Segoe UI",system-ui,sans-serif;
  font-size:15px; line-height:1.5;
}
.mono{font-family:"IBM Plex Mono",ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums}
a{color:inherit;text-decoration:none}
a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
img{max-width:100%;display:block}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px}

/* ---- masthead: ek patli patti, jaise rack-mount upkaran ka faceplate ---- */
.mast{border-bottom:1px solid var(--line);background:linear-gradient(180deg,#122034,#0E1A2B)}
.mast .wrap{display:flex;align-items:center;gap:20px;height:66px}
.logo{display:flex;align-items:baseline;gap:9px;font-weight:700;letter-spacing:-.02em;font-size:19px}
.logo .dot{width:9px;height:9px;border-radius:50%;background:var(--signal);
  box-shadow:0 0 0 3px rgba(255,176,32,.16);align-self:center}
.logo small{font-weight:500;color:var(--muted);font-size:12px;letter-spacing:.04em}
.find{flex:1;max-width:420px;position:relative}
.find input{width:100%;background:#0B1524;border:1px solid var(--line);color:var(--text);
  border-radius:7px;padding:9px 12px;font:inherit}
.find input::placeholder{color:#6B819B}
.mast .count{margin-left:auto;color:var(--muted);font-size:12.5px}

/* ---- readout strip: dukaan ka apna status, upkaran ke display jaisa ---- */
.readout{border-bottom:1px solid var(--line);background:#0B1524}
.readout .wrap{display:flex;flex-wrap:wrap;gap:0;align-items:stretch}
.cell{padding:13px 22px 14px 0;margin-right:22px;border-right:1px solid var(--line)}
.cell:last-child{border-right:0}
.cell b{display:block;font-family:"IBM Plex Mono",monospace;font-size:16px;font-weight:600;
  color:var(--signal);font-variant-numeric:tabular-nums}
.cell span{font-size:11.5px;color:var(--muted)}
.cell.plain b{color:var(--text)}

/* ---- layout ---- */
.page{display:grid;grid-template-columns:186px 1fr;gap:34px;padding:30px 0 64px}
.rail h3{margin:0 0 10px;font-size:11.5px;font-weight:600;color:var(--muted);letter-spacing:.06em}
.rail a{display:block;padding:7px 10px;border-radius:6px;color:var(--muted);font-size:13.5px}
.rail a:hover{background:var(--ink-2);color:var(--text)}
.rail a.on{background:var(--ink-2);color:var(--text);box-shadow:inset 2px 0 0 var(--signal)}
.rail .n{float:right;font-family:"IBM Plex Mono",monospace;font-size:12px;opacity:.7}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(216px,1fr));gap:16px}
.card{background:var(--ink-2);border:1px solid var(--line);border-radius:10px;overflow:hidden;
  display:flex;flex-direction:column}
.card:hover{border-color:#38536F}
.card .tile{background:var(--tile);aspect-ratio:4/3;display:grid;place-items:center;padding:14px}
.card .tile img{max-height:100%;object-fit:contain;mix-blend-mode:multiply}
.card .body{padding:12px 13px 14px;display:flex;flex-direction:column;gap:7px;flex:1}
.card h2{margin:0;font-size:14px;font-weight:500;line-height:1.35}
.card .brand{font-size:11px;color:var(--muted);letter-spacing:.03em}
.card .price{font-family:"IBM Plex Mono",monospace;font-size:16px;font-weight:600;margin-top:auto}
.card .price em{font-style:normal;font-size:12px;color:var(--muted);font-weight:400}
.meta{display:flex;justify-content:space-between;align-items:center;gap:8px;
  font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted)}
.stock{color:var(--good)} .stock.out{color:var(--low)}

/* ---- detail ---- */
.crumb{padding:22px 0 0;font-size:12.5px;color:var(--muted)}
.crumb a:hover{color:var(--text)}
.detail{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,420px);gap:40px;padding:20px 0 20px}
.shots{display:grid;grid-template-columns:66px 1fr;gap:12px}
.shots .strip{display:flex;flex-direction:column;gap:9px}
.shots .strip a{background:var(--tile);border-radius:7px;padding:6px;aspect-ratio:1;
  display:grid;place-items:center;border:1px solid transparent}
.shots .strip a.on{border-color:var(--signal)}
.shots .strip img{mix-blend-mode:multiply}
.shots .hero{background:var(--tile);border-radius:12px;padding:26px;display:grid;place-items:center;
  aspect-ratio:1}
.shots .hero img{max-height:100%;object-fit:contain;mix-blend-mode:multiply}

h1{margin:0 0 6px;font-size:27px;line-height:1.2;font-weight:600;letter-spacing:-.02em}
.by{color:var(--muted);font-size:13px;margin-bottom:18px}
.bigprice{font-family:"IBM Plex Mono",monospace;font-size:34px;font-weight:600;letter-spacing:-.02em}
.bigprice s{color:var(--muted);font-size:17px;font-weight:400;margin-left:10px}
.tag{display:inline-block;margin-left:9px;padding:2px 7px;border-radius:5px;font-size:11.5px;
  background:rgba(255,176,32,.14);color:var(--signal);vertical-align:middle}

.panel{margin-top:22px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.panel > header{padding:9px 14px;background:#0B1524;border-bottom:1px solid var(--line);
  font-size:11.5px;color:var(--muted);display:flex;justify-content:space-between;gap:10px}
.opt{display:flex;justify-content:space-between;align-items:center;gap:14px;
  padding:10px 14px;border-bottom:1px solid var(--line);font-size:13.5px}
.opt:last-child{border-bottom:0}
.opt.on{background:rgba(255,176,32,.07);box-shadow:inset 3px 0 0 var(--signal)}
.opt.dead{color:#6B819B}
.opt .id{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted);display:block}
.opt .num{font-family:"IBM Plex Mono",monospace;font-size:13.5px;text-align:right;white-space:nowrap}

.pin{display:flex;gap:8px;padding:12px 14px}
.pin input{flex:1;min-width:0;background:#0B1524;border:1px solid var(--line);color:var(--text);
  border-radius:6px;padding:8px 10px;font:inherit}
.pin button{background:var(--signal);color:#10203A;border:0;border-radius:6px;padding:8px 15px;
  font:inherit;font-weight:600;cursor:pointer}
.verdict{padding:0 14px 13px;font-size:13px}
.verdict.no{color:var(--low)}

table.spec{width:100%;border-collapse:collapse;font-size:13.5px}
table.spec td{padding:9px 14px;border-bottom:1px solid var(--line);vertical-align:top}
table.spec tr:last-child td{border-bottom:0}
table.spec td:first-child{color:var(--muted);width:38%}
table.spec td:last-child{font-family:"IBM Plex Mono",monospace}

.copy{padding:14px;font-size:14px;line-height:1.65;white-space:pre-wrap;color:#CFDBE9;
  max-width:68ch}
.rev{padding:13px 14px;border-bottom:1px solid var(--line)}
.rev:last-child{border-bottom:0}
.rev .who{font-size:12.5px;color:var(--muted);margin-bottom:4px}
.rev .stars{color:var(--signal);letter-spacing:1px}
.rel{display:flex;flex-wrap:wrap;gap:8px;padding:13px 14px}
.rel a{font-family:"IBM Plex Mono",monospace;font-size:12px;padding:5px 9px;border-radius:6px;
  background:var(--ink-2);border:1px solid var(--line);color:var(--muted)}
.rel a:hover{color:var(--text);border-color:#38536F}

.foot{border-top:1px solid var(--line);margin-top:40px;padding:20px 0 44px;color:var(--muted);
  font-size:12.5px}
.foot code{font-family:"IBM Plex Mono",monospace;color:var(--signal)}
.empty{padding:60px 0;color:var(--muted)}

@media (max-width:900px){
  .page{grid-template-columns:1fr;gap:22px}
  .rail{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px}
  .rail h3{display:none} .rail .n{display:none}
  .rail a{white-space:nowrap}
  .detail{grid-template-columns:1fr;gap:26px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
`;

function shell(title, body) {
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>${CSS}</style>
</head><body>${body}</body></html>`;
}

const masthead = (q, count) => `
<header class="mast"><div class="wrap">
  <a class="logo" href="/"><span class="dot"></span>Voltline <small>ELECTRONICS</small></a>
  <form class="find" action="/" role="search">
    <input name="q" value="${esc(q)}" placeholder="Search the shelf" aria-label="Search products">
  </form>
  <span class="count mono">${count} products</span>
</div></header>`;

const footer = (line) => `
<footer class="foot"><div class="wrap">${line}</div></footer>`;

// ---------------------------------------------------------------- pages

function gridPage(db, { q, category }) {
  const all = store.allProducts(db);
  const counts = {};
  for (const p of all) counts[p.category] = (counts[p.category] || 0) + 1;

  const needle = q.trim().toLowerCase();
  const shown = all.filter((p) => {
    if (category && p.category !== category) return false;
    if (!needle) return true;
    return [p.title, p.brand, p.category, ...(p.tags || [])]
      .filter(Boolean).join(' ').toLowerCase().includes(needle);
  });

  const stats = store.catalogStats(db);
  const inStock = all.filter((p) => store.variantsOf(db, p.product_id).some((v) => v.stock > 0)).length;

  const rail = `
  <nav class="rail" aria-label="Categories">
    <h3>Shelf</h3>
    <a href="/${q ? '?q=' + encodeURIComponent(q) : ''}" class="${category ? '' : 'on'}">
      Everything <span class="n">${all.length}</span></a>
    ${Object.keys(counts).sort().map((c) => `
    <a href="/?category=${encodeURIComponent(c)}${q ? '&q=' + encodeURIComponent(q) : ''}"
       class="${c === category ? 'on' : ''}">${esc(c.replace(/-/g, ' '))}
       <span class="n">${counts[c]}</span></a>`).join('')}
  </nav>`;

  const cards = shown.map((p) => {
    const variants = store.variantsOf(db, p.product_id);
    const range = store.priceRange(variants);
    const units = variants.reduce((n, v) => n + v.stock, 0);
    return `
    <a class="card" href="/p/${esc(p.product_id)}">
      <div class="tile">${p.images?.[0]
        ? `<img src="${esc(p.images[0])}" alt="${esc(p.title)}" loading="lazy">` : ''}</div>
      <div class="body">
        <span class="brand">${esc(p.brand || p.category.replace(/-/g, ' '))}</span>
        <h2>${esc(p.title)}</h2>
        <p class="price">${rupees(range.min)}${range.max !== range.min
          ? ` <em>to ${rupees(range.max)}</em>` : ''}</p>
        <p class="meta"><span>${esc(p.product_id)}</span>
          <span class="stock ${units ? '' : 'out'}">${units ? units + ' in stock' : 'sold out'}</span></p>
      </div>
    </a>`;
  }).join('');

  return shell(`Voltline Electronics${category ? ' — ' + category : ''}`, `
${masthead(q, all.length)}
<section class="readout"><div class="wrap">
  <div class="cell"><b>${stats.product_count}</b><span>products on the shelf</span></div>
  <div class="cell"><b>${inStock}</b><span>with stock right now</span></div>
  <div class="cell"><b>${(store.SHIPPING_FLAT_PAISE / 100).toFixed(0)}</b>
    <span>flat shipping, free over ${rupees(store.FREE_SHIPPING_ABOVE_PAISE)}</span></div>
  <div class="cell plain"><b>${store.MAX_QTY_PER_VARIANT}</b><span>max units per variant</span></div>
  <div class="cell plain"><b>${store.CANCEL_WINDOW_HOURS}h</b><span>to cancel an order</span></div>
</div></section>

<div class="wrap"><div class="page">
  ${rail}
  <main>
    <h1 class="sr-only">${category ? esc(category.replace(/-/g, ' ')) : 'Everything'} at
      Voltline Electronics${q ? `, matching “${esc(q)}”` : ''}</h1>
    ${shown.length
      ? `<div class="grid">${cards}</div>`
      : `<p class="empty">Nothing on the shelf matches “${esc(q)}”. Try a shorter word,
         or pick a category on the left.</p>`}
  </main>
</div></div>
${footer(`This page and <code>/agent/catalog</code> read the same rows of the same database.
  Nothing here is a copy of anything.`)}`);
}

function detailPage(db, product, { variantId, pincode, shot }) {
  const variants = store.variantsOf(db, product.product_id);
  const chosen = variants.find((v) => v.variant_id === variantId)
    || variants.find((v) => v.stock > 0) || variants[0];
  // Variant ke apne photos hon to wahi dikhte hain — warna product ke. Yahan koi
  // andaza nahi lagaya jata: jo photo variant ka hai wahi variant ka kehlata hai.
  const shots = (chosen?.images?.length ? chosen.images : product.images) || [];
  const shotIndex = Number.isInteger(shot) && shot >= 0 && shot < shots.length ? shot : 0;
  const delivery = /^\d{6}$/.test(pincode || '') ? store.deliveryFor(pincode) : null;
  const keep = (extra) => {
    const qs = new URLSearchParams();
    if (chosen) qs.set('variant', chosen.variant_id);
    if (pincode) qs.set('pincode', pincode);
    for (const [k, v] of Object.entries(extra)) qs.set(k, v);
    return `/p/${product.product_id}?${qs.toString()}`;
  };

  const optionLabel = (v) => {
    const parts = Object.entries(v.options || {});
    return parts.length ? parts.map(([k, val]) => `${k} ${val}`).join(' · ') : 'Standard';
  };

  const variantRows = variants.map((v) => `
    <a class="opt ${v.variant_id === chosen.variant_id ? 'on' : ''} ${v.stock ? '' : 'dead'}"
       href="/p/${esc(product.product_id)}?variant=${esc(v.variant_id)}${
         pincode ? '&pincode=' + esc(pincode) : ''}">
      <span>${esc(optionLabel(v))}<span class="id">${esc(v.variant_id)}</span></span>
      <span class="num">${rupees(v.price_paise)}<br>
        <span class="stock ${v.stock ? '' : 'out'}">${v.stock ? v.stock + ' left' : 'sold out'}</span>
      </span>
    </a>`).join('');

  const specRows = Object.entries(product.attributes || {}).map(([k, v]) => `
    <tr><td>${esc(k.replace(/_/g, ' '))}</td><td>${esc(v)}</td></tr>`).join('');

  const reviews = (product.reviews || []).map((r) => `
    <div class="rev">
      <p class="who"><span class="stars">${'★'.repeat(r.rating)}${'☆'.repeat(5 - r.rating)}</span>
        &nbsp;${esc(r.author)} &nbsp;<span class="mono">${esc((r.created_at || '').slice(0, 10))}</span></p>
      <p style="margin:0">${esc(r.body)}</p>
    </div>`).join('');

  return shell(`${product.title} — Voltline Electronics`, `
${masthead('', store.catalogStats(db).product_count)}
<div class="wrap">
  <p class="crumb"><a href="/">Shelf</a> /
    <a href="/?category=${encodeURIComponent(product.category)}">${esc(product.category.replace(/-/g, ' '))}</a>
    / <span class="mono">${esc(product.product_id)}</span></p>

  <div class="detail">
    <div class="shots">
      <div class="strip">${shots.map((src, i) => `
        <a class="${i === shotIndex ? 'on' : ''}" href="${esc(keep({ shot: i }))}"
           aria-label="View ${i + 1}"${i === shotIndex ? ' aria-current="true"' : ''}>
          <img src="${esc(src)}" alt="" loading="lazy"></a>`).join('')}
      </div>
      <div class="hero">${shots[shotIndex]
        ? `<img src="${esc(shots[shotIndex])}" alt="${esc(product.title)}, view ${shotIndex + 1}">`
        : 'No photograph published'}</div>
    </div>

    <div>
      <h1>${esc(product.title)}</h1>
      <p class="by">${esc(product.brand || 'Unbranded')} ·
        ${product.rating_avg ? `${product.rating_avg.toFixed(1)} out of 5 from ${product.rating_count} reviews`
          : 'Not yet rated'}</p>

      <p class="bigprice">${rupees(chosen.price_paise)}${
        chosen.mrp_paise && chosen.mrp_paise > chosen.price_paise
          ? `<s>${rupees(chosen.mrp_paise)}</s>` : ''}${
        chosen.price_paise < 200000
          ? '<span class="tag">an agent can settle this on its own</span>' : ''}</p>

      <div class="panel">
        <header><span>${variants.length === 1 ? 'One configuration'
          : `${variants.length} configurations`}</span><span>price · units left</span></header>
        ${variantRows}
      </div>

      <form class="panel" method="get" action="/p/${esc(product.product_id)}">
        <header><span>Delivery</span><span>we ship from Bengaluru</span></header>
        ${chosen ? `<input type="hidden" name="variant" value="${esc(chosen.variant_id)}">` : ''}
        <div class="pin">
          <input name="pincode" value="${esc(pincode || '')}" inputmode="numeric" maxlength="6"
                 placeholder="6-digit pincode" aria-label="Delivery pincode">
          <button type="submit">Check</button>
        </div>
        ${delivery ? (delivery.serviceable
          ? `<p class="verdict">Arrives in about ${delivery.eta_days} business days.
             Shipping ${rupees(delivery.shipping_paise)}, free over
             ${rupees(store.FREE_SHIPPING_ABOVE_PAISE)}.</p>`
          : `<p class="verdict no">Voltline does not deliver to ${esc(delivery.pincode)} yet.</p>`)
          : (pincode ? `<p class="verdict no">A pincode is six digits — ${esc(pincode)} is not.</p>` : '')}
      </form>
    </div>
  </div>

  ${specRows ? `<div class="panel"><header><span>Specifications</span>
    <span>as published to buyers and to agents</span></header>
    <table class="spec"><tbody>${specRows}
      <tr><td>product id</td><td>${esc(product.product_id)}</td></tr>
      <tr><td>category</td><td>${esc(product.category)}</td></tr>
      <tr><td>last updated</td><td>${esc(product.updated_at)}</td></tr>
    </tbody></table></div>` : ''}

  <div class="panel"><header><span>About this product</span>
    <span>served byte-for-byte to every buyer</span></header>
    <p class="copy">${esc(product.description)}</p></div>

  ${reviews ? `<div class="panel"><header><span>Reviews</span>
    <span>${(product.reviews || []).length} most recent</span></header>${reviews}</div>` : ''}

  ${(product.related_ids || []).length ? `<div class="panel">
    <header><span>Also on this shelf</span><span></span></header>
    <div class="rel">${product.related_ids.map((id) =>
      `<a href="/p/${esc(id)}">${esc(id)}</a>`).join('')}</div></div>` : ''}
</div>
${footer(`Every number on this page comes from the same row an AI buyer reads through
  <code>/agent/products/${esc(product.product_id)}</code>.`)}`);
}

// ---------------------------------------------------------------- router

export function shopRouter(db) {
  const router = express.Router();

  router.get('/', (req, res) => {
    res.type('html').send(gridPage(db, {
      q: req.query.q ? String(req.query.q) : '',
      category: req.query.category ? String(req.query.category) : '',
    }));
  });

  router.get('/p/:product_id', (req, res) => {
    const product = store.productRow(db, req.params.product_id);
    if (!product) {
      return res.status(404).type('html').send(shell('Not found', `
        ${masthead('', store.catalogStats(db).product_count)}
        <div class="wrap"><p class="empty">No product called
          <span class="mono">${esc(req.params.product_id)}</span> on this shelf.
          <a href="/" style="color:var(--signal)">Back to the shelf</a>.</p></div>`));
    }
    res.type('html').send(detailPage(db, product, {
      variantId: req.query.variant ? String(req.query.variant) : null,
      pincode: req.query.pincode ? String(req.query.pincode).trim() : '',
      shot: req.query.shot === undefined ? 0 : Number(req.query.shot),
    }));
  });

  router.get('/healthz', (req, res) => res.json({ ok: true, merchant: store.MERCHANT_ID }));

  return router;
}
