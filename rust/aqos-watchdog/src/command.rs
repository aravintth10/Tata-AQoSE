//! Small bounded subprocess runner for tc/nft. No shell expansion.
use crate::Result;
use std::{
    io::{Read, Write},
    os::unix::process::CommandExt,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
pub fn run(name: &str, args: &[&str], input: Option<&str>) -> Result<Vec<u8>> {
    run_timeout(name, args, input, Duration::from_secs(2))
}
pub fn run_timeout(
    name: &str,
    args: &[&str],
    input: Option<&str>,
    timeout: Duration,
) -> Result<Vec<u8>> {
    let mut child = Command::new(name)
        .args(args)
        .process_group(0)
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let pid = rustix::process::Pid::from_raw(child.id() as i32).ok_or("invalid child PID")?;
    let stdout = child.stdout.take().ok_or("missing stdout")?;
    let stderr = child.stderr.take().ok_or("missing stderr")?;
    let collect = |mut stream: Box<dyn Read + Send>| -> std::io::Result<Vec<u8>> {
        let mut out = Vec::new();
        let mut buf = [0; 4096];
        loop {
            let n = stream.read(&mut buf)?;
            if n == 0 {
                break;
            }
            if out.len() < 8 << 20 {
                out.extend_from_slice(&buf[..n.min((8 << 20) - out.len())]);
            }
        }
        Ok(out)
    };
    let out = thread::spawn(move || collect(Box::new(stdout)));
    let err = thread::spawn(move || collect(Box::new(stderr)));
    let stdin = child.stdin.take();
    let input = input.map(str::to_owned);
    let writer = thread::spawn(move || -> std::io::Result<()> {
        if let (Some(mut fd), Some(s)) = (stdin, input) {
            fd.write_all(s.as_bytes())?;
        }
        Ok(())
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => {}
            Err(e) => break Err(e),
        }
        if Instant::now() >= deadline {
            break Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "command deadline exceeded",
            ));
        }
        thread::sleep(Duration::from_millis(2));
    };
    // Also close inherited pipes held by descendants after the leader exits.
    let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::KILL);
    let _ = child.wait();
    let stdout = out.join().map_err(|_| "stdout reader panicked")??;
    let stderr = err.join().map_err(|_| "stderr reader panicked")??;
    let input_result = writer.join().map_err(|_| "stdin writer panicked")?;
    let status = status.map_err(|e| format!("{name}: {e}"))?;
    if !status.success() {
        return Err(format!(
            "{name} {}: {status}: {}",
            args.join(" "),
            String::from_utf8_lossy(&stderr).trim()
        )
        .into());
    }
    input_result?;
    Ok(stdout)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timeout_kills_children_and_closes_pipes() {
        let t = Instant::now();
        assert!(
            run_timeout(
                "sh",
                &["-c", "sleep 10 & wait"],
                None,
                Duration::from_millis(30)
            )
            .is_err()
        );
        assert!(t.elapsed() < Duration::from_secs(1));
    }
    #[test]
    fn communicates() {
        assert_eq!(run("cat", &[], Some("hello\n")).unwrap(), b"hello\n");
    }
}
