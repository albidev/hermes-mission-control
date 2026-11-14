import assert from 'node:assert/strict';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const playwrightModule = process.env.PLAYWRIGHT_MODULE;
assert.ok(playwrightModule, 'PLAYWRIGHT_MODULE must point to installed read-only playwright-core');
const { chromium } = await import(pathToFileURL(playwrightModule));
const root = process.cwd();
const cache = mkdtempSync(path.join(tmpdir(), 'room-invite-browser-'));
const server = await createServer({ root, configFile: false, logLevel: 'silent', plugins: [react()], server: { host: '127.0.0.1', port: 0 } });
await server.listen();
const address = server.httpServer.address();
const base = `http://127.0.0.1:${address.port}`;
const result = { base, checks: [], errors: [], blocked: [] };
const check = (value, name, detail = {}) => { assert.ok(value, name); result.checks.push({ name, detail }); };
let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--disable-background-networking', '--disable-component-update', '--no-first-run'], env: { PATH: process.env.PATH, HOME: cache } });
  const page = await browser.newPage({ viewport: { width: 320, height: 568 }, hasTouch: true });
  page.on('pageerror', error => result.errors.push(String(error)));
  await page.route('**/*', route => { const url = new URL(route.request().url()); if (url.origin !== base) { result.blocked.push(url.href); return route.abort(); } return route.continue(); });
  await page.goto(`${base}/tests/fixtures/room-invite-panel/index.html`);
  await page.getByRole('textbox').fill('Synthetic recipient');
  await page.getByRole('button', { name: 'Issue invite', exact: true }).click();
  await page.locator('.invite-code').waitFor();
  for (const [width, height] of [[320, 568], [568, 320]]) {
    await page.setViewportSize({ width, height });
    const geometry = await page.evaluate(() => {
      const panel = document.querySelector('section.invite-panel');
      const code = document.querySelector('.invite-code');
      const copy = document.querySelector('.invite-issue .chat-control');
      const close = panel.querySelector('header button:last-child');
      const box = node => { const r = node.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, width: r.width, height: r.height }; };
      const textRects = [...copy.childNodes].filter(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()).flatMap(node => { const range = document.createRange(); range.selectNodeContents(node); return [...range.getClientRects()].map(rect => ({ x: rect.x, right: rect.right, y: rect.y, bottom: rect.bottom })); });
      return { panel: box(panel), code: { ...box(code), clientWidth: code.clientWidth, scrollWidth: code.scrollWidth }, copy: box(copy), close: box(close), textContained: textRects.every(rect => rect.x >= copy.getBoundingClientRect().x - 1 && rect.right <= copy.getBoundingClientRect().right + 1), innerOverflow: [...panel.querySelectorAll('*')].filter(node => node.clientWidth && node.scrollWidth > node.clientWidth + 1).map(node => node.className) };
    });
    check(geometry.copy.width >= 44 && geometry.copy.height >= 44, `${width}x${height}: copy hit target >=44`, geometry.copy);
    check(geometry.close.width >= 44 && geometry.close.height >= 44 && geometry.close.right <= width + 1 && geometry.close.y >= -1, `${width}x${height}: close hit target visible`, geometry.close);
    check(geometry.code.scrollWidth <= geometry.code.clientWidth + 1, `${width}x${height}: code inner width contained`, geometry.code);
    check(geometry.textContained, `${width}x${height}: copy label contained`, geometry);
    check(geometry.innerOverflow.length === 0, `${width}x${height}: no inner horizontal overflow`, geometry.innerOverflow);
  }
  check((await page.locator('section[aria-label]').count()) === 1, 'actual RoomInvitePanel mounted');
  check(result.errors.length === 0, 'zero browser page errors', result.errors);
  check(result.blocked.length === 0, 'zero external network requests', result.blocked);
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.failure = String(error.stack || error);
  throw error;
} finally {
  if (browser) await browser.close();
  await server.close();
  rmSync(cache, { recursive: true, force: true });
  console.log(JSON.stringify({ passed: result.passed, checks: result.checks.length, errors: result.errors, blocked: result.blocked, failure: result.failure }, null, 2));
}
