//! Dukaan ka sach, ek jagah. Dono darwaze — `/agent/*` aur browser wali UI — yahin se
//! padhte hain, isliye "AI ko dikhaya gaya daam" aur "insaan ko dikhaya gaya daam" alag
//! ho hi nahi sakte. Wo alag ho sakna hi is poore project ka sabse aasan jhooth hota.

use crate::db;
use crate::util::{iso, now};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

pub const MERCHANT_ID: &str = "marigold-bazaar";
pub const NAME: &str = "Marigold Bazaar";
pub const CURRENCY: &str = "INR";
pub const SPEC_VERSION: &str = "1.0";
pub const PAYMENT_MODES: [&str; 3] = ["payment_link", "checkout", "cod"];

pub const SHIPPING_FLAT_PAISE: i64 = 3_900; // Rs 39
pub const FREE_SHIPPING_ABOVE_PAISE: i64 = 149_900; // Rs 1,499
pub const CANCEL_WINDOW_HOURS: i64 = 72;

/// **Do se zyada nahi.** Teenon dukaanon me sabse sakht, aur ye jaan-boojhkar hai.
///
/// Layer ka apna ceiling 5 hai aur SPEC 3 kehti hai ki dono me se **sakht** jeetega. Wo
/// `min()` do session tak sirf ek disha me chala tha (Layer sakht, Northwind 10 declare
/// karta hai); Voltline ne 3 se doosri disha zinda ki. 2 se teen alag effective ceiling
/// ban jate hain — 5, 3, 2 — yaani ek hi agent ek hi call par teen dukaanon me teen alag
/// jawab paata hai, aur "sakht wala jeetega" ek line ki jagah ek naapi hui cheez ban jati
/// hai.
pub const MAX_QTY_PER_VARIANT: i64 = 2;

/// Bina pay hue order kitni der stock roke rakh sakta hai (SPEC 9).
///
/// **Ghadi ORDER ki hai, provider ke instrument ki nahi.** Razorpay ka checkout order
/// khud kabhi expire nahi hota, to agar hum provider ke bharose baithte to theek wahi
/// mode — jisse ek agent pay karta hai — stock hamesha ke liye rok leta.
pub const UNPAID_ORDER_TTL_MINUTES: i64 = 30;

/// **Marigold poorvottar bhejta hai — aur yahi is dukaan ki ek asli zimmedari hai.**
///
/// Northwind aur Voltline dono `7` se shuru hone wale pincode mana karte hain. Yaani
/// "ek dukaan ne mana kiya, agent ne doosri se le liya" wala failure-recovery demo do
/// merchants ke saath **chal hi nahi sakta tha** — dono ek hi pincode mana karte hain,
/// aur buyer ke paas koi teesra raasta tha hi nahi. Marigold `7` serve karta hai aur `6`
/// (deep south) nahi — to recovery dono dishaon me asli hai:
///
///     781001 (Guwahati)  Northwind NO   Voltline NO   Marigold YES
///     682001 (Kochi)     Northwind YES  Voltline YES  Marigold NO
///
/// Har dukaan ka apna delivery footprint hota hai; ye us baat ka imaandaar roop hai, aur
/// isi se `NOT_SERVICEABLE` ek dead end ki jagah ek raasta ban jata hai.
pub fn serviceable(pincode: &str) -> bool {
    pincode.len() == 6 && pincode.bytes().all(|b| b.is_ascii_digit()) && !pincode.starts_with('6')
}

/// Marigold ka warehouse Jaipur me hai — zone pehle ank se, aur ye Northwind (flat 3) aur
/// Voltline (2 ya 4) dono se alag nikalta hai. Teen dukaanein, teen alag waade.
pub fn eta_days(pincode: &str) -> i64 {
    match pincode.as_bytes().first() {
        Some(b'1') | Some(b'2') | Some(b'3') => 2, // north + west, paas
        Some(b'4') | Some(b'5') => 3,
        Some(b'7') => 6, // poorvottar door hai, aur ye BATAYA jata hai
        _ => 4,
    }
}

