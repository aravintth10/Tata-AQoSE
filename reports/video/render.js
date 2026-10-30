// node render.js preview t1,t2,...   -> PNGs in preview/
// node render.js video out.mp4 [fps]  -> full video
const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');
const fs = require('fs');
(async () => {
  const [mode, arg, fpsArg] = process.argv.slice(2);
  const b = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox', '--force-device-scale-factor=1', '--hide-scrollbars'] });
  const p = await b.newPage();
  p.on('pageerror', e => console.error('PAGE ERROR', e.message));
  p.on('console', m => { if (m.type() === 'error') console.error('CONSOLE', m.text()); });
  await p.setViewport({ width: 1920, height: 1080 });
  await p.goto('file://' + __dirname + '/' + (process.env.PAGE || 'aqose.html'), { waitUntil: 'networkidle0' });
  await p.evaluate(async () => { for (const f of ['700 20px "Space Grotesk"', '500 20px "Space Grotesk"', '400 20px "IBM Plex Sans"', '600 20px "IBM Plex Sans"', '700 20px "IBM Plex Sans"', '500 20px "IBM Plex Mono"']) await document.fonts.load(f); await document.fonts.ready; });
  if (process.env.DEEP) await p.evaluate(tj => window.setTiming(tj), JSON.parse(fs.readFileSync(process.env.DEEP)));
  console.log('fonts', await p.evaluate(() => [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family + ' ' + f.weight).join(', ')));
  if (mode === 'preview') {
    fs.mkdirSync(__dirname + '/preview', { recursive: true });
    for (const t of arg.split(',').map(Number)) {
      await p.evaluate(t => window.render(t), t);
      await p.screenshot({ path: `${__dirname}/preview/t${String(t).padStart(6, '0')}.png` });
    }
  } else {
    const fps = +(fpsArg || 30);
    // Optional narration timing: real time -> animation time, scene by scene.
    const tj = process.env.TIMING ? JSON.parse(fs.readFileSync(process.env.TIMING)) : null;
    const dur = process.env.DEEP ? JSON.parse(fs.readFileSync(process.env.DEEP)).duration : tj ? tj.duration : await p.evaluate(() => window.DUR), n = Math.round(dur * fps);
    const mapT = tau => { if (!tj) return tau; for (const s of tj.segments) if (tau < s.B) return s.a + (tau - s.A) / s.k; const s = tj.segments[tj.segments.length - 1]; return Math.min(s.b, s.a + (tau - s.A) / s.k); };
    const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-i', '-', '-vf', 'scale=in_range=full:out_range=limited:in_color_matrix=bt601:out_color_matrix=bt709,format=yuv420p', '-color_range', 'tv', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-c:v', 'libopenh264', '-b:v', process.env.BITRATE || '10M', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', arg], { stdio: ['pipe', 'inherit', 'inherit'] });
    const t0 = Date.now();
    for (let f = 0; f < n; f++) {
      await p.evaluate(t => window.render(t), mapT(f / fps));
      const buf = await p.screenshot({ type: 'jpeg', quality: 93 });
      if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
      if (f % 300 === 0) console.log(`frame ${f}/${n}  ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    }
    ff.stdin.end();
    await new Promise(r => ff.on('close', r));
  }
  await b.close();
})();
