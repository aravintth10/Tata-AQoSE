//! One owner for flow, policy and verifier state. Heartbeats follow this loop's progress.
use crate::{
    FeatureRecord, Result, Tin,
    capacity::{Estimator, Prober},
    client::{Answer, Client, Failure},
    contracts::{FlowLabel, Proposal, ShaperRate, default_dscp},
    enforce::{Enforcer, LabelResult},
    extract::Key,
    intent::{CLASSES, Server, Store},
    live::Event,
    now_ns, random_id,
    reclass::{Ask, Watcher},
    shield::Tracker,
    verify,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs::{File, OpenOptions},
    io::Write,
};
pub struct Config {
    pub max_classify: usize,
    pub confirm: u32,
    pub ask_every: u32,
    pub heartbeat: String,
    pub run_id: String,
    pub t0: i64,
    pub verify: bool,
    pub rollback: bool,
}
#[derive(Default)]
pub struct Logs {
    pub metrics: Option<File>,
    pub actions: Option<File>,
    pub verdicts: Option<File>,
}
pub fn append_file(path: &str) -> Result<Option<File>> {
    if path.is_empty() {
        Ok(None)
    } else {
        Ok(Some(
            OpenOptions::new().append(true).create(true).open(path)?,
        ))
    }
}
fn write(file: &mut Option<File>, value: &impl serde::Serialize) -> Result<()> {
    if let Some(f) = file {
        let mut b = serde_json::to_vec(value)?;
        b.push(b'\n');
        if f.write(&b)? != b.len() {
            return Err("short append to runtime log".into());
        }
    }
    Ok(())
}
#[derive(Clone)]
enum Watch {
    Label {
        flow: String,
        host: String,
        from: Option<Tin>,
        to: Tin,
        rule: String,
    },
    Rate {
        dir: usize,
        from: i64,
        to: i64,
    },
    Lease {
        id: String,
        host: String,
    },
}
pub struct Parts {
    pub client: Client,
    pub shield: Tracker,
    pub enforcer: Enforcer,
    pub leases: Store,
    pub server: Option<Server>,
    pub reclass: Option<Watcher>,
    pub estimator: Option<Estimator>,
    pub prober: Option<Prober>,
}
pub struct Controller {
    config: Config,
    pub parts: Parts,
    logs: Logs,
    pub counts: BTreeMap<&'static str, u64>,
    flows: HashMap<String, Key>,
    hosts: HashSet<String>,
    asked: HashMap<String, (Tin, u32)>,
    windows: HashMap<String, u32>,
    again: HashSet<String>,
    why: HashMap<String, Ask>,
    second: i64,
    sent: usize,
    relabelled: u64,
    last_extract: i64,
    last_beat: i64,
    last_second: i64,
    last_load: i64,
    bytes: [Option<(i64, f64)>; 2],
    tin_packets: HashMap<(usize, usize), f64>,
    history: Vec<verify::Sample>,
    watches: Vec<(f64, Watch)>,
    seen_leases: HashSet<String>,
    held: HashMap<String, i64>,
}
impl Controller {
    pub fn new(config: Config, parts: Parts, logs: Logs) -> Self {
        let now = now_ns();
        Self {
            config,
            parts,
            logs,
            counts: [
                "records",
                "labelled",
                "rate_changes",
                "over_cap",
                "service_down",
                "service_busy",
                "timeouts",
                "refused",
                "rejected",
                "flow_gone",
                "mismatch",
                "errors",
                "relabelled",
                "windows",
                "unchanged",
                "held",
                "triggers",
                "rechecks",
                "watched",
                "regressed",
                "improved",
                "rolled_back",
                "held_back",
            ]
            .into_iter()
            .map(|s| (s, 0))
            .collect(),
            flows: HashMap::new(),
            hosts: HashSet::new(),
            asked: HashMap::new(),
            windows: HashMap::new(),
            again: HashSet::new(),
            why: HashMap::new(),
            second: 0,
            sent: 0,
            relabelled: 0,
            last_extract: now,
            last_beat: 0,
            last_second: now,
            last_load: 0,
            bytes: [None; 2],
            tin_packets: HashMap::new(),
            history: Vec::new(),
            watches: Vec::new(),
            seen_leases: HashSet::new(),
            held: HashMap::new(),
        }
    }
    fn count(&mut self, key: &'static str) {
        *self.counts.get_mut(key).expect("known counter") += 1;
    }
    fn time(&self) -> f64 {
        (now_ns() - self.config.t0) as f64 / 1e9
    }
    fn sample(&mut self, metric: &str, value: f64) -> Result<()> {
        let ts = self.time();
        if ts >= 0.0 {
            write(
                &mut self.logs.metrics,
                &json!({"run_id":self.config.run_id,"t_s":ts,"source":"controller","metric":metric,"value":value}),
            )?;
        }
        Ok(())
    }
    pub fn event(&mut self, event: Event) -> Result<()> {
        match event {
            Event::Initial(key, rec) => {
                self.gone(&rec.flow_id);
                self.count("records");
                self.flows.insert(rec.flow_id.clone(), key);
                if self.hosts.len() < 65536 {
                    self.hosts.insert(rec.host_id.clone());
                }
                self.parts.shield.seen(&rec.flow_id, &rec.host_id);
                self.classify(&rec, None, None);
            }
            Event::Window(_, rec) => {
                self.count("windows");
                let Some(f) = self.parts.shield.state.flows.get(&rec.flow_id) else {
                    return Ok(());
                };
                if f.pinned {
                    return Ok(());
                }
                let previous = f.tin;
                let mut ask = None;
                if let Some(watcher) = &mut self.parts.reclass {
                    let age = self.windows.entry(rec.flow_id.clone()).or_default();
                    *age = age.saturating_add(1);
                    let again = self.again.remove(&rec.flow_id);
                    if let Some(tin) = previous {
                        if let Some(w) = &rec.window {
                            ask = watcher.window(&rec.flow_id, tin, w);
                        }
                        if ask.is_none() && !again {
                            return Ok(());
                        }
                    } else if !age.is_multiple_of(self.config.ask_every.max(1)) {
                        return Ok(());
                    }
                }
                if let Some(why) = &ask {
                    self.count(if why.recheck { "rechecks" } else { "triggers" });
                }
                self.classify(&rec, previous, ask);
            }
            Event::Gone(id) => self.gone(&id),
            Event::Second(now) => self.last_extract = now,
        }
        Ok(())
    }
    fn gone(&mut self, id: &str) {
        self.parts.client.cancel(id);
        self.flows.remove(id);
        self.asked.remove(id);
        self.windows.remove(id);
        self.again.remove(id);
        self.why.remove(id);
        self.held.remove(id);
        self.parts.shield.state.flows.remove(id);
        if let Some(w) = &mut self.parts.reclass {
            w.gone(id);
        }
    }
    fn classify(&mut self, rec: &FeatureRecord, previous: Option<Tin>, why: Option<Ask>) {
        let second = now_ns() / 1_000_000_000;
        if second != self.second {
            self.second = second;
            self.sent = 0;
        }
        if self.config.max_classify > 0 && self.sent >= self.config.max_classify {
            self.count("over_cap");
            return;
        }
        self.sent += 1;
        let leased = if self.parts.leases.leased(&rec.host_id) {
            &CLASSES[..]
        } else {
            &[]
        };
        match self.parts.client.classify(rec, previous, leased) {
            Ok(()) => {
                if let Some(why) = why {
                    self.why.insert(rec.flow_id.clone(), why);
                }
            }
            Err(e) => self.failure(e),
        }
    }
    fn failure(&mut self, e: Failure) {
        self.count(match e {
            Failure::Down => "service_down",
            Failure::Busy => "service_busy",
            Failure::Timeout => "timeouts",
            Failure::Refused => "refused",
        });
    }
    fn handle(&mut self, answer: Answer) -> Result<()> {
        let ask = self.why.remove(&answer.flow_id);
        let response = match answer.result {
            Ok(r) => r,
            Err(e) => {
                self.failure(e);
                return Ok(());
            }
        };
        let (Some(mut proposal), Some(classification)) =
            (response.proposal, response.classification)
        else {
            self.count("refused");
            return Ok(());
        };
        if !response.ok
            || response.error.is_some()
            || !classification.valid(&answer.flow_id)
            || proposal
                .flow_label
                .as_ref()
                .is_none_or(|l| l.flow_id != answer.flow_id)
        {
            self.count("refused");
            return Ok(());
        }
        self.sample("inference_us", classification.inference_us)?;
        let triggered = ask.as_ref().is_some_and(|a| !a.recheck);
        if triggered {
            proposal.source = "reclassifier".into();
            proposal.reason = format!(
                "{}. {}",
                ask.as_ref().expect("trigger").reason,
                proposal.reason
            );
        }
        let label = proposal.flow_label.as_ref().expect("validated flow label");
        let tin = label.tin;
        let class = label.top_class;
        if matches!(tin, Tin::Voice | Tin::Video)
            && self
                .held
                .get(&answer.flow_id)
                .is_some_and(|&until| now_ns() < until)
        {
            self.count("held_back");
            return Ok(());
        }
        // Validate stale replies before confirmation can advance its state.
        if proposal.validate().is_err()
            || self
                .parts
                .shield
                .state
                .flows
                .get(&answer.flow_id)
                .is_some_and(|f| f.tin != label.previous_tin)
        {
            self.propose(proposal)?;
            return Ok(());
        }
        let current = self.parts.shield.state.flows.get(&answer.flow_id);
        let previous = current.and_then(|f| f.tin);
        let leased = current.is_some_and(|f| self.parts.leases.leased(&f.host_id));
        let settled = if previous == Some(tin) {
            self.asked.remove(&answer.flow_id);
            self.count("unchanged");
            true
        } else {
            let confirmed = if previous.is_none()
                || triggered
                || leased && matches!(tin, Tin::Voice | Tin::Video)
            {
                true
            } else {
                let a = self.asked.entry(answer.flow_id.clone()).or_insert((tin, 0));
                if a.0 != tin {
                    *a = (tin, 0);
                }
                a.1 += 1;
                a.1 >= self.config.confirm.max(1)
            };
            if !confirmed {
                self.count("held");
                self.again.insert(answer.flow_id.clone());
                false
            } else {
                self.asked.remove(&answer.flow_id);
                self.propose(proposal)?
            }
        };
        if settled && let (Some(w), Some(class)) = (&mut self.parts.reclass, class) {
            w.answered(&answer.flow_id, class);
        }
        Ok(())
    }
    fn watch(&mut self, watch: Watch) {
        if self.config.verify && self.watches.len() < 256 {
            self.watches.push((self.time(), watch));
        }
    }
    pub fn propose(&mut self, proposal: Proposal) -> Result<bool> {
        let now = now_ns();
        let out = self.parts.shield.decide(&proposal, now);
        let mut outcome = "rejected";
        let mut detail = String::new();
        if out.unlabel {
            if let Some(l) = &proposal.flow_label
                && let Some(&key) = self.flows.get(&l.flow_id)
            {
                match self.parts.enforcer.set_label(key, None) {
                    Ok(_) => self.parts.shield.cleared(&l.flow_id),
                    Err(e) => detail = format!("clearing the label failed; will retry: {e}"),
                }
            }
            self.count("rejected");
        } else if out.verdict.verdict == "reject" || out.verdict.applied.is_none() {
            self.count("rejected");
        } else if let Some(l) = &proposal.flow_label {
            if let Some(&key) = self.flows.get(&l.flow_id) {
                let previous = self
                    .parts
                    .shield
                    .state
                    .flows
                    .get(&l.flow_id)
                    .and_then(|f| f.tin);
                match self.parts.enforcer.set_label(key, Some(l.tin)) {
                    Ok(LabelResult::Gone) => {
                        outcome = "flow_gone";
                        self.count("flow_gone");
                    }
                    Ok(LabelResult::Applied) => {
                        outcome = "applied";
                        self.count("labelled");
                        self.parts.shield.applied(&proposal, &out.verdict, now_ns());
                        if previous.is_some() && previous != Some(l.tin) {
                            self.count("relabelled");
                            self.relabelled += 1;
                        }
                        if proposal.source != "verifier"
                            && previous != Some(l.tin)
                            && matches!(l.tin, Tin::Voice | Tin::Video)
                        {
                            self.watch(Watch::Label {
                                flow: l.flow_id.clone(),
                                host: l.host_id.clone(),
                                from: previous,
                                to: l.tin,
                                rule: l.decision_rule.clone(),
                            });
                        }
                    }
                    Err(e) => {
                        detail = e.to_string();
                        outcome = if detail.contains("read-back mismatch") {
                            self.count("mismatch");
                            "mismatch"
                        } else {
                            self.count("errors");
                            "error"
                        };
                    }
                }
            } else {
                outcome = "flow_gone";
                self.count("flow_gone");
            }
        } else if let Some(r) = &proposal.shaper_rate {
            let rate = out
                .verdict
                .applied
                .as_ref()
                .and_then(|a| a.rate_kbit)
                .ok_or("shield omitted applied rate")?;
            let dir = usize::from(r.direction == "down");
            let previous = self.parts.shield.state.rates[&r.direction].kbit;
            match self.parts.enforcer.set_rate(&r.direction, rate) {
                Ok(()) => {
                    outcome = "applied";
                    self.count("rate_changes");
                    self.parts.shield.applied(&proposal, &out.verdict, now_ns());
                    if let Some(e) = &mut self.parts.estimator {
                        e.applied(dir, rate);
                    }
                    if proposal.source != "verifier" && rate > previous {
                        self.watch(Watch::Rate {
                            dir,
                            from: previous,
                            to: rate,
                        });
                    }
                }
                Err(e) => {
                    detail = e.to_string();
                    outcome = if detail.contains("read-back mismatch") {
                        self.count("mismatch");
                        "mismatch"
                    } else {
                        self.count("errors");
                        "error"
                    };
                }
            }
        }
        let mut action = json!({"t_s":self.time(),"proposal":proposal,"verdict":out.verdict,"code":out.code,"outcome":outcome});
        if !detail.is_empty() {
            action["detail"] = detail.into();
        }
        write(&mut self.logs.actions, &action)?;
        Ok(outcome == "applied")
    }
    fn base_proposal(source: &str, reason: String) -> Result<Proposal> {
        Ok(Proposal {
            schema_version: 1,
            proposal_id: random_id()?,
            ts_ns: now_ns(),
            source: source.into(),
            kind: String::new(),
            reason,
            flow_label: None,
            shaper_rate: None,
        })
    }
    fn rate_proposal(
        source: &str,
        dir: usize,
        rate: i64,
        previous: i64,
        reason: String,
    ) -> Result<Proposal> {
        let mut p = Self::base_proposal(source, reason)?;
        p.kind = "shaper_rate".into();
        p.shaper_rate = Some(ShaperRate {
            direction: ["up", "down"][dir].into(),
            rate_kbit: rate,
            previous_rate_kbit: Some(previous),
        });
        Ok(p)
    }
    fn read_load(&mut self, now: i64) -> Result<()> {
        for dir in 0..2 {
            let q = match self.parts.enforcer.cake(["up", "down"][dir]) {
                Ok(q) => q,
                Err(_) => continue,
            };
            let Some(tins) = q["tins"].as_array().filter(|t| t.len() == 4) else {
                continue;
            };
            let sent = tins
                .iter()
                .filter_map(|t| t["sent_bytes"].as_f64())
                .sum::<f64>();
            if let Some((at, before)) = self.bytes[dir]
                && sent >= before
                && now > at
            {
                let achieved = (sent - before) * 8.0 / 1000.0 / ((now - at) as f64 / 1e9);
                if let Some(e) = &mut self.parts.estimator {
                    e.load(
                        dir,
                        (q["options"]["bandwidth"].as_f64().unwrap_or(0.0) * 8.0 / 1000.0).round()
                            as i64,
                        achieved,
                        now,
                    );
                }
            }
            self.bytes[dir] = Some((now, sent));
        }
        Ok(())
    }
    fn every_second(&mut self) -> Result<()> {
        let labelled = self
            .parts
            .shield
            .state
            .flows
            .values()
            .filter(|f| f.tin.is_some())
            .count();
        self.sample("flows_labelled", labelled as f64)?;
        self.sample(
            "flows_unlabelled",
            self.flows.len().saturating_sub(labelled) as f64,
        )?;
        self.sample("reclassifications", self.relabelled as f64)?;
        self.relabelled = 0;
        let retry: Vec<_> = self
            .parts
            .shield
            .state
            .flows
            .iter()
            .filter(|(_, f)| f.pending_clear)
            .take(50)
            .map(|(id, _)| id.clone())
            .collect();
        for id in retry {
            if let Some(&key) = self.flows.get(&id) {
                match self.parts.enforcer.set_label(key, None) {
                    Ok(_) => self.parts.shield.cleared(&id),
                    Err(e) => eprintln!("aqosd-rs: label clear remains pending: {e}"),
                }
            }
        }
        self.parts.leases.expire()?;
        if !self.config.verify {
            return Ok(());
        }
        let mut sample = verify::Sample {
            ts: self.time(),
            ..Default::default()
        };
        for dir in 0..2 {
            let q = match self.parts.enforcer.cake(["up", "down"][dir]) {
                Ok(q) => q,
                Err(_) => continue,
            };
            let Some(tins) = q["tins"].as_array().filter(|t| t.len() == 4) else {
                continue;
            };
            for i in [2, 3] {
                let pkts = tins[i]["sent_packets"].as_f64().unwrap_or(0.0);
                if self
                    .tin_packets
                    .insert((dir, i), pkts)
                    .is_some_and(|before| pkts > before)
                    && let Some(delay) = tins[i]["avg_delay_us"].as_f64()
                {
                    sample.tin = Some(sample.tin.unwrap_or(0.0).max(delay / 1000.0));
                }
            }
        }
        if let Some(e) = &self.parts.estimator {
            let (ms, known) = e.delay();
            if known {
                sample.probe = Some(ms);
            }
        }
        self.history.push(sample);
        self.history.retain(|s| s.ts >= sample.ts - 25.0);
        let leases = self.parts.leases.list()?;
        let current: HashSet<_> = leases.iter().map(|l| l.id.clone()).collect();
        for l in leases {
            if self.seen_leases.insert(l.id.clone()) {
                self.watch(Watch::Lease {
                    id: l.id,
                    host: l.host,
                });
            }
        }
        self.seen_leases.retain(|id| current.contains(id));
        self.judge()?;
        Ok(())
    }
    fn judge(&mut self) -> Result<()> {
        let now = self.time();
        let watches = std::mem::take(&mut self.watches);
        for (at, w) in watches {
            if now < at + 10.0 {
                self.watches.push((at, w));
                continue;
            }
            let judgement = verify::judge(verify::Config::default(), &self.history, at);
            self.count("watched");
            if judgement.verdict == "regressed" {
                self.count("regressed");
            } else if judgement.verdict == "improved" {
                self.count("improved");
            }
            let (kind, target, from, to) = match &w {
                Watch::Label { flow, from, to, .. } => (
                    "promotion",
                    flow.clone(),
                    from.map(tin_name).unwrap_or_default(),
                    tin_name(*to),
                ),
                Watch::Rate { dir, from, to } => (
                    "rate_increase",
                    ["up", "down"][*dir].into(),
                    from.to_string(),
                    to.to_string(),
                ),
                Watch::Lease { id, host } => ("lease", id.clone(), String::new(), host.clone()),
            };
            let mut rolled = false;
            let mut detail = String::new();
            if judgement.verdict == "regressed" {
                if self.config.rollback {
                    (rolled, detail) = self.rollback(w, &judgement.reason)?;
                } else {
                    detail = "rollback is switched off".into();
                }
            }
            if rolled {
                self.count("rolled_back");
            }
            let mut line = serde_json::to_value(judgement)?;
            let obj = line.as_object_mut().ok_or("verdict is not an object")?;
            for (k, v) in [
                ("t_s", json!(now)),
                ("changed_at_s", json!(at)),
                ("kind", json!(kind)),
                ("target", json!(target)),
                ("from", json!(from)),
                ("to", json!(to)),
                ("rolled_back", json!(rolled)),
            ] {
                obj.insert(k.into(), v);
            }
            if !detail.is_empty() {
                obj.insert("detail".into(), detail.into());
            }
            write(&mut self.logs.verdicts, &line)?;
        }
        let now = now_ns();
        self.held.retain(|_, until| *until > now);
        Ok(())
    }
    fn rollback(&mut self, w: Watch, why: &str) -> Result<(bool, String)> {
        let reason = format!("Undone by the verifier: {why}.");
        let result = match w {
            Watch::Label {
                flow,
                host,
                from,
                to,
                rule,
            } => {
                if self
                    .parts
                    .shield
                    .state
                    .flows
                    .get(&flow)
                    .is_none_or(|f| f.tin != Some(to))
                {
                    return Ok((false, "the flow has ended or been moved since".into()));
                }
                let back = from.unwrap_or(Tin::BestEffort);
                let mut p = Self::base_proposal("verifier", reason)?;
                p.kind = "flow_label".into();
                p.flow_label = Some(FlowLabel {
                    flow_id: flow.clone(),
                    host_id: host,
                    tin: back,
                    dscp: default_dscp(back).into(),
                    previous_tin: Some(to),
                    top_class: None,
                    expected_damage: None,
                    decision_rule: rule,
                });
                let applied = self.propose(p)?;
                if applied {
                    self.held.insert(flow, now_ns() + 60_000_000_000);
                }
                applied
            }
            Watch::Rate { dir, from, to } => {
                if self
                    .parts
                    .shield
                    .state
                    .rates
                    .get(["up", "down"][dir])
                    .is_none_or(|r| r.kbit != to)
                {
                    return Ok((false, "the rate has been changed since".into()));
                }
                self.propose(Self::rate_proposal("verifier", dir, from, to, reason)?)?
            }
            Watch::Lease { id, .. } => {
                if let Err(e) = self.parts.leases.delete(&id) {
                    return Ok((false, e.to_string()));
                }
                true
            }
        };
        Ok((
            result,
            if result {
                String::new()
            } else {
                "rollback proposal was not applied; see action log".into()
            },
        ))
    }
    pub fn tick(&mut self) -> Result<()> {
        let mut answers = Vec::new();
        self.parts.client.poll(&mut answers);
        for answer in answers {
            self.handle(answer)?;
        }
        if let Some(server) = &mut self.parts.server {
            server.poll(&mut self.parts.leases, &self.hosts);
        }
        let now = now_ns();
        if now - self.last_load >= 200_000_000 && self.parts.estimator.is_some() {
            self.read_load(now)?;
            self.last_load = now;
        }
        let changes = if let (Some(prober), Some(estimator)) =
            (&mut self.parts.prober, &mut self.parts.estimator)
        {
            if prober.poll(estimator, now) {
                estimator.decide(now)
            } else {
                Vec::new()
            }
        } else {
            Vec::new()
        };
        for change in changes {
            self.propose(Self::rate_proposal(
                "capacity_estimator",
                change.direction,
                change.rate,
                change.previous,
                change.reason,
            )?)?;
        }
        if now - self.last_second >= 1_000_000_000 {
            self.every_second()?;
            self.last_second = now;
        }
        // Never put this in a timer thread: blocking or failed control work
        // must stop the heartbeat and let the independent watchdog recover.
        let now = now_ns();
        if now - self.last_beat >= 250_000_000
            && now - self.last_extract < 3_000_000_000
            && !self.config.heartbeat.is_empty()
        {
            std::fs::write(&self.config.heartbeat, b"alive\n")?;
            self.last_beat = now;
        }
        Ok(())
    }
    pub fn summary(&self, health: &crate::client::Response, stats: &crate::pcap::Stats) -> Value {
        let cpu = rustix::time::clock_gettime(rustix::time::ClockId::ProcessCPUTime);
        json!({"model_version":health.model_version,"decision_rule":health.decision_rule,"cpu_s":cpu.tv_sec as f64+cpu.tv_nsec as f64/1e9,"flows":self.counts,"shield":self.parts.shield.counts,"extractor":{"packets":stats.packets,"overflows":stats.overflows,"untracked":stats.untracked}})
    }
}
fn tin_name(t: Tin) -> String {
    serde_json::to_value(t)
        .expect("tin")
        .as_str()
        .expect("tin string")
        .into()
}
