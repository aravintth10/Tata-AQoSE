//! Native classifier and decision policy. Only validated models, records and
//! probability distributions reach the computational core; no packet handling.
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, fmt::Write, fs, path::Path};

pub const FEATURES: usize = 28;
pub type Error = Box<dyn std::error::Error + Send + Sync>;
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
#[repr(usize)]
pub enum Class {
    Telephony,
    RealtimeInteractive,
    MultimediaConferencing,
    MultimediaStreaming,
    Standard,
    HighThroughputData,
    LowPriorityData,
}
pub const CLASSES: [Class; 7] = [
    Class::Telephony,
    Class::RealtimeInteractive,
    Class::MultimediaConferencing,
    Class::MultimediaStreaming,
    Class::Standard,
    Class::HighThroughputData,
    Class::LowPriorityData,
];

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
#[repr(usize)]
pub enum Tin {
    Voice,
    Video,
    BestEffort,
    Bulk,
}
pub const TINS: [Tin; 4] = [Tin::Voice, Tin::Video, Tin::BestEffort, Tin::Bulk];
impl Tin {
    pub fn name(self) -> &'static str {
        match self {
            Self::Voice => "Voice",
            Self::Video => "Video",
            Self::BestEffort => "Best Effort",
            Self::Bulk => "Bulk",
        }
    }
    fn tie_rank(self) -> usize {
        match self {
            Self::BestEffort => 0,
            Self::Video => 1,
            Self::Voice => 2,
            Self::Bulk => 3,
        }
    }
}
impl Class {
    pub fn interactive(self) -> bool {
        (self as usize) < 3
    }
    pub fn plain(self) -> &'static str {
        match self {
            Self::Telephony => "a voice call",
            Self::RealtimeInteractive => "a game or other real-time traffic",
            Self::MultimediaConferencing => "a video call",
            Self::MultimediaStreaming => "streaming video",
            Self::Standard => "ordinary web traffic",
            Self::HighThroughputData => "a large download",
            Self::LowPriorityData => "background transfer",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Initial,
    Window,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Window {
    pub samples: u8,
    pub pkts_up_per_s: f64,
    pub pkts_down_per_s: f64,
    pub bytes_up_per_s: f64,
    pub bytes_down_per_s: f64,
    pub mean_pkt_size_up: f64,
    pub mean_pkt_size_down: f64,
    pub pkt_size_spread: f64,
    pub rate_cv: f64,
    pub burstiness: f64,
}
impl Window {
    fn values(&self) -> [f64; 9] {
        [
            self.pkts_up_per_s,
            self.pkts_down_per_s,
            self.bytes_up_per_s,
            self.bytes_down_per_s,
            self.mean_pkt_size_up,
            self.mean_pkt_size_down,
            self.pkt_size_spread,
            self.rate_cv,
            self.burstiness,
        ]
    }
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FeatureRecord {
    pub schema_version: u32,
    pub flow_id: String,
    pub host_id: String,
    pub kind: Kind,
    pub ts_ns: u64,
    pub ip_version: u8,
    pub protocol: u8,
    pub src_port: u16,
    pub dst_port: u16,
    pub pkt_sizes: [i32; 8],
    pub pkt_gaps_us: [u64; 7],
    #[serde(deserialize_with = "required_window")]
    pub window: Option<Window>,
}
fn required_window<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<Window>, D::Error> {
    Option::<Window>::deserialize(d)
}
/// Borrowing a validated record prevents changing its fields during inference.
pub struct ValidRecord<'a>(&'a FeatureRecord);
pub fn valid_id(id: &str) -> bool {
    id.len() == 16
        && id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
impl FeatureRecord {
    pub fn validate(&self) -> Result<ValidRecord<'_>> {
        if self.schema_version != 1
            || !valid_id(&self.flow_id)
            || !valid_id(&self.host_id)
            || ![4, 6].contains(&self.ip_version)
            || self.ts_ns > i64::MAX as u64
            || self
                .pkt_sizes
                .iter()
                .any(|&n| n == 0 || !(-65535..=65535).contains(&n))
            || (self.kind == Kind::Window && self.window.is_none())
        {
            return Err("invalid feature record".into());
        }
        if let Some(w) = &self.window
            && (!(1..=5).contains(&w.samples)
                || w.values().iter().any(|v| !v.is_finite() || *v < 0.0))
        {
            return Err("invalid window statistics".into());
        }
        Ok(ValidRecord(self))
    }
}
impl ValidRecord<'_> {
    pub fn record(&self) -> &FeatureRecord {
        self.0
    }
    pub fn vector(&self) -> [f64; FEATURES] {
        let r = self.0;
        let mut x = [f64::NAN; FEATURES];
        for (to, from) in x[..8].iter_mut().zip(r.pkt_sizes) {
            *to = f64::from(from);
        }
        for (to, from) in x[8..15].iter_mut().zip(r.pkt_gaps_us) {
            *to = from as f64;
        }
        x[15] = f64::from(r.ip_version);
        x[16] = f64::from(r.protocol);
        let port = |p| {
            if p < 1024 {
                0.0
            } else if p < 49152 {
                1.0
            } else {
                2.0
            }
        };
        x[17] = port(r.src_port);
        x[18] = port(r.dst_port);
        if let Some(w) = &r.window {
            x[19..].copy_from_slice(&w.values());
        }
        x
    }
}

#[derive(Deserialize)]
struct RawModel {
    model_version: String,
    features_version: u32,
    model_classes: Vec<Class>,
    temperature: f64,
    trees: Vec<RawTree>,
}
#[derive(Deserialize)]
struct RawTree {
    class: usize,
    root: i64,
    nodes: Vec<(usize, f64, bool, bool, i64, i64)>,
    leaves: Vec<f64>,
}
#[derive(Clone, Copy)]
// A validated 31-bit index plus a leaf tag keeps each child to four bytes.
// The loader checks the tag limit as well as bounds before constructing one.
struct Child(u32);
impl Child {
    const LEAF: u32 = 1 << 31;
    fn node(self) -> Option<usize> {
        (self.0 & Self::LEAF == 0).then_some(self.0 as usize)
    }
    fn leaf(self) -> usize {
        (self.0 & !Self::LEAF) as usize
    }
}
struct Node {
    feature: u8,
    threshold: f64,
    missing_left: bool,
    left: Child,
    right: Child,
}
struct Tree {
    class: usize,
    root: Child,
    nodes: Vec<Node>,
    leaves: Vec<f64>,
}
/// Fields are private: a Model can only be constructed by validating its input.
pub struct Model {
    version: String,
    temperature: f64,
    classes: [Class; 7],
    trees: Vec<Tree>,
}
impl Model {
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        Self::from_json(&fs::read(path)?)
    }
    pub fn from_json(json: &[u8]) -> Result<Self> {
        let raw: RawModel = serde_json::from_slice(json)?;
        if raw.features_version != 1
            || raw.model_version.is_empty()
            || raw.model_classes.len() != 7
            || !raw.temperature.is_finite()
            || raw.temperature <= 0.0
            || raw.trees.is_empty()
        {
            return Err("invalid model metadata".into());
        }
        let mut seen = [false; 7];
        for &class in &raw.model_classes {
            if std::mem::replace(&mut seen[class as usize], true) {
                return Err("duplicate model class".into());
            }
        }
        let classes: [Class; 7] = raw
            .model_classes
            .try_into()
            .map_err(|_| "invalid class count")?;
        let mut trees = Vec::with_capacity(raw.trees.len());
        let mut bounds = [0.0_f64; 7];
        let mut tree_classes = [false; 7];
        for t in raw.trees {
            if t.class >= 7 || t.leaves.is_empty() || t.leaves.iter().any(|v| !v.is_finite()) {
                return Err("invalid tree class or leaves".into());
            }
            tree_classes[t.class] = true;
            bounds[t.class] += t.leaves.iter().fold(0.0_f64, |m, v| m.max(v.abs()));
            if !bounds[t.class].is_finite() || !(bounds[t.class] * raw.temperature).is_finite() {
                return Err("model scores can overflow".into());
            }
            let child = |n: i64| -> Result<Child> {
                if n >= 0 {
                    let i = usize::try_from(n)?;
                    if i < t.nodes.len() && i < Child::LEAF as usize {
                        return Ok(Child(u32::try_from(i)?));
                    }
                } else if let Some(i) = n.checked_neg().and_then(|v| v.checked_sub(1)) {
                    let i = usize::try_from(i)?;
                    if i < t.leaves.len() && i < Child::LEAF as usize {
                        return Ok(Child(u32::try_from(i)? | Child::LEAF));
                    }
                }
                Err("tree child/root out of bounds".into())
            };
            let root = child(t.root)?;
            let mut nodes = Vec::with_capacity(t.nodes.len());
            for &(feature, threshold, default_left, nan_missing, l, r) in &t.nodes {
                if feature >= FEATURES || !threshold.is_finite() {
                    return Err("invalid split".into());
                }
                nodes.push(Node {
                    feature: u8::try_from(feature)?,
                    threshold,
                    // Resolve the model's missing-value policy once at load time.
                    missing_left: if nan_missing {
                        default_left
                    } else {
                        0.0 <= threshold
                    },
                    left: child(l)?,
                    right: child(r)?,
                });
            }
            // Iterative DFS validates every node, including unreachable nodes.
            // A malformed deep tree cannot overflow the call stack.
            let mut colors = vec![0_u8; nodes.len()];
            for start in 0..nodes.len() {
                if colors[start] == 2 {
                    continue;
                }
                let mut stack = vec![(start, false)];
                while let Some((i, leaving)) = stack.pop() {
                    if leaving {
                        colors[i] = 2;
                        continue;
                    }
                    match colors[i] {
                        1 => return Err("cycle in model tree".into()),
                        2 => continue,
                        _ => {}
                    }
                    colors[i] = 1;
                    stack.push((i, true));
                    for c in [nodes[i].right, nodes[i].left] {
                        if let Some(j) = c.node() {
                            stack.push((j, false));
                        }
                    }
                }
            }
            trees.push(Tree {
                class: t.class,
                root,
                nodes,
                leaves: t.leaves,
            });
        }
        if tree_classes.contains(&false) {
            return Err("model is missing a class's trees".into());
        }
        Ok(Self {
            version: raw.model_version,
            temperature: raw.temperature,
            classes,
            trees,
        })
    }
    pub fn version(&self) -> &str {
        &self.version
    }
    pub fn probabilities(&self, x: &[f64; FEATURES]) -> Probabilities {
        let mut raw = [0.0; 7];
        for tree in &self.trees {
            let mut child = tree.root;
            while let Some(i) = child.node() {
                let n = &tree.nodes[i];
                let v = x[n.feature as usize];
                let left = v <= n.threshold || (v.is_nan() && n.missing_left);
                child = if left { n.left } else { n.right };
            }
            raw[tree.class] += tree.leaves[child.leaf()];
        }
        let mut top = f64::NEG_INFINITY;
        for v in &mut raw {
            *v *= self.temperature;
            top = top.max(*v);
        }
        let mut sum = 0.0;
        for v in &mut raw {
            *v = (*v - top).exp();
            sum += *v;
        }
        let mut out = [0.0; 7];
        for (i, v) in raw.iter().enumerate() {
            out[self.classes[i] as usize] = v / sum;
        }
        Probabilities(out)
    }
}
/// Cannot construct an unchecked distribution outside this module.
///
/// ```compile_fail
/// use aqos_classify_rs::Probabilities;
/// let unchecked = Probabilities([f64::NAN; 7]);
/// ```
#[derive(Clone, Debug)]
pub struct Probabilities([f64; 7]);
impl Probabilities {
    pub fn new(values: [f64; 7]) -> Result<Self> {
        if values
            .iter()
            .any(|v| !v.is_finite() || !(0.0..=1.0).contains(v))
            || (values.iter().sum::<f64>() - 1.0).abs() > 1e-6
        {
            return Err("invalid probability distribution".into());
        }
        Ok(Self(values))
    }
    pub fn values(&self) -> &[f64; 7] {
        &self.0
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    ExpectedDamage,
    TopLabel,
}
impl Mode {
    pub fn name(self) -> &'static str {
        match self {
            Self::ExpectedDamage => "expected_damage",
            Self::TopLabel => "top_label",
        }
    }
}
#[derive(Deserialize)]
struct ClassTax {
    tin: Tin,
    dscp: String,
}
#[derive(Deserialize)]
struct TinTax {
    dscp: String,
}
#[derive(Deserialize)]
struct Taxonomy {
    classes: HashMap<Class, ClassTax>,
    tins: HashMap<Tin, TinTax>,
}
pub struct Engine {
    damage: [[f64; 4]; 7],
    class_tins: [Tin; 7],
    class_dscp: [String; 7],
    tin_dscp: [String; 4],
    bulky: [bool; 7],
    mode: Mode,
    bulk_guard: bool,
}
#[derive(Debug)]
pub struct Choice {
    pub tin: Tin,
    pub top: Class,
    pub expected: [f64; 4],
    pub held: bool,
    pub guarded: bool,
}
impl Engine {
    pub fn load(path: impl AsRef<Path>, mode: Mode, bulk_guard: bool) -> Result<Self> {
        #[derive(Deserialize)]
        struct DamageDoc {
            damage: HashMap<Class, HashMap<Tin, f64>>,
        }
        let doc: DamageDoc = serde_saphyr::from_str(&fs::read_to_string(path)?)?;
        let tax: Taxonomy = serde_json::from_str(include_str!("../../../contracts/taxonomy.json"))?;
        let mut damage = [[0.0; 4]; 7];
        let mut class_tins = [Tin::BestEffort; 7];
        let mut class_dscp: [String; 7] = std::array::from_fn(|_| String::new());
        let mut tin_dscp: [String; 4] = std::array::from_fn(|_| String::new());
        let mut bulky = [false; 7];
        for class in CLASSES {
            let i = class as usize;
            let c = tax.classes.get(&class).ok_or("missing taxonomy class")?;
            class_tins[i] = c.tin;
            class_dscp[i] = c.dscp.clone();
            for tin in TINS {
                let d = *doc
                    .damage
                    .get(&class)
                    .and_then(|r| r.get(&tin))
                    .ok_or("incomplete damage table")?;
                if !d.is_finite() || !(0.0..=10.0).contains(&d) {
                    return Err("damage must be finite and in [0, 10]".into());
                }
                damage[i][tin as usize] = d;
            }
            bulky[i] = match mode {
                Mode::TopLabel => c.tin == Tin::Bulk,
                Mode::ExpectedDamage => {
                    damage[i][3] <= damage[i].iter().copied().fold(f64::INFINITY, f64::min)
                }
            };
        }
        for t in TINS {
            tin_dscp[t as usize] = tax.tins.get(&t).ok_or("missing taxonomy tin")?.dscp.clone();
        }
        Ok(Self {
            damage,
            class_tins,
            class_dscp,
            tin_dscp,
            bulky,
            mode,
            bulk_guard,
        })
    }
    pub fn mode(&self) -> Mode {
        self.mode
    }
    pub fn choose(
        &self,
        probs: &Probabilities,
        leased: &[Class],
        rec: Option<&ValidRecord<'_>>,
    ) -> Choice {
        let p = probs.values();
        let mut top = 0;
        for i in 1..7 {
            if p[i] > p[top] {
                top = i;
            }
        }
        let mut lease = [false; 7];
        for &c in leased {
            lease[c as usize] = c.interactive();
        }
        let mut expected = [0.0; 4];
        for t in TINS {
            for i in 0..7 {
                let mut d = self.damage[i][t as usize];
                if lease[i] && matches!(t, Tin::BestEffort | Tin::Bulk) {
                    d *= 5.0;
                }
                expected[t as usize] += p[i] * d;
            }
        }
        let own = self.class_tins[top];
        let mut tin = own;
        if matches!(self.mode, Mode::ExpectedDamage) {
            let voice: f64 = (0..7)
                .filter(|&i| self.class_tins[i] == Tin::Voice)
                .map(|i| p[i])
                .sum();
            let allowed = |t: Tin| t != Tin::Voice || voice > 0.5;
            let lowest = TINS
                .iter()
                .filter(|&&t| allowed(t))
                .map(|&t| expected[t as usize])
                .fold(f64::INFINITY, f64::min);
            let tied = |t: Tin| allowed(t) && expected[t as usize] - lowest <= 0.05;
            if own == Tin::Bulk || !tied(own) {
                // BestEffort is always allowed; at least one finite minimum exists.
                let mut rank = usize::MAX;
                for t in TINS {
                    if tied(t) && t.tie_rank() < rank {
                        tin = t;
                        rank = t.tie_rank();
                    }
                }
            }
        }
        let mut guarded = false;
        let mut held = false;
        if tin == Tin::Bulk {
            let bulk: f64 = (0..7).filter(|&i| self.bulky[i]).map(|i| p[i]).sum();
            guarded = self.bulk_guard
                && rec.is_some_and(|r| r.0.kind == Kind::Initial || r.0.protocol == 17);
            held = bulk <= 0.8 || guarded;
            if held {
                tin = Tin::BestEffort;
            }
        }
        Choice {
            tin,
            top: CLASSES[top],
            expected,
            held,
            guarded,
        }
    }
    pub fn dscp(&self, top: Class, tin: Tin) -> &str {
        if self.class_tins[top as usize] == tin {
            &self.class_dscp[top as usize]
        } else {
            &self.tin_dscp[tin as usize]
        }
    }
    pub fn reason(&self, probs: &Probabilities, c: &Choice, leased: bool) -> String {
        let mut s = String::with_capacity(256);
        self.reason_into(&mut s, probs, c, leased);
        s
    }
    /// Reuse the caller's buffer; formatting into a String is infallible.
    pub fn reason_into(&self, s: &mut String, probs: &Probabilities, c: &Choice, leased: bool) {
        s.clear();
        let own = self.class_tins[c.top as usize];
        write!(
            s,
            "Looks like {} ({:.0}% confident)",
            c.top.plain(),
            (probs.0[c.top as usize] * 100.0).round()
        )
        .expect("String formatting");
        if c.guarded {
            s.push_str("; not demoted to Bulk on its first packets, or while it is UDP, as calls and games are, so it stays in Best Effort.");
        } else if c.held {
            s.push_str("; not sure enough to demote it to Bulk (needs over 80%), so it stays in Best Effort.");
        } else if matches!(self.mode, Mode::TopLabel) {
            write!(s, "; {} is the tin for that class.", c.tin.name()).expect("String formatting");
        } else if c.tin != Tin::Voice && c.expected[0] < c.expected[c.tin as usize] - 0.05 {
            write!(s,
                "; not sure enough that it is a call or a game to put it in Voice (needs over 50%), so {}, the next lowest expected damage.",
                c.tin.name()
            ).expect("String formatting");
        } else if c.tin == own {
            write!(
                s,
                "; the {} tin has the lowest expected damage.",
                c.tin.name()
            )
            .expect("String formatting");
        } else {
            write!(
                s,
                "; {} has the lowest expected damage ({:.2}, against {:.2} for {}).",
                c.tin.name(),
                c.expected[c.tin as usize],
                c.expected[own as usize],
                own.name()
            )
            .expect("String formatting");
        }
        if leased {
            s.push_str(" A priority lease is active for this device.");
        }
    }
}
