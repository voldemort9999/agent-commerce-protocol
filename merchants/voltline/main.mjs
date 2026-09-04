// Voltline Electronics — Merchant B. Express + Node, port 8002.
//
//   node merchants/voltline/main.mjs
//     shop        http://127.0.0.1:8002/
//     one product http://127.0.0.1:8002/p/vl-121
//     contract    http://127.0.0.1:8002/agent/manifest   (needs X-Agent-Key)
//
// Northwind (FastAPI + Python) aur Voltline (Express + Node) me ek line bhi saanjhi
// nahi hai — na code, na models, na DB driver, na HTTP client. Sirf `SPEC.md` saanjhi
// hai. Isi liye ek hi conformance suite dono par bina badle chalti hai.

import express from 'express';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import * as db from './db.mjs';
import { agentRouter, fail } from './agent.mjs';
import { shopRouter } from './shop.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENV = path.join(HERE, '..', '..', '.env');
if (fs.existsSync(ENV)) process.loadEnvFile(ENV);   // Node 20.12+, koi dotenv dep nahi

// Ek hosted platform apna port ENV se deta hai aur loopback par bindings ko dekh hi
// nahi sakta. Default 127.0.0.1 par hi rehta hai — local bartav, docs aur scripts sab
// waise ke waise — aur sirf ek host use badal sakta hai.
const PORT = Number(process.env.PORT || process.env.VOLTLINE_PORT || 8002);
const HOST = process.env.HOST || '127.0.0.1';

if (!fs.existsSync(db.DB_PATH)) {
  console.error(`voltline.db nahi mili. Pehle chalao:  node ${path.join(HERE, 'seed.mjs')}`);
  process.exit(1);
}

const conn = db.connect();
const app = express();
app.disable('x-powered-by');

app.use(express.json({ limit: '256kb' }));

// SPEC 11: required field absent YA malformed -> 400 MISSING_FIELD, humare envelope me.
// Express apna HTML error page deta hai, aur ek agent uspe kuch nahi kar sakta.
app.use((err, req, res, next) => {
  if (err && (err.type === 'entity.parse.failed' || err instanceof SyntaxError)) {
    return fail(res, 400, 'MISSING_FIELD', 'Request body is not valid JSON.',
      { detail: err.message });
  }
  return next(err);
});

app.use('/agent', agentRouter(conn));
app.use('/', shopRouter(conn));

// /agent ke andar koi anjaan raasta -> spec envelope, HTML nahi
app.use('/agent', (req, res) =>
  fail(res, 404, 'PRODUCT_NOT_FOUND', `No such endpoint: ${req.method} /agent${req.path}`));

app.use((err, req, res, next) => {          // eslint-disable-line no-unused-vars
  console.error('[voltline] unhandled:', err);
  if (res.headersSent) return;
  fail(res, 500, 'INTERNAL_ERROR', 'Something went wrong on this store.');
});

const server = app.listen(PORT, HOST, () => {
  console.log(`Voltline Electronics  ->  http://127.0.0.1:${PORT}/`);
  console.log(`  agent contract      ->  http://127.0.0.1:${PORT}/agent/manifest  (X-Agent-Key)`);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => { server.close(); conn.close(); process.exit(0); });
}
