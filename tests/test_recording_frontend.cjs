// Run with: node --test tests/test_recording_frontend.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const appCode = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const recordingCode = fs.readFileSync(path.join(__dirname, '../web/recording.js'), 'utf8');
const settle = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const copy = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };

function project(id='project-a', overrides={}) {
  return {id, state:'ready', created_at:'2026-10-08T12:00:00Z', revision:4,
    source:{path:`C:/recordings/${id}/source.mkv`, metadata:{duration:80,width:1920,height:1080,audio_tracks:[{index:0,title:'system'}]}},
    markers:[{event_id:`${id}-marker`, time:12,end:13,event_types:['kill'],state:'confirmed',source:'realtime'}],
    selection:[`${id}-marker`], settings:{pre:5,post:2}, coverage:[], ...overrides};
}

function page({sessions=[project()], status={state:'idle',busy:false}, onRequest, storage=new Map()}={}) {
  const nodes = new Map(), requests = [], timers = new Map();
  const defaults = {'voice-mode':'keep','music-voice-mode':'keep','scan-scope':'all',
    'capture-audio':'system','capture-fps':'60','project-pre':'5','project-post':'2',
    'target':'90','strength':'.5','game-volume':'.65','music-volume':'.65','threshold':'.82',
    'audio-track':'0','encoder':'cpu','effect-preset':'light'};
  let nextTimer=0;
  function node(id, tag='div') {
    if (!nodes.has(id)) {
      const handlers = {}, callbacks = {};
      nodes.set(id, {id,tagName:tag.toUpperCase(),value:defaults[id] ?? '', textContent:'',hidden:true,
        disabled:false,checked:id==='capture-detect',children:[],handlers,paused:0,
        addEventListener(name, fn) { (callbacks[name] ||= []).push(fn); handlers[name] = async event => {
          for (const callback of callbacks[name]) await callback(event || {target:this});
        }; },
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children=[...children]; if (this.id==='capture-source') this.value=children[0]?.value ?? ''; },
        add(child) { this.children.push(child); }, after() {}, getContext() {return {};}, pause() {this.paused++;}
      });
    }
    return nodes.get(id);
  }
  const descendants = root => root.children.flatMap(child => [child,...descendants(child)]);
  const response = (body, statusCode=200) => ({status:statusCode,ok:statusCode<400,json:async()=>copy(body)});
  const context=vm.createContext({
    document:{getElementById:node, querySelector:()=>({content:'test-token'}),
      createElement:tag=>node(randomUUID(),tag), createTextNode:text=>({textContent:text,children:[]}),
      querySelectorAll:()=>descendants(node('project-markers')).filter(item=>['INPUT','BUTTON'].includes(item.tagName))},
    window:{addEventListener(){}}, sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    crypto:{randomUUID},Option:function(text,value){this.textContent=text;this.value=value;this.children=[];},
    setInterval(){},setTimeout(fn,delay){const id=++nextTimer;timers.set(id,{fn,delay});return id;},
    clearTimeout(id){timers.delete(id);},AbortSignal,
    fetch:async (route, options) => {
      const body=options?.body ? JSON.parse(options.body) : null;
      requests.push({route,body});
      if(route==='/api/state')return response({profiles:[{id:'delta-auto',name:'Delta',kind:'delta_auto',templates:[]}],
        auto_detection:{available:true},active:null,version:'0.4.0',desktop:true,logs:{write_error:false},audio:{ready:true}});
      if(route==='/api/preferences')return response({});
      if(route==='/api/update')return response({state:'idle',supported:true});
      if(route==='/api/client-error')return response({ok:true,logs:{write_error:false}});
      if(onRequest){const value=await onRequest(route,body);if(value!==undefined)return response(value);}
      if(route==='/api/recording/capabilities')return response({available:true});
      if(route==='/api/recordings')return response({sessions});
      if(route==='/api/recording')return response(status);
      if(route==='/api/recording/sources')return response({sources:[{kind:'window',id:'server-window',name:'Delta',width:1920,height:1080}]});
      if(route.startsWith('/api/recording/project?')){
        const id=new URL(route,'http://local').searchParams.get('id');return response(sessions.find(item=>item.id===id));
      }
      if(route==='/api/recording/selection'){
        const found=sessions.find(item=>item.id===body.session_id);
        found.selection=body.selected_ids;found.settings={pre:body.pre,post:body.post};found.revision++;
        return response(found);
      }
      if(route==='/api/probe')return response({duration:80,width:1920,height:1080,audio_tracks:[{index:0,title:'system'}]});
      throw new Error('Unexpected test request: '+route);
    }
  });
  vm.runInContext(appCode,context,{filename:'web/app.js'});
  vm.runInContext(recordingCode,context,{filename:'web/recording.js'});
  return {node,requests,storage,context,timers,
    async click(id){const item=typeof id==='string'?node(id):id;if(!item.disabled)await item.handlers.click?.();await settle();},
    async tick(delay=1500){const item=[...timers].find(([,value])=>value.delay===delay);assert.ok(item,`No ${delay} ms timer`);timers.delete(item[0]);await item[1].fn();await settle();},
    async open(index=0){await this.click(node('recording-sessions').children[index]);},
    async chooseSource(){await this.click('refresh-capture-sources');node('capture-source').value='0';await node('capture-source').handlers.change();}
  };
}

