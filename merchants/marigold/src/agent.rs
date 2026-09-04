//! SPEC ke chhe endpoints. Ek file, koi framework nahi, aur router ek `match` hai.

use crate::db;
use crate::razorpay::{self, Provider};
use crate::store;
use crate::util::{base64, base64_decode, iso, now};
use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};
use std::collections::HashMap;

pub struct Req {
    pub method: String,
    pub path: String,
    pub query: HashMap<String, String>,
    pub headers: HashMap<String, String>,
    pub body: String,
}

pub struct Res {
    pub status: u16,
    pub body: Value,
    /// `no-store`. SPEC 5: product detail **kabhi cache se nahi aata** — iske price aur
    /// stock wahi values hain jinpe paisa verify hota hai. Ek proxy jo ise cache kar le,
    /// wo ek purane daam par order banwa dega, aur wo galti kahin error nahi banti.
    pub no_store: bool,
    /// Sirf `429` par. SPEC 2.7 kehti hai *"You may return 429 with a Retry-After header
    /// in seconds. The Layer honours it"* — aur Layer me uska transport pehle se hai. Ye
    /// field na hone par wo vaada ek taraf se adhoora reh jata: Layer intezaar karne ko
    /// taiyar hai par merchant use bata hi nahi raha ki kitna.
    pub retry_after: Option<u64>,
}

pub fn ok(body: Value) -> Res {
    Res { status: 200, body, retry_after: None, no_store: false }
}

/// Har non-2xx ka ek hi envelope (SPEC 2.6). `details` optional hai par bina uske ek
/// agent sirf itna jaanta hai ki "na" hui — kis cheez par, ye nahi. Ek samjhaya hua "na"
/// sudhaara ja sakta hai; ek khaali "na" retry loop banata hai.
pub fn err(status: u16, code: &str, message: &str, details: Value) -> Res {
    let mut e = Map::new();
    e.insert("code".into(), json!(code));
    e.insert("message".into(), json!(message));
    if !details.is_null() {
        e.insert("details".into(), details);
    }
    Res {
        status,
        body: json!({ "error": Value::Object(e) }),
        retry_after: None,
        no_store: false,
    }
}

fn provider_error(p: Provider) -> Res {
    match p {
        Provider::Refused {
            code,
            status,
            message,
            provider_message,
            retry_after,
        } => {
            let mut res = err(
                status,
                code,
                &message,
                json!({ "provider_message": provider_message }),
            );
            res.retry_after = retry_after;
            res
        }
        // Provider tak baat hi nahi pahunchi — ye humara toota hua code nahi hai, par
        // agent ke liye iska matlab wahi hai: abhi nahi ho paya.
        Provider::Unreachable(why) => err(
            502,
            "INTERNAL_ERROR",
            "Payment provider could not be reached.",
            json!({ "provider_message": why }),
        ),
    }
}

// ------------------------------------------------------------------------ auth

pub fn authorised(req: &Req) -> bool {
    let expected = std::env::var("MARIGOLD_AGENT_KEY")
        .unwrap_or_else(|_| "mg_agentkey_local_dev_only".into());
    req.headers.get("x-agent-key").map(String::as_str) == Some(expected.as_str())
}

// ----------------------------------------------------------------- 1. manifest

pub fn manifest(conn: &Connection) -> Res {
    ok(store::manifest(conn))
}

// ------------------------------------------------------------------ 2. catalog

