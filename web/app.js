'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let activeJob = null, timer = null, frameImage = null, rect = null, drag = null, frameSource = null;
let audioReady = false;
let desktopMode = false, appClosing = false, updateTimer = null;
let updateState = {state:'idle', supported:false, message:'点击检查更新后才会联网。'};
const preferenceIds = ['target','strength','game-volume','music-volume','threshold','encoder','voice-mode','music-voice-mode'];
const changedPreferences = new Set();
let preferenceWrites = Promise.resolve();
let activeJobKind = null, testResult = null, testFrameVersion = 0, testFrameBusy = false;
let profiles = [], templateControls = [], excludedEvents = new Set();
let maskImage = null, maskErase = [], maskDrag = null, calibrationBusy = false, maskVersion = 0;
const typeLabels = {knockdown:'击倒',kill:'普通击杀',headshot:'爆头击杀',other:'其他提示',all:'所有事件'};
const roleLabels = {trigger:'事件图标',player:'玩家辅助',npc:'小兵辅助'};
const classLabels = {player:'玩家',npc:'小兵',unknown:'待确认'};
const reported = new WeakSet(), queueKey = 'videocut-pending-errors-v1';
let pendingErrors = [], sendingErrors = false, logWriteError = false, queueStored = true, sessionExpired = false;
function scrub(value) {
  return String(value || '').split(token).join('[REDACTED]')
    .replace(/(Bearer\s+)\S+/gi, '$1[REDACTED]')
    .replace(/((?:token|password|passwd|secret|api[_-]?key|authorization|cookie)["']?\s*[:=]\s*["']?)[^\s&"'<>;,}\]]+/gi, '$1[REDACTED]');
}
function cleanReport(item) {
  const limits = {message:2000,stack:6000,source:300,task_id:64,client_id:64,client_time:64};
  return Object.fromEntries(Object.entries(limits).map(([key,limit]) => [key,scrub(item?.[key]).slice(0,limit)]));
}
try {
  const saved = JSON.parse(sessionStorage.getItem(queueKey) || '[]');
  if (Array.isArray(saved)) pendingErrors = saved.slice(-20).map(cleanReport);
} catch (_) { /* Storage can be disabled; the current page keeps an in-memory queue. */ }
function logStatus() {
  const node = $('log-status'); if (!node) return;
  node.textContent = logWriteError ? '日志写入失败，请检查磁盘空间和文件夹权限。' : pendingErrors.length ? `有 ${pendingErrors.length} 条页面错误待写入日志；${sessionExpired ? '服务会话已失效，请刷新页面后补交。' : '连接恢复后自动补交。'}${queueStored ? '关闭此标签页前请保持服务运行。' : '暂存不可用，刷新或关闭页面会丢失待交记录。'}` : '日志保存在本机，报错时可提供错误编号。';
}
function savePending() {
  try {sessionStorage.setItem(queueKey, JSON.stringify(pendingErrors));queueStored=true;} catch (_) {queueStored=false;}
  logStatus();
}
async function flushErrors() {
  if (appClosing || sendingErrors || sessionExpired || !pendingErrors.length) return;
  sendingErrors = true;
  try {
    while (pendingErrors.length) {
      const item = pendingErrors[0];
      const response = await fetch('/api/client-error', {method:'POST',headers:{'X-Session-Token':token,'Content-Type':'application/json'},body:JSON.stringify(item),signal:AbortSignal.timeout(5000)});
      const result = await response.json();
      sessionExpired = response.status === 403;
      if (result.logs) logWriteError = result.logs.write_error;
      if (!response.ok || !result.ok) break;
      pendingErrors = pendingErrors.filter(entry => entry.client_id !== item.client_id);savePending();
    }
  } catch (_) { /* Reporting must never recursively report its own network failure. */ }
  finally {sendingErrors=false;logStatus();}
}
function reportError(error, source='ui') {
  if (appClosing) return;
  if (error?.requestId || error?.taskId) return;
  if (error && typeof error === 'object') {
    if (reported.has(error)) return;
    reported.add(error);
  }
  const clientId = crypto.randomUUID();
  if (error && typeof error === 'object') {try {error.clientId=clientId;} catch (_) {}}
  pendingErrors.push(cleanReport({client_id:clientId,client_time:new Date().toISOString(),message:error?.message || String(error),stack:error?.stack || '',source,task_id:activeJob || ''}));
  pendingErrors = pendingErrors.slice(-20);savePending();void flushErrors();
}
window.addEventListener('error', event => reportError(event.error || new Error(event.message || `资源载入失败：${event.target?.tagName || 'unknown'}`), 'window.error'), true);
window.addEventListener('unhandledrejection', event => reportError(event.reason, 'unhandledrejection'));
window.addEventListener('online', () => void flushErrors());
setInterval(() => void flushErrors(), 15000);
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers: {'X-Session-Token': token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const result = await response.json();
  if (result.logs) {logWriteError=result.logs.write_error;logStatus();}
  if (!response.ok || result.error && !result.state) {const error=new Error(result.error || '操作失败');error.requestId=result.request_id;throw error;}
  void flushErrors();
  return result;
}
function showError(error) { reportError(error);const id=error?.requestId || error?.taskId || error?.clientId;$('error').textContent = `${error?.message || String(error)}${id ? `\n${error.taskId ? '任务编号' : '错误编号'}：${id}` : ''}`; $('error').hidden = false; }
function clearError() { $('error').hidden = true; }
function safe(fn) { return async (...args) => {clearError(); try { await fn(...args); } catch (error) {showError(error);} }; }
function mediaUrl(id) { return `/media?id=${encodeURIComponent(id)}&token=${encodeURIComponent(token)}`; }
function clock(value) { const n = Math.max(0, Number(value)); return `${Math.floor(n/60).toString().padStart(2,'0')}:${(n%60).toFixed(1).padStart(4,'0')}`; }
function demoNote() { $('demo-note').hidden = $('profile').value !== 'demo'; }
function musicSettings(busy=!!activeJob) {
  const hasMusic=!!$('music').value.trim();
  ['music-start','music-volume','music-voice-mode','clear-music'].forEach(id => $(id).disabled=busy || !hasMusic);
  if (!hasMusic) $('music-info').textContent='未添加 BGM，按镜头原有节奏剪辑，保留原声和已开启的效果音。';
}
function voiceSettings() {
  const mode=$('voice-mode').value;
  $('game-track-group').hidden=mode!=='game_track';$('source-track-group').hidden=mode==='game_track';
  $('voice-hint').textContent=mode==='reduce' ? 'AI 尝试减弱说话声，也可能影响枪声、环境声和角色语音；处理后请试听。' : mode==='game_track' ? '只使用你选择的游戏声轨，其他音轨不会进入成片。' : '保留所选音轨中的游戏声和说话声，适合保留搞笑语音。';
}
function audioStatus(state) {
  audioReady=!!state?.ready;
  $('audio-ai-status').textContent=audioReady ? 'AI 人声组件已就绪 · 本机 CPU 处理，可取消。AI 音频处理与 NVIDIA 视频编码分开运行。' : 'AI 人声组件尚未安装；保留原声和独立游戏音轨可以直接使用。';
  $('audio-setup').hidden=audioReady;
}
async function refreshProfiles(selected) {
  const state = await api('/api/state');
  desktopMode = !!state.desktop;
  $('app-version').textContent = `v${state.version || '—'}`;
  $('update-version').textContent = `当前 v${state.version || '—'}`;
  $('data-directory').textContent = state.data_dir ? `模板、设置和成片位置：${state.data_dir}` : '';
  renderUpdate();
  audioStatus(state.audio);
  const prior = selected || $('profile').value;
  $('profile').replaceChildren(new Option('选择检测配置，或在下方标定', ''));
  state.profiles.forEach(p => $('profile').add(new Option(p.name, p.id)));
  if (prior) $('profile').value = prior;
  profiles = state.profiles;
  const destination = selected || $('profile-destination').value;
  $('profile-destination').replaceChildren(new Option('新建游戏配置', ''));
  profiles.filter(p=>p.kind!=='demo').forEach(p=>$('profile-destination').add(new Option(p.name,p.id)));
  $('profile-destination').value = destination==='demo' ? '' : destination;
  renderTemplates(); calibrationControls();
  demoNote(); return state;
}

