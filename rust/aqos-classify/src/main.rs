use aqos_classify_rs::{CLASSES, Class, Engine, FeatureRecord, Kind, Mode, Model, Result, Tin};
use serde::{Deserialize, Serialize, Serializer, ser::SerializeMap};
use serde_json::json;
use signal_hook::{
    consts::{SIGINT, SIGTERM},
    iterator::Signals,
};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

const MAX_REQUEST: u64 = 1 << 20;
const MAX_CLIENTS: usize = 64;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    op: String,
    id: u64,
    record: Option<FeatureRecord>,
    previous_tin: Option<Tin>,
    leased_classes: Option<Vec<Class>>,
}
struct Service {
    model: Model,
    engine: Engine,
}
// Borrow response strings and serialize the fixed probability array directly.
// No per-request hash tables or intermediate serde_json::Value tree are needed.
struct ClassProbabilities<'a>(&'a [f64; 7]);
impl Serialize for ClassProbabilities<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(CLASSES.len()))?;
        for (class, value) in CLASSES.iter().zip(self.0) {
            map.serialize_entry(class, value)?;
        }
        map.end()
    }
}
#[derive(Serialize)]
struct Classification<'a> {
    schema_version: u32,
    flow_id: &'a str,
    model_version: &'a str,
    probs: ClassProbabilities<'a>,
    confidence: f64,
    inference_us: f64,
}
#[derive(Serialize)]
struct FlowLabel<'a> {
    flow_id: &'a str,
    host_id: &'a str,
    previous_tin: Option<Tin>,
    tin: Tin,
    dscp: &'a str,
    top_class: Class,
    expected_damage: f64,
    decision_rule: Mode,
}
#[derive(Serialize)]
struct Proposal<'a> {
    schema_version: u32,
    proposal_id: &'a str,
    ts_ns: i64,
    source: &'static str,
    kind: &'static str,
    reason: &'a str,
    flow_label: FlowLabel<'a>,
}
#[derive(Serialize)]
struct Answer<'a> {
    id: u64,
    ok: bool,
    classification: Classification<'a>,
    proposal: Proposal<'a>,
}
impl Service {
    fn answer(&self, line: &[u8], output: &mut Vec<u8>, reason: &mut String) -> Result<()> {
        let req: Request = match serde_json::from_slice(line) {
            Ok(r) => r,
            Err(e) => {
                serde_json::to_writer(
                    output,
                    &json!({"id": null, "ok": false, "error": format!("invalid request: {e}")}),
                )?;
                return Ok(());
            }
        };
        let fail = |output: &mut Vec<u8>, error: String| -> Result<()> {
            serde_json::to_writer(output, &json!({"id": req.id, "ok": false, "error": error}))?;
            Ok(())
        };
        if req.op == "health" {
            serde_json::to_writer(
                output,
                &json!({"id": req.id, "ok": true, "ready": true, "model_version": self.model.version(), "decision_rule": self.engine.mode()}),
            )?;
            return Ok(());
        }
        if req.op != "classify" {
            return fail(output, format!("unknown operation: {}", req.op));
        }
        let Some(rec) = req.record else {
            return fail(output, "record is required".into());
        };
        let valid = match rec.validate() {
            Ok(r) => r,
            Err(e) => return fail(output, e.to_string()),
        };
        let started = Instant::now();
        let probs = self.model.probabilities(&valid.vector());
        let inference_us = started.elapsed().as_secs_f64() * 1e6;
        let leases = req.leased_classes.unwrap_or_default();
        let ch = self.engine.choose(&probs, &leases, Some(&valid));
        let mut id_bytes = [0_u8; 8];
        if let Err(e) = getrandom::fill(&mut id_bytes) {
            return fail(output, format!("proposal id: {e}"));
        }
        let mut hex = [0_u8; 16];
        const DIGITS: &[u8; 16] = b"0123456789abcdef";
        for (byte, pair) in id_bytes.iter().zip(hex.as_chunks_mut::<2>().0) {
            pair[0] = DIGITS[(byte >> 4) as usize];
            pair[1] = DIGITS[(byte & 15) as usize];
        }
        let proposal_id = std::str::from_utf8(&hex)?;
        let now = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
        let ts_ns = i128::from(now.tv_sec) * 1_000_000_000 + i128::from(now.tv_nsec);
        let Ok(ts_ns) = i64::try_from(ts_ns) else {
            return fail(output, "monotonic clock overflow".into());
        };
        self.engine
            .reason_into(reason, &probs, &ch, leases.iter().any(|c| c.interactive()));
        serde_json::to_writer(
            output,
            &Answer {
                id: req.id,
                ok: true,
                classification: Classification {
                    schema_version: 1,
                    flow_id: &rec.flow_id,
                    model_version: self.model.version(),
                    probs: ClassProbabilities(probs.values()),
                    confidence: probs.values().iter().copied().fold(0.0, f64::max),
                    inference_us,
                },
                proposal: Proposal {
                    schema_version: 1,
                    proposal_id,
                    ts_ns,
                    source: if rec.kind == Kind::Window {
                        "reclassifier"
                    } else {
                        "decision_engine"
                    },
                    kind: "flow_label",
                    reason,
                    flow_label: FlowLabel {
                        flow_id: &rec.flow_id,
                        host_id: &rec.host_id,
                        previous_tin: req.previous_tin,
                        tin: ch.tin,
                        dscp: self.engine.dscp(ch.top, ch.tin),
                        top_class: ch.top,
                        expected_damage: (ch.expected[ch.tin as usize] * 1e6).round() / 1e6,
                        decision_rule: self.engine.mode(),
                    },
                },
            },
        )?;
        Ok(())
    }
    fn serve(&self, mut stream: UnixStream) -> Result<()> {
        stream.set_write_timeout(Some(Duration::from_secs(5)))?;
        let mut reader = BufReader::new(stream.try_clone()?);
        let mut line = Vec::new();
        let mut response = Vec::with_capacity(2048);
        let mut reason = String::with_capacity(256);
        loop {
            line.clear();
            let n = reader
                .by_ref()
                .take(MAX_REQUEST + 1)
                .read_until(b'\n', &mut line)?;
            if n == 0 {
                return Ok(());
            }
            if n as u64 > MAX_REQUEST {
                return Err("request exceeds 1 MiB".into());
            }
            if line.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            // Match the Go service's one-buffer write. Serialising directly to
            // UnixStream makes a syscall for every small JSON fragment.
            response.clear();
            self.answer(&line, &mut response, &mut reason)?;
            response.push(b'\n');
            stream.write_all(&response)?;
        }
    }
}
struct SocketGuard(PathBuf);
impl Drop for SocketGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
struct ClientGuard(Arc<AtomicUsize>);
impl Drop for ClientGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

