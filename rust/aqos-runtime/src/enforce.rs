//! Verified kernel updates. Only AQoSE's low three mark bits are owned here.
use crate::{
    Result, Tin,
    command::run,
    extract::Key,
    netfilter::{Conntrack, no_entry},
    tin_mark,
};
use serde_json::Value;
pub const MASK: u32 = 7;
pub struct Enforcer {
    pub wan: String,
    pub ifb: String,
    pub ct: Conntrack,
    pub test_fault: Option<String>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LabelResult {
    Applied,
    Gone,
}
fn valid_dev(dev: &str) -> bool {
    !dev.is_empty()
        && dev.len() < 16
        && dev
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-.".contains(&c))
}
impl Enforcer {
    pub fn open(wan: &str, ifb: &str) -> Result<Self> {
        if !valid_dev(wan) || !valid_dev(ifb) || wan == ifb {
            return Err("invalid WAN/IFB interface".into());
        }
        Ok(Self {
            wan: wan.into(),
            ifb: ifb.into(),
            ct: Conntrack::open()?,
            test_fault: None,
        })
    }
    fn dev(&self, dir: &str) -> Result<&str> {
        match dir {
            "up" => Ok(&self.wan),
            "down" => Ok(&self.ifb),
            _ => Err("direction must be up or down".into()),
        }
    }
    pub fn cake(&self, dir: &str) -> Result<Value> {
        let dev = self.dev(dir)?;
        let out = run("tc", &["-j", "-s", "qdisc", "show", "dev", dev], None)?;
        let all: Vec<Value> = serde_json::from_slice(&out)?;
        all.into_iter()
            .find(|q| q["kind"] == "cake" && q["root"] == true)
            .ok_or_else(|| format!("{dev} has no root CAKE qdisc").into())
    }
    pub fn rate(&self, dir: &str) -> Result<i64> {
        let q = self.cake(dir)?;
        let n = q["options"]["bandwidth"]
            .as_f64()
            .ok_or("missing bandwidth")?
            * 8.0
            / 1000.0;
        if !n.is_finite() || n < 1.0 || n > i64::MAX as f64 {
            return Err("invalid CAKE bandwidth".into());
        }
        Ok(n.round() as i64)
    }
    fn write_rate(&self, dir: &str, kbit: i64) -> Result<()> {
        run(
            "tc",
            &[
                "qdisc",
                "change",
                "dev",
                self.dev(dir)?,
                "root",
                "cake",
                "bandwidth",
                &format!("{kbit}kbit"),
            ],
            None,
        )?;
        Ok(())
    }
    pub fn set_rate(&self, dir: &str, kbit: i64) -> Result<()> {
        if kbit < 1 {
            return Err("rate must be positive".into());
        }
        let before = self.rate(dir)?;
        let change = self.write_rate(dir, kbit).and_then(|()| {
            if self.test_fault.as_deref() == Some("rate") {
                self.write_rate(dir, 1234)?;
            }
            if self.rate(dir)? == kbit {
                Ok(())
            } else {
                Err("rate read-back mismatch".into())
            }
        });
        if let Err(e) = change {
            self.write_rate(dir, before)
                .map_err(|r| format!("{e}; restoring {before} kbit failed: {r}"))?;
            if self.rate(dir)? != before {
                return Err(format!("{e}; restored rate could not be verified").into());
            }
            return Err(format!("{e}; previous rate restored").into());
        }
        Ok(())
    }
    pub fn set_label(&mut self, key: Key, tin: Option<Tin>) -> Result<LabelResult> {
        self.set_owned_mark(key, tin_mark(tin))
    }
    pub fn set_owned_mark(&mut self, key: Key, want: u32) -> Result<LabelResult> {
        let Some(before) = self.ct.get(key)? else {
            return Ok(LabelResult::Gone);
        };
        let change = self.ct.set_mark(key, want, MASK);
        if change.as_ref().is_err_and(|e| no_entry(&**e)) {
            return Ok(LabelResult::Gone);
        }
        let verified = change.and_then(|()| {
            if self.test_fault.as_deref() == Some("label") {
                self.ct.set_mark(key, 1, MASK)?;
            }
            match self.ct.get(key)? {
                None => Ok(LabelResult::Gone),
                Some(after)
                    if after.mark & MASK == want & MASK
                        && after.mark & !MASK == before.mark & !MASK =>
                {
                    Ok(LabelResult::Applied)
                }
                _ => Err("mark read-back mismatch".into()),
            }
        });
        match verified {
            Ok(v) => Ok(v),
            Err(e) => {
                if let Err(r) = self.ct.set_mark(key, before.mark, MASK) {
                    if no_entry(&*r) {
                        return Ok(LabelResult::Gone);
                    }
                    return Err(format!("{e}; restoring owned mark bits failed: {r}").into());
                }
                match self.ct.get(key)? {
                    None => Ok(LabelResult::Gone),
                    Some(r) if r.mark & MASK == before.mark & MASK => {
                        Err(format!("{e}; previous owned mark restored").into())
                    }
                    _ => Err(format!("{e}; mark restoration could not be verified").into()),
                }
            }
        }
    }
    fn ingress(&self, mark: bool) -> Result<()> {
        // matchall rejects in-place replacement on supported kernels. Keep
        // B2's priority-10 redirect intact; our priority-9 redirect shadows it.
        // Removing our filter immediately exposes the working plain redirect.
        let filters: Vec<Value> = serde_json::from_slice(&run(
            "tc",
            &["-j", "filter", "show", "dev", &self.wan, "parent", "ffff:"],
            None,
        )?)?;
        if filters.iter().any(|f| f["pref"] == 9) {
            run(
                "tc",
                &[
                    "filter", "del", "dev", &self.wan, "parent", "ffff:", "prio", "9",
                ],
                None,
            )?;
        }
        let baseline = filters
            .iter()
            .find(|f| f["pref"] == 10)
            .ok_or("B2 priority-10 ingress redirect is missing")?;
        if baseline.to_string().contains("ctinfo") {
            return Err("legacy ctinfo at priority 10: restore B2 before migrating".into());
        }
        if mark {
            run(
                "tc",
                &[
                    "filter", "add", "dev", &self.wan, "parent", "ffff:", "protocol", "all",
                    "prio", "9", "matchall", "action", "ctinfo", "cpmark", "0x7", "action",
                    "mirred", "egress", "redirect", "dev", &self.ifb,
                ],
                None,
            )?;
        }
        Ok(())
    }
    fn fwmark(&self, dir: &str, mask: &str) -> Result<()> {
        crate::netfilter::cake_fwmark(
            self.dev(dir)?,
            u32::from_str_radix(mask.trim_start_matches("0x"), 16)?,
        )
    }
    fn mark_rules(&self) -> String {
        format!(
            "table inet aqos_mark {{\n chain postrouting {{\n type filter hook postrouting priority mangle;\n oifname \"{}\" meta mark set (meta mark & 0xfffffff8) | (ct mark & 0x7)\n }}\n}}\n",
            self.wan
        )
    }
    fn nft_present() -> Result<bool> {
        let v: Value = serde_json::from_slice(&run("nft", &["-j", "list", "tables"], None)?)?;
        Ok(v["nftables"]
            .as_array()
            .ok_or("missing nftables list")?
            .iter()
            .any(|v| v["table"]["family"] == "inet" && v["table"]["name"] == "aqos_mark"))
    }
    fn mask(&self, dir: &str) -> Result<u32> {
        let q = self.cake(dir)?;
        let v = &q["options"]["fwmark"];
        if v.is_null() {
            return Ok(0);
        }
        if let Some(n) = v.as_u64() {
            return Ok(n as u32);
        }
        let s = v.as_str().ok_or("invalid fwmark")?;
        Ok(if let Some(hex) = s.strip_prefix("0x") {
            u32::from_str_radix(hex, 16)?
        } else {
            s.parse()?
        })
    }
    pub fn marking_installed(&self) -> Result<bool> {
        let filter = run(
            "tc",
            &["filter", "show", "dev", &self.wan, "parent", "ffff:"],
            None,
        )?;
        Ok(Self::nft_present()?
            && String::from_utf8_lossy(&filter).contains("ctinfo")
            && self.mask("up")? == MASK
            && self.mask("down")? == MASK)
    }
    pub fn install_marking(&mut self) -> Result<()> {
        self.rate("up")?;
        self.rate("down")?;
        // Disable stale readers before clearing marks left by a previous process.
        self.remove_marking()?;
        let change = (|| -> Result<()> {
            let mut stale = Vec::new();
            self.ct.dump(|e| {
                if e.mark & MASK != 0 {
                    stale.push(e.key);
                }
            })?;
            for key in stale {
                self.set_label(key, None)?;
            }
            run("nft", &["-f", "-"], Some(&self.mark_rules()))?;
            self.ingress(true)?;
            self.fwmark("up", "0x7")?;
            self.fwmark("down", "0x7")?;
            if !self.marking_installed()? {
                return Err("marking installation read-back mismatch".into());
            }
            Ok(())
        })();
        if let Err(e) = change {
            if let Err(r) = self.remove_marking() {
                return Err(format!("{e}; cleanup failed: {r}").into());
            }
            return Err(e);
        }
        Ok(())
    }
    /// Attempt every cleanup step, then verify every component independently.
    pub fn remove_marking(&self) -> Result<()> {
        let mut errors = Vec::new();
        match Self::nft_present() {
            Ok(true) => {
                if let Err(e) = run("nft", &["delete", "table", "inet", "aqos_mark"], None) {
                    errors.push(e.to_string());
                }
            }
            Ok(false) => {}
            Err(e) => errors.push(e.to_string()),
        }
        for result in [
            self.ingress(false),
            self.fwmark("up", "0x0"),
            self.fwmark("down", "0x0"),
        ] {
            if let Err(e) = result {
                errors.push(e.to_string());
            }
        }
        let verify = (|| -> Result<()> {
            if Self::nft_present()? {
                return Err("AQoSE marking rule still installed".into());
            }
            let bytes = run(
                "tc",
                &["filter", "show", "dev", &self.wan, "parent", "ffff:"],
                None,
            )?;
            let filter = String::from_utf8_lossy(&bytes);
            if filter.contains("ctinfo")
                || !filter.contains(&self.ifb)
                || !filter.contains("mirred")
            {
                return Err("plain IFB redirect was not restored".into());
            }
            for dir in ["up", "down"] {
                if self.mask(dir)? != 0 {
                    return Err(format!("{dir} CAKE fwmark still enabled").into());
                }
            }
            Ok(())
        })();
        if let Err(e) = verify {
            errors.push(e.to_string());
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; ").into())
        }
    }
}
