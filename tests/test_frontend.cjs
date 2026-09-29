// Run with: node --test tests/test_frontend.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const code = fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const settle = async () => {await new Promise(setImmediate);await new Promise(setImmediate);};

function page({storage=new Map(),online=true,serverFailure=false,audioReady=true,token='private-session-token'}={}) {
  const nodes=new Map(), events={}, reports=[], requests=[];
  function node(id) {
    if (!nodes.has(id)) nodes.set(id,{value:['voice-mode','music-voice-mode'].includes(id)?'keep':'',textContent:'',hidden:true,handlers:{},addEventListener(name,fn){this.handlers[name]=fn;},getContext(){return {};},append(){},after(){},replaceChildren(){},add(){},pause(){}});
    return nodes.get(id);
  }
  const response = (body,status=200) => ({status,ok:status<400,json:async()=>body});
  const context=vm.createContext({
    document:{getElementById:node,querySelector:()=>({content:token}),createElement:()=>node(randomUUID())},
    window:{addEventListener:(name,fn)=>events[name]=fn},
    sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    crypto:{randomUUID},Option:function(){},setInterval(){},setTimeout,clearTimeout,AbortSignal,
    fetch:async(path,options)=>{
      if (!online) throw new Error('offline');
      requests.push({path,body:options?.body?JSON.parse(options.body):null});
      if (path==='/api/state') return response({profiles:[],active:null,logs:{write_error:false},audio:{ready:audioReady}});
      if (path==='/api/client-error') {reports.push(JSON.parse(options.body));return response({ok:true,logs:{write_error:false}});}
      if (path==='/api/generate' && serverFailure) return response({error:'invalid test video',request_id:'request-test-id'},400);
      throw new Error('unexpected test request: '+path);
    }
  });
  vm.runInContext(code,context,{filename:'web/app.js'});
  return {nodes,events,reports,storage,node,requests};
}

test('caught UI error is persisted with the displayed reference ID',async()=>{
  const p=page();await settle();
  await p.node('generate').handlers.click();await settle();
  assert.equal(p.reports.length,1);
  assert.match(p.reports[0].message,/选择录像/);
  assert.ok(p.node('error').textContent.includes(p.reports[0].client_id));
  assert.equal(p.storage.get('videocut-pending-errors-v1'),'[]');
});

test('global errors and unhandled rejections are scrubbed before persistence',async()=>{
  const p=page();await settle();
  p.events.error({error:new Error('private-session-token token=hidden-value')});
  p.events.unhandledrejection({reason:new Error('promise test')});await settle();
  assert.equal(p.reports.length,2);
  const raw=JSON.stringify(p.reports)+p.storage.get('videocut-pending-errors-v1');
  assert.ok(!raw.includes('private-session-token'));
  assert.ok(!raw.includes('hidden-value'));
  assert.deepEqual(p.reports.map(r=>r.source),['window.error','unhandledrejection']);
});

test('offline reports survive reload and flush with the new session',async()=>{
  const p=page({online:false});await settle();
  await p.node('generate').handlers.click();await settle();
  const queued=JSON.parse(p.storage.get('videocut-pending-errors-v1'));
  assert.ok(queued.length>0);
  assert.match(p.node('log-status').textContent,/待写入/);
  const restored=page({storage:p.storage,token:'new-session-token'});await settle();
  assert.deepEqual(restored.reports.map(r=>r.client_id),queued.map(r=>r.client_id));
  assert.equal(restored.storage.get('videocut-pending-errors-v1'),'[]');
});

test('server errors show their request ID without duplicate frontend reports',async()=>{
  const p=page({serverFailure:true});await settle();
  p.node('video').value='test.mp4';p.node('music').value='test.wav';p.node('profile').value='demo';
  await p.node('generate').handlers.click();await settle();
  assert.match(p.node('error').textContent,/request-test-id/);
  assert.equal(p.reports.length,0);
});

test('voice selectors default to keeping both sources and toggle explicit track selection',async()=>{
  const p=page();await settle();
  assert.equal(p.node('voice-mode').value,'keep');assert.equal(p.node('music-voice-mode').value,'keep');
  assert.equal(p.node('game-track-group').hidden,true);
  p.node('voice-mode').value='game_track';p.node('voice-mode').handlers.change();
  assert.equal(p.node('game-track-group').hidden,false);assert.equal(p.node('source-track-group').hidden,true);
});

test('independent track mode never silently selects the first track',async()=>{
  const p=page();await settle();
  p.node('video').value='test.mp4';p.node('music').value='test.wav';p.node('profile').value='demo';p.node('voice-mode').value='game_track';
  await p.node('generate').handlers.click();await settle();
  assert.match(p.node('error').textContent,/手动选择独立游戏音轨/);
  assert.ok(!p.requests.some(r=>r.path==='/api/generate'));
});

test('AI dependency unavailable shows a setup message before a job is submitted',async()=>{
  const p=page({audioReady:false});await settle();
  p.node('video').value='test.mp4';p.node('music').value='test.wav';p.node('profile').value='demo';p.node('voice-mode').value='reduce';
  await p.node('generate').handlers.click();await settle();
  assert.equal(p.node('audio-setup').hidden,false);assert.match(p.node('error').textContent,/尚未安装/);
  assert.ok(!p.requests.some(r=>r.path==='/api/generate'));
});

test('video and music voice settings are sent independently',async()=>{
  const p=page({serverFailure:true});await settle();
  p.node('video').value='test.mp4';p.node('music').value='test.wav';p.node('profile').value='demo';
  p.node('voice-mode').value='reduce';p.node('music-voice-mode').value='keep';
  await p.node('generate').handlers.click();await settle();
  const sent=p.requests.find(r=>r.path==='/api/generate').body;
  assert.equal(sent.voice_mode,'reduce');assert.equal(sent.music_voice_mode,'keep');
});