fn run() -> Result<()> {
    let mut socket = None;
    let mut model = None;
    let mut damage = String::from("configs/damage_table.yaml");
    let mut mode = Mode::ExpectedDamage;
    let mut bulk_guard = true;
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        match flag.trim_start_matches('-') {
            "socket" => socket = Some(args.next().ok_or("missing socket")?),
            "model" => model = Some(args.next().ok_or("missing model")?),
            "damage" => damage = args.next().ok_or("missing damage file")?,
            "mode" => {
                mode = match args.next().as_deref() {
                    Some("expected_damage") => Mode::ExpectedDamage,
                    Some("top_label") => Mode::TopLabel,
                    _ => return Err("mode must be expected_damage or top_label".into()),
                }
            }
            "no-bulk-guard" => bulk_guard = false,
            "help" | "h" => {
                println!(
                    "aqos-classify-rs -socket PATH -model TREES.json [-damage FILE] [-mode expected_damage|top_label] [-no-bulk-guard]"
                );
                return Ok(());
            }
            _ => return Err(format!("unknown argument: {flag}").into()),
        }
    }
    let socket = socket.ok_or("-socket is required")?;
    let service = Arc::new(Service {
        model: Model::load(model.ok_or("-model is required")?)?,
        engine: Engine::load(damage, mode, bulk_guard)?,
    });
    // Never unlink an existing socket or file: it may belong to a live service.
    let listener = UnixListener::bind(&socket)?;
    let _cleanup = SocketGuard(PathBuf::from(&socket));
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
    let stop = Arc::new(AtomicBool::new(false));
    let mut signals = Signals::new([SIGINT, SIGTERM])?;
    let flag = Arc::clone(&stop);
    let wake = socket.clone();
    thread::spawn(move || {
        if signals.forever().next().is_some() {
            flag.store(true, Ordering::Release);
            let _ = UnixStream::connect(Path::new(&wake));
        }
    });
    let active = Arc::new(AtomicUsize::new(0));
    eprintln!(
        "aqos-classify-rs: {}, deciding by {}, on {}",
        service.model.version(),
        mode.name(),
        socket
    );
    for stream in listener.incoming() {
        if stop.load(Ordering::Acquire) {
            break;
        }
        let stream = stream?;
        if active.load(Ordering::Relaxed) >= MAX_CLIENTS {
            drop(stream);
            continue;
        }
        active.fetch_add(1, Ordering::Relaxed);
        let guard = ClientGuard(Arc::clone(&active));
        let service = Arc::clone(&service);
        thread::Builder::new()
            .name("classify-client".into())
            .spawn(move || {
                let _guard = guard;
                if let Err(e) = service.serve(stream) {
                    eprintln!("aqos-classify-rs client: {e}");
                }
            })?;
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("aqos-classify-rs: {e}");
        std::process::exit(1);
    }
}
