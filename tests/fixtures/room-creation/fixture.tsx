import React,{useState,StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {ChatDrawer} from '/src/components/ChatDrawer';
import {GroupGatewayClient} from '/src/lib/group-gateway';
import {I18nProvider,LOCALE_STORAGE_KEY} from '/src/lib/i18n';
import '/src/styles.css';
const params=new URLSearchParams(location.search);
localStorage.setItem(LOCALE_STORAGE_KEY,params.has('it')?'it':'en');
const count=params.has('empty')?0:params.has('many')?32:8;
const handles=['default','research','release','quality','design','docs','ops','review'];
window.__fixture={calls:[],requests:[],unexpected:[],closed:0,changes:[],outside:0,escape:0};
window.__fixtureProfiles=Array.from({length:count},(_,i)=>({name:params.has('long')&&i===2?'long_handle_'+ 'unbroken'.repeat(18):handles[i]||`agent-${i}`,display_name:params.has('long')&&i===2?'LongDisplayName'.repeat(12):i===1?'Research assistant':handles[i]||`Agent ${i}`,model:i===1?'Synthetic reasoning model':'Synthetic general model',is_bot:true}));
const room={id:'selected',name:'Release Readiness · Demo',authorityGatewayId:'fixture',authorityEpoch:1,latestSeq:1,disbandedAt:null,members:[]};
GroupGatewayClient.prototype.capabilities=async()=>({driver:true,protocolVersion:2,methods:[],maxLogLimit:100,authorityGatewayId:'fixture'});
GroupGatewayClient.prototype.list=async()=>({rooms:[room],nextOffset:null});
GroupGatewayClient.prototype.state=async()=>({room,driverStatus:{}});
GroupGatewayClient.prototype.log=async()=>({events:[{id:'fixture-event',seq:1,kind:'message',actor:{kind:'user',id:'fixture'},message:{text:'The selected room and this transcript stay here while you create another room.'},createdAt:0}],cursor:1,latestSeq:1,hasMore:false,authority:null});
GroupGatewayClient.prototype.create=async body=>{window.__fixture.calls.push(body);if(params.has('saving'))await new Promise(resolve=>window.__finishSaving=resolve);throw Error(params.has('error')?'Synthetic callback failure: '+ 'long-error-'.repeat(30):'Synthetic create rejected — no real room was created.');};
window.fetch=async(url,options={})=>{
 const path=new URL(String(url),location.origin).pathname;
 window.__fixture.requests.push({path,method:options.method||'GET'});
 let data;
 if(path==='/api/local/candidates/vaults')data={vaults:[{id:'fixture-vault',label:'Core (synthetic)'}],default_vault:'fixture-vault'};
 else if(path==='/api/local/room-invites/paired-peers'){
  if(params.has('localOnly'))return new Response('{}',{status:404});
  const probes=window.__fixture.requests.filter(r=>r.path===path).length;
  if(params.has('loading')&&probes>2)await new Promise(()=>{});
  if(params.has('peerError')&&probes>2)return new Response('{"code":"fixture_peer_error"}',{status:503});
  data={owner_profile:'synthetic-owner',peers:params.has('noPeers')?[]:[{peer_id:'synthetic-peer',profile:'Synthetic peer',installation_id:'fixture',capability_digest:'fixture'}]};
 }else if(path==='/api/local/room-invites/guest-rooms')data={rooms:[]};
 else if(path==='/api/local/room/tools')data={tools:[]};
 else if(path==='/api/local/room-invites/lobbies'&&options.method==='POST'){
  window.__fixture.inviteBody=JSON.parse(options.body);
  if(params.has('inviteSaving'))await new Promise(resolve=>window.__finishInvite=resolve);
  return new Response('{"code":"synthetic_rejection"}',{status:409});
 }else{window.__fixture.unexpected.push({path,method:options.method||'GET'});return new Response('{}',{status:404});}
 return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
};
window.WebSocket=class{constructor(){window.__fixture.unexpected.push('WebSocket');throw Error('Synthetic fixture forbids WebSocket');}};
document.addEventListener('keydown',e=>{if(e.key==='Escape')window.__fixture.escape++;});
function Fixture(){const [open,setOpen]=useState(true);return <><main style={{padding:32,maxWidth:600}}><h1 style={{fontSize:24}}>Mission Control</h1><p>Isolated UI fixture · synthetic data only</p><button id="outside" className="chat-control" onClick={()=>window.__fixture.outside++}>Outside drawer action</button></main><ChatDrawer open={open} chatMode="room" roomId="selected" storedToken="" onClose={()=>{window.__fixture.closed++;setOpen(false);}} onRoomChange={id=>window.__fixture.changes.push(id)}/></>;}
createRoot(document.getElementById('root')).render(<StrictMode><I18nProvider><Fixture/></I18nProvider></StrictMode>);
