use crate::{
    FeatureRecord, Kind, Result,
    command::run,
    extract::{Counters, Key, Table, header_len, parse_packet},
    netfilter::{Conntrack, Nflog},
    now_ns,
    pcap::Stats,
};
use std::{
    collections::HashSet,
    io::Write,
    time::{Duration, Instant},
};
pub enum Event {
    Initial(Key, FeatureRecord),
    Window(Key, FeatureRecord),
    Gone(String),
    Second(i64),
}
pub struct Live {
    pub table: Table,
    log: Nflog,
    ct: Conntrack,
    next: Instant,
    every: u64,
    tick: u64,
    pub stats: Stats,
    classified: std::collections::HashMap<Key, u64>,
}
impl Live {
    pub fn set_poll_timeout(&self, timeout: Duration) -> Result<()> {
        self.log.set_timeout(timeout)
    }
    pub fn new(table: Table, group: u16, every: u64) -> Result<Self> {
        Ok(Self {
            table,
            log: Nflog::open(group)?,
            ct: Conntrack::open()?,
            next: Instant::now() + Duration::from_secs(1),
            every,
            tick: 0,
            stats: Stats::default(),
            classified: std::collections::HashMap::new(),
        })
    }
    pub fn poll(
        &mut self,
        events: &mut Vec<Event>,
        dump: &mut Option<Box<dyn Write>>,
    ) -> Result<()> {
        events.clear();
        let before = self.log.overflows;
        self.log.read(|ts, ip| {
            if let Some(w) = dump {
                write_dump_packet(w, ts, ip)?;
            }
            let Some(packet) = parse_packet(ip, ts) else {
                return Ok(());
            };
            self.stats.packets += 1;
            if let Some((key, Some(mut rec), _)) = self.table.packet(packet) {
                rec.ts_ns = now_ns() as u64;
                self.stats.initial += 1;
                self.classified.insert(key, self.tick);
                events.push(Event::Initial(key, rec));
            }
            Ok(())
        })?;
        if self.log.overflows != before {
            self.table.overflow();
        }
        if Instant::now() >= self.next {
            // No fabricated one-second deltas after a stalled dump.
            let late = Instant::now().duration_since(self.next) >= Duration::from_secs(1);
            self.next = Instant::now() + Duration::from_secs(1);
            self.tick += 1;
            let mut seen = HashSet::new();
            self.ct.dump(|e| {
                if let Some((key, true)) = self.table.lookup(e.key) {
                    let f = self.table.flows.get_mut(&key).expect("lookup succeeded");
                    seen.insert(key);
                    let c = if f.lan_is_a {
                        Counters {
                            pkts_up: e.orig_pkts,
                            pkts_down: e.reply_pkts,
                            bytes_up: e.orig_bytes,
                            bytes_down: e.reply_bytes,
                        }
                    } else {
                        Counters {
                            pkts_up: e.reply_pkts,
                            pkts_down: e.orig_pkts,
                            bytes_up: e.reply_bytes,
                            bytes_down: e.orig_bytes,
                        }
                    };
                    if late {
                        f.reset_counters();
                    }
                    f.counters(c);
                }
            })?;
            let now = now_ns();
            self.table.flows.retain(|key, f| {
                if !seen.contains(key) {
                    f.missing = f.missing.saturating_add(1);
                    if f.missing > 1 {
                        self.classified.remove(key);
                        events.push(Event::Gone(f.flow_id.clone()));
                        return false;
                    }
                } else {
                    f.missing = 0;
                    if self.every > 0
                        && self
                            .classified
                            .get(key)
                            .is_some_and(|at| (self.tick - at).is_multiple_of(self.every))
                        && let Some(rec) = f.record(Kind::Window, now)
                        && rec
                            .window
                            .as_ref()
                            .is_some_and(|w| w.pkts_up_per_s + w.pkts_down_per_s > 0.0)
                    {
                        self.stats.window += 1;
                        events.push(Event::Window(*key, rec));
                    }
                }
                true
            });
            events.push(Event::Second(now));
        }
        self.stats.overflows = self.log.overflows;
        self.stats.untracked = self.table.untracked;
        self.stats.flows = self.table.flows.len() as u64;
        Ok(())
    }
}
pub struct Rule;
impl Rule {
    pub fn install(group: u16) -> Result<Self> {
        // An nft batch replaces our own table atomically.
        let tables: serde_json::Value =
            serde_json::from_slice(&run("nft", &["-j", "list", "tables"], None)?)?;
        let exists = tables["nftables"]
            .as_array()
            .ok_or("invalid nft table list")?
            .iter()
            .any(|v| v["table"]["family"] == "inet" && v["table"]["name"] == "aqos_extract");
        let rule = format!(
            "{}table inet aqos_extract {{\n chain forward {{\n type filter hook forward priority filter + 5;\n ct packets < 9 log group {group} snaplen 64\n }}\n}}\n",
            if exists {
                "delete table inet aqos_extract\n"
            } else {
                ""
            }
        );
        run("nft", &["-f", "-"], Some(&rule))?;
        Ok(Self)
    }
    pub fn remove(self) -> Result<()> {
        let result = run("nft", &["delete", "table", "inet", "aqos_extract"], None).map(|_| ());
        std::mem::forget(self);
        result
    }
}
impl Drop for Rule {
    fn drop(&mut self) {
        if let Err(e) = run("nft", &["delete", "table", "inet", "aqos_extract"], None) {
            eprintln!("extractor cleanup failed: {e}");
        }
    }
}
pub fn write_dump_header(w: &mut dyn Write) -> Result<()> {
    let mut h = [0; 24];
    h[..4].copy_from_slice(&0xa1b23c4du32.to_le_bytes());
    h[4..6].copy_from_slice(&2u16.to_le_bytes());
    h[6..8].copy_from_slice(&4u16.to_le_bytes());
    h[16..20].copy_from_slice(&64u32.to_le_bytes());
    h[20..24].copy_from_slice(&101u32.to_le_bytes());
    w.write_all(&h)?;
    Ok(())
}
fn write_dump_packet(w: &mut dyn Write, ts: i64, ip: &[u8]) -> Result<()> {
    let n = header_len(ip);
    if n == 0 {
        return Ok(());
    }
    let mut h = [0; 16];
    for (chunk, value) in h.as_chunks_mut::<4>().0.iter_mut().zip([
        (ts / 1_000_000_000) as u32,
        (ts % 1_000_000_000) as u32,
        n as u32,
        ip.len() as u32,
    ]) {
        chunk.copy_from_slice(&value.to_le_bytes());
    }
    w.write_all(&h)?;
    w.write_all(&ip[..n])?;
    Ok(())
}
