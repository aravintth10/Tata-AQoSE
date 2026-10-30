//! Public-dataset flow tables use the same features as PCAP and NFLOG.
use crate::{
    Kind, Result,
    extract::{Counters, Packet, Table},
    pcap::json_line,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Read, Write},
    net::{IpAddr, SocketAddr},
};
#[derive(Deserialize)]
struct Row {
    source: String,
    file: String,
    folder: String,
    protocol: u8,
    ip_version: u8,
    src_port: u16,
    dst_port: u16,
    src_private: bool,
    dst_private: bool,
    start_s: f64,
    duration_s: f64,
    packets: u64,
    bytes: u64,
    oversize: u64,
    from_start: bool,
    first: Vec<[i64; 2]>,
    seconds: Vec<Vec<u64>>,
}
#[derive(Default, Serialize)]
pub struct Stats {
    pub rows: u64,
    pub initial: u64,
    pub window: u64,
    pub began_before_capture: u64,
    pub merged_first_packets: u64,
    pub too_short: u64,
    pub windows_withheld: u64,
}
fn fake(v6: bool, lan: bool, n: u32) -> IpAddr {
    if v6 {
        let mut b = [0; 16];
        if lan {
            b[0] = 0xfd;
        } else {
            b[..4].copy_from_slice(&[0x20, 1, 0x0d, 0xb8]);
        }
        b[12..].copy_from_slice(&n.to_be_bytes());
        b.into()
    } else {
        [
            if lan { 10 } else { 203 },
            (n >> 16) as u8,
            (n >> 8) as u8,
            n as u8,
        ]
        .into()
    }
}
pub fn read(path: &str, out: &mut dyn Write, every: u64) -> Result<Stats> {
    let file = std::fs::File::open(path)?;
    let input: Box<dyn Read> = if path.ends_with(".gz") {
        Box::new(flate2::read::MultiGzDecoder::new(file))
    } else {
        Box::new(file)
    };
    let mut input = BufReader::new(input);
    let mut line = Vec::new();
    let mut stats = Stats::default();
    loop {
        line.clear();
        let count = input
            .by_ref()
            .take((1 << 28) + 1)
            .read_until(b'\n', &mut line)?;
        if count == 0 {
            break;
        }
        if count > 1 << 28 {
            return Err("flow-table row exceeds 256 MiB".into());
        }
        let row: Row = serde_json::from_slice(&line)?;
        stats.rows += 1;
        if !row.from_start {
            stats.began_before_capture += 1;
            continue;
        }
        if row.first.len() < 8 {
            stats.too_short += 1;
            continue;
        }
        if row.first[..8].iter().any(|p| p[1] > 1500 || p[1] < -1500) {
            stats.merged_first_packets += 1;
            continue;
        }
        if ![4, 6].contains(&row.ip_version)
            || ![6, 17].contains(&row.protocol)
            || !row.start_s.is_finite()
            || row.start_s < 0.0
            || !row.duration_s.is_finite()
            || row.duration_s < 0.0
            || row.duration_s > 31_536_000.0
        {
            return Err("invalid flow-table timing or protocol".into());
        }
        let key = Sha256::digest(format!("{}/{}/{}", row.source, row.folder, row.file).as_bytes());
        let mut table = Table::new(vec!["10.0.0.0/8".parse()?, "fd00::/8".parse()?], &key, 1);
        let src_lan = row.src_private || !row.dst_private;
        let n = u32::try_from(stats.rows)?;
        let src = SocketAddr::new(fake(row.ip_version == 6, src_lan, n), row.src_port);
        let dst = SocketAddr::new(fake(row.ip_version == 6, !src_lan, n), row.dst_port);
        let began = (row.start_s * 1e9) as i64;
        let mut initial = None;
        let mut flowkey = None;
        for p in &row.first[..8] {
            let ts = began
                .checked_add(p[0].checked_mul(1000).ok_or("packet timestamp overflow")?)
                .ok_or("packet timestamp overflow")?;
            let packet = Packet {
                ts_ns: ts,
                protocol: row.protocol,
                src: if p[1] < 0 { dst } else { src },
                dst: if p[1] < 0 { src } else { dst },
                l3_len: p[1].abs() as i32,
                tcp_flags: 0,
            };
            if let Some((k, r, _)) = table.packet(packet) {
                flowkey = Some(k);
                initial = r;
            }
        }
        let Some(rec) = initial else {
            continue;
        };
        let mut emit = |rec: crate::FeatureRecord| -> Result<()> {
            let mut v = serde_json::to_value(rec)?;
            let obj = v.as_object_mut().ok_or("record must be object")?;
            obj.insert("source".into(), row.source.clone().into());
            obj.insert("file".into(), row.file.clone().into());
            obj.insert("folder".into(), row.folder.clone().into());
            obj.insert("flow_bytes".into(), row.bytes.into());
            obj.insert("flow_packets".into(), row.packets.into());
            json_line(out, &v)
        };
        emit(rec)?;
        stats.initial += 1;
        if every == 0 {
            continue;
        }
        if row.oversize > 0 {
            stats.windows_withheld += 1;
            continue;
        }
        let f = table
            .flows
            .get_mut(&flowkey.ok_or("missing flow")?)
            .ok_or("missing flow")?;
        let seconds: HashMap<u64, &[u64]> = row
            .seconds
            .iter()
            .filter(|s| s.len() == 5)
            .map(|s| (s[0], &s[1..]))
            .collect();
        let mut total = Counters::default();
        for sec in 0..row.duration_s as u64 {
            if let Some(s) = seconds.get(&sec) {
                let (up, down) = if f.lan_is_a { (0, 1) } else { (1, 0) };
                total.pkts_up = total
                    .pkts_up
                    .checked_add(s[up])
                    .ok_or("packet count overflow")?;
                total.pkts_down = total
                    .pkts_down
                    .checked_add(s[down])
                    .ok_or("packet count overflow")?;
                total.bytes_up = total
                    .bytes_up
                    .checked_add(s[up + 2])
                    .ok_or("byte count overflow")?;
                total.bytes_down = total
                    .bytes_down
                    .checked_add(s[down + 2])
                    .ok_or("byte count overflow")?;
            }
            f.counters(total);
            if (sec + 1).is_multiple_of(every) {
                let ts = began
                    .checked_add((sec + 1) as i64 * 1_000_000_000)
                    .ok_or("window timestamp overflow")?;
                if let Some(rec) = f.record(Kind::Window, ts)
                    && rec
                        .window
                        .as_ref()
                        .is_some_and(|w| w.pkts_up_per_s + w.pkts_down_per_s > 0.0)
                {
                    emit(rec)?;
                    stats.window += 1;
                }
            }
        }
    }
    out.flush()?;
    Ok(stats)
}
