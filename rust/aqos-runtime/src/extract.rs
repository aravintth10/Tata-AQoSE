use crate::{FeatureRecord, Kind, Window, hex};
use hmac::{Hmac, Mac};
use ipnet::IpNet;
use sha2::Sha256;
use std::{
    collections::HashMap,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
};

pub const HEADER_BYTES: usize = 64;
pub const WINDOW_SECONDS: usize = 5;
pub const PRIVATE_NETS: [&str; 4] = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"];
#[derive(Clone, Copy, Debug)]
pub struct Packet {
    pub ts_ns: i64,
    pub protocol: u8,
    pub src: SocketAddr,
    pub dst: SocketAddr,
    pub l3_len: i32,
    pub tcp_flags: u8,
}
pub fn parse_packet(ip: &[u8], ts_ns: i64) -> Option<Packet> {
    if ip.len() < 20 {
        return None;
    }
    let be16 = |i| u16::from_be_bytes([ip[i], ip[i + 1]]);
    let (src, dst, protocol, l4, l3_len) = match ip[0] >> 4 {
        4 => {
            let l4 = usize::from(ip[0] & 15) * 4;
            if l4 < 20 || be16(6) & 0x1fff != 0 {
                return None;
            }
            (
                IpAddr::V4(Ipv4Addr::from(<[u8; 4]>::try_from(&ip[12..16]).ok()?)),
                IpAddr::V4(Ipv4Addr::from(<[u8; 4]>::try_from(&ip[16..20]).ok()?)),
                ip[9],
                l4,
                i32::from(be16(2)),
            )
        }
        6 if ip.len() >= 40 => (
            IpAddr::V6(Ipv6Addr::from(<[u8; 16]>::try_from(&ip[8..24]).ok()?)),
            IpAddr::V6(Ipv6Addr::from(<[u8; 16]>::try_from(&ip[24..40]).ok()?)),
            ip[6],
            40,
            i32::from(be16(4)) + 40,
        ),
        _ => return None,
    };
    if ![6, 17].contains(&protocol) || ip.len() < l4 + 4 || l3_len < l4 as i32 {
        return None;
    }
    Some(Packet {
        ts_ns,
        protocol,
        src: SocketAddr::new(src, be16(l4)),
        dst: SocketAddr::new(dst, be16(l4 + 2)),
        l3_len,
        tcp_flags: if protocol == 6 {
            ip.get(l4 + 13).copied().unwrap_or(0)
        } else {
            0
        },
    })
}
/// Limit saved captures to headers, even when a 64-byte NFLOG snapshot includes UDP payload.
pub fn header_len(ip: &[u8]) -> usize {
    let Some(p) = parse_packet(ip, 0) else {
        return 0;
    };
    let l4 = if ip[0] >> 4 == 4 {
        usize::from(ip[0] & 15) * 4
    } else {
        40
    };
    let transport = if p.protocol == 17 { 8 } else { 20 };
    ip.len().min(HEADER_BYTES).min(l4 + transport)
}
#[derive(Clone)]
pub struct Hasher(Hmac<Sha256>);
impl Hasher {
    pub fn new(key: &[u8]) -> Self {
        Self(Hmac::<Sha256>::new_from_slice(key).expect("HMAC accepts every key size"))
    }
    fn id(&self, parts: &[&[u8]]) -> String {
        let mut mac = self.0.clone();
        for part in parts {
            mac.update(part);
        }
        hex(&mac.finalize().into_bytes()[..8])
    }
    pub fn host_id(&self, ip: IpAddr) -> String {
        match ip {
            IpAddr::V4(a) => self.id(&[b"host", &a.octets()]),
            IpAddr::V6(a) => self.id(&[b"host", &a.octets()]),
        }
    }
    pub fn flow_id(&self, key: Key) -> String {
        // Match netip.AddrPort.MarshalBinary: address bytes then little-endian port.
        fn endpoint(a: SocketAddr) -> Vec<u8> {
            let mut out = match a.ip() {
                IpAddr::V4(ip) => ip.octets().to_vec(),
                IpAddr::V6(ip) => ip.octets().to_vec(),
            };
            out.extend_from_slice(&a.port().to_le_bytes());
            out
        }
        self.id(&[&[key.protocol], &endpoint(key.a), &endpoint(key.b)])
    }
}
#[derive(Clone, Copy, Default, Debug, serde::Serialize, serde::Deserialize)]
pub struct Counters {
    pub pkts_up: u64,
    pub pkts_down: u64,
    pub bytes_up: u64,
    pub bytes_down: u64,
}
pub fn window(samples: &[Counters]) -> Option<Window> {
    let samples = &samples[samples.len().saturating_sub(WINDOW_SECONDS)..];
    if samples.is_empty() {
        return None;
    }
    let mut sum = [0.0; 4];
    let mut sizes = [0.0; 5];
    let mut size_count = 0;
    let mut pkts = [0.0; 5];
    let mut bytes = [0.0; 5];
    for (i, s) in samples.iter().enumerate() {
        for (a, b) in sum
            .iter_mut()
            .zip([s.pkts_up, s.pkts_down, s.bytes_up, s.bytes_down])
        {
            *a += b as f64;
        }
        pkts[i] = s.pkts_up as f64 + s.pkts_down as f64;
        bytes[i] = s.bytes_up as f64 + s.bytes_down as f64;
        if pkts[i] > 0.0 {
            sizes[size_count] = bytes[i] / pkts[i];
            size_count += 1;
        }
    }
    let n = samples.len();
    let (mean_pkts, std_pkts) = mean_std(&pkts[..n]);
    let (mean_bytes, _) = mean_std(&bytes[..n]);
    Some(Window {
        samples: n as u8,
        pkts_up_per_s: sum[0] / n as f64,
        pkts_down_per_s: sum[1] / n as f64,
        bytes_up_per_s: sum[2] / n as f64,
        bytes_down_per_s: sum[3] / n as f64,
        mean_pkt_size_up: if sum[0] > 0.0 { sum[2] / sum[0] } else { 0.0 },
        mean_pkt_size_down: if sum[1] > 0.0 { sum[3] / sum[1] } else { 0.0 },
        pkt_size_spread: mean_std(&sizes[..size_count]).1,
        rate_cv: if mean_pkts > 0.0 {
            std_pkts / mean_pkts
        } else {
            0.0
        },
        burstiness: if mean_bytes > 0.0 {
            bytes[..n].iter().copied().fold(0.0, f64::max) / mean_bytes
        } else {
            0.0
        },
    })
}
fn mean_std(values: &[f64]) -> (f64, f64) {
    if values.is_empty() {
        return (0.0, 0.0);
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    (
        mean,
        (values.iter().map(|v| (v - mean) * (v - mean)).sum::<f64>() / values.len() as f64).sqrt(),
    )
}
#[derive(Clone, Copy, Hash, PartialEq, Eq, Debug)]
pub struct Key {
    pub protocol: u8,
    pub a: SocketAddr,
    pub b: SocketAddr,
}
impl Key {
    pub fn reversed(self) -> Self {
        Self {
            protocol: self.protocol,
            a: self.b,
            b: self.a,
        }
    }
}
pub struct Flow {
    pub key: Key,
    pub flow_id: String,
    pub host_id: String,
    pub lan_is_a: bool,
    pub seen: usize,
    pub sizes: [i32; 8],
    pub gaps: [u64; 7],
    last_ts: i64,
    pub truncated: bool,
    pub closed: u8,
    pub total: Counters,
    prev: Option<Counters>,
    samples: [Counters; 5],
    sample_count: usize,
    pub last_seen: i64,
    pub first_seen: i64,
    pub initial_at: Option<i64>,
    pub missing: u8,
}
impl Flow {
    pub fn reset_counters(&mut self) {
        self.prev = None;
        self.sample_count = 0;
    }
    pub fn counters(&mut self, now: Counters) {
        if let Some(p) = self.prev {
            // A reset of any counter starts a fresh baseline; never wrap byte deltas.
            if now.pkts_up >= p.pkts_up
                && now.pkts_down >= p.pkts_down
                && now.bytes_up >= p.bytes_up
                && now.bytes_down >= p.bytes_down
            {
                if self.sample_count == 5 {
                    self.samples.copy_within(1..5, 0);
                } else {
                    self.sample_count += 1;
                }
                self.samples[self.sample_count - 1] = Counters {
                    pkts_up: now.pkts_up - p.pkts_up,
                    pkts_down: now.pkts_down - p.pkts_down,
                    bytes_up: now.bytes_up - p.bytes_up,
                    bytes_down: now.bytes_down - p.bytes_down,
                };
            } else {
                self.sample_count = 0;
            }
        }
        self.prev = Some(now);
    }
    pub fn window(&self) -> Option<Window> {
        window(&self.samples[..self.sample_count])
    }
    pub fn record(&self, kind: Kind, ts_ns: i64) -> Option<FeatureRecord> {
        if self.seen != 8 || self.truncated || ts_ns < 0 {
            return None;
        }
        let window = self.window();
        if kind == Kind::Window && window.is_none() {
            return None;
        }
        Some(FeatureRecord {
            schema_version: 1,
            flow_id: self.flow_id.clone(),
            host_id: self.host_id.clone(),
            kind,
            ts_ns: ts_ns as u64,
            ip_version: if self.key.a.is_ipv4()
                || matches!(self.key.a.ip(),IpAddr::V6(v) if v.to_ipv4_mapped().is_some())
            {
                4
            } else {
                6
            },
            protocol: self.key.protocol,
            src_port: self.key.a.port(),
            dst_port: self.key.b.port(),
            pkt_sizes: self.sizes,
            pkt_gaps_us: self.gaps,
            window,
        })
    }
    pub fn key_line(&self, began: i64) -> serde_json::Value {
        serde_json::json!({"flow_id":self.flow_id,"protocol":self.key.protocol,"src_ip":self.key.a.ip().to_string(),"src_port":self.key.a.port(),"dst_ip":self.key.b.ip().to_string(),"dst_port":self.key.b.port(),"first_s":(self.first_seen-began) as f64/1e9,"lan_is_src":self.lan_is_a})
    }
}
pub struct Table {
    pub flows: HashMap<Key, Flow>,
    pub hasher: Hasher,
    lan: Vec<IpNet>,
    max: usize,
    pub untracked: u64,
}
impl Table {
    pub fn new(lan: Vec<IpNet>, key: &[u8], max: usize) -> Self {
        Self {
            flows: HashMap::new(),
            hasher: Hasher::new(key),
            lan,
            max,
            untracked: 0,
        }
    }
    pub fn lookup(&self, key: Key) -> Option<(Key, bool)> {
        if self.flows.contains_key(&key) {
            Some((key, true))
        } else if self.flows.contains_key(&key.reversed()) {
            Some((key.reversed(), false))
        } else {
            None
        }
    }
    pub fn packet(&mut self, p: Packet) -> Option<(Key, Option<FeatureRecord>, bool)> {
        let incoming = Key {
            protocol: p.protocol,
            a: p.src,
            b: p.dst,
        };
        let mut found = self.lookup(incoming);
        if let Some((key, _)) = found
            && p.protocol == 6
            && p.tcp_flags & 0x12 == 2
            && self.flows[&key].closed == 3
        {
            self.flows.remove(&key);
            found = None;
        }
        let new = found.is_none();
        let (key, forward) = if let Some(v) = found {
            v
        } else {
            if self.max > 0 && self.flows.len() >= self.max {
                self.untracked += 1;
                return None;
            }
            let a = self.lan.iter().any(|n| n.contains(&p.src.ip()));
            let b = self.lan.iter().any(|n| n.contains(&p.dst.ip()));
            let lan_is_a = a || !b;
            self.flows.insert(
                incoming,
                Flow {
                    key: incoming,
                    flow_id: self.hasher.flow_id(incoming),
                    host_id: self
                        .hasher
                        .host_id(if lan_is_a { p.src.ip() } else { p.dst.ip() }),
                    lan_is_a,
                    seen: 0,
                    sizes: [0; 8],
                    gaps: [0; 7],
                    last_ts: 0,
                    truncated: false,
                    closed: 0,
                    total: Counters::default(),
                    prev: None,
                    samples: [Counters::default(); 5],
                    sample_count: 0,
                    last_seen: p.ts_ns,
                    first_seen: p.ts_ns,
                    initial_at: None,
                    missing: 0,
                },
            );
            (incoming, true)
        };
        let f = self.flows.get_mut(&key)?;
        let up = forward == f.lan_is_a;
        f.last_seen = p.ts_ns;
        if up {
            f.total.pkts_up += 1;
            f.total.bytes_up += p.l3_len as u64;
        } else {
            f.total.pkts_down += 1;
            f.total.bytes_down += p.l3_len as u64;
        }
        if p.protocol == 6 {
            if p.tcp_flags & 4 != 0 {
                f.closed = 3;
            } else if p.tcp_flags & 1 != 0 {
                f.closed |= if forward { 1 } else { 2 };
            }
        }
        if f.seen == 8 || f.truncated {
            return Some((key, None, new));
        }
        if f.seen > 0 {
            f.gaps[f.seen - 1] = p.ts_ns.saturating_sub(f.last_ts).max(0) as u64 / 1000;
        }
        f.sizes[f.seen] = if up { p.l3_len } else { -p.l3_len };
        f.last_ts = p.ts_ns;
        f.seen += 1;
        Some((key, f.record(Kind::Initial, p.ts_ns), new))
    }
    pub fn overflow(&mut self) {
        for f in self.flows.values_mut() {
            if f.seen < 8 {
                f.truncated = true;
            }
        }
    }
}
