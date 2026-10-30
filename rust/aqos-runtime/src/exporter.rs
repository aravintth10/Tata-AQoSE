//! Bounded metrics tailer. Incomplete lines survive polls; rotation resets the run.
use crate::Result;
use serde_json::Value;
use std::{
    collections::BTreeMap, fmt::Write as _, fs::File, io::Read, os::unix::fs::MetadataExt,
    path::PathBuf,
};
const BUCKETS: [f64; 15] = [
    1., 2., 5., 10., 20., 30., 50., 75., 100., 150., 200., 300., 500., 1000., 2000.,
];
const MAX_SERIES: usize = 65536;
const MAX_LINE: usize = 1 << 20;
#[derive(Default)]
struct Histogram {
    counts: [u64; 16],
    sum: f64,
}
#[derive(Default)]
pub struct Store {
    run_id: String,
    lines: u64,
    gauges: BTreeMap<(String, String), f64>,
    histograms: BTreeMap<String, Histogram>,
}
fn escape(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}
fn braces(s: &str) -> String {
    if s.is_empty() {
        String::new()
    } else {
        format!("{{{s}}}")
    }
}
impl Store {
    pub fn add(&mut self, line: &[u8]) {
        let Ok(v) = serde_json::from_slice::<Value>(line) else {
            return;
        };
        let (Some(metric), Some(value)) = (v["metric"].as_str(), v["value"].as_f64()) else {
            return;
        };
        if metric.is_empty()
            || !metric
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_')
            || !value.is_finite()
        {
            return;
        }
        let labels = [
            "host",
            "traffic_id",
            "profile",
            "true_class",
            "tin",
            "direction",
            "component",
        ]
        .iter()
        .filter_map(|name| {
            v[*name]
                .as_str()
                .map(|s| format!("{name}=\"{}\"", escape(s)))
        })
        .collect::<Vec<_>>()
        .join(",");
        let full = self.gauges.len() + self.histograms.len() >= MAX_SERIES;
        if metric == "latency_ms" {
            if full && !self.histograms.contains_key(&labels) {
                return;
            }
            let h = self.histograms.entry(labels).or_default();
            h.counts[BUCKETS.partition_point(|b| *b < value)] += 1;
            h.sum += value;
        } else {
            let key = (metric.to_owned(), labels);
            if full && !self.gauges.contains_key(&key) {
                return;
            }
            self.gauges.insert(key, value);
        }
        self.lines += 1;
        if let Some(id) = v["run_id"].as_str() {
            self.run_id = id.to_owned();
        }
    }
    pub fn render(&self) -> String {
        let mut out = format!(
            "# TYPE aqos_run_info gauge\naqos_run_info{{run_id=\"{}\"}} 1\n# TYPE aqos_samples_read counter\naqos_samples_read {}\n",
            escape(&self.run_id),
            self.lines
        );
        let mut previous = "";
        for ((metric, labels), value) in &self.gauges {
            if previous != metric {
                let _ = writeln!(out, "# TYPE aqos_{metric} gauge");
                previous = metric;
            }
            let _ = writeln!(out, "aqos_{metric}{} {value}", braces(labels));
        }
        if !self.histograms.is_empty() {
            out.push_str("# TYPE aqos_latency_ms histogram\n");
        }
        for (labels, h) in &self.histograms {
            let mut total = 0;
            let prefix = if labels.is_empty() {
                String::new()
            } else {
                format!("{labels},")
            };
            for (i, count) in h.counts.iter().enumerate() {
                total += count;
                let bound = BUCKETS
                    .get(i)
                    .map_or_else(|| "+Inf".to_owned(), ToString::to_string);
                let _ = writeln!(
                    out,
                    "aqos_latency_ms_bucket{{{prefix}le=\"{bound}\"}} {total}"
                );
            }
            let _ = writeln!(
                out,
                "aqos_latency_ms_sum{} {}\naqos_latency_ms_count{} {total}",
                braces(labels),
                h.sum,
                braces(labels)
            );
        }
        out
    }
}
pub struct Tail {
    pub store: Store,
    path: PathBuf,
    file: Option<File>,
    identity: (u64, u64),
    size: u64,
    partial: Vec<u8>,
    discarding: bool,
}
impl Tail {
    pub fn new(path: PathBuf) -> Self {
        Self {
            store: Store::default(),
            path,
            file: None,
            identity: (0, 0),
            size: 0,
            partial: Vec::new(),
            discarding: false,
        }
    }
    pub fn poll(&mut self) -> Result<()> {
        let metadata = match std::fs::metadata(&self.path) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.into()),
        };
        if self.file.is_none()
            || self.identity != (metadata.dev(), metadata.ino())
            || metadata.len() < self.size
        {
            let file = File::open(&self.path)?;
            let opened = file.metadata()?;
            self.identity = (opened.dev(), opened.ino());
            self.file = Some(file);
            self.store = Store::default();
            self.partial.clear();
            self.discarding = false;
        }
        self.size = metadata.len();
        let mut buffer = [0; 16384];
        for _ in 0..64 {
            let n = self
                .file
                .as_mut()
                .expect("opened above")
                .read(&mut buffer)?;
            if n == 0 {
                break;
            }
            for byte in &buffer[..n] {
                if *byte == b'\n' {
                    if !self.discarding {
                        self.store.add(&self.partial);
                    }
                    self.partial.clear();
                    self.discarding = false;
                } else if !self.discarding {
                    if self.partial.len() >= MAX_LINE {
                        self.partial.clear();
                        self.discarding = true;
                    } else {
                        self.partial.push(*byte);
                    }
                }
            }
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn histogram_and_gauges_follow_existing_contract() {
        let mut store = Store::default();
        store.add(br#"{"metric":"queue_delay_ms","value":1,"tin":"voice","run_id":"run"}"#);
        store.add(br#"{"metric":"queue_delay_ms","value":2.5,"tin":"voice"}"#);
        store.add(br#"{"metric":"latency_ms","value":23.4,"host":"laptop"}"#);
        let text = store.render();
        assert!(text.contains("aqos_queue_delay_ms{tin=\"voice\"} 2.5\n"));
        assert!(text.contains("aqos_latency_ms_bucket{host=\"laptop\",le=\"20\"} 0\n"));
        assert!(text.contains("aqos_latency_ms_bucket{host=\"laptop\",le=\"30\"} 1\n"));
        assert!(text.contains("aqos_latency_ms_count{host=\"laptop\"} 1\n"));
        assert!(text.contains("aqos_samples_read 3\n"));
    }
    #[test]
    fn partial_lines_rotation_and_truncation() -> Result<()> {
        let dir = std::env::temp_dir().join(format!("aqos-exporter-{}", crate::random_id()?));
        std::fs::create_dir(&dir)?;
        let path = dir.join("metrics");
        std::fs::write(&path, br#"{"metric":"cpu_s","value":1"#)?;
        let mut tail = Tail::new(path.clone());
        tail.poll()?;
        assert_eq!(tail.store.lines, 0);
        File::options()
            .append(true)
            .open(&path)?
            .write_all(b"}\n")?;
        tail.poll()?;
        assert_eq!(tail.store.lines, 1);
        std::fs::rename(&path, dir.join("old"))?;
        std::fs::write(&path, b"{\"metric\":\"cpu_s\",\"value\":20}\n")?;
        tail.poll()?;
        assert_eq!(tail.store.lines, 1);
        assert!(tail.store.render().contains("aqos_cpu_s 20\n"));
        std::fs::write(&path, b"{}\n")?;
        tail.poll()?;
        assert_eq!(tail.store.lines, 0);
        std::fs::remove_dir_all(dir)?;
        Ok(())
    }
}
