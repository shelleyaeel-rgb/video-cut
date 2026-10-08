// Run with: node --test tests/test_frontend.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const code = fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const settle = async () => {await new Promise(setImmediate);await new Promise(setImmediate);};

function page({storage=new Map(),online=true,serverFailure=false,audioReady=true,token='private-session-token',onRequest,desktop=false,preferences={},update={state:'idle',supported:false}}={}) {
  const nodes=new Map(), events={}, reports=[], requests=[];
  function node(id) {
    if (!nodes.has(id)) nodes.set(id,{value:['voice-mode','music-voice-mode'].includes(id)?'keep':'',textContent:'',hidden:true,handlers:{},children:[],addEventListener(name,fn){this.handlers[name]=fn;},getContext(){return {};},append(child){this.children.push(child);},after(){},replaceChildren(){this.children=[];},add(){},pause(){}});
    return nodes.get(id);
  }
  const response = (body,status=200) => ({status,ok:status<400,json:async()=>body});
  const context=vm.createContext({
    document:{getElementById:node,querySelector:()=>({content:token}),createElement:()=>node(randomUUID()),createTextNode:text=>({textContent:text})},
    window:{addEventListener:(name,fn)=>events[name]=fn},
    sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    crypto:{randomUUID},Option:function(){},setInterval(){},setTimeout,clearTimeout,AbortSignal,
    fetch:async(path,options)=>{
      if (!online) throw new Error('offline');
      requests.push({path,body:options?.body?JSON.parse(options.body):null});
      if (path==='/api/state') return response({profiles:[],active:null,version:'0.2.0',desktop,data_dir:'C:/local-data',logs:{write_error:false},audio:{ready:audioReady}});
      if (path==='/api/update') return response(update);
      if (path==='/api/preferences') {if(options.body)Object.assign(preferences,JSON.parse(options.body));return response(preferences);}
      if (path==='/api/client-error') {reports.push(JSON.parse(options.body));return response({ok:true,logs:{write_error:false}});}
      if (path==='/api/generate' && serverFailure) return response({error:'invalid test video',request_id:'request-test-id'},400);
      if (onRequest) return response(await onRequest(path, options?.body?JSON.parse(options.body):null));
      throw new Error('unexpected test request: '+path);
    }
  });
  vm.runInContext(code,context,{filename:'web/app.js'});
  return {nodes,events,reports,storage,node,requests};
}

test('updates are opt-in and source mode cannot install or quit',async()=>{
  const p=page();await settle();
  assert.equal(p.node('app-version').textContent,'v0.2.0');
  assert.ok(!p.requests.some(r=>r.path==='/api/update/check'));
  assert.equal(p.node('download-update').hidden,true);
  assert.equal(p.node('quit-app').hidden,true);
  assert.match(p.node('update-mode').textContent,/源码/);
});

test('desktop update errors remain readable and can be retried',async()=>{
  const update={state:'error',supported:true,error:'无法连接 GitHub'};
  const p=page({desktop:true,update,onRequest:async path=>{
    assert.equal(path,'/api/update/check');Object.assign(update,{state:'available',error:null,message:'发现新版本 0.3.0',version:'0.3.0'});return update;
  }});await settle();
  assert.match(p.node('update-status').textContent,/无法连接/);
  assert.equal(p.node('check-update').disabled,false);
  await p.node('check-update').handlers.click();await settle();
  assert.equal(p.node('download-update').hidden,false);
  assert.match(p.node('update-status').textContent,/0.3.0/);
});

test('active jobs disable prepared update installation and desktop exit',async()=>{
  const p=page({desktop:true,update:{state:'ready',supported:true},onRequest:async path=>{
    if(path==='/api/demo')return {id:'busy-job'};
    if(path.startsWith('/api/job'))return new Promise(()=>{});
    throw new Error(path);
  }});await settle();
  assert.equal(p.node('install-update').disabled,false);
  await p.node('demo').handlers.click();await settle();
  assert.equal(p.node('install-update').disabled,true);
  assert.equal(p.node('quit-app').disabled,true);
  assert.match(p.node('update-status').textContent,/等待任务/);
});

