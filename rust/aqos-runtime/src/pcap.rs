use crate::{
    Kind, Result,
    extract::{Table, parse_packet},
};
use std::{
    fs::File,
    io::{BufReader, Read, Write},
    path::Path,
};

fn u16_at(b: &[u8], i: usize, le: bool) -> u16 {
    let a = [b[i], b[i + 1]];
    if le {
        u16::from_le_bytes(a)
    } else {
        u16::from_be_bytes(a)
    }
}
fn u32_at(b: &[u8], i: usize, le: bool) -> u32 {
    let a = [b[i], b[i + 1], b[i + 2], b[i + 3]];
    if le {
        u32::from_le_bytes(a)
    } else {
        u32::from_be_bytes(a)
    }
}
fn ip_offset(link: u32, frame: &[u8]) -> Option<usize> {
    match link {
        101 | 12 | 228 | 229 => Some(0),
        113 => Some(16),
        276 => Some(20),
        1 if frame.len() >= 14 => {
            let mut offset = 14;
            let mut kind = u16_at(frame, 12, false);
            while [0x8100, 0x88a8].contains(&kind) && frame.len() >= offset + 4 {
                kind = u16_at(frame, offset + 2, false);
                offset += 4;
            }
            [0x0800, 0x86dd].contains(&kind).then_some(offset)
        }
        _ => None,
    }
}
fn read_or_end(reader: &mut impl Read, out: &mut [u8]) -> Result<bool> {
    match reader.read_exact(out) {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => Ok(false),
        Err(e) => Err(e.into()),
    }
}
pub fn read(path: impl AsRef<Path>, mut emit: impl FnMut(i64, &[u8]) -> Result<()>) -> Result<()> {
    let mut reader = BufReader::with_capacity(1 << 20, File::open(path)?);
    let mut magic = [0; 4];
    reader.read_exact(&mut magic)?;
    let mut frame = |ts, link, bytes: &[u8]| -> Result<()> {
        if let Some(offset) = ip_offset(link, bytes)
            && offset < bytes.len()
        {
            emit(ts, &bytes[offset..])?;
        }
        Ok(())
    };
    if magic == [10, 13, 13, 10] {
        return read_ng(magic, &mut reader, &mut frame);
    }
    let (le, mult) = match magic {
        [0xd4, 0xc3, 0xb2, 0xa1] => (true, 1000),
        [0x4d, 0x3c, 0xb2, 0xa1] => (true, 1),
        [0xa1, 0xb2, 0xc3, 0xd4] => (false, 1000),
        [0xa1, 0xb2, 0x3c, 0x4d] => (false, 1),
        _ => return Err("not a pcap or pcapng file".into()),
    };
    let mut head = [0; 20];
    reader.read_exact(&mut head)?;
    let link = u32_at(&head, 16, le) & 0xffff;
    let mut record = [0; 16];
    let mut packet = Vec::new();
    while read_or_end(&mut reader, &mut record)? {
        let size = u32_at(&record, 8, le) as usize;
        if size > 1 << 20 {
            return Err("capture packet exceeds 1 MiB".into());
        }
        packet.resize(size, 0);
        if !read_or_end(&mut reader, &mut packet)? {
            break;
        }
        let ts = i64::from(u32_at(&record, 0, le)) * 1_000_000_000
            + i64::from(u32_at(&record, 4, le)) * mult;
        frame(ts, link, &packet)?;
    }
    Ok(())
}
fn read_ng(
    first: [u8; 4],
    r: &mut impl Read,
    emit: &mut impl FnMut(i64, u32, &[u8]) -> Result<()>,
) -> Result<()> {
    let mut head = [0; 8];
    head[..4].copy_from_slice(&first);
    let mut first = true;
    let mut le = true;
    let mut interfaces = Vec::new();
    let mut body = Vec::new();
    loop {
        if first {
            r.read_exact(&mut head[4..])?;
            first = false;
        } else if !read_or_end(r, &mut head)? {
            break;
        }
        if head[..4] == [10, 13, 13, 10] {
            let mut bom = [0; 4];
            if !read_or_end(r, &mut bom)? {
                break;
            }
            le = match bom {
                [0x4d, 0x3c, 0x2b, 0x1a] => true,
                [0x1a, 0x2b, 0x3c, 0x4d] => false,
                _ => return Err("invalid pcapng byte order".into()),
            };
            let len = u32_at(&head, 4, le) as usize;
            if !(28..=1 << 24).contains(&len) || !len.is_multiple_of(4) {
                return Err("invalid pcapng section".into());
            }
            body.resize(len - 12, 0);
            if !read_or_end(r, &mut body)? {
                break;
            }
            interfaces.clear();
            continue;
        }
        let kind = u32_at(&head, 0, le);
        let len = u32_at(&head, 4, le) as usize;
        if !(12..=1 << 24).contains(&len) || !len.is_multiple_of(4) {
            return Err("invalid pcapng block".into());
        }
        body.resize(len - 8, 0);
        if !read_or_end(r, &mut body)? {
            break;
        }
        if u32_at(&body, body.len() - 4, le) as usize != len {
            return Err("pcapng block length mismatch".into());
        }
        let b = &body[..body.len() - 4];
        if kind == 1 && b.len() >= 8 {
            let link = u32::from(u16_at(b, 0, le));
            let mut units = 1_000_000_u64;
            let mut pos = 8;
            while pos + 4 <= b.len() {
                let code = u16_at(b, pos, le);
                let size = usize::from(u16_at(b, pos + 2, le));
                if code == 0 {
                    break;
                }
                if pos + 4 + size > b.len() {
                    return Err("truncated pcapng option".into());
                }
                if code == 9 && size == 1 {
                    let v = b[pos + 4];
                    units = if v & 128 != 0 { 2_u64 } else { 10_u64 }
                        .checked_pow(u32::from(v & 127))
                        .ok_or("timestamp resolution overflow")?;
                }
                pos += 4 + size.div_ceil(4) * 4;
            }
            interfaces.push((link, units));
        } else if kind == 6 && b.len() >= 20 {
            let id = u32_at(b, 0, le) as usize;
            let (link, per) = *interfaces.get(id).ok_or("unknown pcapng interface")?;
            let size = u32_at(b, 12, le) as usize;
            if size > b.len() - 20 {
                return Err("truncated pcapng packet".into());
            }
            let units = (u64::from(u32_at(b, 4, le)) << 32) | u64::from(u32_at(b, 8, le));
            let ts = (u128::from(units) * 1_000_000_000) / u128::from(per);
            emit(i64::try_from(ts)?, link, &b[20..20 + size])?;
        }
    }
    Ok(())
}
pub fn json_line(out: &mut (impl Write + ?Sized), value: &impl serde::Serialize) -> Result<()> {
    serde_json::to_writer(&mut *out, value)?;
    out.write_all(b"\n")?;
    Ok(())
}
#[derive(Default, Debug, serde::Serialize)]
pub struct Stats {
    pub packets: u64,
    pub initial: u64,
    pub window: u64,
    pub flows: u64,
    pub untracked: u64,
    pub overflows: u64,
}
pub fn offline(
    path: impl AsRef<Path>,
    table: &mut Table,
    every: i64,
    out: &mut impl Write,
    keys: &mut impl Write,
) -> Result<Stats> {
    let mut stats = Stats::default();
    let mut began = 0;
    let mut next = 0;
    let mut second = 0;
    read(path, |ts, ip| {
        let Some(packet) = parse_packet(ip, ts) else {
            return Ok(());
        };
        if stats.packets == 0 {
            began = ts;
            next = ts.saturating_add(1_000_000_000);
        }
        stats.packets += 1;
        while ts >= next {
            second += 1;
            let now = began + second * 1_000_000_000;
            table.flows.retain(|_, f| {
                let limit = if f.key.protocol == 6 {
                    if f.closed == 3 { 10 } else { 300 }
                } else {
                    120
                };
                now.saturating_sub(f.last_seen) <= limit * 1_000_000_000
            });
            for f in table.flows.values_mut() {
                f.counters(f.total);
                if let Some(at) = f.initial_at
                    && every > 0
                    && second != at
                    && (second - at) % every == 0
                    && let Some(rec) = f.record(Kind::Window, now)
                    && rec
                        .window
                        .as_ref()
                        .is_some_and(|w| w.pkts_up_per_s + w.pkts_down_per_s > 0.0)
                {
                    json_line(out, &rec)?;
                    stats.window += 1;
                }
            }
            next = next
                .checked_add(1_000_000_000)
                .ok_or("capture time overflow")?;
        }
        if let Some((key, record, new)) = table.packet(packet) {
            if new {
                stats.flows += 1;
            }
            if let Some(rec) = record {
                let f = table.flows.get_mut(&key).ok_or("flow disappeared")?;
                f.initial_at = Some(second);
                json_line(out, &rec)?;
                json_line(keys, &f.key_line(began))?;
                stats.initial += 1;
            }
        }
        Ok(())
    })?;
    stats.untracked = table.untracked;
    out.flush()?;
    keys.flush()?;
    Ok(stats)
}