test('reload reads server recording status and restores the active controls without restarting capture',async()=>{
  const status={state:'recording',busy:true,session_id:'project-a',elapsed_seconds:45,marker_count:7,hotkey:true};
  const p=page({status});await settle();
  assert.equal(p.node('stop-recording').disabled,false);
  assert.equal(p.node('mark-recording').disabled,false);
  assert.equal(p.node('start-recording').disabled,true);
  assert.equal(p.node('generate').disabled,true);
  assert.equal(Number(p.node('recording-marker-count').textContent),7);
  assert.equal(p.requests.filter(item=>item.route==='/api/recording/start').length,0);
});

test('server selection, padding and original source survive refresh and reopening the project',async()=>{
  const sessions=[project()];
  const p=page({sessions});await settle();await p.open();
  const checkbox=p.node('project-markers').children[0].children[0].children[0];
  checkbox.checked=false;await checkbox.handlers.change();
  p.node('project-pre').value='8';p.node('project-post').value='3';
  await p.click('save-project-selection');
  const saved=p.requests.find(item=>item.route==='/api/recording/selection').body;
  assert.deepEqual(saved,{session_id:'project-a',selected_ids:[],revision:4,pre:8,post:3});
  const refreshed=page({sessions,storage:p.storage});await settle();await refreshed.open();
  assert.equal(refreshed.node('project-markers').children[0].children[0].children[0].checked,false);
  assert.equal(Number(refreshed.node('project-pre').value),8);
  assert.equal(refreshed.node('project-source').textContent,sessions[0].source.path);
  assert.equal(refreshed.node('export-collection').disabled,false);
});

test('a failed start releases pending controls and submits only the server source descriptor',async()=>{
  const p=page({onRequest:async(route)=>{if(route==='/api/recording/start')throw new Error('Encoder unavailable');}});
  await settle();await p.chooseSource();
  p.node('capture-source').children[1].textContent='misleading client title';
  await p.click('start-recording');
  assert.deepEqual(p.requests.find(item=>item.route==='/api/recording/start').body.source,
    {kind:'window',id:'server-window',name:'Delta',width:1920,height:1080});
  assert.equal(p.node('start-recording').disabled,false);
  assert.equal(p.node('capture-source').disabled,false);
  assert.equal(p.node('generate').disabled,false);
  assert.equal(p.node('stop-recording').disabled,true);
});

test('stopping keeps import/export locked until server finalization and then opens the saved project',async()=>{
  const status={state:'recording',busy:true,session_id:'project-a'};
  const p=page({status,onRequest:async(route)=>{if(route==='/api/recording/stop'){Object.assign(status,{state:'stopping',busy:true});return status;}}});
  await settle();await p.click('stop-recording');
  assert.equal(p.node('stop-recording').disabled,true);
  assert.equal(p.node('mark-recording').disabled,true);
  assert.equal(p.node('generate').disabled,true);
  Object.assign(status,{state:'ready',busy:false});await p.tick();
  assert.equal(p.node('recording-project').hidden,false);
  assert.equal(p.node('project-source').textContent,'C:/recordings/project-a/source.mkv');
  assert.equal(p.node('export-collection').disabled,false);
  assert.equal(p.node('generate').disabled,false);
});