pub fn catalog(conn: &Connection, req: &Req) -> Res {
    let limit: i64 = req
        .query
        .get("limit")
        .and_then(|v| v.parse().ok())
        .unwrap_or(100)
        .clamp(1, 500);

    let since = req.query.get("updated_since").cloned();
    if let Some(s) = &since {
        // RFC 3339 UTC, `Z` ke saath. Yahan poora parser nahi hai aur zarurat bhi nahi:
        // in strings ki lexicographic tulna waqt ke kram wali tulna hai, isliye shakal
        // jaanchna hi kaafi hai.
        if s.len() != 20 || !s.ends_with('Z') || !s.contains('T') {
            return err(
                400,
                "MISSING_FIELD",
                "updated_since must be RFC 3339 UTC, like 2026-09-03T00:00:00Z.",
                json!({ "updated_since": s }),
            );
        }
    }

    // Cursor ke andar wahi (updated_at, product_id) hai jispe ordering khadi hai — yaani
    // cursor apne aap aage badhta hai. SPEC 4: `has_more: true` ke saath cursor dena
    // LAZMI hai, warna Layer ya usi page par ghoomti rahegi ya aadha catalog index karke
    // ruk jayegi — aur dono me koi error nahi aata.
    let cursor = match req.query.get("cursor") {
        None => None,
        Some(c) => match base64_decode(c)
            .and_then(|b| String::from_utf8(b).ok())
            .and_then(|s| s.split_once('|').map(|(a, b)| (a.to_string(), b.to_string())))
        {
            Some(pair) => Some(pair),
            None => {
                return err(
                    400,
                    "MISSING_FIELD",
                    "cursor is not a cursor this store issued.",
                    json!({ "cursor": c }),
                )
            }
        },
    };

    let mut sql = String::from("SELECT * FROM products WHERE 1=1");
    let mut args: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    if let Some(s) = &since {
        sql.push_str(" AND updated_at > ?");
        args.push(Box::new(s.clone()));
    }
    if let Some((ua, pid)) = &cursor {
        sql.push_str(" AND (updated_at, product_id) > (?, ?)");
        args.push(Box::new(ua.clone()));
        args.push(Box::new(pid.clone()));
    }
    // SPEC 4: kram `updated_at` phir `product_id`. Bina stable kram ke cursor rows chhod
    // deta hai, aur wo chhoot chup-chaap hoti hai.
    sql.push_str(" ORDER BY updated_at, product_id LIMIT ?");
    args.push(Box::new(limit + 1));

    let mut stmt = match conn.prepare(&sql) {
        Ok(s) => s,
        Err(e) => return err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null),
    };
    let refs: Vec<&dyn rusqlite::ToSql> = args.iter().map(|b| b.as_ref()).collect();
    let rows: Vec<Value> = match stmt.query_map(refs.as_slice(), |r| store::catalog_row(conn, r)) {
        Ok(it) => it.filter_map(|r| r.ok()).collect(),
        Err(e) => return err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null),
    };

    let has_more = rows.len() as i64 > limit;
    let page: Vec<Value> = rows.into_iter().take(limit as usize).collect();
    let next = if has_more {
        page.last().map(|p| {
            base64(
                format!(
                    "{}|{}",
                    p["updated_at"].as_str().unwrap_or(""),
                    p["product_id"].as_str().unwrap_or("")
                )
                .as_bytes(),
            )
        })
    } else {
        None
    };

    ok(json!({
        "spec_version": store::SPEC_VERSION,
        "products": page,
        "cursor": next,
        "has_more": has_more
    }))
}

// ------------------------------------------------------------ 3. product detail

pub fn product(conn: &Connection, req: &Req, product_id: &str) -> Res {
    if let Some(pin) = req.query.get("pincode") {
        // SPEC 5: galat shakal ka pincode "hum wahan deliver nahi karte" NAHI hai. Ek
        // kehta hai *request theek karo*, doosra *doosri dukaan dekho* — aur agent dono
        // par alag kaam karta hai.
        if pin.len() != 6 || !pin.bytes().all(|b| b.is_ascii_digit()) {
            return err(
                400,
                "MISSING_FIELD",
                "pincode must be exactly 6 digits.",
                json!({ "pincode": pin }),
            );
        }
    }
    match store::detail(conn, product_id, req.query.get("pincode").map(String::as_str)) {
        // Ye endpoint kabhi cache se nahi aata — iske price aur stock wo values hain
        // jinpe paisa verify hota hai (SPEC 5).
        Some(v) => {
            let mut res = ok(v);
            res.no_store = true;
            res
        }
        None => err(
            404,
            "PRODUCT_NOT_FOUND",
            &format!("No product with id {}.", product_id),
            json!({ "product_id": product_id }),
        ),
    }
}

// -------------------------------------------------------------- 4. create order

fn need_str(v: &Value, path: &[&str]) -> Result<String, Res> {
    let mut cur = v;
    for key in path {
        cur = cur.get(*key).unwrap_or(&Value::Null);
    }
    match cur.as_str().map(str::trim) {
        Some(s) if !s.is_empty() => Ok(s.to_string()),
        _ => Err(err(
            400,
            "MISSING_FIELD",
            &format!("{} is required and must be a non-empty string.", path.join(".")),
            json!({ "field": path.join(".") }),
        )),
    }
}