test('accepted installation stops update polling and suppresses shutdown errors',async()=>{
  const p=page({desktop:true,update:{state:'ready',supported:true},onRequest:async path=>{
    assert.equal(path,'/api/update/install');return {state:'installing',supported:true};
  }});await settle();
  const polls=p.requests.filter(r=>r.path==='/api/update').length;
  await p.node('install-update').handlers.click();await settle();
  p.events.error({error:new Error('connection closed for restart')});await settle();
  assert.equal(p.requests.filter(r=>r.path==='/api/update').length,polls);
  assert.equal(p.reports.length,0);
  assert.equal(p.node('check-update').disabled,true);
  assert.match(p.node('update-status').textContent,/重新打开/);
});

test('user defaults restore and save only edited preference fields',async()=>{
  const preferences={target:60,'voice-mode':'game_track',strength:.2};
  const p=page({preferences});await settle();
  assert.equal(p.node('target').value,'60');
  assert.equal(p.node('game-track-group').hidden,false);
  assert.equal(p.node('strength-value').textContent,'轻量');
  p.node('video').value='private.mp4';p.node('target').value='80';
  await p.node('target').handlers.change();await settle();
  assert.deepEqual(p.requests.find(r=>r.path==='/api/preferences'&&r.body).body,{target:'80'});
  assert.equal(preferences.target,'80');
});

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

test('blank BGM submits with video and profile even with stale BGM AI settings',async()=>{
  const p=page({serverFailure:true,audioReady:false});await settle();
  p.node('video').value='test.mp4';p.node('profile').value='demo';p.node('music').value='  ';
  p.node('music-voice-mode').value='instrumental';p.node('music-start').value='bad';
  await p.node('generate').handlers.click();await settle();
  const sent=p.requests.find(r=>r.path==='/api/generate').body;
  assert.equal(sent.music,'');assert.equal(sent.music_start,0);assert.equal(sent.music_volume,0);assert.equal(sent.music_voice_mode,'keep');
});

test('adding and clearing BGM toggles only music controls',async()=>{
  const p=page();await settle();
  const controls=['music-start','music-volume','music-voice-mode','clear-music'];
  controls.forEach(id=>assert.equal(p.node(id).disabled,true));
  p.node('music').value='test.wav';p.node('music').handlers.input();
  controls.forEach(id=>assert.equal(p.node(id).disabled,false));
  p.node('clear-music').handlers.click();
  assert.equal(p.node('music').value,'');assert.match(p.node('music-info').textContent,/未添加 BGM/);
  controls.forEach(id=>assert.equal(p.node(id).disabled,true));
  assert.notEqual(p.node('voice-mode').disabled,true);
  assert.notEqual(p.node('game-volume').disabled,true);
});

function profileResult(events=[]) {
  return {video:'test.mp4',profile:'demo',profile_name:'测试模板',threshold:.82,start:8,duration:8,requested_duration:8,events,elapsed_seconds:.2};
}
function fillProfileTest(p) {
  for(const [id,value] of Object.entries({video:'test.mp4',profile:'demo','test-start':'8','test-duration':'8',threshold:'.82'})) p.node(id).value=value;
}

test('profile testing ignores music and AI settings, shows empty results, and preserves montage',async()=>{
  const p=page({audioReady:false,onRequest:async(path)=>{
    if(path==='/api/test-profile')return {id:'test-job'};
    if(path.startsWith('/api/job'))return {id:'test-job',kind:'profile_test',state:'complete',stage:'配置测试完成',progress:100,result:profileResult()};
    throw new Error(path);
  }});await settle();fillProfileTest(p);
  p.node('music').value='missing.wav';p.node('voice-mode').value='reduce';
  p.node('result-video').src='previous.mp4';p.node('result-video').hidden=false;
  await p.node('test-profile').handlers.click();await settle();
  assert.deepEqual(p.requests.find(r=>r.path==='/api/test-profile').body,{video:'test.mp4',profile:'demo',start:8,duration:8,threshold:.82});
  assert.match(p.node('test-status').textContent,/未找到候选事件/);
  assert.equal(p.node('test-result').hidden,false);
  assert.equal(p.node('result-video').src,'previous.mp4');assert.equal(p.node('result-video').hidden,false);
  assert.ok(!p.requests.some(r=>r.path==='/api/generate'));
});

