//! Razorpay, seedhe REST par — koi SDK nahi.
//!
//! Is file ka poora kaam ek hi hai: provider ki dikkat ko **teen alag matlab** me
//! baantna, kyunki SPEC 11 ke hisaab se ek agent teenon par teen alag kaam karta hai:
//!
//!     500  humara code toota       -> agent haar jata hai
//!     429  thodi der baad phir     -> agent ruk kar retry karta hai
//!     409  aise kabhi nahi chalega -> agent request badal deta hai
//!
//! Provider ki har baat ko `500` bana dena ek sudharne layak haalat ko chhoda hua
//! checkout bana deta hai — aur wo galti humari dikhti hai, provider ki nahi.

use crate::util::base64;
use serde_json::{json, Value};

const API: &str = "https://api.razorpay.com/v1";
const TIMEOUT_SECS: u64 = 20;

/// **Keys CALL waqt padhi jati hain, kisi static me nahi baithtin.** Voltline me ye ek
/// asli bug tha (ESM ke imports `.env` load hone se pehle chal jate the) aur uska chehra
/// bilkul "provider neeche hai" jaisa dikhta tha — do conformance test us skip ke andar
/// chhup gaye the.
fn key_id() -> String {
    std::env::var("RAZORPAY_KEY_ID").unwrap_or_default()
}
fn secret() -> String {
    std::env::var("RAZORPAY_KEY_SECRET").unwrap_or_default()
}
pub fn configured() -> bool {
    !key_id().is_empty() && !secret().is_empty()
}
pub fn publishable_key() -> String {
    key_id()
}

#[derive(Debug)]
pub enum Provider {
    /// Provider ne SAAF-SAAF mana kiya. Code merchant ka apna hai, provider ka nahi.
    Refused {
        code: &'static str,
        status: u16,
        message: String,
        provider_message: String,
        retry_after: Option<u64>,
    },
    /// Baat hi nahi pahunchi. **Ye "unpaid" ka saboot NAHI hai** (SPEC 9) — timeout par
    /// likha gaya `failed` kisi ke poore ho chuke payment ke upar likha ja sakta hai.
    Unreachable(String),
}

impl Provider {
    fn refused(code: &'static str, status: u16, message: &str, provider_message: String) -> Self {
        Provider::Refused {
            code,
            status,
            message: message.to_string(),
            provider_message,
            retry_after: None,
        }
    }
}

fn agent() -> ureq::Agent {
    let tls = native_tls::TlsConnector::new().expect("tls");
    ureq::builder()
        .timeout(std::time::Duration::from_secs(TIMEOUT_SECS))
        .tls_connector(std::sync::Arc::new(tls))
        .build()
}

fn call(method: &str, route: &str, body: Option<Value>) -> Result<Value, Provider> {
    if !configured() {
        return Err(Provider::Unreachable(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET set nahi hain".into(),
        ));
    }
    let auth = format!("Basic {}", base64(format!("{}:{}", key_id(), secret()).as_bytes()));
    let url = format!("{}{}", API, route);
    let req = agent().request(method, &url).set("Authorization", &auth);

    let result = match body {
        Some(b) => req.send_json(b),
        None => req.call(),
    };

    match result {
        Ok(resp) => Ok(resp.into_json::<Value>().unwrap_or_else(|_| json!({}))),
        Err(ureq::Error::Status(status, resp)) => {
            let retry_after = resp
                .header("retry-after")
                .and_then(|v| v.parse::<u64>().ok());
            let text = resp.into_string().unwrap_or_default();
            let data: Value = serde_json::from_str(&text).unwrap_or(json!({}));
            let description = data
                .pointer("/error/description")
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .unwrap_or_else(|| text.chars().take(300).collect());
            let lower = description.to_lowercase();

            // "Thodi der baad phir." Agent `500` par haar jata hai aur `429` par rukta
            // hai — aur ye haalat asal me transient hai.
            if status == 429 || lower.contains("too many requests") || lower.contains("rate limit")
            {
                return Err(Provider::Refused {
                    code: "RATE_LIMITED",
                    status: 429,
                    message: "Payment provider is rate limiting this store. Try again shortly."
                        .into(),
                    provider_message: description,
                    retry_after: Some(retry_after.unwrap_or(5)),
                });
            }
            // Har instrument ki apni amount ceiling hai. Ye rejection is order ke liye
            // PERMANENT hai aur agent ise theek kar sakta hai — chhota order, ya doosra
            // mode. Isliye 409, 500 nahi.
            if lower.contains("amount") && (400..500).contains(&status) {
                return Err(Provider::refused(
                    "AMOUNT_LIMIT_EXCEEDED",
                    409,
                    "The payment provider refused this order because of its amount.",
                    description,
                ));
            }
            Err(Provider::refused(
                "INTERNAL_ERROR",
                500,
                "Payment provider call failed.",
                description,
            ))
        }
        // Network, DNS, timeout — kuch pata nahi chala.
        Err(e) => Err(Provider::Unreachable(e.to_string())),
    }
}