function renderTemplates() {
  const profile = profiles.find(p=>p.id===$('profile').value);
  templateControls = []; $('template-list').replaceChildren();
  if (!profile) { $('template-list').textContent='请先选择配置。'; return; }
  (profile.templates || []).forEach(item=>{
    const card=document.createElement('div'), title=document.createElement('strong'), image=document.createElement('img');
    card.className='template-card';title.textContent=`${item.name} · ${roleLabels[item.role]} · ${typeLabels[item.event_type]}`;
    image.src=`/api/template-image?profile=${encodeURIComponent(profile.id)}&template=${encodeURIComponent(item.id)}&token=${encodeURIComponent(token)}`;
    image.alt=`${item.name} 的识别区域`;image.className='template-image';
    card.append(title);card.append(image);
    const controls=document.createElement('div'), enabledLabel=document.createElement('label'), enabled=document.createElement('input');
    controls.className='template-controls';enabled.type='checkbox';enabled.checked=item.enabled!==false;
    enabledLabel.append(enabled);enabledLabel.append(document.createTextNode('启用'));
    const thresholdLabel=document.createElement('label'), threshold=document.createElement('input'), save=document.createElement('button');
    thresholdLabel.textContent='独立阈值';threshold.type='number';threshold.min='.5';threshold.max='.99';threshold.step='.01';threshold.placeholder='跟随默认';threshold.value=item.threshold ?? '';
    thresholdLabel.append(threshold);save.textContent='应用';save.type='button';save.className='secondary';
    [enabled,threshold,save].forEach(node=>{node.disabled=!!activeJob || calibrationBusy || profile.kind==='demo';templateControls.push(node);});
    save.addEventListener('click',safe(async()=>{
      calibrationBusy=true;setBusy(!!activeJob);
      try {await api('/api/template-update',{profile:profile.id,template_id:item.id,enabled:enabled.checked,threshold:threshold.value});invalidateTest();await refreshProfiles(profile.id);}
      finally {calibrationBusy=false;setBusy(!!activeJob);renderTemplates();}
    }));
    controls.append(enabledLabel);controls.append(thresholdLabel);controls.append(save);card.append(controls);$('template-list').append(card);
  });
}

