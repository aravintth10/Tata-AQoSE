//! Bounded nonblocking classifier connection; late or unrelated replies cannot act.
use crate::{Class, FeatureRecord, Result, Tin, contracts::Proposal};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, VecDeque},
    io::{BufRead, BufReader, Read, Write},
    os::unix::net::UnixStream,
    time::{Duration, Instant},
};
// AF_UNIX connect can block on a full listen backlog unless NONBLOCK is
// set before connecting; controller deadlines must include that case.
fn connect(path: &str) -> std::io::Result<UnixStream> {
    use rustix::net::{self, AddressFamily, SocketAddrUnix, SocketFlags, SocketType};
    let fd = net::socket_with(
        AddressFamily::UNIX,
        SocketType::STREAM,
        SocketFlags::CLOEXEC | SocketFlags::NONBLOCK,
        None,
    )?;
    net::connect(&fd, &SocketAddrUnix::new(path)?)?;
    Ok(UnixStream::from(fd))
}
#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Classification {
    pub schema_version: u32,
    pub flow_id: String,
    pub model_version: String,
    pub probs: HashMap<Class, f64>,
    pub confidence: f64,
    pub inference_us: f64,
}
impl Classification {
    pub fn valid(&self, id: &str) -> bool {
        self.schema_version == 1
            && self.flow_id == id
            && !self.model_version.is_empty()
            && self.probs.len() == 7
            && self
                .probs
                .values()
                .all(|p| p.is_finite() && (0.0..=1.0).contains(p))
            && (self.probs.values().sum::<f64>() - 1.0).abs() <= 1e-6
            && (self.confidence - self.probs.values().copied().fold(0.0, f64::max)).abs() <= 1e-6
            && self.inference_us.is_finite()
            && self.inference_us >= 0.0
    }
}
#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Response {
    pub id: Option<u64>,
    pub ok: bool,
    pub error: Option<String>,
    pub classification: Option<Classification>,
    pub proposal: Option<Proposal>,
    pub model_version: Option<String>,
    pub decision_rule: Option<String>,
    pub ready: Option<bool>,
}
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Failure {
    Down,
    Busy,
    Timeout,
    Refused,
}
pub struct Answer {
    pub flow_id: String,
    pub result: std::result::Result<Response, Failure>,
}
struct Pending {
    flow_id: String,
    sent: Instant,
}
struct Frame {
    id: u64,
    bytes: Vec<u8>,
    written: usize,
}
pub struct Client {
    path: String,
    deadline: Duration,
    max: usize,
    socket: Option<UnixStream>,
    dial_at: Option<Instant>,
    next: u64,
    pending: HashMap<u64, Pending>,
    by_flow: HashMap<String, u64>,
    write: VecDeque<Frame>,
    read: Vec<u8>,
}
impl Client {
    pub fn new(path: &str, deadline: Duration, max: usize) -> Result<Self> {
        if deadline.is_zero() || max == 0 || max > 65536 {
            return Err("invalid classifier deadline or pending limit".into());
        }
        Ok(Self {
            path: path.into(),
            deadline,
            max,
            socket: None,
            dial_at: None,
            next: 0,
            pending: HashMap::new(),
            by_flow: HashMap::new(),
            write: VecDeque::new(),
            read: Vec::with_capacity(65536),
        })
    }
    pub fn health(&self, timeout: Duration) -> Result<Response> {
        let until = Instant::now() + timeout;
        let mut last = "classifier unavailable".to_owned();
        while Instant::now() < until {
            let attempt = (|| -> Result<Response> {
                let mut socket = connect(&self.path)?;
                socket.set_nonblocking(false)?;
                let limit = until
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_secs(1))
                    .max(Duration::from_millis(1));
                socket.set_read_timeout(Some(limit))?;
                socket.set_write_timeout(Some(limit))?;
                socket.write_all(b"{\"op\":\"health\",\"id\":0}\n")?;
                let mut line = Vec::new();
                BufReader::new(socket)
                    .take((1 << 20) + 1)
                    .read_until(b'\n', &mut line)?;
                if line.len() > 1 << 20 {
                    return Err("oversized classifier health response".into());
                }
                let r: Response = serde_json::from_slice(&line)?;
                if r.id != Some(0)
                    || !r.ok
                    || r.ready != Some(true)
                    || r.model_version.as_ref().is_none_or(String::is_empty)
                    || !matches!(
                        r.decision_rule.as_deref(),
                        Some("rules" | "top_label" | "expected_damage")
                    )
                {
                    return Err("invalid classifier health response".into());
                }
                Ok(r)
            })();
            match attempt {
                Ok(r) => return Ok(r),
                Err(e) => last = e.to_string(),
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        Err(last.into())
    }
    pub fn classify(
        &mut self,
        record: &FeatureRecord,
        previous: Option<Tin>,
        leased: &[Class],
    ) -> std::result::Result<(), Failure> {
        if self.pending.len() >= self.max
            || self.write.len() >= self.max
            || self.by_flow.contains_key(&record.flow_id)
        {
            return Err(Failure::Busy);
        }
        if self.socket.is_none() {
            if self
                .dial_at
                .is_some_and(|at| at.elapsed() < Duration::from_secs(1))
            {
                return Err(Failure::Down);
            }
            self.dial_at = Some(Instant::now());
            let socket = connect(&self.path).map_err(|_| Failure::Down)?;
            socket.set_nonblocking(true).map_err(|_| Failure::Down)?;
            self.socket = Some(socket);
        }
        #[derive(Serialize)]
        struct Request<'a> {
            op: &'static str,
            id: u64,
            record: &'a FeatureRecord,
            previous_tin: Option<Tin>,
            leased_classes: &'a [Class],
        }
        self.next = self.next.checked_add(1).ok_or(Failure::Down)?;
        let id = self.next;
        let mut bytes = serde_json::to_vec(&Request {
            op: "classify",
            id,
            record,
            previous_tin: previous,
            leased_classes: leased,
        })
        .map_err(|_| Failure::Refused)?;
        bytes.push(b'\n');
        self.write.push_back(Frame {
            id,
            bytes,
            written: 0,
        });
        self.pending.insert(
            id,
            Pending {
                flow_id: record.flow_id.clone(),
                sent: Instant::now(),
            },
        );
        self.by_flow.insert(record.flow_id.clone(), id);
        Ok(())
    }
    pub fn cancel(&mut self, flow: &str) {
        if let Some(id) = self.by_flow.remove(flow) {
            self.pending.remove(&id);
        }
    }
    fn drop_connection(&mut self, answers: &mut Vec<Answer>) {
        self.socket = None;
        self.read.clear();
        self.write.clear();
        self.by_flow.clear();
        answers.extend(self.pending.drain().map(|(_, p)| Answer {
            flow_id: p.flow_id,
            result: Err(Failure::Down),
        }));
    }
    pub fn poll(&mut self, answers: &mut Vec<Answer>) {
        let Some(socket) = &mut self.socket else {
            return;
        };
        let mut broken = false;
        while let Some(frame) = self.write.front_mut() {
            if !self.pending.contains_key(&frame.id) && frame.written == 0 {
                self.write.pop_front();
                continue;
            }
            match socket.write(&frame.bytes[frame.written..]) {
                Ok(0) => {
                    broken = true;
                    break;
                }
                Ok(n) => {
                    frame.written += n;
                    if frame.written == frame.bytes.len() {
                        self.write.pop_front();
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(_) => {
                    broken = true;
                    break;
                }
            }
        }
        let mut buf = [0; 16384];
        for _ in 0..16 {
            match socket.read(&mut buf) {
                Ok(0) => {
                    broken = true;
                    break;
                }
                Ok(n) => {
                    self.read.extend_from_slice(&buf[..n]);
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(_) => {
                    broken = true;
                    break;
                }
            }
        }
        let mut consumed = 0;
        while let Some(n) = self.read[consumed..].iter().position(|b| *b == b'\n') {
            let end = consumed + n;
            if n > 1 << 20 {
                broken = true;
                break;
            }
            if let Ok(r) = serde_json::from_slice::<Response>(&self.read[consumed..end])
                && let Some(p) = r.id.and_then(|id| self.pending.remove(&id))
            {
                self.by_flow.remove(&p.flow_id);
                let result = if p.sent.elapsed() > self.deadline {
                    Err(Failure::Timeout)
                } else {
                    Ok(r)
                };
                answers.push(Answer {
                    flow_id: p.flow_id,
                    result,
                });
            }
            consumed = end + 1;
        }
        if consumed > 0 {
            self.read.drain(..consumed);
        }
        if self.read.len() > 1 << 20 {
            broken = true;
        }
        let expired: Vec<_> = self
            .pending
            .iter()
            .filter(|(_, p)| p.sent.elapsed() > self.deadline)
            .map(|(&id, _)| id)
            .collect();
        for id in expired {
            if let Some(p) = self.pending.remove(&id) {
                self.by_flow.remove(&p.flow_id);
                answers.push(Answer {
                    flow_id: p.flow_id,
                    result: Err(Failure::Timeout),
                });
                if self
                    .write
                    .front()
                    .is_some_and(|f| f.id == id && f.written > 0)
                {
                    broken = true;
                }
            }
        }
        if broken {
            self.drop_connection(answers);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn connected(max: usize) -> (Client, UnixStream, FeatureRecord) {
        let (a, b) = UnixStream::pair().unwrap();
        a.set_nonblocking(true).unwrap();
        let mut client = Client::new("unused", Duration::from_secs(1), max).unwrap();
        client.socket = Some(a);
        let record = serde_json::from_str(include_str!(
            "../../../contracts/examples/feature_record.initial.json"
        ))
        .unwrap();
        (client, b, record)
    }
    #[test]
    fn cancelled_or_unknown_replies_cannot_complete_new_request() {
        let (mut client, mut peer, record) = connected(2);
        let mut answers = Vec::new();
        client.classify(&record, None, &[]).unwrap();
        client.poll(&mut answers);
        client.cancel(&record.flow_id);
        client.classify(&record, None, &[]).unwrap();
        client.poll(&mut answers);
        peer.write_all(b"{\"id\":1,\"ok\":true}\n{\"id\":999,\"ok\":true}\n{\"id\":2,")
            .unwrap();
        client.poll(&mut answers);
        assert!(answers.is_empty());
        assert_eq!(client.pending.len(), 1);
        peer.write_all(b"\"ok\":true}\n").unwrap();
        client.poll(&mut answers);
        assert_eq!(answers.len(), 1);
        assert_eq!(answers[0].flow_id, record.flow_id);
        assert_eq!(answers[0].result.as_ref().unwrap().id, Some(2));
        assert!(client.pending.is_empty());
    }
    #[test]
    fn queue_limits_deadline_and_disconnect_fail_without_stale_results() {
        let (mut client, mut peer, record) = connected(1);
        let mut answers = Vec::new();
        client.classify(&record, None, &[]).unwrap();
        assert_eq!(client.classify(&record, None, &[]), Err(Failure::Busy));
        client.pending.get_mut(&1).unwrap().sent = Instant::now() - Duration::from_secs(2);
        peer.write_all(b"{\"id\":1,\"ok\":true}\n").unwrap();
        client.poll(&mut answers);
        assert!(matches!(answers[0].result, Err(Failure::Timeout)));
        answers.clear();
        client.classify(&record, None, &[]).unwrap();
        client.poll(&mut answers);
        peer.shutdown(std::net::Shutdown::Both).unwrap();
        drop(peer);
        client.poll(&mut answers);
        assert_eq!(answers.len(), 1);
        assert!(matches!(answers[0].result, Err(Failure::Down)));
        assert!(client.socket.is_none());
    }
    #[test]
    fn malformed_response_cannot_bypass_deadline() {
        let (mut client, mut peer, record) = connected(1);
        let mut answers = Vec::new();
        client.classify(&record, None, &[]).unwrap();
        peer.write_all(b"{\"id\":1,\"ok\":true,\"unexpected\":1}\nnot json\n")
            .unwrap();
        client.poll(&mut answers);
        assert!(answers.is_empty());
        client.pending.get_mut(&1).unwrap().sent = Instant::now() - Duration::from_secs(2);
        client.poll(&mut answers);
        assert_eq!(answers.len(), 1);
        assert!(matches!(answers[0].result, Err(Failure::Timeout)));
    }
}
