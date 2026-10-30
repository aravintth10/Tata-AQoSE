use aqos_runtime_rs::{
    Result,
    args::Args,
    capacity,
    client::Client,
    controller::{Config, Controller, Logs, Parts, append_file},
    enforce::Enforcer,
    extract::Table,
    intent::{Server, Store},
    live::{Live, Rule},
    now_ns, reclass,
    shield::{Config as ShieldConfig, Rate, Tracker},
};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
fn duration(s: &str) -> Result<Duration> {
    for (suffix, scale) in [("ms", 0.001), ("us", 0.000001), ("s", 1.0)] {
        if let Some(n) = s.strip_suffix(suffix) {
            let n = n.parse::<f64>()? * scale;
            if !n.is_finite() || n <= 0.0 || n > 60.0 {
                return Err("deadline must be between zero and 60 seconds".into());
            }
            return Ok(Duration::from_secs_f64(n));
        }
    }
    Err("deadline needs a unit: us, ms or s".into())
}
fn run() -> Result<()> {
    let args = Args::parse(
        &[
            "socket",
            "lan",
            "shield",
            "run-id",
            "t0-ns",
            "until-s",
            "metrics",
            "actions",
            "summary",
            "heartbeat",
            "group",
            "max-flows",
            "max-classify",
            "deadline",
            "max-pending",
            "window-every",
            "confirm",
            "reflectors",
            "intent",
            "leases",
            "envelopes",
            "outside-windows",
            "verdicts",
            "deaf",
        ],
        &["verify", "rollback"],
    )?;
    let socket = args.get("socket", "");
    if socket.is_empty() {
        return Err("-socket is required".into());
    }
    let max_flows: usize = args.number("max-flows", 65536)?;
    if max_flows == 0 || max_flows > 1 << 20 {
        return Err("max-flows must be between 1 and 1048576".into());
    }
    let until: f64 = args.number("until-s", 0.0)?;
    if !until.is_finite() || until < 0.0 {
        return Err("invalid run duration".into());
    }
    let t0 = args.number("t0-ns", 0_i64)?;
    let t0 = if t0 == 0 { now_ns() } else { t0 };
    let stop = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(signal, stop.clone())?;
    }
    let prefixes = args
        .get("lan", "10.77.0.0/24,fd77:0:0:1::/64")
        .split(',')
        .map(|s| s.trim().parse())
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let cfg = ShieldConfig::load(args.get("shield", "configs/shield.yaml"))?;
    let mut tracker = Tracker::new(cfg.clone())?;
    let mut enforcer = Enforcer::open("wan0", "ifb0")?;
    let mut base = [0; 2];
    for (i, dir) in ["up", "down"].into_iter().enumerate() {
        let rate = enforcer.rate(dir)?;
        base[i] = rate;
        tracker.state.rates.insert(
            dir.into(),
            Rate {
                kbit: rate,
                last_increase: None,
            },
        );
    }
    let client = Client::new(
        socket,
        duration(args.get("deadline", "50ms"))?,
        args.number("max-pending", 1024)?,
    )?;
    let health = client.health(Duration::from_secs(20))?;
    eprintln!(
        "aqosd-rs: ML service is {}, deciding by {}",
        health.model_version.as_deref().unwrap_or("unknown"),
        health.decision_rule.as_deref().unwrap_or("unknown")
    );
    let mut key = [0; 32];
    getrandom::fill(&mut key)?;
    let table = Table::new(prefixes, &key, max_flows);
    let leases = Store::new(args.get("leases", ""), table.hasher.clone())?;
    let server = if args.get("intent", "").is_empty() {
        None
    } else {
        Some(Server::bind(args.get("intent", ""))?)
    };
    let every = args.number("window-every", 5_u32)?;
    let watcher = if args.get("envelopes", "").is_empty() {
        None
    } else {
        let mut c = reclass::Config::default();
        let outside = args.number("outside-windows", 0_u32)?;
        if outside > 0 {
            c.windows = outside;
            c.interactive = outside;
        }
        Some(reclass::Watcher::new(
            c,
            reclass::load(args.get("envelopes", ""))?,
            u64::from_ne_bytes(key[..8].try_into()?),
        )?)
    };
    let (mut estimator, mut prober) = (None, None);
    if !args.get("reflectors", "").is_empty() {
        let addresses = args
            .get("reflectors", "")
            .split(',')
            .map(|s| {
                s.trim()
                    .parse::<IpAddr>()
                    .map(|ip| SocketAddr::new(ip, 7077))
            })
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let deaf = match args.get("deaf", "hold") {
            "hold" => true,
            "minimum" => false,
            _ => return Err("-deaf is hold or minimum".into()),
        };
        prober = Some(capacity::Prober::new(&addresses)?);
        estimator = Some(capacity::Estimator::new(capacity::Config {
            base,
            minimum: [
                cfg.min_rate_kbit["up"].max(base[0] / 10),
                cfg.min_rate_kbit["down"].max(base[1] / 10),
            ],
            reflectors: addresses.len(),
            deaf_hold: deaf,
            raise_every_ns: cfg
                .increase_interval_ms
                .saturating_add(500)
                .saturating_mul(1_000_000),
        })?);
    }
    let logs = Logs {
        metrics: append_file(args.get("metrics", ""))?,
        actions: append_file(args.get("actions", ""))?,
        verdicts: append_file(args.get("verdicts", ""))?,
    };
    let config = Config {
        max_classify: args.number("max-classify", 200)?,
        confirm: args.number("confirm", 2_u32)?.max(1),
        ask_every: every.max(1),
        heartbeat: args.get("heartbeat", "").into(),
        run_id: args.get("run-id", "").into(),
        t0,
        verify: args.boolean("verify", true)?,
        rollback: args.boolean("rollback", true)?,
    };
    let group = args.number("group", 5_u16)?;
    let mut live = Live::new(
        table,
        group,
        if watcher.is_some() {
            1
        } else {
            u64::from(every)
        },
    )?;
    live.set_poll_timeout(Duration::from_millis(5))?;
    let rule = Rule::install(group)?;
    enforcer.install_marking()?;
    let mut ctl = Controller::new(
        config,
        Parts {
            client,
            shield: tracker,
            enforcer,
            leases,
            server,
            reclass: watcher,
            estimator,
            prober,
        },
        logs,
    );
    let running = (|| -> Result<()> {
        let mut events = Vec::new();
        let mut dump = None;
        while !stop.load(Ordering::Relaxed)
            && (until == 0.0 || (now_ns() - t0) as f64 / 1e9 < until)
        {
            live.poll(&mut events, &mut dump)?;
            for event in events.drain(..) {
                ctl.event(event)?;
            }
            ctl.tick()?;
        }
        Ok(())
    })();
    let cleanup = ctl.parts.enforcer.remove_marking();
    let capture_cleanup = rule.remove();
    let summary = ctl.summary(&health, &live.stats);
    eprintln!("aqosd-rs: {}", summary);
    if !args.get("summary", "").is_empty() {
        std::fs::write(
            args.get("summary", ""),
            serde_json::to_vec_pretty(&summary)?,
        )?;
    }
    if let Err(e) = cleanup {
        return Err(format!("marking cleanup failed: {e}; runtime result: {running:?}").into());
    }
    capture_cleanup?;
    running
}
fn main() {
    if let Err(e) = run() {
        eprintln!("aqosd-rs: {e}");
        std::process::exit(1);
    }
}