function calibrationControls() {
  const busy=!!activeJob || calibrationBusy;
  ['profile-destination','template-role','template-type','template-name','template-threshold','frame-time','mask-mode','apply-region','region-x','region-y','region-w','region-h'].forEach(id=>$(id).disabled=busy);
  $('profile-name').disabled=busy || !!$('profile-destination').value;
  $('mask-cutoff').disabled=busy || !['light','dark'].includes($('mask-mode').value);
  $('preview-mask').disabled=busy || !rect;
  $('reset-mask').disabled=busy || !maskErase.length;
  $('save-profile').disabled=busy || !rect || !maskImage;
  $('helper-hint').hidden=$('template-role').value==='trigger';
  $('template-type-all').disabled=$('template-role').value==='trigger';
}

function invalidateMask() {
  maskVersion++;maskImage=null;maskErase=[];maskDrag=null;$('mask-editor').hidden=true;calibrationControls();
}

function calibrationSpec() {
  if(!rect||!frameSource)throw new Error('请先读取画面并框选。');
  if(frameSource.video!==$('video').value||frameSource.time!==Number($('frame-time').value))throw new Error('录像或时间已改变，请重新读取画面再保存。');
  return {...frameSource,rect,mask_mode:$('mask-mode').value,cutoff:Number($('mask-cutoff').value),erase:maskErase};
}

