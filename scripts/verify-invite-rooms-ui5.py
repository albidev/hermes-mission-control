"""Local-only UI.5 regression probe. Python 3.11 + existing Chrome/CDP driver."""
from __future__ import annotations
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    root, evidence = args.root.resolve(), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('ui_driver', root / 'scripts/verify-invite-rooms-ui.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = Path(tempfile.mkdtemp(prefix='ui5-browser-', dir=os.environ['TMPDIR']))
    # Fail before spawning anything if the assigned port is occupied.
    with socket.socket() as guard:
        guard.bind(('127.0.0.1', 5180))
    processes, results = [], []
    log = (evidence / 'process.log').open('w')
    driver = None
    def check(name, operation):
        try:
            detail = operation()
            results.append({'check': name, 'pass': True, 'detail': detail})
        except Exception as error:
            results.append({'check': name, 'pass': False, 'error': str(error)})
        print(json.dumps(results[-1]), flush=True)
    try:
        vite = subprocess.Popen([str(root / 'node_modules/.bin/vite'), '--host', '127.0.0.1', '--port', '5180', '--strictPort'], cwd=root, stdout=log, stderr=log, start_new_session=True)
        processes.append(vite)
        for _ in range(300):
            if vite.poll() is not None:
                raise RuntimeError('Vite failed to start')
            try:
                module.HTTP.open('http://127.0.0.1:5180/', timeout=1).close()
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('Vite readiness timeout')
        profile = runtime / 'profile'
        chrome = subprocess.Popen([module.CHROME, '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-background-networking', '--disable-sync', '--use-mock-keychain', '--disable-component-update', '--no-service-autorun', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1', f'--user-data-dir={profile}', 'about:blank'], stdout=log, stderr=log, start_new_session=True)
        processes.append(chrome)
        for _ in range(300):
            if (profile / 'DevToolsActivePort').exists():
                break
            time.sleep(.05)
        driver = module.Driver(int((profile / 'DevToolsActivePort').read_text().splitlines()[0]))
        driver.call('Page.enable')
        driver.call('Network.enable')
        # No live API/WS traffic, even for the normal-app root contract check.
        driver.call('Network.setBlockedURLs', urls=['*/api/*', 'ws://*', 'wss://*'])
        driver.call('Page.addScriptToEvaluateOnNewDocument', source="""
          localStorage.setItem('mission-control-locale','it');
          window.uiErrors=[];
          addEventListener('error',e=>window.uiErrors.push(e.message));
          addEventListener('unhandledrejection',e=>window.uiErrors.push(String(e.reason)));
          document.addEventListener('DOMContentLoaded',()=>document.documentElement.classList.add('dark'));
        """)
        def viewport(w, h, mobile=True):
            driver.call('Emulation.setDeviceMetricsOverride', width=w, height=h, deviceScaleFactor=1, mobile=mobile)
            driver.call('Emulation.setTouchEmulationEnabled', enabled=mobile)
        def swipe(w, h):
            driver.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[{'x': w * .65, 'y': h - 90}])
            for i in range(1, 13):
                driver.call('Input.dispatchTouchEvent', type='touchMove', touchPoints=[{'x': w * .65, 'y': h - 90 - i * (h - 190) / 12}])
                time.sleep(.025)
            driver.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
            time.sleep(.25)
        def shot(name):
            data = driver.call('Page.captureScreenshot', format='png', captureBeyondViewport=False)
            (evidence / f'{name}.png').write_bytes(base64.b64decode(data['data']))
        def scroll_screen(w, h, screen):
            viewport(w, h)
            driver.navigate(screen, '&approvals=populated')
            if screen == 'invitation':
                driver.type('textarea', 'LOCAL_FIXTURE_INVITATION')
                driver.click("Leggi l'invito")
                driver.click('Leggi i dettagli della stanza')
                driver.click('Dettagli controllati, continua')
                driver.click("Riscatta l'invito")
                driver.click('Confermo questo scope esatto')
            # Navigation helpers may scroll into view; start from the actual top.
            driver.evaluate("document.querySelector('.invite-mock-page').scrollTop=0")
            target = {'pairing': '.invite-card:last-child p:last-child', 'invitation': '.invite-stepper .is-current button:last-child', 'guest': '.guest-disclaimer'}[screen]
            before = driver.evaluate("({bodyOverflow:getComputedStyle(document.body).overflowY,y:document.querySelector('.invite-mock-page').scrollTop})")
            for _ in range(7):
                swipe(w, h)
            geometry = driver.evaluate(f"""(()=>{{const p=document.querySelector('.invite-mock-page'),r=document.querySelector({json.dumps(target)}).getBoundingClientRect();return {{top:r.top,bottom:r.bottom,y:p.scrollTop,max:p.scrollHeight-p.clientHeight,windowY:scrollY,width:document.documentElement.scrollWidth,viewport:innerWidth}}}})()""")
            shot(f'{w}-{screen}-bottom')
            assert geometry['bottom'] <= h + 1 and geometry['top'] >= 0, geometry
            assert geometry['width'] <= w, geometry
            assert geometry['windowY'] == 0, geometry
            if geometry['max'] > 1:
                assert geometry['y'] > 20 and abs(geometry['y'] - geometry['max']) <= 2, geometry
            assert driver.evaluate('window.uiErrors.length') == 0
            return {'before': before, 'after': geometry, 'gesture': 'CDP touchStart/12 touchMove/touchEnd ×7'}
        for w, h in [(375, 667), (390, 844)]:
            for screen in ['pairing', 'invitation', 'guest']:
                check(f'touch bottom {w}x{h} {screen}', lambda w=w, h=h, screen=screen: scroll_screen(w, h, screen))
        def copy_case(case):
            viewport(390, 844)
            driver.navigate('pairing')
            driver.click('Inizio io')
            driver.wait("!!document.querySelector('[role=dialog]')")
            # Runtime.evaluate is deliberately the default/main execution context.
            driver.evaluate("""
              window.copyEvents=0;window.copyExact=false;window.nativeWrites=0;
              document.addEventListener('copy',e=>{window.copyEvents++;const f=document.querySelector('[role=dialog] textarea');window.copyExact=!!f&&f.value.slice(f.selectionStart,f.selectionEnd)==='mcpair1.offer.MOCK_PUBLIC_OFFER';e.preventDefault()});
            """)
            native = case.startswith('native')
            native_body = "window.nativeWrites++;if(value!=='mcpair1.offer.MOCK_PUBLIC_OFFER')throw Error('Unexpected fixture');" + ("throw new DOMException('denied','NotAllowedError');" if 'denied' in case else '')
            driver.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:" + ("{writeText:async(value)=>{" + native_body + "}}" if native else 'undefined') + '})')
            if case.endswith('false'):
                driver.evaluate('document.execCommand=()=>false')
            if case.endswith('throw'):
                driver.evaluate("document.execCommand=()=>{throw Error('Fixture refusal')}")
            assert driver.evaluate('window.copyEvents+window.nativeWrites') == 0
            driver.click('Copia il codice')
            driver.wait("!document.querySelector('[role=dialog] button:disabled')")
            success = case.endswith('success')
            data = driver.evaluate("""(()=>{const f=document.querySelector('[role=dialog] textarea');return {apiError:!!document.querySelector('[role=alert]'),active:!!document.querySelector('[role=timer]'),copied:document.querySelector('[role=dialog]').textContent.includes('Copiato'),manual:!!document.querySelector('[role=dialog] [role=status]'),selected:!!f&&f.value.slice(f.selectionStart,f.selectionEnd)==='mcpair1.offer.MOCK_PUBLIC_OFFER',copyEvents:window.copyEvents,copyExact:window.copyExact,nativeWrites:window.nativeWrites,errors:window.uiErrors}})()""")
            shot(f'copy-{case}')
            assert not data['apiError'] and data['active'], data
            assert data['copied'] == success, data
            if not success:
                assert data['manual'] and data['selected'], data
            elif not native or 'denied' in case:
                assert data['copyEvents'] == 1 and data['copyExact'], data
            driver.click('Copiato' if success else 'Copia il codice')
            driver.key('Escape', 'Escape', 27)
            driver.type('textarea', 'LOCAL_FIXTURE_ACCEPT')
            driver.click('Controlla e continua')
            driver.key('Escape', 'Escape', 27)
            driver.click('Codice inviato — confronta il SAS')
            driver.type('input[inputmode=numeric]', '482913')
            driver.click("Confermo l'abbinamento")
            driver.wait("document.body.innerText.includes('Conferma locale salvata')")
            assert not data['errors'], data
            return data
        for case in ['absent-success', 'native-success', 'native-denied-success', 'absent-false', 'absent-throw', 'native-denied-false']:
            check(f'copy main-world {case}', lambda case=case: copy_case(case))
        def modal():
            viewport(375, 667)
            driver.navigate('pairing')
            driver.click('Inizio io')
            before = driver.evaluate("document.querySelector('.invite-mock-page').scrollTop")
            for _ in range(3):
                swipe(375, 667)
            assert driver.evaluate("document.querySelector('.invite-mock-page').scrollTop") == before
            assert driver.evaluate("document.body.style.overflow==='hidden'")
            driver.key('Escape', 'Escape', 27)
            assert driver.evaluate("document.body.style.overflow===''")
            assert driver.evaluate("getComputedStyle(document.body).overflowY==='hidden'")
            swipe(375, 667)
            assert driver.evaluate("document.querySelector('.invite-mock-page').scrollTop") > before
            return 'overlay touch does not move background; body lock restored, own scroller resumes'
        check('modal touch lock', modal)
        for screen in ['pairing', 'invitation', 'guest']:
            def desktop(screen=screen):
                viewport(1440, 900, False)
                driver.navigate(screen)
                shot(f'1440-{screen}')
                assert driver.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert driver.evaluate('window.uiErrors.length') == 0
                return '1440×900, no horizontal overflow or JS errors'
            check(f'desktop {screen}', desktop)
        def normal():
            driver.call('Page.navigate', url='http://127.0.0.1:5180/')
            driver.wait("!!document.querySelector('#root').firstElementChild")
            time.sleep(.3)
            assert driver.evaluate("getComputedStyle(document.body).overflowY==='hidden'")
            assert driver.evaluate("!document.querySelector('.invite-mock-page')")
            shot('normal-app-root')
            return 'normal app root retains overflow:hidden; API/WS blocked, no live shell data exercised'
        check('normal app root scroll contract', normal)
    finally:
        if driver:
            driver.ws.close()
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        log.close()
        (evidence / 'results.json').write_text(json.dumps(results, indent=2))
    failed = sum(not row['pass'] for row in results)
    print(f'{len(results)-failed}/{len(results)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
