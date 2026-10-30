use aqos_runtime_rs::{Result, args::Args, cake, now_ns};
use std::{
    io::Write,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
fn run() -> Result<()> {
    let a = Args::parse(&["run-id", "t0-ns", "until-s", "out", "up", "down"], &[])?;
    let t0 = a.number("t0-ns", 0_i64)?;
    let until = a.number("until-s", 0.0_f64)?;
    let id = a.get("run-id", "");
    if t0 == 0 || !until.is_finite() || until <= 0.0 || id.is_empty() || a.get("out", "").is_empty()
    {
        return Err("run-id, t0-ns, until-s and out are required".into());
    }
    let mut out = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(a.get("out", ""))?;
    let stop = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, stop.clone())?;
    }
    let mut readers = [cake::Reader::default(), cake::Reader::default()];
    let devices = [a.get("up", "wan0"), a.get("down", "ifb0")];
    for second in 1..=until.floor() as u64 {
        if (now_ns() - t0) as f64 / 1e9 > second as f64 {
            continue;
        }
        while (now_ns() - t0) as f64 / 1e9 < second as f64 {
            if stop.load(Ordering::Relaxed) {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }
        let mut batch = Vec::new();
        for i in 0..2 {
            if let Ok(stats) = cake::read(devices[i]) {
                for sample in readers[i].samples(stats, second as f64, id, ["up", "down"][i]) {
                    serde_json::to_writer(&mut batch, &sample)?;
                    batch.push(b'\n');
                }
            }
        }
        if out.write(&batch)? != batch.len() {
            return Err("short metrics append".into());
        }
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("aqos-cake-reader-rs: {e}");
        std::process::exit(1);
    }
}
