use aqos_runtime_rs::Result;
use std::{
    net::{SocketAddr, UdpSocket},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
fn run() -> Result<()> {
    let addresses = std::env::args()
        .skip(1)
        .map(|s| s.parse::<SocketAddr>())
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if addresses.is_empty() || addresses.len() > 32 {
        return Err("usage: aqos-reflector-rs <address:port>... (at most 32)".into());
    }
    let mut sockets = Vec::new();
    for addr in addresses {
        let s = UdpSocket::bind(addr)?;
        s.set_nonblocking(true)?;
        sockets.push(s);
    }
    let stop = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, stop.clone())?;
    }
    let mut poll: Vec<_> = sockets
        .iter()
        .map(|s| rustix::event::PollFd::new(s, rustix::event::PollFlags::IN))
        .collect();
    let timeout = rustix::event::Timespec {
        tv_sec: 0,
        tv_nsec: 100_000_000,
    };
    let mut buf = [0; 2048];
    while !stop.load(Ordering::Relaxed) {
        match rustix::event::poll(&mut poll, Some(&timeout)) {
            Ok(_) => {}
            Err(rustix::io::Errno::INTR) => continue,
            Err(e) => return Err(e.into()),
        }
        for (i, fd) in poll.iter().enumerate() {
            if !fd.revents().contains(rustix::event::PollFlags::IN) {
                continue;
            }
            for _ in 0..64 {
                match sockets[i].recv_from(&mut buf) {
                    Ok((n, from)) => {
                        let _ = sockets[i].send_to(&buf[..n], from);
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                    Err(e) => return Err(e.into()),
                }
            }
        }
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("aqos-reflector-rs: {e}");
        std::process::exit(1);
    }
}