test('marking sends no browser timestamp and displays the fresh server count',async()=>{
  const status={state:'recording',busy:true,session_id:'project-a',marker_count:2};
  const p=page({status,onRequest:async(route)=>{if(route==='/api/recording/mark'){status.marker_count=9;return {time:123.456};}}});
  await settle();p.node('recording-clock').textContent='99:59.9';await p.click('mark-recording');
  assert.deepEqual(p.requests.find(item=>item.route==='/api/recording/mark').body,{});
  assert.equal(Number(p.node('recording-marker-count').textContent),9);
});

test('recovering an interrupted session reloads its verified server asset before allowing export',async()=>{
  const sessions=[project('project-a',{state:'interrupted'})];
  const p=page({sessions,onRequest:async(route)=>{
    if(route==='/api/recording/recover')return {id:'recovery-job'};
    if(route==='/api/job?id=recovery-job'){
      sessions[0]={...sessions[0],state:'ready',revision:5,
        source:{...sessions[0].source,path:'C:/recordings/project-a/recovered.mkv'}};
      return {id:'recovery-job',state:'complete',progress:100,result:sessions[0]};
    }
  }});await settle();await p.open();
  assert.equal(p.node('recover-recording').hidden,false);
  assert.equal(p.node('export-collection').disabled,true);
  await p.click('recover-recording');
  assert.deepEqual(p.requests.find(item=>item.route==='/api/recording/recover').body,{session_id:'project-a'});
  assert.equal(p.node('project-source').textContent,'C:/recordings/project-a/recovered.mkv');
  assert.equal(p.node('recover-recording').hidden,true);
  assert.equal(p.node('export-collection').disabled,false);
  assert.equal(p.node('project-markers').children[0].children[0].children[0].checked,true);
});

test('a rejected stop keeps capture busy and permits retry without enabling montage',async()=>{
  const status={state:'recording',busy:true,session_id:'project-a'};
  const p=page({status,onRequest:async(route)=>{if(route==='/api/recording/stop')throw new Error('Temporary connection failure');}});
  await settle();await p.click('stop-recording');
  assert.equal(p.node('stop-recording').disabled,false);
  assert.equal(p.node('start-recording').disabled,true);
  assert.equal(p.node('generate').disabled,true);
  assert.equal(vm.runInContext('recordingBusy',p.context),true);
});

for(const finalState of ['failed','cancelled']){
  test(`a ${finalState} project job releases preview and export controls`,async()=>{
    const p=page({onRequest:async(route)=>{
      if(route==='/api/recording/preview')return {id:'preview-job'};
      if(route==='/api/job?id=preview-job')return {id:'preview-job',state:finalState,stage:finalState,progress:100,error:'preview failed'};
    }});await settle();await p.open();
    await p.click(p.node('project-markers').children[0].children[1]);
    assert.equal(p.node('export-collection').disabled,false);
    assert.equal(p.node('prepare-montage').disabled,false);
    assert.equal(p.node('project-markers').children[0].children[1].disabled,false);
    assert.equal(p.node('project-preview').hidden,true);
    assert.equal(p.node('generate').disabled,false);
  });
}

test('a late preview cannot display media belonging to the previously selected project',async()=>{
  const waiting=deferred();
  const p=page({sessions:[project(),project('project-b')],onRequest:async(route)=>{
    if(route==='/api/recording/preview')return {id:'old-preview'};
    if(route==='/api/job?id=old-preview')return waiting.promise;
  }});await settle();await p.open();
  await p.click(p.node('project-markers').children[0].children[1]);
  await p.open(1);
  waiting.resolve({id:'old-preview',state:'complete',progress:100,result:{media_id:'old-media',start:7,end:15}});
  await settle();
  assert.equal(p.node('project-source').textContent,'C:/recordings/project-b/source.mkv');
  assert.equal(p.node('project-preview').hidden,true);
  assert.ok(!String(p.node('project-preview').src).includes('old-media'));
  assert.equal(p.node('export-collection').disabled,false);
});