pub struct Link {
    pub id: String,
    pub url: String,
}

pub fn create_payment_link(
    amount_paise: i64,
    description: &str,
    contact: &Value,
    expires_epoch: i64,
) -> Result<Link, Provider> {
    let mut customer = json!({
        "name": contact.get("name").and_then(|v| v.as_str()).unwrap_or(""),
        "contact": contact.get("phone").and_then(|v| v.as_str()).unwrap_or("")
    });
    if let Some(email) = contact.get("email").and_then(|v| v.as_str()) {
        customer["email"] = json!(email);
    }
    let body = json!({
        "amount": amount_paise,
        "currency": "INR",
        "description": description.chars().take(250).collect::<String>(),
        "customer": customer,
        "notify": {"sms": false, "email": false},
        "reminder_enable": false,
        // Razorpay ka apna minimum 15 minute hai. Order ki ghadi phir bhi humari hai
        // (SPEC 9) — ye sirf provider ki taraf ka darwaza band karta hai.
        "expire_by": expires_epoch
    });
    let v = call("POST", "/payment_links", Some(body))?;
    Ok(Link {
        id: v["id"].as_str().unwrap_or_default().to_string(),
        url: v["short_url"].as_str().unwrap_or_default().to_string(),
    })
}

pub fn create_checkout_order(amount_paise: i64, receipt: &str) -> Result<String, Provider> {
    let v = call(
        "POST",
        "/orders",
        Some(json!({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt.chars().take(40).collect::<String>()
        })),
    )?;
    Ok(v["id"].as_str().unwrap_or_default().to_string())
}

pub struct Paid {
    pub payment_id: String,
    pub paid_at: String,
}

/// "Kya ye pay ho gaya?" — dono instruments ke liye ek hi jawab shape.
///
/// `Ok(None)` ka matlab hai **abhi tak nahi**, aur wo sirf tab lauta hai jab provider ne
/// saaf-saaf bataya ho. Baat na ho paye to `Unreachable` — kyunki order ko `failed`
/// sirf tab kiya ja sakta hai jab uska unpaid hona CONFIRM hua ho (SPEC 9).
pub fn fetch_payment(
    mode: &str,
    link_id: Option<&str>,
    order_id: Option<&str>,
) -> Result<Option<Paid>, Provider> {
    if mode == "payment_link" {
        let Some(id) = link_id else { return Ok(None) };
        let link = call("GET", &format!("/payment_links/{}", id), None)?;
        if link["status"].as_str() != Some("paid") {
            return Ok(None);
        }
        let empty = vec![];
        let payments = link["payments"].as_array().unwrap_or(&empty);
        let chosen = payments
            .iter()
            .find(|p| p["status"].as_str() == Some("captured"))
            .or_else(|| payments.first());
        return Ok(chosen.map(|p| Paid {
            payment_id: p["payment_id"].as_str().unwrap_or_default().to_string(),
            paid_at: crate::util::iso(p["created_at"].as_i64().unwrap_or(0)),
        }));
    }
    let Some(id) = order_id else { return Ok(None) };
    let res = call("GET", &format!("/orders/{}/payments", id), None)?;
    let empty = vec![];
    let captured = res["items"]
        .as_array()
        .unwrap_or(&empty)
        .iter()
        .find(|p| p["status"].as_str() == Some("captured"))
        .cloned();
    Ok(captured.map(|p| Paid {
        payment_id: p["id"].as_str().unwrap_or_default().to_string(),
        paid_at: crate::util::iso(p["created_at"].as_i64().unwrap_or(0)),
    }))
}

pub struct Refund {
    pub id: String,
    pub state: &'static str,
}

pub fn create_refund(payment_id: &str, amount_paise: i64) -> Result<Refund, Provider> {
    let v = call(
        "POST",
        &format!("/payments/{}/refund", payment_id),
        Some(json!({"amount": amount_paise, "speed": "normal"})),
    )?;
    Ok(Refund {
        id: v["id"].as_str().unwrap_or_default().to_string(),
        state: normalise_refund(v["status"].as_str().unwrap_or("")),
    })
}

pub fn fetch_refund(refund_id: &str) -> Result<Refund, Provider> {
    let v = call("GET", &format!("/refunds/{}", refund_id), None)?;
    Ok(Refund {
        id: v["id"].as_str().unwrap_or_default().to_string(),
        state: normalise_refund(v["status"].as_str().unwrap_or("")),
    })
}

/// **Bheja hua refund aur aaya hua refund do alag baatein hain** (SPEC 7). Jab tak
/// provider `processed` na kahe, paisa grahak tak pahuncha nahi hai — aur agent order
/// padhta hai, cancel ki purani response nahi.
fn normalise_refund(status: &str) -> &'static str {
    match status {
        "processed" => "processed",
        "failed" => "failed",
        "created" => "initiated",
        _ => "pending",
    }
}
