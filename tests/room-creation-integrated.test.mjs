import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import React,{act} from 'react';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';

const dom=new JSDOM('<!doctype html><div id="root"></div>',{url:'http://localhost/',pretendToBeVisual:true});
for(const k of ['window','document','HTMLElement','HTMLInputElement','HTMLTextAreaElement','Event','KeyboardEvent'])globalThis[k]=dom.window[k];
Object.defineProperty(globalThis,'navigator',{configurable:true,value:dom.window.navigator});
globalThis.localStorage=window.localStorage;globalThis.IS_REACT_ACT_ENVIRONMENT=true;
window.HTMLElement.prototype.scrollTo=function(){};window.HTMLElement.prototype.scrollIntoView=function(){};
let releaseCreate;
let rejectCreate;
let releaseInviteCreate;
let rejectInviteCreate;
let createPending;
let inviteAvailable=true;
globalThis.fetch=async(url,init={})=>{
  const value=String(url);
  if(value.includes('/room-invites/paired-peers')) return inviteAvailable
    ? new Response(JSON.stringify({owner_profile:'owner',peers:[{peer_id:'peer',profile:'peer'}]}),{status:200,headers:{'content-type':'application/json'}})
    : new Response('{}',{status:404});
  if(value.endsWith('/lobbies') && init.method==='POST') { createPending=new Promise((resolve,reject)=>{releaseInviteCreate=()=>resolve(new Response(JSON.stringify({lobby:{lobby_id:'lobby-1'}}),{status:201,headers:{'content-type':'application/json'}}));rejectInviteCreate=()=>reject(new Error('synthetic invite creation failure'));}); return createPending; }
  if(value.endsWith('/lobbies/lobby-1')) return new Response(JSON.stringify({lobby:{lobby_id:'lobby-1',name:'Invite pending',state:'open',roster:[]}}),{status:200,headers:{'content-type':'application/json'}});
  if(value.endsWith('/imports/preview') && init.method==='POST') return new Response(JSON.stringify({import:{id:'import-1',status:'preview',host:{profile:'host-'+('x'.repeat(80)),installation_id:'install-'+('y'.repeat(120)),fingerprint:'fp-'+('z'.repeat(160))}}}),{status:200,headers:{'content-type':'application/json'}});
  if(value.endsWith('/imports/import-1/metadata') && init.method==='POST') return new Response(JSON.stringify({import:{id:'import-1',status:'metadata',metadata:{name:'Room '+('very-long-name-'.repeat(24)),expires_at:'2099-12-31T23:59:59.000Z'}}}),{status:200,headers:{'content-type':'application/json'}});
  return new Response('{}',{status:404});
};
const vite=await createServer({root:process.cwd(),configFile:false,cacheDir:'.tmp-room-integrated',logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false},plugins:[react()]});
const {createRoot}=await import('react-dom/client');
const {I18nProvider}=await vite.ssrLoadModule('/src/lib/i18n.tsx');
const {ChatDrawer}=await vite.ssrLoadModule('/src/components/ChatDrawer.tsx');
const {GroupGatewayClient}=await vite.ssrLoadModule('/src/lib/group-gateway.ts');
const room={id:'selected',name:'Synthetic room',authorityGatewayId:'fixture',authorityEpoch:1,latestSeq:0,disbandedAt:null,members:[{id:'a',profile:'alpha',handle:'alpha'},{id:'b',profile:'bravo',handle:'bravo'}]};
GroupGatewayClient.prototype.capabilities=async()=>({driver:true,protocolVersion:2,methods:[],maxLogLimit:100,authorityGatewayId:'fixture'});
GroupGatewayClient.prototype.list=async()=>({rooms:[room],nextOffset:null});
GroupGatewayClient.prototype.state=async()=>({room,driverStatus:{}});
GroupGatewayClient.prototype.log=async()=>({events:[],cursor:0,latestSeq:0,hasMore:false,authority:null});
GroupGatewayClient.prototype.create=async()=>new Promise((resolve,reject)=>{releaseCreate=()=>resolve({id:'created',name:'Created',members:[]});rejectCreate=()=>reject(new Error('synthetic local creation failure'));});
const root=createRoot(document.getElementById('root'));
const flush=async(fn=()=>{})=>act(async()=>{fn();for(let i=0;i<35;i++)await new Promise(setImmediate);});
const find=s=>{const n=document.querySelector(s);assert.ok(n,s);return n;};
const setValue=(node,value)=>{const setter=Object.getOwnPropertyDescriptor(node.constructor.prototype,'value')?.set;setter?.call(node,value);node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}));};
try {
 await test('integrated local pending creation disables both rail choices and stays drawer-local',async()=>{
  await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>{}}))));
  const opener=find('button[aria-label="Create room"]');await flush(()=>opener.click());
  const layer=find('.room-create-choice');assert.equal(layer.getAttribute('aria-modal'),null);
  const join=find('.room-create-choice-rail button:nth-child(2)');assert.equal(join.disabled,false);
  const createRadio=find('.room-create-choice-rail button:nth-child(1)');await flush(()=>{createRadio.focus();createRadio.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));});
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').getAttribute('aria-checked'),'true');
  assert.equal(document.activeElement,find('.room-create-choice-rail button:nth-child(2)'));
  await flush(()=>find('.room-create-choice-rail button:nth-child(2)').dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true,cancelable:true})));
  assert.equal(find('.room-create-choice-rail button:nth-child(1)').getAttribute('aria-checked'),'true');
  assert.equal(document.activeElement,find('.room-create-choice-rail button:nth-child(1)'));
  await flush(()=>find('.room-create-choice-rail button:nth-child(1)').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true,cancelable:true})));
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').getAttribute('aria-checked'),'true');
  assert.equal(document.activeElement,find('.room-create-choice-rail button:nth-child(2)'));
  await flush(()=>find('.room-create-choice').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true})));
  assert.equal(document.querySelector('.room-create-choice'),null);await flush(()=>opener.click());
  await flush(()=>setValue(find('input:not([type="checkbox"]):not([type="search"])'),'Local pending'));
  await flush(()=>find('.room-create-selection').click());await flush(()=>find('.room-create-submit').click());
  assert.equal(find('.room-create-choice-rail button:nth-child(1)').disabled,true);
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').disabled,true);
  releaseCreate();await flush();
 });
 await test('integrated invite-only pending creation also disables Join',async()=>{
  await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>{}}))));
  await flush(()=>find('button[aria-label="Create room"]').click());
  const invite=find('.room-create-kind-options button:nth-child(2)');await flush(()=>invite.click());
  const name=find('input[aria-label="Lobby name"]');await flush(()=>setValue(name,'Invite pending'));
  const select=find('select[aria-label="Paired peer"]');await flush(()=>{select.value='peer';select.dispatchEvent(new Event('change',{bubbles:true}));});
  const create=[...document.querySelectorAll('button')].find(button=>button.textContent?.includes('Create lobby'));assert.ok(create);await flush(()=>create.click());
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').disabled,true);
  releaseInviteCreate();await flush();
 assert.equal(document.querySelector('.room-create-choice'),null);
 assert.ok(document.querySelector('.chat-transcript.rooms-empty'));
 });
 await test('integrated local creation failure settles in place and rejects pointer/keyboard rail navigation while busy',async()=>{
  await flush(()=>root.render(null));
  await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>{}}))));
  await flush(()=>find('button[aria-label="Create room"]').click());
  await flush(()=>setValue(find('input:not([type="checkbox"]):not([type="search"])'),'Local failure'));
  await flush(()=>find('.room-create-selection').click());
  await flush(()=>find('.room-create-submit').click());
  const createRail=find('.room-create-choice-rail button:nth-child(1)');
  const joinRail=find('.room-create-choice-rail button:nth-child(2)');
  assert.equal(createRail.disabled,true);assert.equal(joinRail.disabled,true);
  await flush(()=>{joinRail.click();createRail.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));});
  assert.equal(createRail.getAttribute('aria-checked'),'true');
  rejectCreate();await flush();
  assert.ok(find('.room-create-form [role="alert"]'));
  assert.equal(find('.room-create-choice').isConnected,true);
  assert.equal(find('.room-create-choice-rail button:nth-child(1)').getAttribute('aria-checked'),'true');
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').disabled,false);
 });
 await test('integrated invite-only creation failure settles in place and rejects pointer/keyboard rail navigation while busy',async()=>{
  await flush(()=>root.render(null));
  await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>{}}))));
  await flush(()=>find('button[aria-label="Create room"]').click());
  await flush(()=>find('.room-create-kind-options button:nth-child(2)').click());
  await flush(()=>setValue(find('input[aria-label="Lobby name"]'),'Invite failure'));
  await flush(()=>{const select=find('select[aria-label="Paired peer"]');select.value='peer';select.dispatchEvent(new Event('change',{bubbles:true}));});
  const create=[...document.querySelectorAll('button')].find(button=>button.textContent?.includes('Create lobby'));assert.ok(create);await flush(()=>create.click());
  const createRail=find('.room-create-choice-rail button:nth-child(1)');
  const joinRail=find('.room-create-choice-rail button:nth-child(2)');
  assert.equal(createRail.disabled,true);assert.equal(joinRail.disabled,true);
  await flush(()=>{joinRail.click();createRail.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));});
  assert.equal(createRail.getAttribute('aria-checked'),'true');
  rejectInviteCreate();await flush();
  assert.ok(find('[role="alert"]'));
  assert.equal(find('.room-create-choice').isConnected,true);
  assert.equal(find('.room-create-kind-options button:nth-child(2)').getAttribute('aria-checked'),'true');
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').disabled,false);
 });
 await test('integrated local completion cannot close a replacement Join surface after same-turn owner invalidation',async()=>{
  await flush(()=>root.render(null));
  await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>{}}))));
  await flush(()=>find('button[aria-label="Create room"]').click());
  await flush(()=>setValue(find('input:not([type="checkbox"]):not([type="search"])'),'Local race'));
  await flush(()=>find('.room-create-selection').click());
  await act(async()=>{
    find('.room-create-submit').click();
    // Exercise the event-turn race before the child effect publishes busy.
    find('.room-create-choice-rail button:nth-child(2)').click();
  });
  for(let i=0;i<25;i++)await new Promise(setImmediate);
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').getAttribute('aria-checked'),'true');
  const code=find('textarea');setValue(code,'REPLACEMENT-CODE');
  releaseCreate();await flush();
  assert.equal(find('textarea').value,'REPLACEMENT-CODE');
 });
 await test('integrated invite completion cannot close a replacement Join surface after same-turn owner invalidation',async()=>{
  await flush(()=>root.render(null));
  await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>{}}))));
  await flush(()=>find('button[aria-label="Create room"]').click());
  await flush(()=>find('.room-create-kind-options button:nth-child(2)').click());
  await flush(()=>setValue(find('input[aria-label="Lobby name"]'),'Invite race'));
  await flush(()=>{const select=find('select[aria-label="Paired peer"]');select.value='peer';select.dispatchEvent(new Event('change',{bubbles:true}));});
  await act(async()=>{
    const create=[...document.querySelectorAll('button')].find(button=>button.textContent?.includes('Create lobby'));assert.ok(create);create.click();
    // The rail must invalidate the invite owner before its late response settles.
    find('.room-create-choice-rail button:nth-child(2)').click();
  });
  for(let i=0;i<25;i++)await new Promise(setImmediate);
  assert.equal(find('.room-create-choice-rail button:nth-child(2)').getAttribute('aria-checked'),'true');
  setValue(find('textarea'),'INVITE-REPLACEMENT');
  releaseInviteCreate();await flush();
  assert.equal(find('textarea').value,'INVITE-REPLACEMENT');
 });
 await test('integrated Join metadata renders long host identity and room name inside the real drawer surface',async()=>{
  await flush(()=>root.render(null));
  await flush(()=>root.render(React.createElement(I18nProvider,null,React.createElement(ChatDrawer,{open:true,chatMode:'room',roomId:'selected',storedToken:'',onClose:()=>{}}))));
  await flush(()=>find('button[aria-label="Create room"]').click());
  await flush(()=>find('.room-create-choice-rail button:nth-child(2)').click());
  const textarea=find('.invite-ui textarea');setValue(textarea,'LONG-SYNTHETIC-CODE');
  assert.equal(textarea.value,'LONG-SYNTHETIC-CODE');
  assert.equal([...document.querySelectorAll('.invite-ui button')].filter(button=>button.textContent?.includes('Preview invitation')).at(-1)?.disabled,false);
  await flush(()=>[...document.querySelectorAll('.invite-ui button')].filter(button=>button.textContent?.includes('Preview invitation')).at(-1)?.click());
  assert.ok(find('.invite-notice').textContent.includes('fp-'+('z'.repeat(160))));
  await flush(()=>[...document.querySelectorAll('.invite-ui button')].filter(button=>button.textContent?.includes('Read room details')).at(-1)?.click());
  const longName='Room '+('very-long-name-'.repeat(24));
  assert.ok(find('.invite-ui').textContent.includes(longName));
  assert.ok(find('.room-create-scroll'));
 });
 await test('integrated availability loss tears down Join and clears its task boundary',async()=>{
  inviteAvailable=true;await flush(()=>find('button[aria-label="Create room"]').click());
  await flush(()=>find('.room-create-choice-rail button:nth-child(2)').click());
  const code=document.querySelector('textarea');if(code){await flush(()=>setValue(code,'SYNTHETIC-CODE'));}
  inviteAvailable=false;await flush(()=>window.dispatchEvent(new Event('focus')));
  assert.equal(document.querySelector('.room-create-join'),null);
 });
} finally { await vite.close(); dom.window.close(); }
