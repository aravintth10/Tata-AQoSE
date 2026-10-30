//! Two bounded, expiring leases; the HTTP adapter never blocks the controller.
use crate::{Class, Result, extract::Hasher, is_id, random_id};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    io::{Read, Write},
    net::{IpAddr, TcpListener, TcpStream},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
pub const CLASSES: [Class; 3] = [
    Class::Telephony,
    Class::RealtimeInteractive,
    Class::MultimediaConferencing,
];
#[derive(Clone, Serialize, Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Lease {
    pub id: String,
    pub host: String,
    pub expires: String,
    pub seconds_left: u64,
    #[serde(skip)]
    deadline: Option<Instant>,
}
pub struct Store {
    leases: Vec<Lease>,
    path: Option<PathBuf>,
    hasher: Hasher,
}
impl Store {
    pub fn new(path: &str, hasher: Hasher) -> Result<Self> {
        let path = (!path.is_empty()).then(|| PathBuf::from(path));
        let mut leases = Vec::new();
        if let Some(path) = &path {
            match std::fs::read(path) {
                Ok(data) => {
                    let saved: Vec<Lease> = serde_json::from_slice(&data)?;
                    for mut l in saved {
                        let left = (OffsetDateTime::parse(&l.expires, &Rfc3339)?
                            - OffsetDateTime::now_utc())
                        .as_seconds_f64();
                        if left <= 0.0 || left > 7200.0 {
                            continue;
                        }
                        if !is_id(&l.id)
                            || l.host.parse::<IpAddr>().is_err()
                            || leases.len() >= 2
                            || leases
                                .iter()
                                .any(|old: &Lease| old.host == l.host || old.id == l.id)
                        {
                            return Err("invalid saved leases".into());
                        }
                        l.deadline = Some(Instant::now() + Duration::from_secs_f64(left));
                        leases.push(l);
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
        }
        Ok(Self {
            leases,
            path,
            hasher,
        })
    }
    fn save(&self) -> Result<()> {
        let Some(path) = &self.path else {
            return Ok(());
        };
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        std::fs::create_dir_all(parent)?;
        let tmp = parent.join(format!(".aqos-leases-{}.tmp", random_id()?));
        let result = (|| -> Result<()> {
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&tmp)?;
            serde_json::to_writer(&mut f, &self.leases)?;
            f.sync_all()?;
            std::fs::rename(&tmp, path)?;
            std::fs::File::open(parent)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(tmp);
        }
        result
    }
    pub fn expire(&mut self) -> Result<()> {
        let before = self.leases.len();
        let now = Instant::now();
        self.leases.retain(|l| l.deadline.is_some_and(|d| now < d));
        if before != self.leases.len() {
            self.save()?;
        }
        Ok(())
    }
    pub fn list(&mut self) -> Result<Vec<Lease>> {
        self.expire()?;
        let now = Instant::now();
        let mut leases = self.leases.clone();
        for l in &mut leases {
            l.seconds_left = l
                .deadline
                .map(|d| d.saturating_duration_since(now).as_secs_f64().round() as u64)
                .unwrap_or(0);
        }
        leases.sort_by_key(|l| l.seconds_left);
        Ok(leases)
    }
    pub fn leased(&self, host_id: &str) -> bool {
        let now = Instant::now();
        self.leases.iter().any(|l| {
            l.deadline.is_some_and(|d| now < d)
                && l.host
                    .parse::<IpAddr>()
                    .is_ok_and(|ip| self.hasher.host_id(ip) == host_id)
        })
    }
    pub fn delete(&mut self, id: &str) -> Result<()> {
        self.expire()?;
        let old = self.leases.clone();
        self.leases.retain(|l| l.id != id);
        if old.len() == self.leases.len() {
            return Err("no such lease".into());
        }
        if let Err(e) = self.save() {
            self.leases = old;
            return Err(e);
        }
        Ok(())
    }
    pub fn create(
        &mut self,
        host: IpAddr,
        minutes: f64,
        known: &HashSet<String>,
    ) -> std::result::Result<Lease, (u16, String)> {
        let err = |status, msg: &str| Err((status, msg.to_owned()));
        if !minutes.is_finite() || minutes <= 0.0 {
            return err(400, "the duration must be more than zero");
        }
        if minutes > 120.0 {
            return err(400, "a lease can run for at most two hours");
        }
        if !known.contains(&self.hasher.host_id(host)) {
            return err(
                404,
                "no device with that address has been seen on the network",
            );
        }
        self.expire().map_err(|e| (500, e.to_string()))?;
        let host = host.to_string();
        if self.leases.iter().any(|l| l.host == host) {
            return err(409, "that device already has a lease");
        }
        if self.leases.len() >= 2 {
            return err(409, "two leases are already active; end one first");
        }
        let duration = Duration::from_secs_f64(minutes * 60.0);
        let expires = (OffsetDateTime::now_utc() + time::Duration::seconds_f64(minutes * 60.0))
            .format(&Rfc3339)
            .map_err(|e| (500, e.to_string()))?;
        let lease = Lease {
            id: random_id().map_err(|e| (500, e.to_string()))?,
            host,
            expires,
            seconds_left: duration.as_secs(),
            deadline: Some(Instant::now() + duration),
        };
        self.leases.push(lease.clone());
        if let Err(e) = self.save() {
            self.leases.pop();
            return Err((500, e.to_string()));
        }
        Ok(lease)
    }
}
struct Client {
    socket: TcpStream,
    input: Vec<u8>,
    output: Vec<u8>,
    written: usize,
    started: Instant,
}
pub struct Server {
    listener: TcpListener,
    clients: Vec<Client>,
}
impl Server {
    pub fn bind(addr: &str) -> Result<Self> {
        let listener = TcpListener::bind(addr)?;
        listener.set_nonblocking(true)?;
        Ok(Self {
            listener,
            clients: Vec::new(),
        })
    }
    pub fn poll(&mut self, store: &mut Store, hosts: &HashSet<String>) {
        for _ in 0..16 {
            match self.listener.accept() {
                Ok((socket, _)) => {
                    if self.clients.len() >= 16 {
                        continue;
                    }
                    if socket.set_nonblocking(true).is_err() {
                        continue;
                    }
                    self.clients.push(Client {
                        socket,
                        input: Vec::with_capacity(1024),
                        output: Vec::new(),
                        written: 0,
                        started: Instant::now(),
                    });
                }
                Err(_) => break,
            }
        }
        self.clients.retain_mut(|c| {
            if c.started.elapsed() > Duration::from_secs(2) {
                return false;
            }
            if c.output.is_empty() {
                let mut buf = [0; 4096];
                for _ in 0..4 {
                    match c.socket.read(&mut buf) {
                        Ok(0) => return false,
                        Ok(n) => {
                            c.input.extend_from_slice(&buf[..n]);
                            if c.input.len() > 12288 {
                                c.output = response(
                                    413,
                                    "application/json",
                                    br#"{"error":"request too large"}"#,
                                );
                                break;
                            }
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                        Err(_) => return false,
                    }
                }
                if c.output.is_empty() {
                    match request(&c.input, store, hosts) {
                        Ok(Some(out)) => c.output = out,
                        Ok(None) => return true,
                        Err(e) => {
                            c.output = response(
                                400,
                                "application/json",
                                serde_json::json!({"error":e.to_string()})
                                    .to_string()
                                    .as_bytes(),
                            )
                        }
                    }
                }
            }
            while c.written < c.output.len() {
                match c.socket.write(&c.output[c.written..]) {
                    Ok(0) => return false,
                    Ok(n) => c.written += n,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return true,
                    Err(_) => return false,
                }
            }
            false
        });
    }
}
fn response(status: u16, kind: &str, body: &[u8]) -> Vec<u8> {
    let reason = match status {
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Content Too Large",
        _ => "Internal Server Error",
    };
    let mut out=format!("HTTP/1.1 {status} {reason}\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len()).into_bytes();
    out.extend_from_slice(body);
    out
}
fn request(bytes: &[u8], store: &mut Store, hosts: &HashSet<String>) -> Result<Option<Vec<u8>>> {
    let mut headers = [httparse::EMPTY_HEADER; 32];
    let mut request = httparse::Request::new(&mut headers);
    let offset = match request.parse(bytes)? {
        httparse::Status::Partial => return Ok(None),
        httparse::Status::Complete(n) => n,
    };
    if offset > 8192 {
        return Err("headers too large".into());
    }
    let mut length = None;
    for h in request.headers.iter() {
        if h.name.eq_ignore_ascii_case("transfer-encoding") {
            return Err("transfer encoding is not supported".into());
        }
        if h.name.eq_ignore_ascii_case("content-length") {
            if length.is_some() {
                return Err("duplicate content length".into());
            }
            length = Some(std::str::from_utf8(h.value)?.parse::<usize>()?);
        }
    }
    let len = length.unwrap_or(0);
    if len > 4096 {
        return Err("body too large".into());
    }
    if bytes.len() < offset + len {
        return Ok(None);
    }
    let method = request.method.unwrap_or("");
    let path = request.path.unwrap_or("");
    let answer = match (method, path) {
        ("GET", "/") => response(
            200,
            "text/html; charset=utf-8",
            include_bytes!("intent.html"),
        ),
        ("GET", "/leases") => response(
            200,
            "application/json",
            &serde_json::to_vec(&store.list()?)?,
        ),
        ("POST", "/leases") => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Create {
                host: String,
                minutes: f64,
            }
            let p: Create = serde_json::from_slice(&bytes[offset..offset + len])?;
            let host = p.host.trim().parse::<IpAddr>()?;
            match store.create(host, p.minutes, hosts) {
                Ok(lease) => response(201, "application/json", &serde_json::to_vec(&lease)?),
                Err((status, msg)) => response(
                    status,
                    "application/json",
                    serde_json::json!({"error":msg}).to_string().as_bytes(),
                ),
            }
        }
        ("DELETE", path) if path.starts_with("/leases/") => match store.delete(&path[8..]) {
            Ok(()) => response(204, "application/json", b""),
            Err(e) => response(
                404,
                "application/json",
                serde_json::json!({"error":e.to_string()})
                    .to_string()
                    .as_bytes(),
            ),
        },
        _ => response(404, "application/json", br#"{"error":"not found"}"#),
    };
    Ok(Some(answer))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn http_framing_and_unknown_hosts_cannot_create_leases() -> Result<()> {
        let hasher = Hasher::new(&[0; 32]);
        let mut store = Store::new("", hasher)?;
        let hosts = HashSet::new();
        let body = br#"{"host":"192.168.1.2","minutes":10}"#;
        let header = format!(
            "POST /leases HTTP/1.1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        );
        assert!(request(header.as_bytes(), &mut store, &hosts)?.is_none());
        let mut message = header.into_bytes();
        message.extend_from_slice(body);
        let reply = request(&message, &mut store, &hosts)?.unwrap();
        assert!(reply.starts_with(b"HTTP/1.1 404 "));
        for malformed in [
            &b"POST /leases HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n"[..],
            &b"POST /leases HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"[..],
            &b"POST /leases HTTP/1.1\r\nContent-Length: 4097\r\n\r\n"[..],
        ] {
            assert!(request(malformed, &mut store, &hosts).is_err());
        }
        assert!(store.list()?.is_empty());
        Ok(())
    }
    #[test]
    fn leases_persist_privately_and_failed_deletes_restore_memory() -> Result<()> {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("aqos-lease-{}", crate::random_id()?));
        std::fs::create_dir(&dir)?;
        let path = dir.join("leases.json");
        let hasher = Hasher::new(&[0; 32]);
        let host = "192.168.1.2".parse::<IpAddr>()?;
        let hosts = HashSet::from([hasher.host_id(host)]);
        let mut store = Store::new(path.to_str().unwrap(), hasher.clone())?;
        let lease = store.create(host, 10.0, &hosts).unwrap();
        assert_eq!(
            std::fs::metadata(&path)?.permissions().mode() & 0o777,
            0o600
        );
        let mut reloaded = Store::new(path.to_str().unwrap(), hasher)?;
        assert_eq!(reloaded.list()?[0].id, lease.id);
        let moved = dir.with_extension("moved");
        std::fs::rename(&dir, &moved)?;
        std::fs::write(&dir, b"not a directory")?;
        assert!(reloaded.delete(&lease.id).is_err());
        assert_eq!(reloaded.list()?[0].id, lease.id);
        std::fs::remove_file(&dir)?;
        std::fs::rename(&moved, &dir)?;
        reloaded.delete(&lease.id)?;
        assert!(reloaded.list()?.is_empty());
        assert_eq!(std::fs::read_to_string(&path)?.trim(), "[]");
        std::fs::remove_dir_all(&dir)?;
        Ok(())
    }
}
