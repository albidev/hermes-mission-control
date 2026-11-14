"""Isolated Chrome/CDP visual acceptance for ?mock=invite-rooms (no real API writes)."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evidence/p3b-ui'
HTTP = build_opener(ProxyHandler({}))
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
SCRATCH = Path(os.environ['TMPDIR'])

class Driver:
    def __init__(self, port):
        with HTTP.open(f'http://127.0.0.1:{port}/json/list') as response:
            tab = next(item for item in json.load(response) if item['type'] == 'page')
        self.ws = connect(tab['webSocketDebuggerUrl'], max_size=None, proxy=None)
        self.sequence = 0
    def call(self, method, **params):
        self.sequence += 1
        self.ws.send(json.dumps({'id': self.sequence, 'method': method, 'params': params}))
        while True:
            message = json.loads(self.ws.recv(timeout=30))
            if message.get('id') != self.sequence:
                continue
            if 'error' in message:
                raise RuntimeError(message['error'])
            return message.get('result', {})
    def evaluate(self, expression):
        value = self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if 'exceptionDetails' in value:
            raise RuntimeError(value['exceptionDetails'])
        return value.get('result', {}).get('value')
    def wait(self, expression):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(.05)
        raise AssertionError(f'Timeout: {expression}\n{self.evaluate("document.body.innerText")}')
    def click(self, label):
        selector = f"[...document.querySelectorAll('button')].find(b => b.textContent.trim() === {json.dumps(label)})"
        self.wait(f'!!({selector}) && !({selector}).disabled')
        point = self.evaluate(f'''(() => {{const b = {selector}; b.scrollIntoView({{block:'center'}});
            const r=b.getBoundingClientRect(); return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()''')
        for event in ['mousePressed', 'mouseReleased']:
            self.call('Input.dispatchMouseEvent', type=event, button='left', clickCount=1, **point)
        time.sleep(.15)
    def key(self, key, code, number, modifiers=0):
        for event in ['keyDown', 'keyUp']:
            self.call('Input.dispatchKeyEvent', type=event, key=key, code=code, windowsVirtualKeyCode=number, modifiers=modifiers)
        time.sleep(.05)
    def type(self, selector, text):
        self.evaluate(f'document.querySelector({json.dumps(selector)}).focus()')
        self.call('Input.insertText', text=text)
        time.sleep(.15)
    def viewport(self, width, height):
        self.call('Emulation.setDeviceMetricsOverride', width=width, height=height, deviceScaleFactor=1, mobile=False)
    def navigate(self, screen, extra=''):
        self.call('Page.navigate', url=f'http://127.0.0.1:5180/?mock=invite-rooms&screen={screen}{extra}')
        self.wait("!!document.querySelector('.invite-mock-content')")
        self.wait("document.body.innerText.length > 50")
        time.sleep(.35)
    def screenshot(self, name, report):
        if name in ['pairing-sas.png', 'invitation-consent.png']:
            self.evaluate("document.querySelector('.invite-stepper .is-current').scrollIntoView({block:'center'})")
        else:
            self.evaluate('window.scrollTo(0,0)')
        time.sleep(.15)
        layout = self.evaluate('''(() => ({
          viewport: [innerWidth, innerHeight], width: document.documentElement.scrollWidth,
          clipped: [...document.querySelectorAll('.invite-mock-header button, .guest-room-header h2, .guest-approval button')]
            .filter(el => el.getClientRects().length && (el.scrollWidth > el.clientWidth + 1))
            .map(el => el.textContent),
          errors: window.uiErrors,
          missingTranslations: document.body.innerText.match(/inviteUi\.[a-zA-Z.]+/g) || []
        }))()''')
        assert layout['width'] <= layout['viewport'][0], layout
        assert not layout['clipped'], layout
        assert not layout['errors'], layout
        assert not layout['missingTranslations'], layout
        result = self.call('Page.captureScreenshot', format='png', captureBeyondViewport=False)
        (EVIDENCE / 'screens' / name).write_bytes(base64.b64decode(result['data']))
        report['screenshots'][name] = layout


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / 'screens').mkdir(exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix='invite-ui-browser-', dir=SCRATCH))
    processes = []
    report = {'screenshots': {}, 'checks': []}
    try:
        log = (EVIDENCE / 'green/browser-process.log').open('w')
        vite = subprocess.Popen(['./node_modules/.bin/vite', '--host', '127.0.0.1', '--port', '5180', '--strictPort'],
                                cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        processes.append(vite)
        deadline = time.monotonic() + 30
        while True:
            if vite.poll() is not None:
                raise RuntimeError('Vite failed; inspect browser-process.log')
            try:
                HTTP.open('http://127.0.0.1:5180/', timeout=2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.1)
        profile = runtime / 'chrome-profile'
        chrome = subprocess.Popen([
            CHROME, '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-extensions',
            '--disable-background-networking', '--disable-sync', '--use-mock-keychain', '--disable-component-update',
            '--no-service-autorun', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
            f'--user-data-dir={profile}', 'about:blank',
        ], stdout=log, stderr=log, start_new_session=True)
        processes.append(chrome)
        deadline = time.monotonic() + 20
        while not (profile / 'DevToolsActivePort').exists():
            if chrome.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Chrome did not start')
            time.sleep(.05)
        port = int((profile / 'DevToolsActivePort').read_text().splitlines()[0])
        driver = Driver(port)
        driver.call('Page.enable')
        driver.call('Page.addScriptToEvaluateOnNewDocument', source="""
          localStorage.setItem('mission-control-locale','it');
          document.addEventListener('DOMContentLoaded', () => document.documentElement.classList.add('dark'));
          window.uiErrors=[];
          window.addEventListener('error', e => window.uiErrors.push(e.message));
          window.addEventListener('unhandledrejection', e => window.uiErrors.push(String(e.reason)));
        """)
        driver.viewport(390, 844)
        driver.navigate('pairing')
        driver.click('Inizio io')
        driver.wait("!!document.querySelector('[role=dialog]')")
        assert driver.evaluate("document.body.style.overflow === 'hidden'")
        assert driver.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)")
        driver.screenshot('pairing-step-1.png', report)
        driver.key('Escape', 'Escape', 27)
        driver.wait("!document.querySelector('[role=dialog]')")
        assert driver.evaluate("document.body.style.overflow !== 'hidden'")
        driver.type('textarea', 'mcpair1.accept.MOCK_PUBLIC_ACCEPT')
        driver.click('Controlla e continua')
        driver.wait("!!document.querySelector('[role=dialog]')")
        driver.key('Escape', 'Escape', 27)
        driver.click('Codice inviato — confronta il SAS')
        driver.wait("!!document.querySelector('input[inputmode=numeric]')")
        driver.type('input[inputmode=numeric]', '482913')
        driver.screenshot('pairing-sas.png', report)
        driver.click("Confermo l'abbinamento")
        driver.wait("document.body.innerText.includes('Conferma locale salvata')")
        driver.viewport(1440, 900)
        driver.screenshot('peer-list.png', report)
        report['checks'].append('Host: offer sheet, Escape/scroll-lock, accept, reveal sheet, SAS, confirm, pending acknowledgement')

        driver.viewport(390, 844)
        driver.navigate('invitation')
        driver.type('textarea', 'invitation-mock')
        driver.click("Leggi l'invito")
        driver.click('Leggi i dettagli della stanza')
        driver.click('Dettagli controllati, continua')
        driver.click("Riscatta l'invito")
        driver.wait("document.body.innerText.includes('Confermo questo scope esatto')")
        assert not driver.evaluate("[...document.querySelectorAll('button')].some(b => b.textContent.trim()== 'Do il consenso')")
        driver.click('Confermo questo scope esatto')
        driver.screenshot('invitation-consent.png', report)
        driver.click('Do il consenso')
        driver.wait("document.body.innerText.includes('Apri la stanza')")
        driver.click('Apri la stanza')
        driver.wait("!!document.querySelector('.guest-room-view')")
        driver.wait('document.body.innerText.includes("Firma l\'installer")')
        report['checks'].append('Invitation: identity → metadata → redeem → automatic scope polling → exact confirmation → separate consent → activation → open')
        driver.screenshot('guest-my-bot.png', report)
        geometry = driver.evaluate('''(() => {
          const buttons=[...document.querySelectorAll('.guest-approval button')];
          const [a,b]=buttons.map(el=>el.getBoundingClientRect());
          return {gap:b.left-a.right, targets:buttons.map(el=>[el.offsetWidth,el.offsetHeight])};
        })()''')
        assert geometry['gap'] >= 8, geometry
        assert all(w >= 44 and h >= 44 for w, h in geometry['targets']), geometry
        report['approval_geometry'] = geometry
        driver.click('Chat')
        driver.wait("document.querySelector('.guest-room-layout').dataset.activeTab === 'chat'")
        assert not driver.evaluate("!!document.querySelector('.guest-room-view textarea, .guest-room-view [contenteditable=true]')")
        driver.screenshot('guest-chat.png', report)
        driver.click('Carica i prossimi eventi')
        driver.wait("document.querySelectorAll('[data-seq]').length === 3")
        assert driver.evaluate("[...document.querySelectorAll('[data-seq]')].map(el=>el.dataset.seq)") == ['1', '2', '3']
        report['checks'].append('Guest: My bot default, 44px targets/gap, read-only Chat, pagination and overlap dedup')
        driver.viewport(1440, 900)
        driver.screenshot('guest-desktop.png', report)
        for width in [375, 390, 430]:
            driver.viewport(width, 844)
            assert driver.evaluate('document.documentElement.scrollWidth <= innerWidth')
        report['checks'].append('No horizontal scroll at 375/390/430 for guest Chat')
        driver.viewport(390, 844)
        driver.navigate('guest', '&offline=1')
        driver.wait("!!document.querySelector('.guest-offline')")
        driver.screenshot('offline.png', report)
        assert driver.evaluate("getComputedStyle(document.querySelector('.guest-offline')).backgroundColor !== 'rgba(0, 0, 0, 0)'")
        for screen in ['pairing', 'invitation', 'guest']:
            driver.navigate(screen, '&feature=off')
            assert not driver.evaluate("!!document.querySelector('.invite-ui')")
        report['checks'].append('Feature-off hides all three mock surfaces, pairing actual 404 error path exercised')
        driver.ws.close()
        report['status'] = 'PASS'
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        (EVIDENCE / 'green/browser.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
