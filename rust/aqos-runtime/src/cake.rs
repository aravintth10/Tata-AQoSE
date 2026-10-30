use crate::{Result, Tin, command::run};
use serde::Deserialize;
pub const TINS: [Tin; 4] = [Tin::Bulk, Tin::BestEffort, Tin::Video, Tin::Voice];
#[derive(Clone, Default, Deserialize)]
pub struct TinStats {
    #[serde(default)]
    pub avg_delay_us: f64,
    #[serde(default)]
    pub peak_delay_us: f64,
    #[serde(default)]
    pub backlog_bytes: f64,
    #[serde(default)]
    pub drops: f64,
    #[serde(default)]
    pub sent_bytes: f64,
    #[serde(default)]
    pub sent_packets: f64,
}
pub struct Stats {
    pub rate_kbit: f64,
    pub tins: Vec<TinStats>,
}
pub fn parse(bytes: &[u8]) -> Result<Option<Stats>> {
    #[derive(Deserialize, Default)]
    struct Options {
        #[serde(default)]
        bandwidth: f64,
    }
    #[derive(Deserialize)]
    struct Qdisc {
        kind: String,
        #[serde(default)]
        root: bool,
        #[serde(default)]
        options: Options,
        #[serde(default)]
        tins: Vec<TinStats>,
    }
    let all: Vec<Qdisc> = serde_json::from_slice(bytes)?;
    Ok(all
        .into_iter()
        .find(|q| q.kind == "cake" && q.root && q.tins.len() == 4)
        .map(|q| Stats {
            rate_kbit: q.options.bandwidth * 8.0 / 1000.0,
            tins: q.tins,
        }))
}
pub fn read(dev: &str) -> Result<Option<Stats>> {
    parse(&run(
        "tc",
        &["-j", "-s", "qdisc", "show", "dev", dev],
        None,
    )?)
}
#[derive(Default)]
pub struct Reader {
    previous: Option<Stats>,
}
impl Reader {
    pub fn samples(
        &mut self,
        current: Option<Stats>,
        ts: f64,
        run_id: &str,
        dir: &str,
    ) -> Vec<serde_json::Value> {
        let Some(current) = current else {
            self.previous = None;
            return Vec::new();
        };
        let sample = |metric, value, tin: Option<Tin>| {
            let mut v = serde_json::json!({"run_id":run_id,"t_s":ts,"source":"cake","metric":metric,"value":value,"direction":dir});
            if let Some(tin) = tin {
                v["tin"] = serde_json::to_value(tin).expect("tin");
            }
            v
        };
        let mut out = vec![sample("shaper_rate_kbit", current.rate_kbit, None)];
        for (i, tin) in current.tins.iter().enumerate() {
            let drops = self
                .previous
                .as_ref()
                .map(|prev| {
                    if tin.drops >= prev.tins[i].drops {
                        tin.drops - prev.tins[i].drops
                    } else {
                        0.0
                    }
                })
                .unwrap_or(0.0);
            out.push(sample(
                "queue_delay_ms",
                tin.avg_delay_us / 1000.0,
                Some(TINS[i]),
            ));
            out.push(sample(
                "queue_backlog_bytes",
                tin.backlog_bytes,
                Some(TINS[i]),
            ));
            out.push(sample("tin_drops", drops, Some(TINS[i])));
        }
        self.previous = Some(current);
        out
    }
}