fn is_e164(phone: &str) -> bool {
    let b = phone.as_bytes();
    b.len() >= 9
        && b.len() <= 16
        && b[0] == b'+'
        && b[1] != b'0'
        && b[1..].iter().all(|c| c.is_ascii_digit())
}

pub fn create_order(conn: &Connection, req: &Req) -> Res {
    let key = match req.headers.get("idempotency-key") {
        Some(k) if !k.is_empty() && k.len() <= 64 => k.clone(),
        // SPEC 6: lazmi hai, kyunki ek retry karta agent kabhi do order na banaye.
        _ => {
            return err(
                400,
                "MISSING_FIELD",
                "Idempotency-Key header is required and must be at most 64 characters.",
                json!({ "field": "Idempotency-Key" }),
            )
        }
    };

    let body: Value = match serde_json::from_str(&req.body) {
        Ok(v) => v,
        Err(e) => {
            return err(
                400,
                "MISSING_FIELD",
                "Request body is not valid JSON.",
                json!({ "detail": e.to_string() }),
            )
        }
    };

    let items = match body.get("items").and_then(|v| v.as_array()) {
        Some(a) if !a.is_empty() => a.clone(),
        _ => {
            return err(
                400,
                "MISSING_FIELD",
                "items must be a non-empty array.",
                json!({ "field": "items" }),
            )
        }
    };
    for (i, line) in items.iter().enumerate() {
        if let Err(e) = need_str(line, &["variant_id"]) {
            return e;
        }
        if line.get("qty").and_then(|v| v.as_i64()).unwrap_or(0) < 1 {
            return err(
                400,
                "MISSING_FIELD",
                &format!("items[{}].qty must be an integer of at least 1.", i),
                json!({ "field": format!("items[{}].qty", i) }),
            );
        }
        if line
            .get("expected_price_paise")
            .and_then(|v| v.as_i64())
            .is_none()
        {
            return err(
                400,
                "MISSING_FIELD",
                &format!("items[{}].expected_price_paise must be an integer.", i),
                json!({ "field": format!("items[{}].expected_price_paise", i) }),
            );
        }
    }
    let Some(expected_total) = body.get("expected_items_total_paise").and_then(|v| v.as_i64())
    else {
        return err(
            400,
            "MISSING_FIELD",
            "expected_items_total_paise must be an integer number of paise.",
            json!({ "field": "expected_items_total_paise" }),
        );
    };

    let contact_name = match need_str(&body, &["contact", "name"]) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let phone = match need_str(&body, &["contact", "phone"]) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let line1 = match need_str(&body, &["address", "line1"]) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let city = match need_str(&body, &["address", "city"]) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let state = match need_str(&body, &["address", "state"]) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let pincode = match need_str(&body, &["address", "pincode"]) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let country = match need_str(&body, &["address", "country"]) {
        Ok(v) => v,
        Err(e) => return e,
    };

    // SPEC 6 (v1.9): **maujoodgi jaanchna shakal jaanchna nahi hai.** Do reference
    // merchants ne `phone: "98765"` aur `country: "ZZ"` par asli order bana diye the —
    // chukaye ja sakne wale order, jinhe koi courier deliver nahi kar sakta.
    if !is_e164(&phone) {
        return err(
            400,
            "MISSING_FIELD",
            "contact.phone must be an E.164 number like +919876543210.",
            json!({ "field": "contact.phone", "phone": phone }),
        );
    }
    if pincode.len() != 6 || !pincode.bytes().all(|b| b.is_ascii_digit()) {
        return err(
            400,
            "MISSING_FIELD",
            "address.pincode must be exactly 6 digits.",
            json!({ "field": "address.pincode", "pincode": pincode }),
        );
    }
    if !country.eq_ignore_ascii_case("IN") {
        return err(
            400,
            "MISSING_FIELD",
            "Marigold Bazaar delivers within India only; address.country must be 'IN'.",
            json!({ "field": "address.country", "country": country }),
        );
    }

    let mode = match need_str(&body, &["payment_mode"]) {
        Ok(v) => v,
        Err(e) => return e,
    };
    if !store::PAYMENT_MODES.contains(&mode.as_str()) {
        return err(
            400,
            "UNSUPPORTED_PAYMENT_MODE",
            &format!("payment_mode {} is not offered by this store.", mode),
            json!({ "payment_mode": mode, "supported": store::PAYMENT_MODES }),
        );
    }

    // Idempotency: wahi key + wahi body -> wahi original 201, verbatim. Wahi key + alag
    // body -> 409. `serde_json` ka map sorted hai, to serialise karna hi canonical hai.
    let canonical = serde_json::to_string(&body).unwrap_or_default();
    if let Ok((prior_body, prior_order)) = conn.query_row(
        "SELECT body, order_id FROM idempotency WHERE key = ?",
        [&key],
        |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
    ) {
        if prior_body != canonical {
            return err(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This Idempotency-Key was already used with a different request body.",
                json!({ "idempotency_key": key }),
            );
        }
        if let Ok(v) = conn.query_row(
            "SELECT * FROM orders WHERE order_id = ?",
            [&prior_order],
            |r| store::order_view(r, true),
        ) {
            return Res { status: 201, body: v, retry_after: None, no_store: false };
        }
    }

    // ---- live verification. Sab kuch DB se DOBARA padha jata hai (SPEC 6).
    let mut lines = Vec::new();
    let mut items_total: i64 = 0;
    for line in &items {
        let variant_id = line["variant_id"].as_str().unwrap_or("").to_string();
        let qty = line["qty"].as_i64().unwrap_or(0);
        let expected = line["expected_price_paise"].as_i64().unwrap_or(-1);

        let Some(v) = store::variant_by_id(conn, &variant_id) else {
            return err(
                400,
                "INVALID_VARIANT",
                &format!("Unknown variant_id {}.", variant_id),
                json!({ "variant_id": variant_id }),
            );
        };
        // Ceiling REQUEST ki property hai, inventory ki nahi — isliye stock se pehle.
        if qty > store::MAX_QTY_PER_VARIANT {
            return err(
                409,
                "QTY_LIMIT_EXCEEDED",
                &format!(
                    "This store allows at most {} units of a variant per order.",
                    store::MAX_QTY_PER_VARIANT
                ),
                json!({
                    "variant_id": variant_id,
                    "requested": qty,
                    "max_qty_per_variant": store::MAX_QTY_PER_VARIANT
                }),
            );
        }
        if v.price_paise != expected {
            // Kabhi chup-chaap naya daam mat lagao — dono taraf verify hona hi wo cheez
            // hai jo is raaste ko single point of failure hone se bachati hai.
            return err(
                409,
                "PRICE_CHANGED",
                &format!("Price for {} changed since it was quoted.", variant_id),
                json!({
                    "variant_id": variant_id,
                    "expected_price_paise": expected,
                    "actual_price_paise": v.price_paise
                }),
            );
        }
        if v.stock < qty {
            return err(
                409,
                "OUT_OF_STOCK",
                &format!("Only {} left of {}.", v.stock, variant_id),
                json!({
                    "variant_id": variant_id,
                    "requested": qty,
                    "available_qty": v.stock,
                    "available": v.stock
                }),
            );
        }
        let title: String = conn
            .query_row(
                "SELECT title FROM products WHERE product_id = ?",
                [&v.product_id],
                |r| r.get(0),
            )
            .unwrap_or_default();
        let opts: Vec<String> = v
            .options
            .as_object()
            .map(|m| {
                m.values()
                    .filter_map(|x| x.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let label = if opts.is_empty() {
            title
        } else {
            format!("{} — {}", title, opts.join(" / "))
        };
        items_total += v.price_paise * qty;
        lines.push((
            v.product_id.clone(),
            json!({
                "variant_id": variant_id,
                "title": label,
                "qty": qty,
                "unit_price_paise": v.price_paise,
                "line_total_paise": v.price_paise * qty
            }),
        ));
    }

    if items_total != expected_total {
        return err(
            409,
            "TOTAL_CHANGED",
            "The items total does not match what was quoted.",
            json!({
                "expected_items_total_paise": expected_total,
                "actual_items_total_paise": items_total
            }),
        );
    }
    if !store::serviceable(&pincode) {
        return err(
            409,
            "NOT_SERVICEABLE",
            &format!("{} does not deliver to {}.", store::NAME, pincode),
            json!({ "pincode": pincode }),
        );
    }

    let coupon = body
        .get("coupon_code")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let Ok(discount) = store::discount_for(coupon.as_deref(), items_total) else {
        return err(
            409,
            "INVALID_COUPON",
            "That coupon is not valid for this order.",
            json!({ "coupon_code": coupon }),
        );
    };
    let shipping = store::shipping_for(items_total);
    let final_total = items_total + shipping - discount;

    // ---- payment instrument
    let created = now();
    let created_at = iso(created);
    let expires_epoch = created + store::UNPAID_ORDER_TTL_MINUTES * 60;
    let order_id = format!("ord_{}", short_id(created, &key));

    let mut payment = Map::new();
    payment.insert("mode".into(), json!(mode));
    // SPEC 7 ka `payment.state`. Order bana hai, paisa aaya nahi — har mode par, `cod`
    // samet. Ye field order-read par LAZMI hai, aur creation par bhi rakhne se ek hi
    // shape dono jagah rehti hai; do jagah do alag shape rakhna wahi cheez hai jispe
    // ek consumer baad me phansta hai.
    payment.insert("state".into(), json!("pending"));
    match mode.as_str() {
        "cod" => {
            payment.insert("link_url".into(), Value::Null);
            payment.insert("razorpay_order_id".into(), Value::Null);
            payment.insert("razorpay_key_id".into(), Value::Null);
            payment.insert("expires_at".into(), Value::Null);
        }
        "payment_link" => {
            let contact = body.get("contact").cloned().unwrap_or(json!({}));
            match razorpay::create_payment_link(
                final_total,
                &format!("{} order", store::NAME),
                &contact,
                expires_epoch,
            ) {
                Ok(link) => {
                    payment.insert("link_url".into(), json!(link.url));
                    payment.insert("link_id".into(), json!(link.id));
                    payment.insert("razorpay_order_id".into(), Value::Null);
                    payment.insert("razorpay_key_id".into(), Value::Null);
                    payment.insert("expires_at".into(), json!(iso(expires_epoch)));
                }
                Err(p) => return provider_error(p),
            }
        }
        _ => match razorpay::create_checkout_order(final_total, &order_id) {
            Ok(rzp_order) => {
                payment.insert("link_url".into(), Value::Null);
                payment.insert("razorpay_order_id".into(), json!(rzp_order));
                // SPEC 6: order id akela kisi se bhi pay nahi hota — har checkout client
                // ko account pehchanne wali PUBLIC key bhi chahiye. Iske bina wo ek aisa
                // instrument hai jise koi settle nahi kar sakta, aur chup-chaap.
                payment.insert("razorpay_key_id".into(), json!(razorpay::publishable_key()));
                payment.insert("expires_at".into(), json!(iso(expires_epoch)));
            }
            Err(p) => return provider_error(p),
        },
    }

    // ---- stock reserve, aur har chhue hue product ka `updated_at` aage
    let tx_ok = (|| -> rusqlite::Result<()> {
        for (product_id, line) in &lines {
            let vid = line["variant_id"].as_str().unwrap_or("");
            let qty = line["qty"].as_i64().unwrap_or(0);
            conn.execute(
                "UPDATE variants SET stock = stock - ? WHERE variant_id = ?",
                params![qty, vid],
            )?;
            // Stock badalna catalog-visible change hai (SPEC 4), aur har product apna
            // alag stamp leta hai — do product ek hi stamp lein to doosra "strictly
            // greater" nahi rehta aur delta sync use kabhi nahi dekhegi.
            let (epoch, stamp) = db::next_updated(conn);
            conn.execute(
                "UPDATE products SET updated_epoch = ?, updated_at = ? WHERE product_id = ?",
                params![epoch, stamp, product_id],
            )?;
        }
        Ok(())
    })();
    if let Err(e) = tx_ok {
        return err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null);
    }

    let delivery = json!({
        "eta_days": store::eta_days(&pincode),
        // SPEC 6: waada order ke saath jam jata hai. Baad me dobara compute karne par
        // har read "aaj se teen din" kehta hai, yaani ek late order kabhi late nahi
        // dikhta.
        "promised_by": iso(created + store::eta_days(&pincode) * 86_400),
        "pincode": pincode
    });
    let cancellable_until = iso(created + store::CANCEL_WINDOW_HOURS * 3_600);
    let items_json: Vec<Value> = lines.iter().map(|(_, l)| l.clone()).collect();
    let timeline = json!([{ "status": "created", "at": created_at }]);
    let contact_json = json!({
        "name": contact_name,
        "phone": phone,
        "email": body.pointer("/contact/email").cloned().unwrap_or(Value::Null)
    });
    let address_json = json!({
        "line1": line1,
        "line2": body.pointer("/address/line2").cloned().unwrap_or(Value::Null),
        "city": city, "state": state, "pincode": pincode, "country": "IN"
    });

    if let Err(e) = conn.execute(
        "INSERT INTO orders (order_id, status, items, items_total_paise, shipping_paise,
             discount_paise, final_total_paise, contact, address, payment, delivery, refund,
             timeline, coupon_code, created_epoch, created_at, cancellable_until, expires_epoch)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)",
        params![
            order_id,
            "created",
            serde_json::to_string(&items_json).unwrap(),
            items_total,
            shipping,
            discount,
            final_total,
            contact_json.to_string(),
            address_json.to_string(),
            Value::Object(payment).to_string(),
            delivery.to_string(),
            timeline.to_string(),
            coupon,
            created,
            created_at,
            cancellable_until,
            if mode == "cod" { None } else { Some(expires_epoch) }
        ],
    ) {
        return err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null);
    }
    let _ = conn.execute(
        "INSERT OR REPLACE INTO idempotency (key, body, order_id, created_epoch) VALUES (?,?,?,?)",
        params![key, canonical, order_id, created],
    );

    match conn.query_row("SELECT * FROM orders WHERE order_id = ?", [&order_id], |r| {
        store::order_view(r, true)
    }) {
        Ok(v) => Res { status: 201, body: v, retry_after: None, no_store: false },
        Err(e) => err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null),
    }
}

