import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
const dom = new JSDOM('<!doctype html><button id="opener">New room</button><div id="root"></div>', { url:'http://localhost/', pretendToBeVisual:true });
for (const k of ['window','document','HTMLElement','HTMLInputElement','HTMLSelectElement','Event','KeyboardEvent']) globalThis[k]=dom.window[k];
Object.defineProperty(globalThis,'navigator',{configurable:true,value:dom.window.navigator});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
window.HTMLElement.prototype.scrollIntoView=()=>{};
const requests=[];
globalThis.fetch=async (url,options={})=>{
  assert.equal(String(url),'/api/local/candidates/vaults');
  assert.equal(options.method??'GET','GET');requests.push(String(url));
  return new Response(JSON.stringify({vaults:[{id:'fixture-vault',label:'Fixture vault'}],default_vault:'fixture-vault'}),{headers:{'Content-Type':'application/json'}});
};
const cacheDir=mkdtempSync(path.join(tmpdir(),'create-room-test-'));
const vite=await createServer({root:process.cwd(),configFile:false,cacheDir,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false},plugins:[react()]});
const {createRoot}=await import('react-dom/client');
const {CreateRoomForm}=await vite.ssrLoadModule('/src/components/chat/GroupRoomView.tsx');
const {I18nProvider}=await vite.ssrLoadModule('/src/lib/i18n.tsx');
const members=[{handle:'alpha',displayName:'First agent',description:'Release engineer'},{handle:'beta',displayName:'Second agent'},{handle:'gamma',description:'Quality specialist'}];
let root,calls,cancelled;
const settle=()=>new Promise(r=>setImmediate(r));
async function flush(fn=()=>{}){await act(async()=>{await fn();for(let i=0;i<6;i++)await settle();});}
async function mount(props={}){
 if(root)await flush(()=>root.unmount());
 calls=[];cancelled=0;document.getElementById('opener').focus();
 root=createRoot(document.getElementById('root'));
 await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(CreateRoomForm,{members,onCreate:async(...args)=>calls.push(args),onCancel:()=>cancelled++,...props}))));
}
const find=s=>{const n=document.querySelector(s);assert.ok(n,`Missing ${s}`);return n;};
const button=text=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===text||b.getAttribute('aria-label')===text);assert.ok(b,`Missing button ${text}`);return b;};
const click=text=>flush(()=>button(text).click());
async function change(selector,value){const n=find(selector);await flush(()=>{Object.getOwnPropertyDescriptor(n.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(n,value);n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));});}
const submit=()=>flush(()=>find('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
try{
 await test('bulk selection is accurately labelled; filtering preserves hidden selections and bulk applies to all members',async()=>{
  await mount();await click('Select all');assert.equal(document.querySelectorAll('input[type=checkbox]:checked').length,3);
  await change('input[type=search]','QUALITY');assert.equal(document.querySelectorAll('input[type=checkbox]').length,1);assert.equal(find('input[type=checkbox]').checked,true);
  await click('Clear selection');assert.equal(find('input[type=checkbox]').checked,false);
  await click('Select all');await change('input[type=search]','');assert.equal(document.querySelectorAll('input[type=checkbox]:checked').length,3);
  await flush(()=>find('input[type=checkbox]').click());await change('input[type=search]','First');assert.equal(find('input[type=checkbox]').checked,false);
  await change('input[type=search]','no-match');assert.match(find('.room-create-empty').textContent,/No members match/);await change('input[type=search]','');assert.equal(document.querySelectorAll('input[type=checkbox]:checked').length,2);
 });
 await test('empty and duplicate rosters have truthful count and unique callback handles',async()=>{
  await mount({members:[]});assert.match(document.body.textContent,/No members available/);assert.equal(button('Select all').disabled,true);
  await mount({members:[members[0],members[0],members[1]]});await click('Select all');assert.equal(document.querySelectorAll('input[type=checkbox]').length,2);
  await change('input[placeholder="e.g. Release triage"]','  Release  ');await submit();assert.deepEqual(calls,[['Release',['alpha','beta'],'fixture-vault']]);
 });
 await test('validation, initial vault and asynchronous failure survive without losing values or selection',async()=>{
  await mount({initialVault:'other-vault',onCreate:async()=>{throw Error('Synthetic failure');}});
  await submit();assert.match(find('[role=alert]').textContent,/Enter a room name/);
  await change('input[placeholder="e.g. Release triage"]','Release');await submit();assert.match(find('[role=alert]').textContent,/at least two/);
  await flush(()=>find('input[type=checkbox]').click());await submit();assert.match(find('[role=alert]').textContent,/at least two/);
  await click('Select all');await submit();assert.match(find('[role=alert]').textContent,/Synthetic failure/);assert.equal(find('select').value,'other-vault');assert.equal(document.querySelectorAll('input[type=checkbox]:checked').length,3);assert.equal(button('Create room').disabled,false);
 });
 await test('saving blocks repeat submission and recovers after completion',async()=>{
  let finish;let count=0;await mount({onCreate:()=>{count++;return new Promise(r=>finish=r);}});
  await change('input[placeholder="e.g. Release triage"]','Release');await click('Select all');await submit();assert.equal(button('Create room').disabled,true);assert.equal(button('Close room creation').disabled,true);await flush(()=>find('h3').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})));assert.equal(cancelled,0);await submit();assert.equal(count,1);await flush(()=>finish());assert.equal(button('Create room').disabled,false);assert.equal(button('Close room creation').disabled,false);
 });
 await test('one close, no redundant cancel; focus restores to opener on unmount',async()=>{
  await mount();await click('Close room creation');assert.equal(cancelled,1);assert.equal([...document.querySelectorAll('button')].some(n=>n.textContent==='Cancel'),false);
  find('input').focus();await flush(()=>root.unmount());root=null;assert.equal(document.activeElement.id,'opener');
 });
 await test('invite slot keeps local name and selection, never nests forms or submits local callback',async()=>{
  await mount({renderInviteOnly:name=>React.createElement('input',{'aria-label':'Synthetic invite name',defaultValue:name})});
  await change('input[placeholder="e.g. Release triage"]','Release');await click('Select all');await click('Invite-only');assert.equal(find('[aria-label="Synthetic invite name"]').value,'Release');assert.equal(document.querySelectorAll('form').length,1);await submit();assert.deepEqual(calls,[]);await click('Local');assert.equal(document.querySelectorAll('input[type=checkbox]:checked').length,3);assert.equal(find('input[placeholder="e.g. Release triage"]').value,'Release');
 });
 await test('selector arrow keys move selection without submitting; Escape dismisses creation only',async()=>{
  await mount({renderInviteOnly:()=>React.createElement('p',null,'Synthetic invite')});
  const local=button('Local');await flush(()=>local.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true})));assert.equal(button('Invite-only').getAttribute('aria-checked'),'true');assert.equal(document.activeElement,button('Invite-only'));await flush(()=>button('Invite-only').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true,cancelable:true})));assert.equal(button('Local').getAttribute('aria-checked'),'true');await flush(()=>find('input').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})));assert.equal(cancelled,1);
 });
 assert.ok(requests.length>0);
}finally{if(root)await flush(()=>root.unmount());await vite.close();dom.window.close();rmSync(cacheDir,{recursive:true,force:true,maxRetries:10,retryDelay:50});}