pub fn shipping_for(items_total_paise: i64) -> i64 {
    // SPEC 3: `free_above_paise` ki tulna DISCOUNT SE PEHLE wale items total se hoti hai,
    // warna ek coupon chup-chaap shipping charge laga deta — grahak ko bachat ke badle
    // ek naya charge milta.
    if items_total_paise >= FREE_SHIPPING_ABOVE_PAISE {
        0
    } else {
        SHIPPING_FLAT_PAISE
    }
}

/// Coupons jaan-boojhkar discoverable nahi hain (Layer ka D-19) — merchant khud jaanchta
/// hai. Teesra coupon toota hua hai, `INVALID_COUPON` ke demo ke liye.
pub fn discount_for(code: Option<&str>, items_total_paise: i64) -> Result<i64, ()> {
    let Some(code) = code else { return Ok(0) };
    match code.to_uppercase().as_str() {
        "MARIGOLD15" => Ok(std::cmp::min(items_total_paise * 15 / 100, 40_000)),
        "FESTIVE100" if items_total_paise >= 100_000 => Ok(10_000),
        _ => Err(()),
    }
}

// --------------------------------------------------------------------- shaping

pub fn manifest(conn: &Connection) -> Value {
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM products", [], |r| r.get(0))
        .unwrap_or(0);
    let last: Option<String> = conn
        .query_row("SELECT MAX(updated_at) FROM products", [], |r| r.get(0))
        .unwrap_or(None);
    // Categories DB se NIKALTI hain, hardcoded nahi. SPEC 4 kehti hai ki har product ki
    // category manifest me honi chahiye — hardcode karne par wo suchi seed se drift kar
    // jati hai aur koi error nahi aata. Northwind me ye galti ho chuki hai.
    let mut cats: Vec<String> = conn
        .prepare("SELECT DISTINCT category FROM products ORDER BY category")
        .and_then(|mut s| {
            s.query_map([], |r| r.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()
        })
        .unwrap_or_default();
    cats.sort();
    json!({
        "spec_version": SPEC_VERSION,
        "merchant_id": MERCHANT_ID,
        "name": NAME,
        "currency": CURRENCY,
        "categories": cats,
        "payment_modes": PAYMENT_MODES,
        "shipping": {
            "pincode_required": true,
            "flat_paise": SHIPPING_FLAT_PAISE,
            "free_above_paise": FREE_SHIPPING_ABOVE_PAISE
        },
        "policies": {
            "cancel_window_hours": CANCEL_WINDOW_HOURS,
            "max_qty_per_variant": MAX_QTY_PER_VARIANT
        },
        "catalog": { "product_count": count, "last_updated_at": last }
    })
}

pub struct VariantRow {
    pub variant_id: String,
    pub product_id: String,
    pub sku: Option<String>,
    pub options: Value,
    pub price_paise: i64,
    pub mrp_paise: Option<i64>,
    pub stock: i64,
    pub images: Value,
}

pub fn variants_of(conn: &Connection, product_id: &str) -> Vec<VariantRow> {
    let mut stmt = conn
        .prepare(
            "SELECT variant_id, product_id, sku, options, price_paise, mrp_paise, stock, images
             FROM variants WHERE product_id = ? ORDER BY position, variant_id",
        )
        .expect("variants stmt");
    let rows = stmt
        .query_map([product_id], |r| {
            Ok(VariantRow {
                variant_id: r.get(0)?,
                product_id: r.get(1)?,
                sku: r.get(2)?,
                options: db::jl(&r.get::<_, String>(3)?, json!({})),
                price_paise: r.get(4)?,
                mrp_paise: r.get(5)?,
                stock: r.get(6)?,
                images: db::jl(&r.get::<_, String>(7)?, json!([])),
            })
        })
        .expect("variants query");
    rows.filter_map(|r| r.ok()).collect()
}

pub fn variant_by_id(conn: &Connection, variant_id: &str) -> Option<VariantRow> {
    let mut stmt = conn
        .prepare(
            "SELECT variant_id, product_id, sku, options, price_paise, mrp_paise, stock, images
             FROM variants WHERE variant_id = ?",
        )
        .ok()?;
    stmt.query_row([variant_id], |r| {
        Ok(VariantRow {
            variant_id: r.get(0)?,
            product_id: r.get(1)?,
            sku: r.get(2)?,
            options: db::jl(&r.get::<_, String>(3)?, json!({})),
            price_paise: r.get(4)?,
            mrp_paise: r.get(5)?,
            stock: r.get(6)?,
            images: db::jl(&r.get::<_, String>(7)?, json!([])),
        })
    })
    .ok()
}

/// Ek product ki catalog wali shakal (SPEC 4). `variant_options` un dimensions ka naam
/// leta hai jo is product me sach me badalti hain.
pub fn catalog_row(conn: &Connection, row: &rusqlite::Row) -> rusqlite::Result<Value> {
    let product_id: String = row.get("product_id")?;
    let variants = variants_of(conn, &product_id);
    let in_stock = variants.iter().any(|v| v.stock > 0);
    let prices: Vec<i64> = variants
        .iter()
        .filter(|v| v.stock > 0)
        .map(|v| v.price_paise)
        .collect();
    let all: Vec<i64> = variants.iter().map(|v| v.price_paise).collect();
    let pool = if prices.is_empty() { &all } else { &prices };

    // Har option dimension ke values, kram bachate hue.
    let mut names: Vec<String> = Vec::new();
    let mut values: Vec<Vec<String>> = Vec::new();
    for v in &variants {
        if let Some(map) = v.options.as_object() {
            for (k, val) in map {
                let val = val.as_str().unwrap_or_default().to_string();
                match names.iter().position(|n| n == k) {
                    Some(i) => {
                        if !values[i].contains(&val) {
                            values[i].push(val);
                        }
                    }
                    None => {
                        names.push(k.clone());
                        values.push(vec![val]);
                    }
                }
            }
        }
    }
    let variant_options: Vec<Value> = names
        .iter()
        .zip(values.iter())
        .map(|(n, v)| json!({"name": n, "values": v}))
        .collect();

    Ok(json!({
        "product_id": product_id,
        "title": row.get::<_, String>("title")?,
        // SPEC 10: VERBATIM. Safai Layer ka kaam hai, humara nahi — aur agar har merchant
        // apni-apni safai karta to Layer ye bata hi nahi paati ki use asal me kya mila.
        "description": row.get::<_, String>("description")?,
        "category": row.get::<_, String>("category")?,
        "tags": db::jl(&row.get::<_, String>("tags")?, json!([])),
        "brand": row.get::<_, Option<String>>("brand")?,
        "images": db::jl(&row.get::<_, String>("images")?, json!([])),
        "price_range_paise": {
            "min": pool.iter().min().copied().unwrap_or(0),
            "max": pool.iter().max().copied().unwrap_or(0)
        },
        "in_stock": in_stock,
        "variant_options": variant_options,
        "variant_count": variants.len().max(1),
        "rating_avg": row.get::<_, Option<f64>>("rating_avg")?,
        "rating_count": row.get::<_, Option<i64>>("rating_count")?,
        "updated_at": row.get::<_, String>("updated_at")?
    }))
}

pub fn detail(conn: &Connection, product_id: &str, pincode: Option<&str>) -> Option<Value> {
    let mut stmt = conn
        .prepare("SELECT * FROM products WHERE product_id = ?")
        .ok()?;
    let mut base = stmt
        .query_row([product_id], |r| catalog_row(conn, r))
        .ok()?;

    let variants: Vec<Value> = variants_of(conn, product_id)
        .into_iter()
        .map(|v| {
            let mut m = Map::new();
            m.insert("variant_id".into(), json!(v.variant_id));
            if let Some(sku) = v.sku {
                m.insert("sku".into(), json!(sku));
            }
            m.insert("options".into(), v.options);
            m.insert("price_paise".into(), json!(v.price_paise));
            if let Some(mrp) = v.mrp_paise {
                m.insert("mrp_paise".into(), json!(mrp));
            }
            m.insert("stock".into(), json!(v.stock));
            m.insert("images".into(), v.images);
            Value::Object(m)
        })
        .collect();

    let extra: (String, String, String) = conn
        .query_row(
            "SELECT attributes, reviews, related FROM products WHERE product_id = ?",
            [product_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .ok()?;

    let obj = base.as_object_mut()?;
    obj.insert("spec_version".into(), json!(SPEC_VERSION));
    obj.insert("attributes".into(), db::jl(&extra.0, json!({})));
    obj.insert("variants".into(), json!(variants));
    obj.insert("reviews".into(), db::jl(&extra.1, json!([])));
    obj.insert("related_product_ids".into(), db::jl(&extra.2, json!([])));
    obj.remove("variant_options");
    obj.remove("price_range_paise");
    obj.remove("in_stock");

    if let Some(pin) = pincode {
        obj.insert("delivery".into(), delivery_block(conn, product_id, pin));
    }
    Some(base)
}

fn delivery_block(conn: &Connection, product_id: &str, pincode: &str) -> Value {
    if !serviceable(pincode) {
        // SPEC 5: ye ek FACT hai, failure nahi — 200 ke andar, error nahi.
        return json!({"serviceable": false, "pincode": pincode});
    }
    let cheapest = variants_of(conn, product_id)
        .iter()
        .map(|v| v.price_paise)
        .min()
        .unwrap_or(0);
    json!({
        "serviceable": true,
        "pincode": pincode,
        "shipping_paise": shipping_for(cheapest),
        "eta_days": eta_days(pincode)
    })
}

// ---------------------------------------------------------------------- orders

/// Order ka poora roop, DB row se. `creation` = `POST /agent/orders` ka 201 (SPEC 6),
/// warna `GET /agent/orders/{id}` ka jawab (SPEC 7) — dono me `delivery` **wahi** rehta
/// hai jo order banate waqt tay hua tha.
pub fn order_view(row: &rusqlite::Row, creation: bool) -> rusqlite::Result<Value> {
    let status: String = row.get("status")?;
    let created_at: String = row.get("created_at")?;
    let cancellable_until: Option<String> = row.get("cancellable_until")?;
    let payment: Value = db::jl(&row.get::<_, String>("payment")?, json!({}));
    let refund: Option<Value> = row
        .get::<_, Option<String>>("refund")?
        .map(|t| db::jl(&t, Value::Null));

    let mut out = Map::new();
    out.insert("spec_version".into(), json!(SPEC_VERSION));
    out.insert("order_id".into(), json!(row.get::<_, String>("order_id")?));
    out.insert("status".into(), json!(status.clone()));
    out.insert(
        "items".into(),
        db::jl(&row.get::<_, String>("items")?, json!([])),
    );
    out.insert(
        "items_total_paise".into(),
        json!(row.get::<_, i64>("items_total_paise")?),
    );
    out.insert(
        "shipping_paise".into(),
        json!(row.get::<_, i64>("shipping_paise")?),
    );
    out.insert(
        "discount_paise".into(),
        json!(row.get::<_, i64>("discount_paise")?),
    );
    out.insert(
        "final_total_paise".into(),
        json!(row.get::<_, i64>("final_total_paise")?),
    );
    out.insert("currency".into(), json!(CURRENCY));
    out.insert("payment".into(), payment);
    out.insert(
        "delivery".into(),
        db::jl(&row.get::<_, String>("delivery")?, json!({})),
    );
    out.insert("created_at".into(), json!(created_at));

    if creation {
        out.insert("cancellable_until".into(), json!(cancellable_until));
    } else {
        let open = cancellable(&status, cancellable_until.as_deref());
        out.insert("cancellable".into(), json!(open));
        // SPEC 7: band window ke bagal me ek future deadline do ulte jawab hain, aur
        // padhne wale ke paas chunne ka koi rule nahi. Isliye `false` ke saath `null`.
        out.insert(
            "cancellable_until".into(),
            if open { json!(cancellable_until) } else { Value::Null },
        );
        out.insert("refund".into(), refund.unwrap_or(Value::Null));
        out.insert(
            "timeline".into(),
            db::jl(&row.get::<_, String>("timeline")?, json!([])),
        );
    }
    Ok(Value::Object(out))
}

pub fn cancellable(status: &str, until: Option<&str>) -> bool {
    if !matches!(status, "created" | "paid" | "confirmed") {
        return false;
    }
    match until {
        Some(u) => iso(now()).as_str() < u,
        None => false,
    }
}
