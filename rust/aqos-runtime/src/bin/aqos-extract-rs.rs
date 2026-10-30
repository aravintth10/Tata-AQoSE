use aqos_runtime_rs::{
    Result,
    args::Args,
    extract::{PRIVATE_NETS, Table},
    pcap,
};
use std::{
    fs::File,
    io::{BufWriter, Write},
};
fn run() -> Result<()> {
    let args = Args::parse(
        &[
            "pcap",
            "out",
            "keys",
            "lan",
            "key",
            "max-flows",
            "window-every",
            "group",
            "dump",
            "until-s",
            "flow-table",
        ],
        &["live", "install-rule"],
    )?;
    if !args.get("flow-table", "").is_empty() {
        if !args.get("pcap", "").is_empty() || args.boolean("live", false)? {
            return Err("choose only one input mode".into());
        }
        let mut out = BufWriter::new(File::create(args.get("out", "features.jsonl"))?);
        let stats = aqos_runtime_rs::flowtable::read(
            args.get("flow-table", ""),
            &mut out,
            args.number("window-every", 0_u64)?,
        )?;
        eprintln!(
            "{}: {} flows, {} initial records, {} window records; left out: {} began before the capture, {} with merged first packets, {} too short; {} kept without window records (merged packets)",
            args.get("flow-table", ""),
            stats.rows,
            stats.initial,
            stats.window,
            stats.began_before_capture,
            stats.merged_first_packets,
            stats.too_short,
            stats.windows_withheld
        );
        return Ok(());
    }
    let pcap_path = args.get("pcap", "");
    let live = args.boolean("live", false)?;
    if pcap_path.is_empty() && !live {
        return Err("-pcap or -live is required".into());
    }
    if !pcap_path.is_empty() && live {
        return Err("choose only one input mode".into());
    }
    let lan = args.get("lan", "");
    let prefixes = if lan.is_empty() {
        PRIVATE_NETS
            .iter()
            .map(|s| s.parse())
            .collect::<std::result::Result<Vec<_>, _>>()?
    } else {
        lan.split(',')
            .map(|s| s.trim().parse())
            .collect::<std::result::Result<Vec<_>, _>>()?
    };
    let key_hex = args.get("key", "");
    let key = if key_hex.is_empty() {
        let mut k = vec![0; 32];
        getrandom::fill(&mut k)?;
        k
    } else {
        if !key_hex.len().is_multiple_of(2) || !key_hex.is_ascii() {
            return Err("invalid hex key".into());
        }
        (0..key_hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&key_hex[i..i + 2], 16))
            .collect::<std::result::Result<Vec<_>, _>>()?
    };
    let max = args.number("max-flows", 1 << 20)?;
    let every = args.number("window-every", 0_i64)?;
    if every < 0 {
        return Err("negative window interval".into());
    }
    let mut table = Table::new(prefixes, &key, max);
    let mut out = BufWriter::new(File::create(args.get("out", "features.jsonl"))?);
    let mut keys: Box<dyn Write> = if args.get("keys", "").is_empty() {
        Box::new(std::io::sink())
    } else {
        Box::new(BufWriter::new(File::create(args.get("keys", ""))?))
    };
    let stats = if live {
        use aqos_runtime_rs::live::{Event, Live, Rule, write_dump_header};
        use std::sync::{
            Arc,
            atomic::{AtomicBool, Ordering},
        };
        let stop = Arc::new(AtomicBool::new(false));
        for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
            signal_hook::flag::register(sig, stop.clone())?;
        }
        let until: f64 = args.number("until-s", 0.0)?;
        if !until.is_finite() || until < 0.0 {
            return Err("invalid run duration".into());
        }
        let group = args.number("group", 5_u16)?;
        let mut source = Live::new(table, group, every as u64)?;
        let rule = if args.boolean("install-rule", true)? {
            Some(Rule::install(group)?)
        } else {
            None
        };
        let mut dump: Option<Box<dyn Write>> = if args.get("dump", "").is_empty() {
            None
        } else {
            Some(Box::new(BufWriter::new(File::create(
                args.get("dump", ""),
            )?)))
        };
        if let Some(w) = &mut dump {
            write_dump_header(w)?;
        }
        let began = std::time::Instant::now();
        let wall = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos() as i64;
        let mut events = Vec::new();
        while !stop.load(Ordering::Relaxed)
            && (until == 0.0 || began.elapsed().as_secs_f64() < until)
        {
            source.poll(&mut events, &mut dump)?;
            for e in &events {
                match e {
                    Event::Initial(key, rec) => {
                        pcap::json_line(&mut out, rec)?;
                        if let Some(f) = source.table.flows.get(key) {
                            pcap::json_line(&mut keys, &f.key_line(wall))?;
                        }
                    }
                    Event::Window(_, rec) => pcap::json_line(&mut out, rec)?,
                    Event::Second(_) => {
                        out.flush()?;
                        keys.flush()?;
                        if let Some(w) = &mut dump {
                            w.flush()?;
                        }
                    }
                    _ => {}
                }
            }
        }
        out.flush()?;
        keys.flush()?;
        if let Some(w) = &mut dump {
            w.flush()?;
        }
        if let Some(rule) = rule {
            rule.remove()?;
        }
        source.stats
    } else {
        pcap::offline(pcap_path, &mut table, every, &mut out, &mut keys)?
    };
    if live {
        eprintln!(
            "live: {} packets, {} initial records, {} window records, {} flows tracked at exit, {} buffer overflows, {} flows not tracked",
            stats.packets,
            stats.initial,
            stats.window,
            stats.flows,
            stats.overflows,
            stats.untracked
        );
    } else {
        eprintln!(
            "{}: {} packets, {} flows, {} initial records, {} window records",
            pcap_path, stats.packets, stats.flows, stats.initial, stats.window
        );
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("aqos-extract-rs: {e}");
        std::process::exit(1);
    }
}