fn short_id(seed: i64, key: &str) -> String {
    // Order id opaque hona chahiye aur stable — koi randomness nahi, taaki ek hi
    // idempotency key wahi id de.
    let mut h: u64 = 0xcbf29ce484222325;
    for b in key.bytes().chain(seed.to_string().bytes()) {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{:012x}", h & 0xffff_ffff_ffff)
}

// --------------------------------------------------------------- 5. read order

pub fn get_order(conn: &Connection, order_id: &str) -> Res {
    if conn
        .query_row("SELECT 1 FROM orders WHERE order_id = ?", [order_id], |_| {
            Ok(())
        })
        .is_err()
    {
        return err(
            404,
            "ORDER_NOT_FOUND",
            &format!("No order with id {}.", order_id),
            json!({ "order_id": order_id }),
        );
    }
    refresh(conn, order_id);
    match conn.query_row("SELECT * FROM orders WHERE order_id = ?", [order_id], |r| {
        store::order_view(r, false)
    }) {
        Ok(v) => ok(v),
        Err(e) => err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null),
    }
}

/// Order padhte waqt provider se dobara poochho: pay hua? refund poora hua? expire ho gaya?
///
/// Yahan koi webhook nahi hai (Layer poll karti hai), to yahi wo ek jagah hai jahan
/// dono sawaal poochhe ja sakte hain.
fn refresh(conn: &Connection, order_id: &str) {
    let Ok((status, payment_text, expires, final_total)) = conn.query_row(
        "SELECT status, payment, expires_epoch, final_total_paise FROM orders WHERE order_id = ?",
        [order_id],
        |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<i64>>(2)?,
                r.get::<_, i64>(3)?,
            ))
        },
    ) else {
        return;
    };
    let mut payment = db::jl(&payment_text, json!({}));
    let mode = payment["mode"].as_str().unwrap_or("").to_string();

    // ---- refund pehle: ek bheja hua refund poora hua ya nahi, ye har read par dobara
    // poochha jata hai. Warna refund cancel ke lamhe wali haalat me jam jata hai aur
    // kabhi complete hota HUA dikh hi nahi sakta (SPEC 7).
    if let Ok(Some(refund_text)) =
        conn.query_row("SELECT refund FROM orders WHERE order_id = ?", [order_id], |r| {
            r.get::<_, Option<String>>(0)
        })
    {
        let mut refund = db::jl(&refund_text, json!({}));
        let state = refund["state"].as_str().unwrap_or("").to_string();
        if state == "initiated" || state == "pending" {
            if let Some(rid) = refund["razorpay_refund_id"].as_str() {
                if let Ok(fresh) = razorpay::fetch_refund(rid) {
                    if fresh.state != state {
                        refund["state"] = json!(fresh.state);
                        payment["state"] = json!(payment_state_for_refund(fresh.state));
                        let _ = conn.execute(
                            "UPDATE orders SET refund = ?, payment = ? WHERE order_id = ?",
                            params![refund.to_string(), payment.to_string(), order_id],
                        );
                    }
                }
            }
        }
    }

    if status != "created" || mode == "cod" {
        return;
    }

    match razorpay::fetch_payment(
        &mode,
        payment["link_id"].as_str(),
        payment["razorpay_order_id"].as_str(),
    ) {
        Ok(Some(paid)) => {
            payment["state"] = json!("paid");
            payment["razorpay_payment_id"] = json!(paid.payment_id);
            payment["paid_at"] = json!(paid.paid_at.clone());
            let _ = conn.execute(
                "UPDATE orders SET status = 'paid', payment = ?,
                     timeline = json_insert(timeline, '$[#]', json(?)) WHERE order_id = ?",
                params![
                    payment.to_string(),
                    json!({"status": "paid", "at": paid.paid_at}).to_string(),
                    order_id
                ],
            );
        }
        Ok(None) => {
            // **Expire sirf ek CONFIRMED-unpaid order ko kiya jata hai** (SPEC 9). Upar
            // wala `Ok(None)` hi wo confirmation hai; `Unreachable` par kuch nahi hota.
            if let Some(exp) = expires {
                if now() > exp {
                    expire(conn, order_id, final_total);
                }
            }
        }
        // Provider tak baat nahi pahunchi — order pending chhodo aur agli read par phir
        // poochho. Timeout par likha gaya `failed` kisi ke ho chuke payment ke upar
        // likha ja sakta hai.
        Err(_) => {}
    }
}

