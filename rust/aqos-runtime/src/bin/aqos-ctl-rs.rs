use aqos_runtime_rs::{
    Result, Tin,
    enforce::{Enforcer, LabelResult},
    extract::Key,
    mark_tin,
    netfilter::no_entry,
};
fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let a: Vec<&str> = args.iter().map(String::as_str).collect();
    if let ["heartbeat", path] = a.as_slice() {
        loop {
            std::fs::write(path, b"alive\n")?;
            std::thread::sleep(std::time::Duration::from_millis(250));
        }
    }
    let mut e = Enforcer::open("wan0", "ifb0")?;
    e.test_fault = std::env::var("AQOS_ENFORCE_FAULT").ok();
    match a.as_slice(){
        ["marking",op]=>{match *op{"install"=>e.install_marking()?,"remove"=>e.remove_marking()?,"status"=>{},_=>return Err("marking install|remove|status".into())}println!("{}",if e.marking_installed()?{"installed"}else{"not installed"});},
        ["rate",dir]=>println!("{}",e.rate(dir)?),
        ["rate",dir,kbit]=>{e.set_rate(dir,kbit.parse()?)?;println!("{}",e.rate(dir)?);},
        ["label",proto,src,dst,..] if a.len()==4||a.len()==5=>{
            let key=Key{protocol:match *proto{"tcp"=>6,"udp"=>17,_=>return Err("protocol must be tcp or udp".into())},a:src.parse()?,b:dst.parse()?};
            if a.len()==5{let tin=if a[4]=="none"{None}else{Some(serde_json::from_value::<Tin>(a[4].into())?)};if e.set_label(key,tin)?==LabelResult::Gone{return Err(std::io::Error::from_raw_os_error(2).into());}}
            let entry=e.ct.get(key)?.ok_or_else(||std::io::Error::from_raw_os_error(2))?;
            println!("{}",mark_tin(entry.mark).map(|v|serde_json::to_value(v).expect("tin").as_str().expect("tin string").to_owned()).unwrap_or("none".into()));
        },
        ["flows"] => e.ct.dump(|entry| {
            let protocol = match entry.key.protocol { 6 => "tcp", 17 => "udp", _ => "other" };
            let tin = mark_tin(entry.mark).map(|t| serde_json::to_value(t).expect("tin").as_str().expect("tin string").to_owned()).unwrap_or_else(|| "none".into());
            println!("{protocol:<4} {:<28} -> {:<28} {tin:<11} mark {} up {} pkts, down {} pkts", entry.key.a, entry.key.b, entry.mark, entry.orig_pkts, entry.reply_pkts);
        })?,
        ["tins"]=>{let mut out=serde_json::Map::new();for dir in ["up","down"]{let cake=e.cake(dir)?;let tins=cake["tins"].as_array().ok_or("CAKE tins missing")?;if tins.len()!=4{return Err("CAKE requires diffserv4".into());}let mut v=serde_json::Map::new();for (name,tin) in ["bulk","best_effort","video","voice"].into_iter().zip(tins){v.insert(name.into(),tin["sent_packets"].clone());}out.insert(dir.into(),v.into());}println!("{}",serde_json::Value::Object(out));},
        _=>return Err("usage: aqos-ctl-rs marking install|remove|status; rate up|down [kbit]; label tcp|udp src dst [tin|none]; flows; tins; heartbeat file".into()),
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        if no_entry(&*e) {
            eprintln!("aqos-ctl-rs: the flow has ended");
        } else {
            eprintln!("aqos-ctl-rs: {e}");
        }
        std::process::exit(if no_entry(&*e) {
            3
        } else if e.to_string().contains("read-back mismatch") && e.to_string().contains("previous")
        {
            4
        } else {
            1
        });
    }
}