test('a late project load cannot replace the latest selected project or its event IDs',async()=>{
  const waiting=deferred();
  const p=page({sessions:[project(),project('project-b')],onRequest:async(route)=>{
    if(route==='/api/recording/project?id=project-a')return waiting.promise;
  }});await settle();
  const old=p.open(0);await settle();await p.open(1);
  waiting.resolve(project());await old;
  assert.equal(p.node('project-source').textContent,'C:/recordings/project-b/source.mkv');
  await p.click('save-project-selection');
  assert.deepEqual(p.requests.find(item=>item.route==='/api/recording/selection').body.selected_ids,['project-b-marker']);
});

test('a late selection save cannot switch back to the old project or prepare its montage',async()=>{
  const waiting=deferred();
  const p=page({sessions:[project(),project('project-b')],onRequest:async(route)=>{
    if(route==='/api/recording/selection')return waiting.promise;
  }});await settle();await p.open();
  const preparing=p.click('prepare-montage');await settle();
  await p.open(1);
  waiting.resolve(project('project-a',{revision:5}));await preparing;
  assert.equal(p.node('project-source').textContent,'C:/recordings/project-b/source.mkv');
  assert.equal(vm.runInContext('recordingMontage',p.context),null);
  assert.ok(!p.requests.some(item=>item.route==='/api/probe'));
});

test('a recording stopped before the first status poll still opens the finalized project',async()=>{
  const status={state:'idle',busy:false};
  const p=page({status,onRequest:async(route)=>{
    if(route==='/api/recording/start'){Object.assign(status,{state:'recording',busy:true,session_id:'project-a'});return {session_id:'project-a'};}
    if(route==='/api/recording/stop'){Object.assign(status,{state:'stopping',busy:true});return status;}
  }});await settle();await p.chooseSource();
  await p.click('start-recording');await p.click('stop-recording');
  Object.assign(status,{state:'ready',busy:false});await p.tick();
  assert.equal(p.node('recording-project').hidden,false);
  assert.equal(p.node('project-source').textContent,'C:/recordings/project-a/source.mkv');
  assert.equal(p.node('export-collection').disabled,false);
});

test('recording busy disables montage preparation and generation across both workspaces',async()=>{
  const status={state:'idle',busy:false};
  const p=page({status});await settle();await p.open();
  Object.assign(status,{state:'recording',busy:true,session_id:'active-recording'});await p.tick();
  assert.equal(p.node('prepare-montage').disabled,true);
  assert.equal(p.node('generate').disabled,true);
  await p.click('prepare-montage');await p.click('show-import');await p.click('generate');
  assert.equal(p.requests.filter(item=>['/api/generate','/api/recording/generate','/api/recording/selection'].includes(item.route)).length,0);
  assert.equal(vm.runInContext('recordingMontage',p.context),null);
});

test('montage loads the server original and submits the project ID without client event lists',async()=>{
  const p=page({onRequest:async(route)=>{
    if(route==='/api/recording/generate')return {id:'montage-job'};
    if(route==='/api/job?id=montage-job')return new Promise(()=>{});
  }});await settle();await p.open();
  p.node('project-source').textContent='C:/wrong-client-path.mp4';
  await p.click('prepare-montage');
  assert.equal(p.node('video').value,'C:/recordings/project-a/source.mkv');
  assert.equal(p.requests.find(item=>item.route==='/api/probe').body.path,'C:/recordings/project-a/source.mkv');
  await p.click('generate');
  const sent=p.requests.find(item=>item.route==='/api/recording/generate').body;
  assert.equal(sent.session_id,'project-a');assert.equal(sent.mode,'montage');
  assert.equal(sent.events,undefined);assert.equal(sent.markers,undefined);assert.equal(sent.selected_ids,undefined);
  assert.ok(!p.requests.some(item=>item.route==='/api/test-profile'));
});

test('changing the imported path clears the recording montage binding before generation',async()=>{
  const p=page({onRequest:async(route)=>{
    if(route==='/api/generate')return {id:'import-job'};
    if(route==='/api/job?id=import-job')return new Promise(()=>{});
  }});await settle();await p.open();await p.click('prepare-montage');
  p.node('video').value='C:/other-source.mp4';await p.node('video').handlers.input();
  assert.equal(vm.runInContext('recordingMontage',p.context),null);
  await p.click('generate');
  assert.ok(p.requests.some(item=>item.route==='/api/generate'&&item.body.video==='C:/other-source.mp4'));
  assert.ok(!p.requests.some(item=>item.route==='/api/recording/generate'));
});
