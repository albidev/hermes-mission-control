import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync,existsSync,openSync,closeSync,realpathSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const modulePath=process.env.PLAYWRIGHT_MODULE;
assert.ok(modulePath,'Set PLAYWRIGHT_MODULE to a known read-only playwright-core module');
const {chromium}=await import(pathToFileURL(modulePath));
const [sourceArg,outArg,phase='final']=process.argv.slice(2),source=realpathSync(resolve(sourceArg)),out=resolve(outArg);
assert.ok(out.includes('/rooms-create-dialog/implementation/'));assert.equal(existsSync(out),false,'Fresh owned evidence root required');
assert.equal(source,resolve(out.split('/implementation/')[0],'source'));mkdirSync(out,{recursive:true});
const isolatedEnv={PATH:process.env.PATH,HOME:join(out,'home'),HERMES_HOME:join(out,'home/hermes'),TMPDIR:out,TMP:out,TEMP:out,LANG:'en_US.UTF-8',NO_COLOR:'1'};mkdirSync(isolatedEnv.HOME);
const log=openSync(join(out,'server.log'),'w'),child=spawn(process.execPath,[new URL('./server.mjs',import.meta.url).pathname,source,out],{env:isolatedEnv,stdio:['ignore',log,log]});
let context,page,birth;const report={phase,source,checks:[],viewports:[],errors:[],blocked:[],synthetic:[],physical_device:false};
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function check(value,message){assert.ok(value,message);report.checks.push(message);}
const write=(name,data)=>writeFileSync(join(out,name),JSON.stringify(data,null,2));
try{
 birth=execFileSync('ps',['-p',String(child.pid),'-o','lstart='],{encoding:'utf8'}).trim();
 for(let i=0;!existsSync(join(out,'server.json'));i++){assert.equal(child.exitCode,null,'Fixture exited');assert.ok(i<600,'Startup timeout');await wait(100);}
 const receipt=JSON.parse(readFileSync(join(out,'server.json')));assert.equal(receipt.birth,birth);const base=`http://127.0.0.1:${receipt.port}`;
 context=await chromium.launchPersistentContext(join(out,'chrome-profile'),{executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,viewport:{width:390,height:844},args:['--disable-background-networking','--disable-component-update','--no-first-run'],env:isolatedEnv});
 write('ownership.json',{server:receipt,runner:{pid:process.pid,birth:execFileSync('ps',['-p',String(process.pid),'-o','lstart='],{encoding:'utf8'}).trim()},browserProcesses:execFileSync('ps',['-axo','pid=,ppid=,lstart=,comm='],{encoding:'utf8'}).trim().split('\n').map(line=>line.trim().split(/\s+/)).filter(row=>Number(row[1])===process.pid&&row.slice(7).join(' ').includes('Google Chrome.app/Contents/MacOS/Google Chrome')).map(row=>({pid:Number(row[0]),birth:row.slice(2,7).join(' '),transport:'owned Playwright pipe; no debug TCP port'}))});
 await context.route('**/*',route=>{const u=new URL(route.request().url());if(u.origin!==base||u.pathname.startsWith('/api/')){report.blocked.push(u.pathname);return route.abort();}return route.continue();});
 page=await context.newPage();page.on('pageerror',e=>report.errors.push(String(e)));
 const dialog=()=>page.locator('.room-create-form'),name=()=>page.getByPlaceholder('e.g. Release triage'),button=s=>dialog().getByRole(['Local','Invite-only'].includes(s)?'radio':'button',{name:s,exact:true});
 const goto=async(query='')=>{if(page.url().startsWith(base)){const data=await page.evaluate(()=>window.__fixture);report.synthetic.push(data);check(data.unexpected.length===0,'no unrecognised synthetic calls before navigation');}await page.goto(base+'/'+query);await page.getByRole('button',{name:'Create room',exact:true}).waitFor();await page.locator('.chat-transcript').waitFor();await page.waitForTimeout(100);await page.getByRole('button',{name:'Create room',exact:true}).click();await dialog().waitFor();await page.waitForTimeout(120);check(await dialog().evaluate(n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;}),'dialog fits visible viewport after navigation/persisted resize');};
 const shot=label=>page.screenshot({path:join(out,label+'.png')});
 const state=async()=>{const data=await page.evaluate(()=>window.__fixture);report.synthetic.push(data);check(data.unexpected.length===0,'no unrecognised synthetic calls');};
 const sizes=phase==='rail-regression'?[]:phase==='critique'?[[390,844],[1440,900],[844,390]]:[[320,568],[390,844],[768,1024],[1280,720],[1440,900],[859,1217],[844,390],[390,340],[320,300]];
 for(const [width,height] of sizes){
  const tag=`${width}x${height}`;await page.setViewportSize({width,height});await goto('?many=1');
  const geometry=await page.evaluate(()=>{const f=document.querySelector('.room-create-form'),d=document.querySelector('aside'),s=f.querySelector('.room-create-scroll');const box=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};return {form:box(f),drawer:box(d),scroll:box(s),overflow:document.documentElement.scrollWidth>innerWidth||f.scrollWidth>f.clientWidth,scrollers:[...f.querySelectorAll('*')].filter(n=>n.scrollHeight>n.clientHeight+1&&['auto','scroll'].includes(getComputedStyle(n).overflowY)).map(n=>n.className),active:document.activeElement.tagName,modal:f.getAttribute('aria-modal'),headerInert:d.querySelector('header').inert,background:!!document.querySelector('.chat-backdrop')};});
  report.viewports.push({width,height,geometry});await shot(`${tag}-top`);
  check(!geometry.overflow,tag+': no horizontal overflow');check(geometry.form.y>=0&&geometry.form.bottom<=height+1,tag+': dialog vertical fit');
  check(Math.abs(geometry.form.x+geometry.form.width/2-geometry.drawer.x-geometry.drawer.width/2)<1,tag+': centered horizontally in actual drawer');
  check(Math.abs(geometry.form.y+geometry.form.height/2-geometry.drawer.y-geometry.drawer.height/2)<1,tag+': centered vertically in actual drawer');
  check(geometry.form.x>=geometry.drawer.x&&geometry.form.right<=geometry.drawer.right,tag+': bounded to drawer');
  if(width===1440)check(geometry.drawer.width<=540&&geometry.form.x>width/2,tag+': narrow desktop drawer, not viewport center');
  check(geometry.scrollers.length===1&&geometry.scrollers[0]==='room-create-scroll',tag+': one deliberate content scroller');check(geometry.active==='H3',tag+': title focus does not force keyboard');
  check(geometry.headerInert&&!geometry.background&&!geometry.modal,tag+': scoped modality, no global scrim or false aria-modal');
  check(await dialog().getByRole('button',{name:'Cancel',exact:true}).count()===0,tag+': no redundant Cancel');
  check(await dialog().locator('.lucide-x').count()===1,tag+': one X');
  for(const control of [button('Close room creation'),button('Create room')])check(await control.evaluate(n=>{const r=n.getBoundingClientRect();return r.width>=44&&r.height>=44&&r.top>=0&&r.bottom<=innerHeight;}),tag+': stable 44px close/action');
  if(phase==='critique')continue;
  // Keep exact nodes: creation must not unmount the selected transcript/context.
  check(await page.locator('button[aria-label="Select a room"]').textContent().then(t=>t.trim()==='Release Readiness · Demo'),tag+': selected room retained');
  const scroller=dialog().locator('.room-create-scroll');await scroller.evaluate(n=>n.scrollTop=n.scrollHeight);
  check(await dialog().locator('.room-create-member').last().evaluate(n=>{const r=n.getBoundingClientRect(),s=n.closest('.room-create-scroll').getBoundingClientRect();return r.bottom<=s.bottom+1&&r.bottom>s.top;}),tag+': last roster row reachable');
  await button('Select all').click();check(await dialog().locator('input[type=checkbox]:checked').count()===32,tag+': count not capped at eight');
  await name().fill('Synthetic room');await button('Create room').click();await dialog().getByRole('alert').waitFor();check(await dialog().getByRole('alert').isVisible(),tag+': synthetic create error reachable');
  check(await page.evaluate(()=>window.__fixture.calls.length)===1,tag+': one synthetic local callback');
  await button('Close room creation').click();check(await dialog().count()===0&&await page.locator('aside.is-open').count()===1,tag+': X closes only creation');
  check(await page.evaluate(()=>document.activeElement.getAttribute('aria-label'))==='Create room',tag+': restores connected opener');
  await page.getByRole('button',{name:'Create room',exact:true}).click();await dialog().waitFor();await page.keyboard.press('Escape');check(await dialog().count()===0&&await page.evaluate(()=>window.__fixture.closed===0&&window.__fixture.escape===0),tag+': Escape does not reach outer drawer');
  await state();
 }
 if(phase==='final'||phase==='rail-regression'){
  await page.setViewportSize({width:1440,height:900});await goto('?many=1');
  const rail=page.locator('.chat-mode-tabs-shell');
  const inner=dialog().locator('.room-create-scroll');
  await inner.evaluate(n=>n.scrollTop=30);await page.waitForTimeout(60);await inner.evaluate(n=>n.scrollTop=240);await page.waitForTimeout(1100);
  const afterResume=await rail.evaluate(n=>({inert:n.inert,hidden:n.getAttribute('aria-hidden')}));
  await page.getByRole('button',{name:'Outside drawer action'}).click();await page.keyboard.press('Tab');
  const backgroundFocused=await page.evaluate(()=>!!document.activeElement.closest('.chat-mode-tabs-shell'));
  await page.keyboard.press('Enter');
  report.railRegression={afterResume,backgroundFocused,changes:await page.evaluate(()=>window.__fixture.changes)};
  check(afterResume.inert&&afterResume.hidden==='true'&&!backgroundFocused&&report.railRegression.changes.length===0,'UI-01: scroll/resume then real outside-click Tab Enter cannot activate background mode');
  if(await dialog().count())await button('Close room creation').click();
  await page.waitForTimeout(250);
  check(await rail.evaluate(n=>n.inert===n.classList.contains('is-hidden')&&(n.getAttribute('aria-hidden')==='true')===n.classList.contains('is-hidden')),'UI-01: rail returns to its own React-owned auto-hide state on close');
  await page.getByRole('tab',{name:'Chat',exact:true}).click();
  check(await page.evaluate(()=>window.__fixture.changes.includes(null)),'UI-01: background Chat action works after closing creation');
 }
 if(phase==='final'){
  await page.setViewportSize({width:1440,height:900});await goto();
  await page.evaluate(()=>{window.__transcript=document.querySelector('.chat-transcript');window.__transcript.scrollTop=17;window.__scroll=window.__transcript.scrollTop;});
  const toolbar=page.locator('button[aria-label="Rename room"]');
  check(await toolbar.evaluate(n=>!!n.closest('[inert]')),'background rename inert');
  const outerClose=await page.locator('aside button[aria-label="Close Group Chat"]').boundingBox();await page.mouse.click(outerClose.x+outerClose.width/2,outerClose.y+outerClose.height/2);check(await page.evaluate(()=>window.__fixture.closed===0),'scrim blocks actual background toolbar pointer click');
  await toolbar.evaluate(n=>n.focus());check(await page.evaluate(()=>document.activeElement.closest('.room-create-form')!==null),'background toolbar cannot steal focus');
  await page.getByRole('button',{name:'Outside drawer action'}).click();check(await page.evaluate(()=>window.__fixture.outside)===1,'outside application stays interactive/undimmed');
  await dialog().locator('h3').focus();await page.keyboard.press('Tab');check(await page.evaluate(()=>document.activeElement.classList.contains('room-create-close')),'Tab from initial title enters X');
  await page.keyboard.press('Shift+Tab');check(await page.evaluate(()=>document.activeElement.classList.contains('room-create-submit')),'Shift-Tab wraps inside local task');await page.keyboard.press('Tab');check(await page.evaluate(()=>document.activeElement.classList.contains('room-create-close')),'Tab wraps from primary to X');
  // Real drawer resizing, no invented expanded variant.
  const hb=await page.locator('.chat-drawer-resize-handle').boundingBox();await page.mouse.move(hb.x+4,100);await page.mouse.down();await page.mouse.move(1440-700,100,{steps:6});await page.mouse.up();
  const resize=await page.evaluate(()=>{const d=document.querySelector('aside').getBoundingClientRect(),f=document.querySelector('.room-create-form').getBoundingClientRect();return {width:d.width,centerError:Math.abs(f.x+f.width/2-d.x-d.width/2)};});check(Math.abs(resize.width-700)<2&&resize.centerError<1,'supported drag resize keeps dialog centered');await shot('desktop-resized');
  await button('Close room creation').click();check(await page.evaluate(()=>window.__transcript===document.querySelector('.chat-transcript')&&window.__transcript.scrollTop===window.__scroll),'transcript node and scroll preserved through open/close');
  // Standard mobile final and full keyboard traversal on visible scroller.
  await page.setViewportSize({width:390,height:844});await goto();await name().fill('Release triage');await button('Select all').click();await shot('mobile-selected');
  await page.mouse.click(3,400);check(await name().inputValue()==='Release triage','accidental local scrim click preserves draft');
  await button('Close room creation').focus();for(let i=0;i<18;i++){await page.keyboard.press('Tab');check(await page.evaluate(()=>{const n=document.activeElement,r=n.getBoundingClientRect(),s=n.closest('.room-create-scroll')?.getBoundingClientRect();return !!n.closest('.room-create-form')&&r.top>=0&&r.bottom<=innerHeight&&(!s||(r.top>=s.top-1&&r.bottom<=s.bottom+1));}),'real Tab traversal '+i+': focus stays reachable within task');}
  const search=dialog().getByRole('searchbox',{name:'Search members'});await search.fill('research');check(await dialog().locator('input[type=checkbox]').count()===1,'search matches distinct display/handle');await button('Clear selection').click();await search.fill('');check(await dialog().locator('input[type=checkbox]:checked').count()===0,'bulk clear includes hidden results');await button('Select all').click();await search.fill('no-match');check(await dialog().locator('.room-create-empty').textContent()==='No members match your search.','no-match state truthful');await shot('mobile-no-matches');await search.fill('');
  await button('Invite-only').click();check(await dialog().locator('form').count()===0,'invite never nests forms');check(await dialog().locator('footer').count()===0,'no empty Invite-only footer');check(await dialog().getByRole('textbox',{name:'Lobby name'}).inputValue()==='Release triage','invite receives local draft');await shot('mobile-invite');await dialog().getByRole('textbox',{name:'Lobby name'}).press('Enter');check(await page.evaluate(()=>window.__fixture.calls.length)===0,'invite Enter never creates local room');
  await dialog().getByRole('combobox').selectOption('synthetic-peer');await button('Create lobby').click();await dialog().getByRole('alert').waitFor();check(await page.evaluate(()=>!!window.__fixture.inviteBody),'real embedded invite uses synthetic fetch only');await shot('mobile-invite-error');await button('Local').click();check(await name().inputValue()==='Release triage'&&await dialog().locator('input[type=checkbox]:checked').count()===8,'local→invite→local draft and selection preserved');
  await button('Close room creation').click();await goto('?empty=1');await shot('mobile-empty');check(await button('Select all').isDisabled(),'empty roster bulk disabled');
  await goto('?long=1&many=1');await dialog().locator('.room-create-member').nth(2).scrollIntoViewIfNeeded();check(await dialog().locator('.room-create-member').nth(2).evaluate(n=>[...n.querySelectorAll('span')].every(t=>t.scrollWidth<=t.clientWidth+1)),'meaningful long identities wrap without clipping');await shot('mobile-long-roster');
  await goto('?error=1');await name().fill('Failure case');await button('Select all').click();await button('Create room').click();await dialog().getByRole('alert').waitFor();await shot('mobile-error');
  await goto('?saving=1');await name().fill('Pending case');await button('Select all').click();await button('Create room').click();check(await button('Close room creation').isDisabled()&&await button('Local').isDisabled(),'pending local request blocks close and mode switch');await dialog().locator('h3').focus();await page.keyboard.press('Escape');check(await dialog().count()===1,'pending Escape cannot imply cancellation');await dialog().evaluate(n=>n.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));check(await page.evaluate(()=>window.__fixture.calls.length)===1,'pending local request deduplicated');await shot('mobile-saving');await page.evaluate(()=>window.__finishSaving());await button('Close room creation').waitFor();await page.waitForTimeout(100);check(await button('Close room creation').isEnabled(),'failure/completion unlocks close');
  await goto('?inviteSaving=1');await button('Invite-only').click();await dialog().getByRole('textbox',{name:'Lobby name'}).fill('Pending invite');await dialog().getByRole('combobox').selectOption('synthetic-peer');await button('Create lobby').click();check(await button('Close room creation').isDisabled(),'pending invite blocks misleading close');await dialog().locator('h3').focus();await page.keyboard.press('Escape');check(await dialog().count()===1,'pending invite Escape remains open');await page.evaluate(()=>window.__finishInvite());await page.waitForTimeout(100);check(await button('Close room creation').isEnabled(),'invite rejection restores close');
  // Invite fit across every geometry and per-control reachability.
  for(const [width,height] of sizes){await page.setViewportSize({width,height});await goto();await button('Invite-only').click();await dialog().getByRole('combobox').waitFor();for(const c of await dialog().locator('button,input,select').all()){await c.scrollIntoViewIfNeeded();check(await c.evaluate(n=>{const b=n.getBoundingClientRect();return b.width>=44&&b.height>=44&&b.y>=0&&b.bottom<=innerHeight;}),`${width}x${height}: real invite control reachable`);}await shot(`${width}x${height}-invite`);}
  await page.setViewportSize({width:390,height:844});await goto('?localOnly=1');check(await dialog().getByRole('radiogroup').count()===0,'capability gating keeps unavailable invite option hidden');
  await page.emulateMedia({reducedMotion:'reduce'});await goto();await shot('mobile-reduced-motion');
  for(const theme of ['light','dark']){
   await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);await shot(`mobile-${theme}`);check(await dialog().isVisible(),theme+' theme rendered');
   const ratios=await page.evaluate(()=>{const f=document.querySelector('.room-create-form'),canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');function rgb(color){ctx.clearRect(0,0,1,1);ctx.fillStyle=color;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data].slice(0,3);}function lum(color){const [r,g,b]=rgb(color).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;});return .2126*r+.7152*g+.0722*b;}function ratio(a,b){const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}const bg=getComputedStyle(f).backgroundColor,button=f.querySelector('.room-create-submit'),input=f.querySelector('input');return {hint:ratio(getComputedStyle(f.querySelector('.room-create-hint')).color,bg),bulk:ratio(getComputedStyle(f.querySelector('.room-create-selection')).color,bg),primary:ratio(getComputedStyle(button).color,getComputedStyle(button).backgroundColor),placeholder:ratio(getComputedStyle(input,'::placeholder').color,getComputedStyle(input).backgroundColor)};});
   report.contrast??={};report.contrast[theme]=ratios;check(Object.values(ratios).every(n=>n>=4.5),theme+': hint/bulk/primary/placeholder text contrast >=4.5');
  }
  for(const [query,label] of [['?loading=1','invite-loading'],['?peerError=1','invite-peer-error'],['?noPeers=1','invite-no-peers']]){await goto(query);await button('Invite-only').click();await page.waitForTimeout(100);await shot(label);check(await dialog().isVisible(),label+' remains usable');}
  await goto();await name().fill('Keyboard approximation');await name().focus();await page.setViewportSize({width:390,height:340});await name().scrollIntoViewIfNeeded();check(await name().evaluate(n=>{const r=n.getBoundingClientRect(),s=n.closest('.room-create-scroll').getBoundingClientRect();return getComputedStyle(n).fontSize==='16px'&&r.top>=s.top-1&&r.bottom<=s.bottom+1;}),'focused input survives visual-viewport approximation (not physical keyboard)');await shot('keyboard-space-focused');
  await state();
  await page.setViewportSize({width:390,height:844});await page.goto(base+'/?it=1');await page.getByRole('button',{name:'Crea stanza',exact:true}).click();await dialog().waitFor();check(await dialog().locator('h3').textContent()==='Crea una stanza','Italian title is coherent');await shot('mobile-italian');
 }
 check(report.errors.length===0,'no browser page errors');check(report.blocked.length===0,'zero external or production API network attempts');report.passed=true;
}catch(error){report.passed=false;report.failure=String(error.stack||error);if(page){try{await page.screenshot({path:join(out,'failure.png')});write('failure-state.json',await page.evaluate(()=>window.__fixture));}catch{}}throw error;}
finally{
 if(context)await context.close();if(child.exitCode===null){assert.equal(execFileSync('ps',['-p',String(child.pid),'-o','lstart='],{encoding:'utf8'}).trim(),birth);const exited=new Promise(r=>child.once('exit',r));child.kill('SIGTERM');await exited;}
 closeSync(log);report.teardown={serverExited:child.exitCode!==null||child.signalCode!==null,browserClosed:!!context};write('result.json',report);writeFileSync(join(out,'COMPLETED'),report.passed?'PASS':'FAIL');console.log(JSON.stringify({passed:report.passed,checks:report.checks.length,viewports:report.viewports.length,failure:report.failure}));
}