test('profile test rejects invalid duration before submitting',async()=>{
  const p=page();await settle();fillProfileTest(p);p.node('test-duration').value='600';
  await p.node('test-profile').handlers.click();await settle();
  assert.match(p.node('error').textContent,/测试时长/);
  assert.ok(!p.requests.some(r=>r.path==='/api/test-profile'));
});

test('event preview uses source time and stale frame responses cannot restore invalidated results',async()=>{
  let resolveFrame;
  const p=page({onRequest:async(path)=>{
    if(path==='/api/test-profile')return {id:'test-job'};
    if(path.startsWith('/api/job'))return {kind:'profile_test',state:'complete',progress:100,result:profileResult([{time:10,end:10.8,confidence:.97}])};
    if(path==='/api/frame')return new Promise(resolve=>{resolveFrame=resolve;});
    throw new Error(path);
  }});await settle();fillProfileTest(p);
  await p.node('test-profile').handlers.click();await settle();
  const click=p.node('test-events').children[0].children[0].handlers.click();await settle();
  assert.deepEqual(p.requests.find(r=>r.path==='/api/frame').body,{video:'test.mp4',time:10});
  p.node('threshold').value='.9';p.node('threshold').handlers.input();
  resolveFrame({image:'data:image/png;base64,old'});await click;await settle();
  assert.equal(p.node('test-result').hidden,true);assert.equal(p.node('test-frame').hidden,true);
  assert.match(p.node('test-status').textContent,/重新测试/);
});

test('classification evidence and manual exclusions are sent to generation and cleared on changes',async()=>{
  const result={...profileResult([
    {time:10,end:10.5,confidence:.96,classification:'unknown',reason:'依据冲突',event_types:['kill'],matches:[{name:'共同图标'}],evidence:[{name:'玩家标记',confidence:.95},{name:'小兵标记',confidence:.96}]},
    {time:12,end:12.5,confidence:.97,classification:'npc',reason:'小兵依据',event_types:['kill'],matches:[{name:'共同图标'}],evidence:[]}
  ]),review_signature:'review-fixture'};
  const p=page({serverFailure:true,onRequest:async(path)=>{
    if(path==='/api/test-profile')return {id:'test-job'};
    if(path.startsWith('/api/job'))return {kind:'profile_test',state:'complete',progress:100,result};
    throw new Error(path);
  }});await settle();fillProfileTest(p);
  await p.node('test-profile').handlers.click();await settle();
  const rows=p.node('test-events').children;
  assert.match(rows[0].children[0].textContent,/待确认/);
  assert.match(rows[0].children[1].textContent,/依据冲突.*玩家标记.*小兵标记/);
  assert.match(rows[1].children[0].textContent,/小兵.*自动过滤/);
  assert.equal(rows[1].children.length,2);
  const checkbox=rows[0].children[2].children[0];checkbox.checked=true;checkbox.handlers.change();
  assert.match(p.node('review-status').textContent,/排除 1/);
  await p.node('generate').handlers.click();await settle();
  assert.deepEqual(p.requests.find(r=>r.path==='/api/generate').body.review,{signature:'review-fixture',excluded:[[10,10.5]]});
  p.node('threshold').value='.9';p.node('threshold').handlers.input();
  await p.node('generate').handlers.click();await settle();
  assert.equal(p.requests.filter(r=>r.path==='/api/generate').at(-1).body.review,undefined);
  assert.match(p.node('review-status').textContent,/排除 0/);
});
