use crate::Result;
use std::collections::HashMap;
pub struct Args {
    values: HashMap<String, String>,
}
impl Args {
    pub fn parse(values: &[&str], bools: &[&str]) -> Result<Self> {
        let mut args = std::env::args().skip(1);
        let mut found = HashMap::new();
        while let Some(arg) = args.next() {
            if !arg.starts_with('-') {
                return Err(format!("unexpected argument: {arg}").into());
            }
            let (name, val) = arg
                .trim_start_matches('-')
                .split_once('=')
                .map_or((arg.trim_start_matches('-'), None), |(k, v)| (k, Some(v)));
            let value = if bools.contains(&name) {
                val.unwrap_or("true").to_owned()
            } else if values.contains(&name) {
                match val {
                    Some(v) => v.to_owned(),
                    None => args
                        .next()
                        .ok_or_else(|| format!("missing value for {name}"))?,
                }
            } else {
                return Err(format!("unknown flag: {name}").into());
            };
            // Match Go flag semantics: later options override earlier defaults.
            found.insert(name.to_owned(), value);
        }
        Ok(Self { values: found })
    }
    pub fn get<'a>(&'a self, name: &str, default: &'a str) -> &'a str {
        self.values.get(name).map_or(default, String::as_str)
    }
    pub fn number<T: std::str::FromStr>(&self, name: &str, default: T) -> Result<T> {
        match self.values.get(name) {
            None => Ok(default),
            Some(v) => v.parse().map_err(|_| format!("invalid {name}: {v}").into()),
        }
    }
    pub fn boolean(&self, name: &str, default: bool) -> Result<bool> {
        self.number(name, default)
    }
}
