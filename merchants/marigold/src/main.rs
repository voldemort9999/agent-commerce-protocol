//! Marigold Bazaar — Merchant C. Port 8003.
//!
//!     marigold.exe          server chalao
//!     marigold.exe seed     catalog dobara bharo
//!
//! **Teesra merchant, aur pehla COMPILED wala.** Northwind Python hai, Voltline
//! JavaScript; dono dynamic. Ek statically-typed compiled zubaan wahi conformance suite
//! **bina ek line badle** pass kare — yahi is merchant ka poora maqsad hai. Framework
//! koi nahi: router neeche ek `match` hai, aur async runtime hai hi nahi.

mod agent;
mod db;
mod razorpay;
mod seed;
mod shop;
mod store;
mod util;

use agent::{Req, Res};
use std::collections::HashMap;

const DEFAULT_PORT: u16 = 8003;

/// Ek hosted platform apna port ENV se deta hai aur loopback par baithe server ko dekh
/// hi nahi sakta. Default `127.0.0.1:8003` hi rehta hai — local bartav, docs aur scripts
/// sab waise ke waise — aur sirf ek host use badal sakta hai.
fn bind_address() -> String {
    let port = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT);
    let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".into());
    format!("{}:{}", host, port)
}

fn main() {
    // `.env` repo root se, aur values `std::env` me BAITHTI hain — koi cached static
    // key nahi. Voltline me top-level constants `.env` load hone se PEHLE chal jate the,
    // key khaali milti thi, aur har payment `RATE_LIMITED` par gir jata tha: bilkul
    // waisa hi jaisa provider sach me neeche ho. Do conformance test us skip ke andar
    // chhup gaye the aur suite green rehti thi.
    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..");
    util::load_env(&root.join(".env"));

    if std::env::args().nth(1).as_deref() == Some("seed") {
        seed::run();
        return;
    }

    let conn = db::open();
    let addr = bind_address();
    let server = tiny_http::Server::http(&addr).unwrap_or_else(|e| {
        eprintln!("{} par baith nahi paya: {}", addr, e);
        std::process::exit(1);
    });

    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM products", [], |r| r.get(0))
        .unwrap_or(0);
    println!("{} listening on http://{}", store::NAME, addr);
    println!("  shop      http://{}/", addr);
    println!("  agent API http://{}/agent/manifest", addr);
    println!("  {} products · Razorpay configured: {}", count, razorpay::configured());
    if count == 0 {
        println!("  catalog khali hai — `marigold seed` chalao");
    }

    // Ek hi thread. `rusqlite::Connection` `Sync` nahi hai, aur ek dukaan jise ek Layer
    // 120 call/minute par chhuti hai, use thread pool ki zarurat hai hi nahi. Jo cheez
    // nahi hai, use test karna bhi nahi padta.
    for request in server.incoming_requests() {
        handle(&conn, request);
    }
}

fn handle(conn: &rusqlite::Connection, mut request: tiny_http::Request) {
    let raw_url = request.url().to_string();
    let method = request.method().as_str().to_string();
    let (path, query) = split_url(&raw_url);

    let mut headers = HashMap::new();
    for h in request.headers() {
        headers.insert(
            h.field.as_str().as_str().to_ascii_lowercase(),
            h.value.as_str().to_string(),
        );
    }
    let mut body = String::new();
    let _ = request.as_reader().read_to_string(&mut body);

    let req = Req {
        method: method.clone(),
        path: path.clone(),
        query,
        headers,
        body,
    };

    // ---- insaan wala darwaza. Yahan koi key nahi lagti; ye ek dukaan ka shop front hai.
    if !req.path.starts_with("/agent") {
        let html = if req.path == "/" {
            Some(shop::grid(conn, req.query.get("category").map(String::as_str)))
        } else if let Some(pid) = req.path.strip_prefix("/p/") {
            shop::detail(conn, pid)
        } else {
            None
        };
        return match html {
            Some(h) => respond_html(request, 200, &h),
            None => respond_html(request, 404, "<h1>404</h1><p><a href=\"/\">shop</a></p>"),
        };
    }

    // ---- agent wala darwaza. **Exactly ek caller** ke paas ye key hoti hai (SPEC 2.5);
    // merchant 500 agents ko nahi pehchanta, ek Layer ko pehchanta hai.
    if !agent::authorised(&req) {
        return respond_json(
            request,
            agent::err(
                401,
                "UNAUTHORIZED",
                "Missing or invalid X-Agent-Key.",
                serde_json::Value::Null,
            ),
        );
    }

    let segments: Vec<&str> = req.path.trim_matches('/').split('/').collect();
    let res: Res = match (req.method.as_str(), segments.as_slice()) {
        ("GET", ["agent", "manifest"]) => agent::manifest(conn),
        ("GET", ["agent", "catalog"]) => agent::catalog(conn, &req),
        ("GET", ["agent", "products", id]) => agent::product(conn, &req, id),
        ("POST", ["agent", "orders"]) => agent::create_order(conn, &req),
        ("GET", ["agent", "orders", id]) => agent::get_order(conn, id),
        ("POST", ["agent", "orders", id, "cancel"]) => agent::cancel_order(conn, &req, id),
        _ => agent::err(
            404,
            "PRODUCT_NOT_FOUND",
            "No such endpoint.",
            serde_json::json!({ "path": req.path.clone() }),
        ),
    };
    respond_json(request, res);
}

fn split_url(url: &str) -> (String, HashMap<String, String>) {
    let (path, qs) = url.split_once('?').unwrap_or((url, ""));
    let mut query = HashMap::new();
    for pair in qs.split('&').filter(|s| !s.is_empty()) {
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        query.insert(percent_decode(k), percent_decode(v));
    }
    (percent_decode(path), query)
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(b) => {
                        out.push(b);
                        i += 3;
                    }
                    Err(_) => {
                        out.push(bytes[i]);
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn respond_json(request: tiny_http::Request, res: Res) {
    let body = serde_json::to_string(&res.body).unwrap_or_else(|_| "{}".into());
    let header = tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
        .expect("header");
    let mut response = tiny_http::Response::from_string(body)
        .with_status_code(res.status)
        .with_header(header);
    if res.no_store {
        if let Ok(h) = tiny_http::Header::from_bytes(
            &b"Cache-Control"[..], &b"no-store"[..]) {
            response = response.with_header(h);
        }
    }
    if let Some(secs) = res.retry_after {
        if let Ok(h) = tiny_http::Header::from_bytes(
            &b"Retry-After"[..], secs.to_string().as_bytes()) {
            response = response.with_header(h);
        }
    }
    let _ = request.respond(response);
}

fn respond_html(request: tiny_http::Request, status: u16, html: &str) {
    let header =
        tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"text/html; charset=utf-8"[..])
            .expect("header");
    let response = tiny_http::Response::from_string(html.to_string())
        .with_status_code(status)
        .with_header(header);
    let _ = request.respond(response);
}
