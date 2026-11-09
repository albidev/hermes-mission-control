import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {JSDOM} from 'jsdom';
import React,{act} from 'react';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
const dom=new JSDOM('<!doctype html><button id="outside">Outside app</button><div id="root"></div>',{url:'http://localhost/',pretendToBeVisual:true});
for(const k of ['window','document','HTMLElement','HTMLInputElement','HTMLTextAreaElement','Event','KeyboardEvent'])globalThis[k]=dom.window[k];
Object.defineProperty(globalThis,'navigator',{configurable:true,value:dom.window.navigator});
globalThis.localStorage=window.localStorage;globalThis.IS_REACT_ACT_ENVIRONMENT=true;
window.HTMLElement.prototype.scrollTo=function(){};window.HTMLElement.prototype.scrollIntoView=function(){};
globalThis.fetch=async()=>new Response('{}',{status:404});
const cacheDir=mkdtempSync(path.join(tmpdir(),'room-dialog-drawer-'));
const vite=await createServer({root:process.cwd(),configFile:false,cacheDir,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false},plugins:[react()]});
const {createRoot}=await import('react-dom/client');
const {I18nProvider}=await vite.ssrLoadModule('/src/lib/i18n.tsx');
const {ChatDrawer}=await vite.ssrLoadModule('/src/components/ChatDrawer.tsx');
const {GroupGatewayClient}=await vite.ssrLoadModule('/src/lib/group-gateway.ts');
const room={id:'selected',name:'Preserved room',authorityGatewayId:'fixture',authorityEpoch:1,latestSeq:0,disbandedAt:null,members:[]};
GroupGatewayClient.prototype.capabilities=async()=>({driver:true,protocolVersion:2,methods:[],maxLogLimit:100,authorityGatewayId:'fixture'});
GroupGatewayClient.prototype.list=async()=>({rooms:[room],nextOffset:null});
GroupGatewayClient.prototype.state=async()=>({room,driverStatus:{}});
GroupGatewayClient.prototype.log=async()=>({events:[],cursor:0,latestSeq:0,hasMore:false,authority:null});
const root=createRoot(document.getElementById('root'));
const flush=async(fn=()=>{})=>act(async()=>{fn();for(let i=0;i<30;i++)await new Promise(setImmediate);});
const find=s=>{const n=document.querySelector(s);assert.ok(n,s);return n;};
let closed=0,escape=0;
const onEscape=e=>{if(e.key==='Escape')escape++;};document.addEventListener('keydown',onEscape);
try{
 await test('real drawer opens scoped creation, keeps selected transcript mounted and closes only creation',async()=>{
  await flush(()=>root.render(React.createElement(React.StrictMode,null,React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>closed++})))));
  const opener=find('button[aria-label="Create room"]'),transcript=find('.chat-transcript');transcript.scrollTop=91;
  opener.focus();await flush(()=>opener.click());
  const dialog=find('.room-create-form');assert.equal(dialog.getAttribute('role'),'dialog');assert.equal(dialog.hasAttribute('aria-modal'),false);
  assert.equal(find('.chat-transcript'),transcript);assert.equal(transcript.scrollTop,91);
  assert.equal(find('button[aria-label="Select a room"]').textContent.trim(),'Preserved room');
  assert.equal(find('.chat-drawer-head').inert,true);assert.equal(find('.chat-drawer-head').getAttribute('aria-hidden'),'true');
  assert.equal(Boolean(document.getElementById('outside').inert),false);assert.equal(document.querySelector('.chat-backdrop'),null);
  assert.equal(dialog.parentElement.classList.contains('room-create-choice'), true);
  assert.equal(dialog.parentElement.parentElement, find('.room-create-layer'));
  assert.equal(dialog.querySelectorAll('button[aria-label="Close room creation"]').length,1);assert.equal([...dialog.querySelectorAll('button')].some(n=>n.textContent==='Cancel'),false);
  assert.equal(opener.querySelector('.lucide-arrow-left'),null);
  await flush(()=>dialog.querySelector('h3').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true})));
  assert.equal(document.querySelector('.room-create-form'),null);assert.equal(closed,0);assert.equal(escape,0);
  assert.equal(document.activeElement,opener);assert.equal(Boolean(find('.chat-drawer-head').inert),false);assert.equal(find('.chat-drawer-head').hasAttribute('aria-hidden'),false);assert.equal(find('.chat-transcript'),transcript);
  await flush(()=>opener.click());await flush(()=>find('.room-create-close').click());assert.equal(document.activeElement,opener);assert.equal(closed,0);
  await flush(()=>opener.click());document.getElementById('outside').focus();await flush(()=>root.unmount());assert.equal(document.activeElement.id,'outside');
 });
 await test('layer fits visual viewport, protects late siblings and restores pre-existing inert/aria state on StrictMode cleanup',async()=>{
  const {RoomCreationLayer}=await vite.ssrLoadModule('/src/components/chat/RoomCreationLayer.tsx');
  const viewport=new window.EventTarget();viewport.offsetTop=30;viewport.height=340;Object.defineProperty(window,'visualViewport',{configurable:true,value:viewport});
  const host=document.createElement('aside'),background=document.createElement('div');background.inert=true;background.setAttribute('aria-hidden','false');host.append(background);document.body.append(host);
  host.getBoundingClientRect=()=>({top:0,bottom:900});
  const target=document.createElement('div');host.append(target);
  const layerRoot=createRoot(target);
  // The production layer is a direct drawer child. A portal preserves that DOM
  // relationship while this harness owns the React mount separately.
  const {createPortal}=await import('react-dom');
  await flush(()=>layerRoot.render(React.createElement(React.StrictMode,null,createPortal(React.createElement(RoomCreationLayer,null,React.createElement('p',null,'Task')),host))));
  const layer=host.querySelector('.room-create-layer');assert.equal(layer.style.top,'30px');assert.equal(layer.style.bottom,'530px');
  const late=document.createElement('button');late.textContent='Late toolbar';host.append(late);await flush();assert.equal(late.inert,true);
  viewport.height=200;await flush(()=>viewport.dispatchEvent(new Event('resize')));assert.equal(layer.style.bottom,'670px');
  await flush(()=>layerRoot.unmount());assert.equal(background.inert,true);assert.equal(background.getAttribute('aria-hidden'),'false');assert.equal(Boolean(late.inert),false);assert.equal(late.hasAttribute('aria-hidden'),false);host.remove();delete window.visualViewport;
 });
}finally{document.removeEventListener('keydown',onEscape);await vite.close();dom.window.close();rmSync(cacheDir,{recursive:true,force:true});}
