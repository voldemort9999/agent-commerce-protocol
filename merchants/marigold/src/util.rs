//! Wo chhoti cheezein jinke liye is merchant ne jaan-boojhkar koi crate nahi li.
//!
//! Teen hain — RFC 3339 time, base64, aur `.env` padhna — aur teenon ke liye crate lena
//! aasan tha. Nahi li gayin kyunki teenon milakar ~60 line hain aur teenon ka apna
//! self-check hai (`cargo test`). Ek merchant jo teesre stack par "spec framework-agnostic
//! hai" sabit karne ke liye bana ho, use apni dependency list chhoti rakhni chahiye —
//! warna sawaal ye ban jata hai ki spec agnostic hai ya crates ka ecosystem.

use std::time::{SystemTime, UNIX_EPOCH};

// ------------------------------------------------------------------------ time

/// Abhi ka waqt, epoch seconds me.
pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Epoch seconds -> RFC 3339 UTC, `Z` ke saath. SPEC 2.2 ka aakar.
///
/// Yahan koi date library nahi hai, aur us faisle ka ek muft faayda bhi mila: RFC 3339
/// UTC strings **lexicographically** usi kram me lagti hain jis kram me waqt chalta hai.
/// Isliye `updated_since` ki tulna ek saada string comparison hai — koi parsing nahi,
/// koi timezone nahi, aur galti ki koi gunjaish nahi.
pub fn iso(epoch: i64) -> String {
    let days = epoch.div_euclid(86_400);
    let secs = epoch.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        y,
        m,
        d,
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

/// Days-since-epoch -> (year, month, day). Howard Hinnant ka `civil_from_days`.
///
/// Ye wo hissa hai jise "ek dependency bacha li" kehkar bina test ke chhod dena sabse
/// aasan hota — aur leap year ki galti chup-chaap ek din ka farq deti hai, jo `expires_at`
/// aur `cancellable_until` dono par paisa hai. Iska test neeche hai, aur usme leap day
/// aur sadi ka mod dono hain.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as i64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (y + if m <= 2 { 1 } else { 0 }, m, d)
}

// ---------------------------------------------------------------------- base64

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Do jagah chahiye: Razorpay ka `Authorization: Basic`, aur catalog ka opaque cursor.
pub fn base64(input: &[u8]) -> String {
    let mut out = String::with_capacity((input.len() + 2) / 3 * 4);
    for chunk in input.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            B64[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            B64[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

pub fn base64_decode(input: &str) -> Option<Vec<u8>> {
    let mut acc: u32 = 0;
    let mut bits = 0;
    let mut out = Vec::new();
    for ch in input.bytes() {
        if ch == b'=' {
            break;
        }
        let v = B64.iter().position(|&c| c == ch)? as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    Some(out)
}

// -------------------------------------------------------------------------- env

/// `.env` padho — repo root se, jahan baaki sab kuch padhta hai.
///
/// **Values `std::env` me tab set hoti hain jab process shuru hota hai, aur padhi
/// CALL waqt jati hain.** Ye Voltline ka mehnga sabak hai naye kapdon me: wahan
/// `razorpay.mjs` ke top-level constants `loadEnvFile()` se PEHLE chal jate the, key
/// khaali milti thi, aur har payment instrument `RATE_LIMITED` par gir jata tha — bilkul
/// waise jaise provider sach me neeche ho. Do conformance test us skip ke andar chhup
/// gaye the. Rust me `static` ke saath bhi wahi ho sakta hai, isliye yahan koi cached
/// static key nahi hai.
pub fn load_env(path: &std::path::Path) {
    let Ok(text) = std::fs::read_to_string(path) else {
        return;
    };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        let v = v.trim().trim_matches('"').trim_matches('\'');
        if std::env::var(k.trim()).is_err() {
            std::env::set_var(k.trim(), v);
        }
    }
}

// ------------------------------------------------------------------------ misc

/// HTML escape. Dukaan ke do page merchant ke apne text se bante hain, aur wo text
/// attacker-controlled hai (SPEC 10) — description verbatim jaati hai, to browser me
/// bhi wo data hai, markup nahi.
pub fn esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(c),
        }
    }
    out
}

/// Paise -> "1,499.00". Indian grouping (2,2,3), kyunki dukaan India me hai.
pub fn rupees(paise: i64) -> String {
    let neg = paise < 0;
    let p = paise.abs();
    let whole = p / 100;
    let frac = p % 100;
    let digits = whole.to_string();
    let grouped = if digits.len() <= 3 {
        digits
    } else {
        let (head, tail) = digits.split_at(digits.len() - 3);
        let mut parts: Vec<String> = Vec::new();
        let mut rest = head.to_string();
        while rest.len() > 2 {
            let cut = rest.len() - 2;
            parts.push(rest.split_off(cut));
        }
        if !rest.is_empty() {
            parts.push(rest);
        }
        parts.reverse();
        format!("{},{}", parts.join(","), tail)
    };
    format!("{}{}.{:02}", if neg { "-" } else { "" }, grouped, frac)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_matches_known_instants() {
        assert_eq!(iso(0), "1970-01-01T00:00:00Z");
        assert_eq!(iso(1_000_000_000), "2001-09-09T01:46:40Z");
        // Leap day, aur sadi ka mod: 2000 leap hai, 1900 nahi tha.
        assert_eq!(iso(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(iso(1_709_164_800), "2024-02-29T00:00:00Z");
        assert_eq!(iso(1_756_857_600), "2025-09-03T00:00:00Z");
    }

    #[test]
    fn iso_strings_sort_the_way_time_runs() {
        // `updated_since` ki poori tulna isi property par khadi hai.
        let mut stamps: Vec<String> = vec![
            iso(1_756_857_600),
            iso(0),
            iso(1_000_000_000),
            iso(951_782_400),
        ];
        let mut by_time = stamps.clone();
        stamps.sort();
        by_time.sort_by_key(|s| s.clone());
        assert_eq!(stamps, by_time);
        assert!(iso(10) > iso(9));
    }

    #[test]
    fn base64_round_trips() {
        for s in ["", "a", "ab", "abc", "abcd", "key_id:secret", "2026-09-03T00:00:00Z|mb-7"] {
            let enc = base64(s.as_bytes());
            assert_eq!(base64_decode(&enc).unwrap(), s.as_bytes(), "{s}");
        }
        assert_eq!(base64(b"abc"), "YWJj");
        assert_eq!(base64(b"ab"), "YWI=");
    }

    #[test]
    fn rupees_groups_the_indian_way() {
        assert_eq!(rupees(0), "0.00");
        assert_eq!(rupees(4900), "49.00");
        assert_eq!(rupees(99900), "999.00");
        assert_eq!(rupees(149900), "1,499.00");
        assert_eq!(rupees(20000000), "2,00,000.00");
        assert_eq!(rupees(72799900), "7,27,999.00");
    }
}
