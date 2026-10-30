//! Independent fail-safe process. No controller, classifier or policy dependency.
mod command;
use command::run;
use serde::Deserialize;
use serde_json::Value;
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime},
};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
fn timeout() -> u64 {
    2000
}
fn wan() -> String {
    "wan0".into()
}
fn ifb() -> String {
    "ifb0".into()
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    heartbeat: String,
    #[serde(default = "timeout")]
    timeout_ms: u64,
    #[serde(default = "wan")]
    wan_dev: String,
    #[serde(default = "ifb")]
    ifb_dev: String,
    safe_up_kbit: i64,
    safe_down_kbit: i64,
}
fn attr(kind: u16, b: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&((b.len() + 4) as u16).to_ne_bytes());
    out.extend_from_slice(&kind.to_ne_bytes());
    out.extend_from_slice(b);
    out.resize((out.len() + 3) & !3, 0);
    out
}
fn clear_mask(dev: &str) -> Result<()> {
    use rustix::net::{
        self, AddressFamily, RecvFlags, SendFlags, SocketFlags, SocketType,
        netlink::SocketAddrNetlink,
    };
    let links: Vec<Value> =
        serde_json::from_slice(&run("ip", &["-j", "link", "show", "dev", dev], None)?)?;
    let index = i32::try_from(
        links
            .first()
            .and_then(|v| v["ifindex"].as_u64())
            .ok_or("missing interface index")?,
    )?;
    let fd = net::socket_with(
        AddressFamily::NETLINK,
        SocketType::RAW,
        SocketFlags::CLOEXEC,
        None,
    )?;
    net::bind(&fd, &SocketAddrNetlink::new(0, 0))?;
    net::sockopt::set_socket_timeout(
        &fd,
        net::sockopt::Timeout::Recv,
        Some(Duration::from_millis(100)),
    )?;
    let mut msg = vec![0; 36];
    msg[4..6].copy_from_slice(&36u16.to_ne_bytes());
    msg[6..8].copy_from_slice(&5u16.to_ne_bytes());
    msg[8..12].copy_from_slice(&1u32.to_ne_bytes());
    msg[20..24].copy_from_slice(&index.to_ne_bytes());
    msg[28..32].copy_from_slice(&u32::MAX.to_ne_bytes());
    msg.extend(attr(1, b"cake\0"));
    msg.extend(attr(2, &attr(18, &0u32.to_ne_bytes())));
    let len = msg.len() as u32;
    msg[..4].copy_from_slice(&len.to_ne_bytes());
    net::sendto(&fd, &msg, SendFlags::empty(), &SocketAddrNetlink::new(0, 0))?;
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut buf = [0; 8192];
    loop {
        if Instant::now() >= deadline {
            return Err("CAKE netlink timeout".into());
        }
        let (_, n, from) = match net::recvfrom(&fd, &mut buf[..], RecvFlags::TRUNC) {
            Ok(r) => r,
            Err(rustix::io::Errno::AGAIN | rustix::io::Errno::INTR) => continue,
            Err(e) => return Err(e.into()),
        };
        if n > buf.len() {
            return Err("truncated CAKE acknowledgement".into());
        }
        let from = SocketAddrNetlink::try_from(from.ok_or("missing sender")?)?;
        if from.pid() != 0 {
            return Err("non-kernel acknowledgement".into());
        }
        let mut at = 0;
        while at + 16 <= n {
            let size = u32::from_ne_bytes(buf[at..at + 4].try_into()?) as usize;
            if size < 16 || size > n - at {
                return Err("invalid acknowledgement length".into());
            }
            let seq = u32::from_ne_bytes(buf[at + 8..at + 12].try_into()?);
            let kind = u16::from_ne_bytes(buf[at + 4..at + 6].try_into()?);
            if seq == 1 && kind == 2 {
                if size < 20 {
                    return Err("short acknowledgement".into());
                }
                let code = i32::from_ne_bytes(buf[at + 16..at + 20].try_into()?);
                return if code == 0 {
                    Ok(())
                } else {
                    Err(std::io::Error::from_raw_os_error(
                        code.checked_neg().ok_or("bad kernel errno")?,
                    )
                    .into())
                };
            }
            at += (size + 3) & !3;
        }
    }
}
fn table_present() -> Result<bool> {
    let v: Value = serde_json::from_slice(&run("nft", &["-j", "list", "tables"], None)?)?;
    Ok(v["nftables"]
        .as_array()
        .ok_or("missing nft table list")?
        .iter()
        .any(|t| t["table"]["family"] == "inet" && t["table"]["name"] == "aqos_mark"))
}
fn safe(cfg: &Config) -> Result<()> {
    let mut errors = Vec::new();
    let mut step = |r: Result<()>| {
        if let Err(e) = r {
            errors.push(e.to_string());
        }
    };
    match table_present() {
        Ok(true) => step(run("nft", &["delete", "table", "inet", "aqos_mark"], None).map(|_| ())),
        Ok(false) => {}
        Err(e) => step(Err(e)),
    }
    for (dev, rate, flow, ingress) in [
        (&cfg.wan_dev, cfg.safe_up_kbit, "dual-srchost", false),
        (&cfg.ifb_dev, cfg.safe_down_kbit, "dual-dsthost", true),
    ] {
        let rate = format!("{rate}kbit");
        let mut args = vec![
            "qdisc",
            "replace",
            "dev",
            dev,
            "root",
            "cake",
            "bandwidth",
            &rate,
            "diffserv4",
            flow,
            "nat",
        ];
        if ingress {
            args.push("ingress");
        }
        step(run("tc", &args, None).map(|_| ()));
        step(clear_mask(dev));
    }
    let filter = (|| -> Result<()> {
        let all: Vec<Value> = serde_json::from_slice(&run(
            "tc",
            &[
                "-j",
                "filter",
                "show",
                "dev",
                &cfg.wan_dev,
                "parent",
                "ffff:",
            ],
            None,
        )?)?;
        if all.iter().any(|f| f["pref"] == 9) {
            run(
                "tc",
                &[
                    "filter",
                    "del",
                    "dev",
                    &cfg.wan_dev,
                    "parent",
                    "ffff:",
                    "prio",
                    "9",
                ],
                None,
            )?;
        }
        let baseline = all.iter().find(|f| f["pref"] == 10);
        if baseline.is_none() || baseline.is_some_and(|v| v.to_string().contains("ctinfo")) {
            if baseline.is_some() {
                run(
                    "tc",
                    &[
                        "filter",
                        "del",
                        "dev",
                        &cfg.wan_dev,
                        "parent",
                        "ffff:",
                        "prio",
                        "10",
                    ],
                    None,
                )?;
            }
            run(
                "tc",
                &[
                    "filter",
                    "add",
                    "dev",
                    &cfg.wan_dev,
                    "parent",
                    "ffff:",
                    "protocol",
                    "all",
                    "prio",
                    "10",
                    "matchall",
                    "action",
                    "mirred",
                    "egress",
                    "redirect",
                    "dev",
                    &cfg.ifb_dev,
                ],
                None,
            )?;
        }
        Ok(())
    })();
    step(filter);
    let verified = (|| -> Result<()> {
        if table_present()? {
            return Err("AQoSE mark rule remains".into());
        }
        let f = run(
            "tc",
            &["filter", "show", "dev", &cfg.wan_dev, "parent", "ffff:"],
            None,
        )?;
        let f = String::from_utf8_lossy(&f);
        if f.contains("ctinfo") || !f.contains("mirred") || !f.contains(&cfg.ifb_dev) {
            return Err("plain ingress redirect not verified".into());
        }
        for (dev, want) in [
            (&cfg.wan_dev, cfg.safe_up_kbit),
            (&cfg.ifb_dev, cfg.safe_down_kbit),
        ] {
            let q: Vec<Value> =
                serde_json::from_slice(&run("tc", &["-j", "qdisc", "show", "dev", dev], None)?)?;
            let cake = q
                .iter()
                .find(|q| q["kind"] == "cake" && q["root"] == true)
                .ok_or("root CAKE missing")?;
            let rate = cake["options"]["bandwidth"]
                .as_f64()
                .ok_or("bandwidth missing")?
                * 8.0
                / 1000.0;
            let mask = &cake["options"]["fwmark"];
            let zero = mask.is_null()
                || mask.as_u64() == Some(0)
                || matches!(mask.as_str(), Some("0" | "0x0"));
            if rate.round() as i64 != want || !zero {
                return Err(format!("safe state read-back failed on {dev}").into());
            }
        }
        Ok(())
    })();
    step(verified);
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; ").into())
    }
}
fn main() -> Result<()> {
    let a: Vec<_> = std::env::args().skip(1).collect();
    if a.len() != 2 || !matches!(a[0].as_str(), "-config" | "--config") {
        return Err("usage: aqos-watchdog-rs -config file.json".into());
    }
    let cfg: Config = serde_json::from_slice(&std::fs::read(&a[1])?)?;
    if cfg.heartbeat.is_empty()
        || cfg.safe_up_kbit < 1
        || cfg.safe_down_kbit < 1
        || !(100..=60000).contains(&cfg.timeout_ms)
        || [&cfg.wan_dev, &cfg.ifb_dev].iter().any(|d| {
            d.is_empty()
                || d.len() >= 16
                || !d
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
        })
    {
        return Err("invalid watchdog configuration".into());
    }
    let stop = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, stop.clone())?;
    }
    let timeout = Duration::from_millis(cfg.timeout_ms);
    let mut last_progress = Instant::now();
    let mut last_stamp = None;
    let mut recovered = false;
    let marker = format!("{}.safe", cfg.heartbeat);
    let _ = std::fs::remove_file(&marker);
    while !stop.load(Ordering::Relaxed) {
        if let Ok(stamp) = std::fs::metadata(&cfg.heartbeat).and_then(|s| s.modified())
            && last_stamp != Some(stamp)
        {
            let age = SystemTime::now().duration_since(stamp).unwrap_or(timeout);
            last_progress = Instant::now()
                .checked_sub(age.min(timeout))
                .unwrap_or_else(Instant::now);
            last_stamp = Some(stamp);
            if age < timeout {
                recovered = false;
                let _ = std::fs::remove_file(&marker);
            }
        }
        if last_progress.elapsed() >= timeout && !recovered {
            let start = Instant::now();
            match safe(&cfg) {
                Ok(()) => {
                    let timestamp = SystemTime::now()
                        .duration_since(SystemTime::UNIX_EPOCH)?
                        .as_nanos();
                    if let Err(e) = std::fs::write(&marker, format!("{timestamp}\n")) {
                        eprintln!("aqos-watchdog-rs: cannot publish safe marker; retrying: {e}");
                    } else {
                        recovered = true;
                    }
                    eprintln!(
                        "aqos-watchdog-rs: verified safe state applied in {:?}",
                        start.elapsed()
                    );
                }
                Err(e) => {
                    eprintln!("aqos-watchdog-rs: recovery incomplete; retrying: {e}");
                    let _ = std::fs::remove_file(&marker);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Ok(())
}
