//! Bounded, sequence-checked Linux netfilter netlink transactions.
use crate::{Result, extract::Key};
use rustix::net::{
    self, AddressFamily, RecvFlags, SendFlags, SocketFlags, SocketType,
    netlink::{NETFILTER, SocketAddrNetlink},
};
use std::{
    net::{IpAddr, SocketAddr},
    os::fd::OwnedFd,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

fn invalid(msg: &str) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, msg)
}
fn u16n(b: &[u8]) -> u16 {
    u16::from_ne_bytes([b[0], b[1]])
}
fn u32n(b: &[u8]) -> u32 {
    u32::from_ne_bytes(b[..4].try_into().expect("validated length"))
}
pub fn attr(kind: u16, payload: &[u8]) -> Vec<u8> {
    assert!(payload.len() <= u16::MAX as usize - 4);
    let mut out = Vec::with_capacity((payload.len() + 7) & !3);
    out.extend_from_slice(&((payload.len() + 4) as u16).to_ne_bytes());
    out.extend_from_slice(&kind.to_ne_bytes());
    out.extend_from_slice(payload);
    out.resize((out.len() + 3) & !3, 0);
    out
}
fn nested(kind: u16, parts: &[Vec<u8>]) -> Vec<u8> {
    attr(kind | 0x8000, &parts.concat())
}
fn attrs<'a>(mut b: &'a [u8], mut f: impl FnMut(u16, &'a [u8]) -> Result<()>) -> Result<()> {
    while !b.is_empty() {
        if b.len() < 4 {
            return Err(invalid("short netlink attribute").into());
        }
        let n = u16n(b) as usize;
        if n < 4 || n > b.len() {
            return Err(invalid("invalid netlink attribute length").into());
        }
        f(u16n(&b[2..]) & 0x3fff, &b[4..n])?;
        b = &b[((n + 3) & !3).min(b.len())..];
    }
    Ok(())
}
fn message(kind: u16, flags: u16, seq: u32, family: u8, group: u16, body: &[u8]) -> Vec<u8> {
    let mut b = vec![0; 20];
    b[4..6].copy_from_slice(&kind.to_ne_bytes());
    b[6..8].copy_from_slice(&flags.to_ne_bytes());
    b[8..12].copy_from_slice(&seq.to_ne_bytes());
    b[16] = family;
    b[18..20].copy_from_slice(&group.to_be_bytes());
    b.extend_from_slice(body);
    let n = b.len() as u32;
    b[..4].copy_from_slice(&n.to_ne_bytes());
    b
}
fn messages(mut b: &[u8], mut f: impl FnMut(u16, u16, u32, &[u8]) -> Result<()>) -> Result<()> {
    while !b.is_empty() {
        if b.len() < 16 {
            return Err(invalid("short netlink header").into());
        }
        let n = u32n(b) as usize;
        if n < 16 || n > b.len() {
            return Err(invalid("invalid netlink message length").into());
        }
        f(u16n(&b[4..]), u16n(&b[6..]), u32n(&b[8..]), &b[16..n])?;
        b = &b[((n + 3) & !3).min(b.len())..];
    }
    Ok(())
}
fn kernel_error(b: &[u8]) -> Result<()> {
    if b.len() < 4 {
        return Err(invalid("short netlink error").into());
    }
    let code = i32::from_ne_bytes(b[..4].try_into()?);
    if code != 0 {
        return Err(std::io::Error::from_raw_os_error(
            code.checked_neg().ok_or_else(|| invalid("invalid errno"))?,
        )
        .into());
    }
    Ok(())
}
struct Socket {
    fd: OwnedFd,
    seq: u32,
    buf: Vec<u8>,
}
impl Socket {
    fn open(size: usize) -> Result<Self> {
        Self::open_protocol(size, Some(NETFILTER))
    }
    fn open_protocol(size: usize, protocol: Option<net::Protocol>) -> Result<Self> {
        let fd = net::socket_with(
            AddressFamily::NETLINK,
            SocketType::RAW,
            SocketFlags::CLOEXEC,
            protocol,
        )?;
        net::bind(&fd, &SocketAddrNetlink::new(0, 0))?;
        net::sockopt::set_socket_recv_buffer_size(&fd, size)?;
        net::sockopt::set_socket_timeout(
            &fd,
            net::sockopt::Timeout::Recv,
            Some(Duration::from_millis(100)),
        )?;
        Ok(Self {
            fd,
            seq: 0,
            buf: vec![0; 1 << 18],
        })
    }
    fn receive(&mut self) -> Result<usize> {
        let (_, n, from) = net::recvfrom(&self.fd, &mut self.buf[..], RecvFlags::TRUNC)?;
        let from =
            SocketAddrNetlink::try_from(from.ok_or_else(|| invalid("missing netlink sender"))?)?;
        if from.pid() != 0 {
            return Err(invalid("non-kernel netlink sender").into());
        }
        if n > self.buf.len() {
            return Err(std::io::Error::from_raw_os_error(105).into());
        }
        Ok(n)
    }
    fn request(
        &mut self,
        kind: u16,
        flags: u16,
        family: u8,
        group: u16,
        body: &[u8],
        mut each: impl FnMut(u16, &[u8]) -> Result<()>,
    ) -> Result<()> {
        self.transaction(message(kind, flags, 0, family, group, body), &mut each)
    }
    fn transaction(
        &mut self,
        mut msg: Vec<u8>,
        mut each: impl FnMut(u16, &[u8]) -> Result<()>,
    ) -> Result<()> {
        self.seq = self.seq.wrapping_add(1).max(1);
        let seq = self.seq;
        msg[8..12].copy_from_slice(&seq.to_ne_bytes());
        if net::sendto(
            &self.fd,
            &msg,
            SendFlags::empty(),
            &SocketAddrNetlink::new(0, 0),
        )? != msg.len()
        {
            return Err("short netlink send".into());
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if Instant::now() >= deadline {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "netlink transaction timed out",
                )
                .into());
            }
            let n = match self.receive() {
                Ok(n) => n,
                Err(e) if transient(&*e) => continue,
                Err(e) => return Err(e),
            };
            let mut done = false;
            messages(&self.buf[..n], |kind, flags, got, b| {
                if got != seq {
                    return Ok(());
                }
                if flags & 0x10 != 0 {
                    return Err("interrupted conntrack dump".into());
                }
                match kind {
                    2 => {
                        kernel_error(b)?;
                        done = true;
                    }
                    3 => {
                        if !b.is_empty() {
                            kernel_error(b)?;
                        }
                        done = true;
                    }
                    4 => return Err("netlink overrun".into()),
                    _ => each(kind, b)?,
                }
                Ok(())
            })?;
            if done {
                return Ok(());
            }
        }
    }
}
/// iproute2 versions that omit zero-valued CAKE attributes cannot disable
/// fwmark with `tc ... fwmark 0`. Send the explicit u32 zero to rtnetlink.
pub fn cake_fwmark(dev: &str, mask: u32) -> Result<()> {
    let links: Vec<serde_json::Value> = serde_json::from_slice(&crate::command::run(
        "ip",
        &["-j", "link", "show", "dev", dev],
        None,
    )?)?;
    let index = links
        .first()
        .and_then(|v| v["ifindex"].as_u64())
        .ok_or("missing interface index")?;
    let index = i32::try_from(index)?;
    let mut msg = vec![0; 36];
    msg[4..6].copy_from_slice(&36u16.to_ne_bytes());
    msg[6..8].copy_from_slice(&5u16.to_ne_bytes());
    msg[20..24].copy_from_slice(&index.to_ne_bytes());
    msg[28..32].copy_from_slice(&u32::MAX.to_ne_bytes());
    msg.extend(attr(1, b"cake\0"));
    msg.extend(attr(2, &attr(18, &mask.to_ne_bytes())));
    let n = msg.len() as u32;
    msg[..4].copy_from_slice(&n.to_ne_bytes());
    Socket::open_protocol(1 << 18, None)?.transaction(msg, |_, _| Ok(()))
}
fn errno(e: &(dyn std::error::Error + 'static)) -> Option<i32> {
    e.downcast_ref::<std::io::Error>()
        .and_then(|e| e.raw_os_error())
        .or_else(|| {
            e.downcast_ref::<rustix::io::Errno>()
                .map(|e| e.raw_os_error())
        })
}
fn transient(e: &(dyn std::error::Error + 'static)) -> bool {
    matches!(errno(e), Some(4 | 11))
}
pub fn no_entry(e: &(dyn std::error::Error + 'static)) -> bool {
    errno(e) == Some(2)
}

#[derive(Clone, Debug)]
pub struct Entry {
    pub key: Key,
    pub orig_pkts: u64,
    pub orig_bytes: u64,
    pub reply_pkts: u64,
    pub reply_bytes: u64,
    pub mark: u32,
}
fn tuple(key: Key) -> Result<Vec<u8>> {
    let ip = match (key.a.ip(), key.b.ip()) {
        (IpAddr::V4(a), IpAddr::V4(b)) => nested(1, &[attr(1, &a.octets()), attr(2, &b.octets())]),
        (IpAddr::V6(a), IpAddr::V6(b)) => nested(1, &[attr(3, &a.octets()), attr(4, &b.octets())]),
        _ => return Err("mixed IP families".into()),
    };
    Ok(nested(
        1,
        &[
            ip,
            nested(
                2,
                &[
                    attr(1, &[key.protocol]),
                    attr(2, &key.a.port().to_be_bytes()),
                    attr(3, &key.b.port().to_be_bytes()),
                ],
            ),
        ],
    ))
}
fn parse_tuple(b: &[u8]) -> Result<Option<Key>> {
    let (mut a, mut d, mut proto, mut sp, mut dp) = (None, None, 0, 0, 0);
    attrs(b, |kind, p| {
        match kind {
            1 => attrs(p, |k, v| {
                let addr = match v.len() {
                    4 => Some(IpAddr::from(<[u8; 4]>::try_from(v)?)),
                    16 => Some(IpAddr::from(<[u8; 16]>::try_from(v)?)),
                    _ => None,
                };
                match k {
                    1 | 3 => a = addr,
                    2 | 4 => d = addr,
                    _ => {}
                }
                Ok(())
            })?,
            2 => attrs(p, |k, v| {
                match k {
                    1 if !v.is_empty() => proto = v[0],
                    2 if v.len() == 2 => sp = u16::from_be_bytes(v.try_into()?),
                    3 if v.len() == 2 => dp = u16::from_be_bytes(v.try_into()?),
                    _ => {}
                }
                Ok(())
            })?,
            _ => {}
        }
        Ok(())
    })?;
    Ok(match (a, d) {
        (Some(a), Some(b)) if matches!(proto, 6 | 17) && a.is_ipv4() == b.is_ipv4() => Some(Key {
            protocol: proto,
            a: SocketAddr::new(a, sp),
            b: SocketAddr::new(b, dp),
        }),
        _ => None,
    })
}
fn parse_entry(b: &[u8]) -> Result<Option<Entry>> {
    let mut key = None;
    let mut counts = [0; 4];
    let mut mark = 0;
    attrs(b, |kind, p| {
        match kind {
            1 => key = parse_tuple(p)?,
            8 if p.len() == 4 => mark = u32::from_be_bytes(p.try_into()?),
            9 | 10 => attrs(p, |k, v| {
                if matches!(k, 1 | 2) && v.len() == 8 {
                    counts[((kind - 9) * 2 + k - 1) as usize] = u64::from_be_bytes(v.try_into()?);
                }
                Ok(())
            })?,
            _ => {}
        }
        Ok(())
    })?;
    Ok(key.map(|key| Entry {
        key,
        orig_pkts: counts[0],
        orig_bytes: counts[1],
        reply_pkts: counts[2],
        reply_bytes: counts[3],
        mark,
    }))
}
pub struct Conntrack {
    socket: Socket,
}
impl Conntrack {
    pub fn open() -> Result<Self> {
        Ok(Self {
            socket: Socket::open(4 << 20)?,
        })
    }
    pub fn dump(&mut self, mut each: impl FnMut(Entry)) -> Result<()> {
        self.socket.request(0x101, 0x301, 0, 0, &[], |kind, b| {
            if kind == 0x100
                && b.len() >= 4
                && let Some(e) = parse_entry(&b[4..])?
            {
                each(e);
            }
            Ok(())
        })
    }
    pub fn get(&mut self, key: Key) -> Result<Option<Entry>> {
        let mut found = None;
        let result = self.socket.request(
            0x101,
            5,
            if key.a.is_ipv4() { 2 } else { 10 },
            0,
            &tuple(key)?,
            |kind, b| {
                if kind == 0x100 && b.len() >= 4 {
                    found = parse_entry(&b[4..])?;
                }
                Ok(())
            },
        );
        match result {
            Ok(()) => Ok(found),
            Err(e) if no_entry(&*e) => Ok(None),
            Err(e) => Err(e),
        }
    }
    pub fn set_mark(&mut self, key: Key, mark: u32, mask: u32) -> Result<()> {
        let body = [
            tuple(key)?,
            attr(8, &(mark & mask).to_be_bytes()),
            attr(21, &mask.to_be_bytes()),
        ]
        .concat();
        self.socket.request(
            0x100,
            5,
            if key.a.is_ipv4() { 2 } else { 10 },
            0,
            &body,
            |_, _| Ok(()),
        )
    }
}
pub struct Nflog {
    socket: Socket,
    _stamp: OwnedFd,
    pub overflows: u64,
}
impl Nflog {
    pub fn set_timeout(&self, timeout: Duration) -> Result<()> {
        net::sockopt::set_socket_timeout(
            &self.socket.fd,
            net::sockopt::Timeout::Recv,
            Some(timeout),
        )?;
        Ok(())
    }
    pub fn open(group: u16) -> Result<Self> {
        let mut socket = Socket::open(8 << 20)?;
        for (family, id, body) in [
            (2, 0, attr(1, &[3])),
            (10, 0, attr(1, &[3])),
            (0, group, attr(1, &[1])),
            (0, group, attr(2, &[0, 0, 0, 64, 2, 0])),
            (0, group, attr(5, &1u32.to_be_bytes())),
        ] {
            socket.request(0x401, 5, family, id, &body, |_, _| Ok(()))?;
        }
        let stamp = net::socket_with(
            AddressFamily::INET,
            SocketType::DGRAM,
            SocketFlags::CLOEXEC,
            None,
        )?;
        nix::sys::socket::setsockopt(&stamp, nix::sys::socket::sockopt::ReceiveTimestampns, &true)?;
        Ok(Self {
            socket,
            _stamp: stamp,
            overflows: 0,
        })
    }
    /// Payload borrows the reusable receive buffer only for the callback.
    pub fn read(&mut self, mut each: impl FnMut(i64, &[u8]) -> Result<()>) -> Result<()> {
        let n = match self.socket.receive() {
            Ok(n) => n,
            Err(e) if transient(&*e) => return Ok(()),
            Err(e) if errno(&*e) == Some(105) => {
                self.overflows += 1;
                return Ok(());
            }
            Err(e) => return Err(e),
        };
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)?
            .as_nanos()
            .min(i64::MAX as u128) as i64;
        messages(&self.socket.buf[..n], |kind, _, _, b| {
            if kind == 4 {
                return Err("NFLOG overrun".into());
            }
            if kind != 0x400 || b.len() < 4 {
                return Ok(());
            }
            let mut ts = now;
            let mut payload = None;
            attrs(&b[4..], |k, v| {
                match k {
                    9 => payload = Some(v),
                    3 if v.len() >= 16 => {
                        let sec = u64::from_be_bytes(v[..8].try_into()?);
                        let us = u64::from_be_bytes(v[8..16].try_into()?);
                        ts = sec
                            .saturating_mul(1_000_000_000)
                            .saturating_add(us.saturating_mul(1000))
                            .min(i64::MAX as u64) as i64;
                    }
                    _ => {}
                }
                Ok(())
            })?;
            if let Some(p) = payload {
                each(ts, p)?;
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tuple_roundtrip() {
        for (a, b) in [
            ("10.0.0.1:40000", "8.8.8.8:443"),
            ("[fd00::1]:12345", "[2001:db8::2]:80"),
        ] {
            let k = Key {
                protocol: 6,
                a: a.parse().unwrap(),
                b: b.parse().unwrap(),
            };
            assert_eq!(parse_entry(&tuple(k).unwrap()).unwrap().unwrap().key, k);
        }
    }
    #[test]
    fn malformed_lengths_fail() {
        for b in [&[0, 0, 0, 0][..], &[8, 0, 0, 0][..], &[1][..]] {
            assert!(attrs(b, |_, _| Ok(())).is_err());
            assert!(messages(b, |_, _, _, _| Ok(())).is_err());
        }
    }
    #[test]
    fn aligned_attrs() {
        let b = [attr(1, &[6]), attr(2, &443u16.to_be_bytes())].concat();
        let mut got = vec![];
        attrs(&b, |k, v| {
            got.push((k, v.to_vec()));
            Ok(())
        })
        .unwrap();
        assert_eq!(got, vec![(1, vec![6]), (2, vec![1, 187])]);
    }
}