function reviewStatus() {
  $('review-status').textContent=`已手动排除 ${excludedEvents.size} 个事件，将用于下一次生成；范围仅限本次测试。`;
}
async function inspect(kind) {
  if (kind === 'music') musicSettings();
  const path = $(kind).value.trim(); if (!path) return;
  const meta = await api('/api/probe', {path, audio: kind === 'music'});
  if ($(kind).value.trim() !== path) return;
  if (kind === 'video') {
    $('video-info').textContent = `${meta.width} × ${meta.height} · ${clock(meta.duration)} · ${meta.audio_tracks.length} 条音轨`;
    $('audio-track').replaceChildren();
    (meta.audio_tracks.length ? meta.audio_tracks : [{index: 0, title: '无原声音轨'}]).forEach(t => $('audio-track').add(new Option(`${t.index+1} · ${t.title}`, t.index)));
    $('game-audio-track').replaceChildren(new Option(meta.audio_tracks.length>1 ? '请选择纯游戏声轨，不会自动选取' : '录像未包含多条音轨', ''));
    if (meta.audio_tracks.length>1) meta.audio_tracks.forEach(t => $('game-audio-track').add(new Option(`${t.index+1} · ${t.title}`,t.index)));
  } else { $('music-info').textContent = `音乐时长 ${clock(meta.duration)} · 可设置起点`; }
}
function testFrameControls(busy=!!activeJob) {
  ['test-read-frame','test-prev-frame','test-next-frame','test-frame-time'].forEach(id => $(id).disabled = busy || testFrameBusy || !testResult);
}
function setBusy(value) { ['generate','demo','frame','pick-video','pick-music','video','music','profile','threshold','voice-mode','audio-track','game-audio-track','test-profile','test-near-frame','test-start','test-duration'].forEach(id => $(id).disabled = value || calibrationBusy); musicSettings(value); $('cancel').hidden = !value; $('test-cancel').hidden = !value; calibrationControls(); templateControls.forEach(node=>node.disabled=value || calibrationBusy || $('profile').value==='demo'); testFrameControls(value); renderUpdate(); }
function invalidateTest() {
  testResult = null; testFrameVersion++; testFrameBusy = false;
  excludedEvents.clear();reviewStatus();
  $('test-result').hidden = true; $('test-frame').hidden = true;
  if (activeJobKind !== 'profile_test' || !activeJob) $('test-progress-area').hidden = true;
  $('test-status').textContent = '录像、配置或测试参数已变更，请重新测试。';
  testFrameControls();
}
async function readTestFrame(time) {
  if (!testResult || activeJob || testFrameBusy) return;
  const result = testResult, version = ++testFrameVersion;
  const end = Math.max(result.start, result.start + result.duration - .1);
  if (!Number.isFinite(time) || time < result.start || time > end + .000001) throw new Error(`查看时间应在 ${result.start.toFixed(1)}–${end.toFixed(1)} 秒之间。`);
  testFrameBusy = true; testFrameControls();
  $('test-frame').hidden = true; $('test-frame-label').textContent = '正在读取原片画面…';
  try {
    const frame = await api('/api/frame', {video:result.video,time});
    if (version !== testFrameVersion) return;
    $('test-frame').src = frame.image; $('test-frame').hidden = false;
    $('test-frame').alt = `原片 ${clock(time)} 的画面`;
    $('test-frame-time').value = time.toFixed(3);
    $('test-frame-label').textContent = `原片 ${clock(time)}`;
  } catch (error) {
    if (version !== testFrameVersion) return;
    $('test-frame-label').textContent = '画面读取失败，可重试。'; throw error;
  } finally {
    if (version === testFrameVersion) {testFrameBusy = false; testFrameControls();}
  }
}
function renderTestResult(result) {
  testResult = result; testFrameVersion++; testFrameBusy = false;
  excludedEvents.clear();reviewStatus();
  $('test-result').hidden = false; $('test-frame').hidden = true;
  $('test-status').textContent = result.events.length ? `测试完成，找到 ${result.events.length} 个候选事件。点击事件查看画面。` : '测试完成，未找到候选事件。可查看区间内已知有提示的画面，检查模板、阈值或测试范围后重试。';
  $('test-summary').textContent = `${result.profile_name} · 阈值 ${result.threshold} · 原片 ${clock(result.start)}–${clock(result.start+result.duration)} · 耗时 ${result.elapsed_seconds.toFixed(2)} 秒${result.duration < result.requested_duration ? ' · 已按片尾缩短' : ''}`;
  $('test-source').textContent = result.video;
  $('test-events').replaceChildren();
  $('test-events').scrollTop=0;
  result.events.forEach((event, index) => {
    const li = document.createElement('li'), button = document.createElement('button');
    button.type = 'button'; button.className = 'secondary';
    const category=event.classification || 'unknown';
    button.textContent = `${clock(event.time)}–${clock(event.end)} · ${classLabels[category]}${category==='npc'?' · 自动过滤':''} · 匹配分数 ${event.confidence.toFixed(3)} · 查看画面`;
    button.addEventListener('click', safe(() => readTestFrame(Math.min(event.time, Math.max(result.start, result.start+result.duration-.1)))));
    li.append(button);
    const evidence=document.createElement('p');evidence.className='hint';
    evidence.textContent=`${(event.event_types || []).map(t=>typeLabels[t]).join(' / ')} · 图标：${(event.matches || []).map(m=>m.name).join('、') || '旧模板'}。${event.reason || '缺少辅助依据，保留待确认'}${event.evidence?.length?'：'+event.evidence.map(e=>`${e.name} (${e.confidence.toFixed(3)})`).join('、'):''}`;
    li.append(evidence);
    if(category!=='npc' && result.review_signature){
      const label=document.createElement('label'), checkbox=document.createElement('input');checkbox.type='checkbox';
      label.className='exclude-event';label.append(checkbox);label.append(document.createTextNode('手动排除此事件'));
      checkbox.addEventListener('change',()=>{if(activeJob){checkbox.checked=excludedEvents.has(index);return;}if(checkbox.checked)excludedEvents.add(index);else excludedEvents.delete(index);reviewStatus();});li.append(label);
    }
    $('test-events').append(li);
  });
  $('test-frame-time').value = Math.min(result.events[0]?.time ?? result.start, Math.max(result.start,result.start+result.duration-.1)).toFixed(3);
  $('test-frame-time').min = result.start; $('test-frame-time').max = Math.max(result.start,result.start+result.duration-.1);
  $('test-frame-label').textContent = '可按时间查看画面，或前后移动 0.5 秒。';
  testFrameControls();
}
function renderResult(result) {
  $('empty-preview').hidden = true; $('result-video').hidden = false; $('result-video').src = mediaUrl(result.media_id);
  $('result').hidden = false; $('result-badge').textContent = result.demo ? '合成演示成片' : '已完成';
  $('stat-duration').textContent = result.duration.toFixed(1); $('stat-clips').textContent = result.clips.length; $('stat-events').textContent = result.events.length;
  $('warnings').replaceChildren();
  result.warnings.forEach(text => {const li = document.createElement('li');li.textContent = text;$('warnings').append(li);});
  $('download').href = mediaUrl(result.media_id); $('output-path').textContent = result.output;
  $('clips').replaceChildren();
  result.clips.forEach(c => {const li = document.createElement('li');li.textContent = `原片 ${clock(c.start)}–${clock(c.end)} → 成片 ${clock(c.output_start)} · ${c.events.length} 个候选事件`;$('clips').append(li);});
}
async function poll() {
  if (!activeJob) return;
  try {
    const job = await api(`/api/job?id=${encodeURIComponent(activeJob)}`);
    activeJobKind = job.kind;
    const prefix = job.kind === 'profile_test' ? 'test-' : '';
    $(prefix+'progress-area').hidden = false;
    $(prefix+'stage').textContent = job.stage; $(prefix+'percent').textContent = `${job.progress}%`; $(prefix+'progress').value = job.progress; $(prefix+'detail').textContent = job.detail || '';
    if (job.state === 'running') { if(prefix){$('test-status').textContent='正在测试提示配置…';}else{taskReference.textContent=`任务编号：${job.id}`;$('result-badge').textContent='处理中';} timer = setTimeout(poll, 800); return; }
    activeJob = null; setBusy(false);
    if (job.state === 'failed') { $(prefix ? 'test-status' : 'result-badge').textContent = '处理失败'; const error=new Error(job.error);error.taskId=job.id;showError(error); }
    else if (job.state === 'cancelled') { $(prefix ? 'test-status' : 'result-badge').textContent = '已取消'; }
    else if (job.kind === 'profile_test') { renderTestResult(job.result); }
    else if (job.kind === 'demo') {
      $('stage').textContent='演示素材就绪';
      $('video').value = job.result.video; $('music').value = job.result.music; $('target').value = job.result.target; $('music-start').value = '0';
      await refreshProfiles(job.result.profile); await inspect('video'); await inspect('music');
      $('detail').textContent = '演示素材已载入，点击“生成混剪”体验完整流程。'; $('result-badge').textContent = '演示就绪';
    } else { renderResult(job.result); }
  } catch (error) { activeJob = null; setBusy(false); showError(error); }
}
const taskReference = document.createElement('div');taskReference.className='task-reference';taskReference.id='task-reference';$('progress-area').append(taskReference);
function startPolling(id, kind=null) {clearTimeout(timer); activeJob=id; activeJobKind=kind; setBusy(true); if(kind){const prefix=kind==='profile_test'?'test-':'';$(prefix+'progress-area').hidden=false;$(prefix+'progress').value=0;$(prefix+'percent').textContent='0%';$(prefix+'stage').textContent='准备中';$(prefix+'detail').textContent='';if(!prefix){taskReference.textContent=`任务编号：${id}`;$('result-badge').textContent='处理中';}} poll();}
$('demo').addEventListener('click', safe(async () => {const job=await api('/api/demo',{});invalidateTest();startPolling(job.id,'demo');}));
['video','music'].forEach(kind => {
  $(`pick-${kind}`).addEventListener('click', safe(async () => { $(`pick-${kind}`).disabled=true; try { const result=await api('/api/pick',{kind}); if(result.path){$(kind).value=result.path; if(kind==='video'){invalidateTest();invalidateMask();} await inspect(kind);} } finally {$(`pick-${kind}`).disabled=!!activeJob;} }));
  $(kind).addEventListener('change',safe(() => inspect(kind)));
});
$('music').addEventListener('input',() => { $('music-info').textContent='音乐和录像都保留在本机。'; musicSettings(); });
$('clear-music').addEventListener('click',() => { $('music').value=''; musicSettings(); });
$('profile').addEventListener('change', () => {demoNote();invalidateTest();renderTemplates();$('profile-destination').value=$('profile').value==='demo'?'':$('profile').value;calibrationControls();});
$('profile-destination').addEventListener('change',calibrationControls);
$('template-role').addEventListener('change',()=>{if($('template-role').value==='trigger' && $('template-type').value==='all')$('template-type').value='kill';calibrationControls();});
['mask-mode','mask-cutoff'].forEach(id=>$(id).addEventListener('change',invalidateMask));
['video','frame-time'].forEach(id=>$(id).addEventListener('input',invalidateMask));
['video','threshold','test-start','test-duration'].forEach(id => $(id).addEventListener('input', invalidateTest));
$('voice-mode').addEventListener('change',voiceSettings);
$('strength').addEventListener('input',() => {$('strength-value').textContent = Number($('strength').value)===0?'关闭':Number($('strength').value)<.4?'轻量':Number($('strength').value)<.8?'适中':'强烈';});
$('generate').addEventListener('click',safe(async () => {
  if (!$('video').value.trim()) throw new Error('请先选择录像，也可以使用合成演示素材。');
  if (!$('profile').value) throw new Error('请先选择检测配置，或在“标定提示与辅助标记”中保存配置。');
  if ($('voice-mode').value==='game_track' && $('game-audio-track').value==='') throw new Error('请先读取录像并手动选择独立游戏音轨。');
  const music=$('music').value.trim();
  if (($('voice-mode').value==='reduce' || music && $('music-voice-mode').value==='instrumental') && !audioReady) throw new Error('AI 人声组件尚未安装，请查看“声音与高级设置”中的安装提示。');
  const spec={video:$('video').value,music,profile:$('profile').value,target:$('target').value,music_start:music?$('music-start').value:0,strength:$('strength').value,game_volume:$('game-volume').value,music_volume:music?$('music-volume').value:0,threshold:$('threshold').value,audio_track:$('audio-track').value,encoder:$('encoder').value,voice_mode:$('voice-mode').value,music_voice_mode:music?$('music-voice-mode').value:'keep',game_audio_track:$('game-audio-track').value};
  if(testResult?.review_signature && excludedEvents.size)spec.review={signature:testResult.review_signature,excluded:[...excludedEvents].map(i=>[testResult.events[i].time,testResult.events[i].end])};
  const job=await api('/api/generate',spec); $('result').hidden=true; $('result-video').pause(); $('result-video').hidden=true; $('empty-preview').hidden=false; startPolling(job.id,'generate');
}));
$('test-profile').addEventListener('click', safe(async () => {
  if (!$('video').value.trim()) throw new Error('请先选择录像。');
  if (!$('profile').value) throw new Error('请先选择或保存提示配置。');
  const number = (id, label, low, high) => {
    const raw=$(id).value.trim(), value=Number(raw);
    if (!raw || !Number.isFinite(value) || value<low || value>high) throw new Error(`${label}必须在 ${low} 到 ${high} 之间。`);
    return value;
  };
  const spec={video:$('video').value,profile:$('profile').value,start:number('test-start','测试起点',0,86400*365),duration:number('test-duration','测试时长',5,60),threshold:number('threshold','检测阈值',.5,.99)};
  setBusy(true);
  try {
    const job=await api('/api/test-profile',spec);
    invalidateTest(); $('test-status').textContent='正在测试提示配置…';
    startPolling(job.id,'profile_test');
  } catch(error) {setBusy(false);throw error;}
}));
$('test-near-frame').addEventListener('click', safe(() => {
  const time=Number($('frame-time').value);
  if (!$('frame-time').value.trim() || !Number.isFinite(time) || time<0) throw new Error('请先填写有效的标定时间。');
  $('test-start').value=Math.max(0,time-5).toFixed(1); invalidateTest();
}));
$('test-read-frame').addEventListener('click', safe(() => readTestFrame($('test-frame-time').value.trim() ? Number($('test-frame-time').value) : NaN)));
[['test-prev-frame',-.5],['test-next-frame',.5]].forEach(([id,step]) => $(id).addEventListener('click', safe(() => {
  if(testResult) return readTestFrame(Math.min(Math.max(testResult.start,testResult.start+testResult.duration-.1),Math.max(testResult.start,Number($('test-frame-time').value)+step)));
})));
$('test-cancel').addEventListener('click',safe(async () => {if(activeJob && activeJobKind==='profile_test'){await api('/api/cancel',{id:activeJob});$('test-detail').textContent='正在停止测试…';}}));
$('cancel').addEventListener('click',safe(async () => {if(activeJob){await api('/api/cancel',{id:activeJob});$('detail').textContent='正在停止当前处理…';}}));
$('open-output').addEventListener('click',safe(() => api('/api/open-output',{})));
$('download-logs').href=`/api/logs.zip?token=${encodeURIComponent(token)}`;
$('open-logs').addEventListener('click',safe(async () => {const result=await api('/api/open-logs',{});$('log-status').textContent=result.opened ? `日志文件夹：${result.path}` : `无法自动打开文件夹。请复制路径到文件资源管理器，或点击“下载日志”：${result.path}`;}));
$('frame').addEventListener('click',safe(async () => {
  calibrationBusy=true;setBusy(!!activeJob);
  try {const video=$('video').value,time=Number($('frame-time').value);const result=await api('/api/frame',{video,time});
    const image = new Image(); await new Promise((resolve,reject) => {image.onload=resolve;image.onerror=reject;image.src=result.image;});
    frameImage=image;frameSource={video,time};rect=null;drag=null;invalidateMask();$('canvas').width=image.width;$('canvas').height=image.height;$('canvas-wrap').hidden=false;$('selection-info').textContent='拖动鼠标框选稳定的提示图案，再预览识别区域。';draw();
  } finally {calibrationBusy=false;setBusy(!!activeJob);}
}));
const canvas=$('canvas'), context=canvas.getContext('2d');
// An explicit keyboard alternative keeps calibration usable without precise dragging.
const keyboardRegion = document.createElement('details');
keyboardRegion.className = 'advanced';
keyboardRegion.innerHTML = '<summary>用数字设置框选范围</summary><div class="two-cols"><div><label for="region-x">左侧位置 / %</label><input id="region-x" type="number" min="0" max="100" step="0.1" value="0"></div><div><label for="region-y">顶部位置 / %</label><input id="region-y" type="number" min="0" max="100" step="0.1" value="0"></div><div><label for="region-w">宽度 / %</label><input id="region-w" type="number" min="0.1" max="100" step="0.1" value="20"></div><div><label for="region-h">高度 / %</label><input id="region-h" type="number" min="0.1" max="100" step="0.1" value="10"></div></div><button class="secondary" id="apply-region" type="button">应用框选范围</button>';
$('selection-info').after(keyboardRegion);
$('apply-region').addEventListener('click',safe(async () => {
  if(!frameImage)throw new Error('请先读取一张画面。');
  const next=['x','y','w','h'].map(k=>Number($('region-'+k).value)/100);
  if(!next.every(Number.isFinite)||next[0]<0||next[1]<0||next[2]*canvas.width<8||next[3]*canvas.height<8||next[0]+next[2]>1||next[1]+next[3]>1)throw new Error('范围必须位于画面内，且宽、高至少为 8 像素。');
  rect=next;invalidateMask();draw();$('selection-info').textContent=`已框选 ${Math.round(rect[2]*canvas.width)} × ${Math.round(rect[3]*canvas.height)} 像素。请预览识别区域。`;
}));
function draw() {if(!frameImage)return; context.drawImage(frameImage,0,0);if(rect){context.strokeStyle='#d3f58e';context.lineWidth=2;context.strokeRect(rect[0]*canvas.width,rect[1]*canvas.height,rect[2]*canvas.width,rect[3]*canvas.height);}}
function point(event) {const bounds=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-bounds.left)/bounds.width)),Math.max(0,Math.min(1,(event.clientY-bounds.top)/bounds.height))];}
canvas.addEventListener('pointerdown',event => {if(!frameImage || activeJob || calibrationBusy)return;invalidateMask();drag=point(event);canvas.setPointerCapture(event.pointerId);});
canvas.addEventListener('pointermove',event => {if(!drag)return;const p=point(event);rect=[Math.min(p[0],drag[0]),Math.min(p[1],drag[1]),Math.abs(p[0]-drag[0]),Math.abs(p[1]-drag[1])];draw();});
canvas.addEventListener('pointerup',() => {if(!drag)return;drag=null;const valid=rect&&rect[2]*canvas.width>=8&&rect[3]*canvas.height>=8;if(!valid)rect=null;calibrationControls();$('selection-info').textContent=valid?`已框选 ${Math.round(rect[2]*canvas.width)} × ${Math.round(rect[3]*canvas.height)} 像素。请预览识别区域。`:'区域太小，请重新框选。';});
canvas.addEventListener('pointercancel',()=>{drag=null;});
$('save-profile').addEventListener('click',safe(async () => {
  const spec=calibrationSpec();
  if(!maskImage)throw new Error('请先预览识别区域。');
  calibrationBusy=true;setBusy(!!activeJob);
  try {
    const profile=await api('/api/profile',{...spec,name:$('profile-name').value,profile_id:$('profile-destination').value || null,template_name:$('template-name').value,role:$('template-role').value,event_type:$('template-type').value,sample_threshold:$('template-threshold').value});
    await refreshProfiles(profile.id);invalidateTest();$('test-start').value=Math.max(0,frameSource.time-5).toFixed(1);$('selection-info').textContent='模板已保存。可继续添加其他图标或辅助标记，再到下方测试。';invalidateMask();
  } finally {calibrationBusy=false;setBusy(!!activeJob);}
}));
const maskCanvas=$('mask-canvas'), maskContext=maskCanvas.getContext('2d');
function drawMask(area=null) {
  if(!maskImage)return;
  maskContext.clearRect(0,0,maskCanvas.width,maskCanvas.height);maskContext.drawImage(maskImage,0,0);
  maskContext.fillStyle='rgba(240,85,65,.65)';
  [...maskErase,...(area?[area]:[])].forEach(([x,y,w,h])=>maskContext.fillRect(x*maskCanvas.width,y*maskCanvas.height,w*maskCanvas.width,h*maskCanvas.height));
}
function maskPoint(event) {const b=maskCanvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-b.left)/b.width)),Math.max(0,Math.min(1,(event.clientY-b.top)/b.height))];}
function eraseArea(p) {return [Math.min(maskDrag[0],p[0]),Math.min(maskDrag[1],p[1]),Math.abs(p[0]-maskDrag[0]),Math.abs(p[1]-maskDrag[1])];}
maskCanvas.addEventListener('pointerdown',event=>{if(!maskImage || activeJob || calibrationBusy)return;maskDrag=maskPoint(event);maskCanvas.setPointerCapture(event.pointerId);});
maskCanvas.addEventListener('pointermove',event=>{if(maskDrag)drawMask(eraseArea(maskPoint(event)));});
maskCanvas.addEventListener('pointerup',event=>{if(!maskDrag)return;const area=eraseArea(maskPoint(event));maskDrag=null;if(area[2]>0 && area[3]>0 && maskErase.length<64)maskErase.push(area);drawMask();calibrationControls();$('mask-status').textContent=`已擦除 ${maskErase.length} 个区域；保存时会验证剩余轮廓。`;});
maskCanvas.addEventListener('pointercancel',()=>{maskDrag=null;drawMask();});
$('preview-mask').addEventListener('click',safe(async()=>{
  const spec=calibrationSpec(), version=++maskVersion;calibrationBusy=true;setBusy(!!activeJob);
  try {
    const result=await api('/api/template-preview',spec), image=new Image();
    await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('蒙版预览载入失败'));image.src=result.image;});
    if(version!==maskVersion)return;
    maskImage=image;maskCanvas.width=image.width;maskCanvas.height=image.height;$('mask-editor').hidden=false;drawMask();$('mask-status').textContent='检查保留的轮廓，可拖动擦除背景后保存。';
  } finally {calibrationBusy=false;setBusy(!!activeJob);}
}));
$('reset-mask').addEventListener('click',()=>{invalidateMask();$('selection-info').textContent='擦除已重置，请重新预览识别区域。';});
function renderUpdate() {
  const state = updateState.state, pending = ['checking','downloading','installing'].includes(state);
  $('check-update').disabled = appClosing || pending;
  $('download-update').hidden = !updateState.supported || state !== 'available';
  $('download-update').disabled = appClosing || pending;
  $('install-update').hidden = state !== 'ready';
  $('install-update').disabled = appClosing || !!activeJob || calibrationBusy || !desktopMode;
  $('quit-app').hidden = !desktopMode;
  $('quit-app').disabled = appClosing || !!activeJob || calibrationBusy || state === 'installing';
  $('update-progress').hidden = state !== 'downloading';
  $('update-progress').value = Number(updateState.progress) || 0;
  const message = updateState.error || updateState.message || '点击检查更新后才会联网。';
  $('update-status').textContent = message + (state === 'ready' && activeJob ? ' 请等待任务完成，或取消任务后再安装。' : '');
  $('update-mode').textContent = updateState.supported ? '更新后重启生效，模板、设置和成片保留。' : '当前为源码运行模式；安装更新请使用 Windows 免安装版。';
}
async function readUpdate() {
  if (appClosing) return;
  updateState = await api('/api/update');
  renderUpdate();
  clearTimeout(updateTimer);
  if (['checking','downloading'].includes(updateState.state)) updateTimer = setTimeout(() => {void readUpdate().catch(updateFailed);}, 800);
}
function updateFailed(error) {
  if (appClosing) return;
  clearTimeout(updateTimer);
  updateState = {...updateState, state:'error', error:error.message, message:'更新失败，请重新检查。'};
  renderUpdate();
}
async function updateAction(action) {
  try {
    updateState = await api(`/api/update/${action}`, {});
    renderUpdate();
    if (updateState.state === 'installing') {
      appClosing = true; clearTimeout(updateTimer); clearTimeout(timer);
      updateState.message = '正在重启安装更新，请等待应用重新打开。';
      renderUpdate();
      return;
    }
    await readUpdate();
  } catch (error) {updateFailed(error);}
}
$('check-update').addEventListener('click', () => updateAction('check'));
$('download-update').addEventListener('click', () => updateAction('download'));
$('install-update').addEventListener('click', () => updateAction('install'));
$('quit-app').addEventListener('click', safe(async () => {
  await api('/api/quit', {});
  appClosing = true; clearTimeout(updateTimer); clearTimeout(timer);
  updateState = {...updateState, message:'应用已退出。浏览器模式下可以关闭此标签页。', error:null};
  renderUpdate();
}));
function strengthLabel() {$('strength-value').textContent = Number($('strength').value)===0?'关闭':Number($('strength').value)<.4?'轻量':Number($('strength').value)<.8?'适中':'强烈';}
async function loadPreferences() {
  const saved = await api('/api/preferences');
  for (const id of preferenceIds) if (saved[id] !== undefined && !changedPreferences.has(id)) $(id).value = String(saved[id]);
  voiceSettings(); musicSettings(); strengthLabel();
}
preferenceIds.forEach(id => $(id).addEventListener('change', async () => {
  changedPreferences.add(id);
  if (id === 'voice-mode') voiceSettings();
  if (id === 'strength') strengthLabel();
  const value = $(id).value;
  try {
    preferenceWrites = preferenceWrites.catch(() => {}).then(() => api('/api/preferences', {[id]: value}));
    await preferenceWrites;
    $('preferences-status').textContent = '常用剪辑参数已保存到本机。';
  } catch (error) {
    $('preferences-status').textContent = `参数未能保存：${error.message}。当前仍可继续操作。`;
  }
}));
logStatus();
voiceSettings();
musicSettings();
safe(async () => {const state=await refreshProfiles();if(state.active)startPolling(state.active);await loadPreferences();await readUpdate();})();