fn expire(conn: &Connection, order_id: &str, _final_total: i64) {
    let items: Value = conn
        .query_row("SELECT items FROM orders WHERE order_id = ?", [order_id], |r| {
            Ok(db::jl(&r.get::<_, String>(0)?, json!([])))
        })
        .unwrap_or(json!([]));
    release_stock(conn, &items);
    // `payment.state` bhi `failed` hona chahiye. Warna order `failed` hai aur payment
    // abhi bhi `pending` — ek hi body me do jawab, aur padhne wale ke paas chunne ka koi
    // rule nahi.
    if let Ok(text) = conn.query_row(
        "SELECT payment FROM orders WHERE order_id = ?", [order_id],
        |r| r.get::<_, String>(0),
    ) {
        let mut payment = db::jl(&text, json!({}));
        payment["state"] = json!("failed");
        let _ = conn.execute(
            "UPDATE orders SET payment = ? WHERE order_id = ?",
            params![payment.to_string(), order_id],
        );
    }
    let at = iso(now());
    let _ = conn.execute(
        "UPDATE orders SET status = 'failed', expires_epoch = NULL,
             timeline = json_insert(timeline, '$[#]', json(?)) WHERE order_id = ?",
        params![
            json!({"status": "failed", "at": at,
                   "note": "payment instrument expired before it was paid"})
            .to_string(),
            order_id
        ],
    );
}

