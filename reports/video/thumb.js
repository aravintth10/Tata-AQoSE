const puppeteer = require('puppeteer-core');
(async () => {
  const b = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox'] });
  for (const [scale, out] of [[1, 'thumbnail-1280x720.png'], [1.5, 'thumbnail-1920x1080.png']]) {
    const p = await b.newPage();
    p.on('pageerror', e => console.error('PAGE ERROR', e.message));
    await p.setViewport({ width: 1280, height: 720, deviceScaleFactor: scale });
    await p.goto('file://' + __dirname + '/thumb.html', { waitUntil: 'networkidle0' });
    await p.evaluate(async () => { for (const f of ['700 20px "Space Grotesk"', '600 20px "IBM Plex Sans"', '700 20px "IBM Plex Sans"']) await document.fonts.load(f); });
    await p.screenshot({ path: __dirname + '/' + out });
  }
  await b.close();
})();
