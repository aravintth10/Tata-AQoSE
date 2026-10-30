//! Flow-level AQoSE runtime. Kernel changes belong only to the enforcer;
//! the independent watchdog is a separate crate with no runtime dependency.
pub mod args;
pub mod cake;
pub mod capacity;
pub mod client;
pub mod command;
pub mod contracts;
pub mod controller;
pub mod enforce;
pub mod exporter;
pub mod extract;
pub mod flowtable;
pub mod intent;
pub mod live;
pub mod netfilter;
pub mod pcap;
pub mod reclass;
pub mod shield;
pub mod verify;
pub use aqos_classify_rs::{Class, FeatureRecord, Kind, Result, Tin, Window};

pub fn now_ns() -> i64 {
    let t = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
    t.tv_sec
        .saturating_mul(1_000_000_000)
        .saturating_add(t.tv_nsec)
}
pub fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 15) as usize] as char);
    }
    out
}
pub fn random_id() -> Result<String> {
    let mut bytes = [0; 8];
    getrandom::fill(&mut bytes)?;
    Ok(hex(&bytes))
}
pub fn is_id(id: &str) -> bool {
    id.len() == 16
        && id
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
/// Explicit kernel encoding; never cast Tin's internal array index to a mark.
pub fn tin_mark(tin: Option<Tin>) -> u32 {
    match tin {
        None => 0,
        Some(Tin::Bulk) => 1,
        Some(Tin::BestEffort) => 2,
        Some(Tin::Video) => 3,
        Some(Tin::Voice) => 4,
    }
}
pub fn mark_tin(mark: u32) -> Option<Tin> {
    match mark & 7 {
        1 => Some(Tin::Bulk),
        2 => Some(Tin::BestEffort),
        3 => Some(Tin::Video),
        4 => Some(Tin::Voice),
        _ => None,
    }
}
