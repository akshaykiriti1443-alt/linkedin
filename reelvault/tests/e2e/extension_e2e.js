// End-to-end: real Chromium + the ReelVault Connector extension + the app.
// Run:  python tests/e2e/run_app.py /tmp/rvhome &   then   node tests/e2e/extension_e2e.js /tmp/rvhome
const { chromium } = require('playwright');
const os = require('os'), path = require('path'), fs = require('fs');
const home = process.argv[2];
const ext = path.join(home, 'chrome-extension');
const API = 'http://127.0.0.1:8765';
const st = async () => (await fetch(API + '/api/status')).json();
const post = async (p) => (await fetch(API + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
const until = async (f, ms = 20000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await f()) return true; await new Promise(r => setTimeout(r, 500)); } return false; };
const check = (ok, what) => { console.log((ok ? 'PASS ' : 'FAIL ') + what); if (!ok) process.exitCode = 1; };
(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'rv-chrome-'));
  const ctx = await chromium.launchPersistentContext(profile, { channel: 'chromium', headless: true,
    args: ['--disable-extensions-except=' + ext, '--load-extension=' + ext] });
  check(await until(async () => (await st()).extension.seen), 'extension checks in with the app');
  check((await post('/api/ig/connect')).ok, 'Connect Instagram starts waiting');
  await ctx.addCookies([   // what Instagram sets when you finish logging in
    { name: 'sessionid', value: 'S', domain: '.instagram.com', path: '/', secure: true, httpOnly: true },
    { name: 'ds_user_id', value: '42', domain: '.instagram.com', path: '/', secure: true },
    { name: 'csrftoken', value: 'tok', domain: '.instagram.com', path: '/', secure: true }]);
  check(await until(async () => (await st()).instagram.connected), 'login reaches the app');
  const s = await st();
  check(s.instagram.username === 'scaleitads' && !s.igWaiting, 'username saved and waiting stops');
  await ctx.close();
  await post('/api/quit');
})().catch(e => { console.error(e); process.exit(1); });
