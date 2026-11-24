/**
 * login.js — 在应用内置内核里登录一个账号，并把会话写进 sessions.db
 *
 * 用法：
 *   node.exe login.js --email a@b.com [--profile <dir>] [--timeout 900]
 *
 * 依赖环境变量（由面板注入）：
 *   REWARDS_APP_ROOT            应用根
 *   REWARDS_DATA_DIR            数据目录（存 sessions.db）
 *   PLAYWRIGHT_BROWSERS_PATH    内置 chromium 目录
 *
 * 结束时在标准输出打印一行 JSON，供面板解析。
 */
const path = require('path');
const fs = require('fs');
const { DatabaseSync } = require('node:sqlite');

const ROOT = process.env.REWARDS_APP_ROOT || path.resolve(__dirname);
const DATA_DIR = process.env.REWARDS_DATA_DIR || path.join(ROOT, 'data');
const PATCHRIGHT = path.join(ROOT, 'app', 'node_modules', 'patchright');
const { chromium } = require(PATCHRIGHT);

const DB_PATH = path.join(DATA_DIR, 'sessions', 'sessions.db');

function arg(name, fallback) {
  const i = process.argv.indexOf('--' + name);
  if (i > -1 && process.argv[i + 1]) return process.argv[i + 1];
  return fallback;
}

const EMAIL = (arg('email', '') || '').trim();
const PROFILE = arg('profile', path.join(DATA_DIR, 'login-profile'));
const TIMEOUT_MS = Number(arg('timeout', '900')) * 1000;

function saveSession(email, storageState) {
  fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
  const db = new DatabaseSync(DB_PATH);
  db.exec('PRAGMA busy_timeout = 5000');
  db.exec(`CREATE TABLE IF NOT EXISTS sessions (
      email TEXT NOT NULL, platform TEXT NOT NULL, storage_state TEXT,
      fingerprint TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY (email, platform))`);
  const stmt = db.prepare(`INSERT INTO sessions (email, platform, storage_state, updated_at)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(email, platform)
      DO UPDATE SET storage_state = excluded.storage_state, updated_at = excluded.updated_at`);
  const payload = JSON.stringify(storageState);
  const now = Date.now();
  for (const p of ['desktop', 'mobile']) stmt.run(email, p, payload, now);
  db.close();
}

(async () => {
  if (!EMAIL) {
    console.log(JSON.stringify({ ok: false, error: 'missing --email' }));
    process.exit(2);
  }
  fs.mkdirSync(PROFILE, { recursive: true });
  console.log(JSON.stringify({ ok: true, stage: 'launching', email: EMAIL }));

  const ctx = await chromium.launchPersistentContext(PROFILE, {
    headless: false,
    viewport: { width: 1280, height: 860 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    args: ['--disable-blink-features=AutomationControlled', '--no-first-run', '--no-default-browser-check'],
  });
  const page = ctx.pages()[0] || (await ctx.newPage());

  try {
    await page.goto('https://rewards.bing.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (e) { /* 继续等手动操作 */ }

  const deadline = Date.now() + TIMEOUT_MS;
  let ok = false;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 3000));
    let url = '';
    try { url = page.url(); } catch { break; }
    if (url.includes('login.live.com') || url.includes('login.microsoftonline.com')) continue;
    if (url.includes('rewards.bing.com')) {
      const cookies = await ctx.cookies();
      if (cookies.some(c => /ESTSAUTH|ESTSAUTHPERSISTENT|__Host-MSAAUTH|RPSSecAuth/i.test(c.name))) {
        ok = true;
        break;
      }
    }
  }

  if (!ok) {
    try { await ctx.close(); } catch (e) { }
    console.log(JSON.stringify({ ok: false, error: 'timeout', email: EMAIL }));
    process.exit(3);
  }

  const state = await ctx.storageState();
  saveSession(EMAIL, state);
  console.log(JSON.stringify({ ok: true, stage: 'saved', email: EMAIL, cookies: state.cookies.length }));
  await new Promise(r => setTimeout(r, 1500));
  try { await ctx.close(); } catch (e) { }
  process.exit(0);
})().catch(e => {
  console.log(JSON.stringify({ ok: false, error: String(e.message || e).slice(0, 200) }));
  process.exit(1);
});
