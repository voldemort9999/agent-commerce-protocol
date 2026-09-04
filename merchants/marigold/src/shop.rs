//! Insaan wala darwaza: do page, wahi database.
//!
//! **Ye demo ka sabse load-bearing hissa hai aur dikhne me sabse kam.** Split screen me
//! ek taraf ye page hota hai aur doosri taraf agent ke tool calls — aur dono ek hi
//! `marigold.db` se padhte hain. Agar UI ka apna data hota to poora demo ek dikhava ban
//! jata: "AI ko dikhaya gaya daam" aur "insaan ko dikhaya gaya daam" alag ho sakte, aur
//! wahi is project ka sabse aasan jhooth hota.
//!
//! CSS haath se likhi hui hai — koi CDN, koi build step, koi framework. Northwind Tailwind
//! CDN par hai aur Voltline apne template literals par; teesri dukaan ka apna roop hona
//! chahiye, warna "teen alag stack" sirf server tak sach rehta hai.

use crate::store;
use crate::util::{esc, rupees};
use rusqlite::Connection;
use serde_json::Value;

const CSS: &str = r#"
:root{
  --ink:#241a10; --muted:#7a6a58; --line:#e8ddcd; --bg:#fdfaf4; --card:#fff;
  --gold:#c8811d; --gold-soft:#fdf1de; --green:#186a3b; --red:#a32b1c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.wrap{max-width:1180px;margin:0 auto;padding:0 22px}

header{background:linear-gradient(180deg,#fff8ea,#fdfaf4);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:baseline;gap:16px;padding:22px 0 6px;flex-wrap:wrap}
.brand{font-size:27px;font-weight:700;letter-spacing:-.3px}
.brand span{color:var(--gold)}
.tag{color:var(--muted);font-size:13.5px}
.facts{display:flex;gap:8px;flex-wrap:wrap;padding:10px 0 20px}
.fact{background:var(--gold-soft);border:1px solid #f0dfc2;color:#7a5312;
  border-radius:999px;padding:4px 11px;font-size:12.5px;white-space:nowrap}

.cats{display:flex;gap:7px;flex-wrap:wrap;padding:18px 0 4px}
.cat{border:1px solid var(--line);background:var(--card);border-radius:999px;
  padding:5px 13px;font-size:13px;color:var(--muted)}
.cat.on{background:var(--ink);border-color:var(--ink);color:#fff}

.grid{display:grid;gap:18px;padding:20px 0 60px;
  grid-template-columns:repeat(auto-fill,minmax(215px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;
  display:flex;flex-direction:column;transition:transform .12s ease,box-shadow .12s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(60,40,10,.09)}
.shot{aspect-ratio:1;background:#faf5ec;display:flex;align-items:center;justify-content:center;
  padding:14px;border-bottom:1px solid var(--line)}
.shot img{max-width:100%;max-height:100%;object-fit:contain;mix-blend-mode:multiply}
.body{padding:12px 13px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
.cat-line{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
.name{font-weight:600;line-height:1.35;font-size:14.5px}
.price{font-size:16.5px;font-weight:700;margin-top:auto}
.mrp{color:var(--muted);text-decoration:line-through;font-weight:400;font-size:13px;
  margin-left:6px}
.meta{display:flex;justify-content:space-between;align-items:center;font-size:12px;
  color:var(--muted)}
.oos{color:var(--red);font-weight:600}
.ok{color:var(--green);font-weight:600}

.detail{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:38px;
  padding:26px 0 60px}
@media(max-width:840px){.detail{grid-template-columns:1fr}}
.gallery{display:grid;gap:10px;grid-template-columns:repeat(2,1fr)}
.gallery .shot{border:1px solid var(--line);border-radius:12px;aspect-ratio:1}
.gallery .shot:first-child{grid-column:1/-1;aspect-ratio:4/3}
h1{font-size:26px;line-height:1.25;margin:0 0 6px}
.brandline{color:var(--muted);font-size:13.5px;margin-bottom:14px}
.big{font-size:30px;font-weight:700}
.desc{white-space:pre-wrap;color:#3d3125;margin:16px 0 6px}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  margin:26px 0 10px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:7px 0;border-bottom:1px solid var(--line);vertical-align:top}
td:first-child{color:var(--muted);width:42%}
.vars{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.var{border:1px solid var(--line);border-radius:10px;padding:9px 11px;background:var(--card)}
.var.out{opacity:.5}
.var b{display:block;font-size:13.5px}
.var small{color:var(--muted);font-size:12px}
.rev{border-left:3px solid var(--gold-soft);padding:2px 0 2px 12px;margin-bottom:14px}
.rev b{font-size:13.5px}
.rev small{color:var(--muted)}
.note{background:#fff;border:1px dashed var(--line);border-radius:12px;padding:13px 15px;
  font-size:13px;color:var(--muted);margin-top:26px}
.note b{color:var(--ink)}
.back{font-size:13.5px;color:var(--muted);display:inline-block;padding:18px 0 0}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:12.5px;
  padding:20px 0 40px}
"#;

fn page(title: &str, inner: &str) -> String {
    format!(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">\
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
<title>{}</title><style>{}</style></head><body>{}</body></html>",
        esc(title),
        CSS,
        inner
    )
}

fn header(active: Option<&str>, cats: &[String]) -> String {
    let mut chips = format!(
        "<a class=\"cat{}\" href=\"/\">All</a>",
        if active.is_none() { " on" } else { "" }
    );
    for c in cats {
        chips.push_str(&format!(
            "<a class=\"cat{}\" href=\"/?category={}\">{}</a>",
            if active == Some(c.as_str()) { " on" } else { "" },
            esc(c),
            esc(&c.replace('-', " "))
        ));
    }
    format!(
        "<header><div class=\"wrap\"><div class=\"bar\">\
<a class=\"brand\" href=\"/\">Marigold <span>Bazaar</span></a>\
<span class=\"tag\">Home, kitchen and everyday things &mdash; Jaipur since 2019</span></div>\
<div class=\"facts\">\
<span class=\"fact\">Shipping {} &middot; free above {}</span>\
<span class=\"fact\">Cancel within {}h</span>\
<span class=\"fact\">Max {} per item, per order</span>\
<span class=\"fact\">Delivers to the North East</span></div>\
<div class=\"cats\">{}</div></div></header>",
        rupees(store::SHIPPING_FLAT_PAISE),
        rupees(store::FREE_SHIPPING_ABOVE_PAISE),
        store::CANCEL_WINDOW_HOURS,
        store::MAX_QTY_PER_VARIANT,
        chips
    )
}

fn categories(conn: &Connection) -> Vec<String> {
    conn.prepare("SELECT DISTINCT category FROM products ORDER BY category")
        .and_then(|mut s| {
            s.query_map([], |r| r.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()
        })
        .unwrap_or_default()
}

pub fn grid(conn: &Connection, category: Option<&str>) -> String {
    let cats = categories(conn);
    let sql = match category {
        Some(_) => "SELECT * FROM products WHERE category = ? ORDER BY position, product_id",
        None => "SELECT * FROM products ORDER BY position, product_id",
    };
    let mut stmt = conn.prepare(sql).expect("grid stmt");
    let rows: Vec<Value> = match category {
        Some(c) => stmt
            .query_map([c], |r| store::catalog_row(conn, r))
            .map(|it| it.filter_map(|r| r.ok()).collect())
            .unwrap_or_default(),
        None => stmt
            .query_map([], |r| store::catalog_row(conn, r))
            .map(|it| it.filter_map(|r| r.ok()).collect())
            .unwrap_or_default(),
    };

    let mut cards = String::new();
    for p in &rows {
        let img = p["images"][0].as_str().unwrap_or("");
        let min = p["price_range_paise"]["min"].as_i64().unwrap_or(0);
        let max = p["price_range_paise"]["max"].as_i64().unwrap_or(0);
        let price = if min == max {
            rupees(min)
        } else {
            format!("{} &ndash; {}", rupees(min), rupees(max))
        };
        let in_stock = p["in_stock"].as_bool().unwrap_or(false);
        let variants = p["variant_count"].as_i64().unwrap_or(1);
        cards.push_str(&format!(
            "<a class=\"card\" href=\"/p/{id}\"><div class=\"shot\"><img loading=\"lazy\" \
src=\"{img}\" alt=\"{title}\"></div><div class=\"body\">\
<div class=\"cat-line\">{cat}</div><div class=\"name\">{title}</div>\
<div class=\"price\">&#8377;{price}</div>\
<div class=\"meta\"><span>{variants}</span><span class=\"{cls}\">{stock}</span></div>\
</div></a>",
            id = esc(p["product_id"].as_str().unwrap_or("")),
            img = esc(img),
            title = esc(p["title"].as_str().unwrap_or("")),
            cat = esc(&p["category"].as_str().unwrap_or("").replace('-', " ")),
            price = price,
            variants = if variants > 1 {
                format!("{} options", variants)
            } else {
                "one option".into()
            },
            cls = if in_stock { "ok" } else { "oos" },
            stock = if in_stock { "In stock" } else { "Sold out" }
        ));
    }

    page(
        &format!("{} — {}", store::NAME, category.unwrap_or("everything")),
        &format!(
            "{}<div class=\"wrap\"><div class=\"grid\">{}</div>\
<footer>{} products on this shelf. This page and <code>/agent/catalog</code> read the \
same database &mdash; the shopper and the AI buyer are looking at one shop, not two.\
</footer></div>",
            header(category, &cats),
            cards,
            rows.len()
        ),
    )
}

pub fn detail(conn: &Connection, product_id: &str) -> Option<String> {
    let d = store::detail(conn, product_id, None)?;
    let cats = categories(conn);
    let empty = vec![];

    let images = d["images"].as_array().unwrap_or(&empty);
    let mut gallery = String::new();
    for img in images.iter().take(4) {
        gallery.push_str(&format!(
            "<div class=\"shot\"><img src=\"{}\" alt=\"{}\"></div>",
            esc(img.as_str().unwrap_or("")),
            esc(d["title"].as_str().unwrap_or(""))
        ));
    }

    let variants = d["variants"].as_array().unwrap_or(&empty);
    let live: Vec<i64> = variants
        .iter()
        .filter(|v| v["stock"].as_i64().unwrap_or(0) > 0)
        .map(|v| v["price_paise"].as_i64().unwrap_or(0))
        .collect();
    let from = live.iter().min().copied().unwrap_or_else(|| {
        variants
            .first()
            .and_then(|v| v["price_paise"].as_i64())
            .unwrap_or(0)
    });
    let mrp = variants
        .iter()
        .filter_map(|v| v["mrp_paise"].as_i64())
        .max()
        .filter(|m| *m > from);

    let mut vhtml = String::new();
    for v in variants {
        let opts: Vec<String> = v["options"]
            .as_object()
            .map(|m| {
                m.iter()
                    .map(|(k, val)| format!("{} {}", k, val.as_str().unwrap_or("")))
                    .collect()
            })
            .unwrap_or_default();
        let stock = v["stock"].as_i64().unwrap_or(0);
        vhtml.push_str(&format!(
            "<div class=\"var{}\"><b>{}</b><small>&#8377;{} &middot; {}</small></div>",
            if stock > 0 { "" } else { " out" },
            esc(&if opts.is_empty() {
                "Standard".to_string()
            } else {
                opts.join(" &middot; ")
            }),
            rupees(v["price_paise"].as_i64().unwrap_or(0)),
            if stock > 0 {
                format!("{} in stock", stock)
            } else {
                "sold out".into()
            }
        ));
    }

    let mut attrs = String::new();
    if let Some(map) = d["attributes"].as_object() {
        for (k, v) in map {
            attrs.push_str(&format!(
                "<tr><td>{}</td><td>{}</td></tr>",
                esc(&k.replace('_', " ")),
                esc(v.as_str().unwrap_or(""))
            ));
        }
    }

    let mut revs = String::new();
    for r in d["reviews"].as_array().unwrap_or(&empty) {
        revs.push_str(&format!(
            "<div class=\"rev\"><b>{}</b> &middot; <small>{} &#9733; &middot; {}</small>\
<div>{}</div></div>",
            esc(r["author"].as_str().unwrap_or("Anonymous")),
            r["rating"].as_i64().unwrap_or(0),
            esc(&r["created_at"].as_str().unwrap_or("").chars().take(10).collect::<String>()),
            // Review ka text bhi merchant ka text hai — verbatim jata hai (SPEC 10), aur
            // browser me escape hokar DATA rehta hai, markup nahi.
            esc(r["body"].as_str().unwrap_or(""))
        ));
    }

    let inner = format!(
        "{header}<div class=\"wrap\"><a class=\"back\" href=\"/\">&larr; all products</a>\
<div class=\"detail\"><div class=\"gallery\">{gallery}</div><div>\
<h1>{title}</h1><div class=\"brandline\">{brand}{cat}</div>\
<div class=\"big\">&#8377;{price}{mrp}</div>\
<div class=\"desc\">{desc}</div>\
<h2>Options</h2><div class=\"vars\">{vars}</div>\
{attrs_block}{revs_block}\
<div class=\"note\">Delivery is quoted per pincode at checkout. \
<b>Marigold ships to the North East</b>, where our neighbours do not, and does not ship \
to pincodes starting with 6. An AI buyer sees exactly these numbers through \
<code>/agent/products/{id}</code> &mdash; same row, same second.</div>\
</div></div><footer>{id} &middot; updated {updated}</footer></div>",
        header = header(d["category"].as_str(), &cats),
        gallery = gallery,
        title = esc(d["title"].as_str().unwrap_or("")),
        brand = d["brand"]
            .as_str()
            .map(|b| format!("{} &middot; ", esc(b)))
            .unwrap_or_default(),
        cat = esc(&d["category"].as_str().unwrap_or("").replace('-', " ")),
        price = rupees(from),
        mrp = mrp
            .map(|m| format!("<span class=\"mrp\">&#8377;{}</span>", rupees(m)))
            .unwrap_or_default(),
        desc = esc(d["description"].as_str().unwrap_or("")),
        vars = vhtml,
        attrs_block = if attrs.is_empty() {
            String::new()
        } else {
            format!("<h2>Details</h2><table>{}</table>", attrs)
        },
        revs_block = if revs.is_empty() {
            String::new()
        } else {
            format!("<h2>Reviews</h2>{}", revs)
        },
        id = esc(product_id),
        updated = esc(d["updated_at"].as_str().unwrap_or(""))
    );
    Some(page(
        &format!("{} — {}", d["title"].as_str().unwrap_or(""), store::NAME),
        &inner,
    ))
}