/// Stock wapas shelf par, aur har chhue hue product ka `updated_at` aage.
fn release_stock(conn: &Connection, items: &Value) {
    for line in items.as_array().unwrap_or(&vec![]) {
        let vid = line["variant_id"].as_str().unwrap_or("");
        let qty = line["qty"].as_i64().unwrap_or(0);
        let _ = conn.execute(
            "UPDATE variants SET stock = stock + ? WHERE variant_id = ?",
            params![qty, vid],
        );
        if let Ok(pid) = conn.query_row(
            "SELECT product_id FROM variants WHERE variant_id = ?",
            [vid],
            |r| r.get::<_, String>(0),
        ) {
            let (epoch, stamp) = db::next_updated(conn);
            let _ = conn.execute(
                "UPDATE products SET updated_epoch = ?, updated_at = ? WHERE product_id = ?",
                params![epoch, stamp, pid],
            );
        }
    }
}

/// Refund ki state se `payment.state` (SPEC 7). **`refunded` sirf tab jab paisa sach me
/// pahunch gaya ho** — bheja hua aur aaya hua refund do alag baatein hain, aur inhe ek
/// bana dena grahak se ye kehna hai ki uske paas paisa hai jo uske paas hai hi nahi.
pub fn payment_state_for_refund(refund_state: &str) -> &'static str {
    match refund_state {
        "processed" => "refunded",
        "failed" => "refund_failed",
        _ => "refund_pending",
    }
}

