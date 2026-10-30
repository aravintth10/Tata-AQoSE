//! Matched Go/Rust benchmark: load and precompute outside the timed region,
//! traverse the same mixed corpus, and retain a checksum of all outputs.
use aqos_classify_rs::{Engine, FeatureRecord, Mode, Model, Result};
use serde::Deserialize;
use std::{hint::black_box, time::Instant};
#[derive(Deserialize)]
struct Corpus {
    cases: Vec<Case>,
}
#[derive(Deserialize)]
struct Case {
    record: FeatureRecord,
}
fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 6 {
        return Err("usage: aqos-core-bench-rs MODEL CORPUS DAMAGE ROUNDS infer|pipeline".into());
    }
    let model = Model::load(&args[1])?;
    let corpus: Corpus = serde_json::from_slice(&std::fs::read(&args[2])?)?;
    let engine = Engine::load(&args[3], Mode::ExpectedDamage, true)?;
    let rounds: usize = args[4].parse()?;
    if rounds == 0 || corpus.cases.is_empty() || !["infer", "pipeline"].contains(&args[5].as_str())
    {
        return Err("invalid benchmark settings".into());
    }
    let records: Vec<_> = corpus
        .cases
        .iter()
        .map(|c| c.record.validate())
        .collect::<Result<_>>()?;
    let vectors: Vec<_> = records.iter().map(|r| r.vector()).collect();
    let pipeline = args[5] == "pipeline";
    let mut checksum = 0.0;
    let started = Instant::now();
    for _ in 0..rounds {
        for (i, r) in records.iter().enumerate() {
            let vector = if pipeline { r.vector() } else { vectors[i] };
            let p = model.probabilities(black_box(&vector));
            checksum += black_box(p.values()[0]);
            if pipeline {
                let c = engine.choose(&p, &[], Some(r));
                checksum += black_box(c.tin as usize as f64 + c.expected[c.tin as usize]);
            }
        }
    }
    let ns = started.elapsed().as_nanos();
    let operations = rounds * records.len();
    println!(
        "{}",
        serde_json::json!({"backend": "rust", "mode": args[5], "operations": operations,
        "elapsed_ns": ns, "ns_per_op": ns as f64 / operations as f64, "checksum": checksum})
    );
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("{e}");
        std::process::exit(1);
    }
}
