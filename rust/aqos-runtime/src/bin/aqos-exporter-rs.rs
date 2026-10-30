use aqos_runtime_rs::{Result, args::Args, exporter::Tail};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
struct Client {
    stream: TcpStream,
    request: Vec<u8>,
    response: Option<Arc<[u8]>>,
    written: usize,
    until: Instant,
}
fn run() -> Result<()> {
    let args = Args::parse(&["file", "listen"], &[])?;
    let mut tail = Tail::new(args.get("file", "results/current/metrics.jsonl").into());
    let address = args.get("listen", ":9108");
    let address = if address.starts_with(':') {
        format!("0.0.0.0{address}")
    } else {
        address.to_owned()
    };
    let listener = TcpListener::bind(address)?;
    listener.set_nonblocking(true)?;
    let stop = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
        signal_hook::flag::register(sig, stop.clone())?;
    }
    let mut clients: Vec<Client> = Vec::new();
    let mut next = Instant::now();
    let mut response: Arc<[u8]> = Arc::from([]);
    while !stop.load(Ordering::Relaxed) {
        if Instant::now() >= next {
            tail.poll()?;
            let body = tail.store.render();
            response=Arc::from(format!("HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).into_bytes());
            next = Instant::now() + Duration::from_millis(250);
        }
        for _ in 0..16 {
            match listener.accept() {
                Ok((stream, _)) => {
                    stream.set_nonblocking(true)?;
                    if clients.len() < 16 {
                        clients.push(Client {
                            stream,
                            request: Vec::new(),
                            response: None,
                            written: 0,
                            until: Instant::now() + Duration::from_secs(2),
                        });
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e.into()),
            }
        }
        clients.retain_mut(|c| {
            if Instant::now()>=c.until{return false;}
            if c.response.is_none() {let mut buffer=[0;2048];match c.stream.read(&mut buffer){Ok(0)=>return false,Ok(n)=>c.request.extend_from_slice(&buffer[..n]),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>return true,Err(_)=>return false};
                if c.request.len()>8192{return false;}
                let mut headers=[httparse::EMPTY_HEADER;32];let mut request=httparse::Request::new(&mut headers);
                match request.parse(&c.request) {Ok(httparse::Status::Complete(_))=>{c.response=Some(if request.method==Some("GET")&&request.path==Some("/metrics"){response.clone()}else{Arc::from(&b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"[..])});},Ok(httparse::Status::Partial)=>return true,Err(_)=>return false}
            }
            let data=c.response.as_ref().expect("parsed above");
            let end=(c.written+65536).min(data.len());
            match c.stream.write(&data[c.written..end]){Ok(0)=>false,Ok(n)=>{c.written+=n;c.written<data.len()},Err(e)=>e.kind()==std::io::ErrorKind::WouldBlock}
        });
        std::thread::sleep(Duration::from_millis(10));
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("aqos-exporter-rs: {e}");
        std::process::exit(1);
    }
}