// ------------------------------------------------------------- 6. cancel order

pub fn cancel_order(conn: &Connection, req: &Req, order_id: &str) -> Res {
    let body: Value = serde_json::from_str(&req.body).unwrap_or(json!({}));
    let reason = match body.get("reason").and_then(|v| v.as_str()) {
        Some(r) if !r.trim().is_empty() => r.trim().to_string(),
        // Bina wajah ki cancellation auditable nahi hai (SPEC 8).
        _ => {
            return err(
                400,
                "MISSING_FIELD",
                "reason is required.",
                json!({ "field": "reason" }),
            )
        }
    };
    if reason.chars().count() > 200 {
        return err(
            400,
            "MISSING_FIELD",
            "reason must be at most 200 characters.",
            json!({ "field": "reason" }),
        );
    }

    let Ok((status, cancellable_until, payment_text, items_text, final_total)) = conn.query_row(
        "SELECT status, cancellable_until, payment, items, final_total_paise
         FROM orders WHERE order_id = ?",
        [order_id],
        |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, i64>(4)?,
            ))
        },
    ) else {
        return err(
            404,
            "ORDER_NOT_FOUND",
            &format!("No order with id {}.", order_id),
            json!({ "order_id": order_id }),
        );
    };

    // Cancellation IDEMPOTENT hai (SPEC 8): dobara cancel karne par wahi natija, error
    // nahi — aur wahi refund, doosra nahi.
    if status == "cancelled" {
        return match conn.query_row("SELECT * FROM orders WHERE order_id = ?", [order_id], |r| {
            cancel_view(r)
        }) {
            Ok(v) => ok(v),
            Err(e) => err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null),
        };
    }
    if !store::cancellable(&status, cancellable_until.as_deref()) {
        return err(
            409,
            "ORDER_NOT_CANCELLABLE",
            &format!("An order in state {} cannot be cancelled.", status),
            json!({ "status": status, "cancel_window_hours": store::CANCEL_WINDOW_HOURS }),
        );
    }

    let mut payment = db::jl(&payment_text, json!({}));
    let items = db::jl(&items_text, json!([]));
    release_stock(conn, &items);

    let mut refund = Value::Null;
    if payment["state"].as_str() == Some("paid") {
        if let Some(pid) = payment["razorpay_payment_id"].as_str() {
            match razorpay::create_refund(pid, final_total) {
                Ok(r) => {
                    refund = json!({
                        "state": r.state,
                        "amount_paise": final_total,
                        "razorpay_refund_id": r.id,
                        // Razorpay normal-speed refunds ~5 kaam ke din lete hain. Ye
                        // number grahak ka asli sawaal hai — "mera paisa kab aayega".
                        "expected_by": iso(now() + 5 * 86_400)
                    });
                    payment["state"] = json!(payment_state_for_refund(r.state));
                }
                Err(p) => return provider_error(p),
            }
        }
    }

    let at = iso(now());
    let _ = conn.execute(
        "UPDATE orders SET status = 'cancelled', payment = ?, refund = ?, expires_epoch = NULL,
             timeline = json_insert(timeline, '$[#]', json(?)) WHERE order_id = ?",
        params![
            payment.to_string(),
            if refund.is_null() {
                None
            } else {
                Some(refund.to_string())
            },
            json!({"status": "cancelled", "at": at, "note": reason}).to_string(),
            order_id
        ],
    );

    match conn.query_row("SELECT * FROM orders WHERE order_id = ?", [order_id], |r| {
        cancel_view(r)
    }) {
        Ok(v) => ok(v),
        Err(e) => err(500, "INTERNAL_ERROR", &e.to_string(), Value::Null),
    }
}

fn cancel_view(row: &rusqlite::Row) -> rusqlite::Result<Value> {
    let refund: Option<String> = row.get("refund")?;
    Ok(json!({
        "spec_version": store::SPEC_VERSION,
        "order_id": row.get::<_, String>("order_id")?,
        "status": row.get::<_, String>("status")?,
        "refund": refund.map(|t| db::jl(&t, Value::Null)).unwrap_or(Value::Null),
        "cancelled_at": iso(now())
    }))
}
