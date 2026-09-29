'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let activeJob = null, timer = null, frameImage = null, rect = null, drag = null, frameSource = null;
let audioReady = false;
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
  if (sendingErrors || sessionExpired || !pendingErrors.length) return;
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
  audioStatus(state.audio);
  const prior = selected || $('profile').value;
  $('profile').replaceChildren(new Option('选择检测配置，或在下方标定', ''));
  state.profiles.forEach(p => $('profile').add(new Option(p.name, p.id)));
  if (prior) $('profile').value = prior;
  demoNote(); return state;
}
async function inspect(kind) {
  const path = $(kind).value.trim(); if (!path) return;
  const meta = await api('/api/probe', {path, audio: kind === 'music'});
  if (kind === 'video') {
    $('video-info').textContent = `${meta.width} × ${meta.height} · ${clock(meta.duration)} · ${meta.audio_tracks.length} 条音轨`;
    $('audio-track').replaceChildren();
    (meta.audio_tracks.length ? meta.audio_tracks : [{index: 0, title: '无原声音轨'}]).forEach(t => $('audio-track').add(new Option(`${t.index+1} · ${t.title}`, t.index)));
    $('game-audio-track').replaceChildren(new Option(meta.audio_tracks.length>1 ? '请选择纯游戏声轨，不会自动选取' : '录像未包含多条音轨', ''));
    if (meta.audio_tracks.length>1) meta.audio_tracks.forEach(t => $('game-audio-track').add(new Option(`${t.index+1} · ${t.title}`,t.index)));
  } else { $('music-info').textContent = `音乐时长 ${clock(meta.duration)} · 可设置起点`; }
}
function setBusy(value) { ['generate','demo','frame','pick-video','pick-music','voice-mode','music-voice-mode','audio-track','game-audio-track'].forEach(id => $(id).disabled = value); $('cancel').hidden = !value; $('save-profile').disabled = value || !rect; }
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
    $('stage').textContent = job.stage; $('percent').textContent = `${job.progress}%`; $('progress').value = job.progress; $('detail').textContent = job.detail || '';
    if (job.state === 'running') { timer = setTimeout(poll, 800); return; }
    activeJob = null; setBusy(false);
    if (job.state === 'failed') { $('result-badge').textContent = '处理失败'; const error=new Error(job.error);error.taskId=job.id;showError(error); }
    else if (job.state === 'cancelled') { $('result-badge').textContent = '已取消'; }
    else if (job.kind === 'demo') {
      $('stage').textContent='演示素材就绪';
      $('video').value = job.result.video; $('music').value = job.result.music; $('target').value = job.result.target; $('music-start').value = '0';
      await refreshProfiles(job.result.profile); await inspect('video'); await inspect('music');
      $('detail').textContent = '演示素材已载入，点击“生成混剪”体验完整流程。'; $('result-badge').textContent = '演示就绪';
    } else { renderResult(job.result); }
  } catch (error) { activeJob = null; setBusy(false); showError(error); }
}
const taskReference = document.createElement('div');taskReference.className='task-reference';taskReference.id='task-reference';$('progress-area').append(taskReference);
function startPolling(id) {clearTimeout(timer); activeJob=id; taskReference.textContent=`任务编号：${id}`; setBusy(true); $('progress-area').hidden=false; $('result-badge').textContent='处理中'; $('progress').value=0; $('percent').textContent='0%'; $('stage').textContent='准备中'; $('detail').textContent=''; poll();}
$('demo').addEventListener('click', safe(async () => {const job=await api('/api/demo',{});startPolling(job.id);}));
['video','music'].forEach(kind => {
  $(`pick-${kind}`).addEventListener('click', safe(async () => { $(`pick-${kind}`).disabled=true; try { const result=await api('/api/pick',{kind}); if(result.path){$(kind).value=result.path; await inspect(kind);} } finally {$(`pick-${kind}`).disabled=!!activeJob;} }));
  $(kind).addEventListener('change',safe(() => inspect(kind)));
});
$('profile').addEventListener('change', demoNote);
$('voice-mode').addEventListener('change',voiceSettings);
$('strength').addEventListener('input',() => {$('strength-value').textContent = Number($('strength').value)===0?'关闭':Number($('strength').value)<.4?'轻量':Number($('strength').value)<.8?'适中':'强烈';});
$('generate').addEventListener('click',safe(async () => {
  if (!$('video').value.trim() || !$('music').value.trim()) throw new Error('请先选择录像和 BGM，也可以使用合成演示素材。');
  if (!$('profile').value) throw new Error('请先选择检测配置，或在“首次标定提示图案”中保存配置。');
  if ($('voice-mode').value==='game_track' && $('game-audio-track').value==='') throw new Error('请先读取录像并手动选择独立游戏音轨。');
  if (($('voice-mode').value==='reduce' || $('music-voice-mode').value==='instrumental') && !audioReady) throw new Error('AI 人声组件尚未安装，请查看“声音与高级设置”中的安装提示。');
  const spec={video:$('video').value,music:$('music').value,profile:$('profile').value,target:$('target').value,music_start:$('music-start').value,strength:$('strength').value,game_volume:$('game-volume').value,music_volume:$('music-volume').value,threshold:$('threshold').value,audio_track:$('audio-track').value,encoder:$('encoder').value,voice_mode:$('voice-mode').value,music_voice_mode:$('music-voice-mode').value,game_audio_track:$('game-audio-track').value};
  const job=await api('/api/generate',spec); $('result').hidden=true; $('result-video').pause(); $('result-video').hidden=true; $('empty-preview').hidden=false; startPolling(job.id);
}));
$('cancel').addEventListener('click',safe(async () => {if(activeJob){await api('/api/cancel',{id:activeJob});$('detail').textContent='正在停止当前处理…';}}));
$('open-output').addEventListener('click',safe(() => api('/api/open-output',{})));
$('download-logs').href=`/api/logs.zip?token=${encodeURIComponent(token)}`;
$('open-logs').addEventListener('click',safe(async () => {const result=await api('/api/open-logs',{});$('log-status').textContent=result.opened ? `日志文件夹：${result.path}` : `无法自动打开文件夹。请复制路径到文件资源管理器，或点击“下载日志”：${result.path}`;}));
$('frame').addEventListener('click',safe(async () => {
  $('frame').disabled=true;
  try {const video=$('video').value,time=Number($('frame-time').value);const result=await api('/api/frame',{video,time});
    const image = new Image(); await new Promise((resolve,reject) => {image.onload=resolve;image.onerror=reject;image.src=result.image;});
    frameImage=image;frameSource={video,time};rect=null;drag=null;$('canvas').width=image.width;$('canvas').height=image.height;$('canvas-wrap').hidden=false;$('save-profile').disabled=true;$('selection-info').textContent='拖动鼠标框选稳定的提示图案。';draw();
  } finally {$('frame').disabled=!!activeJob;}
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
  rect=next;draw();$('save-profile').disabled=!!activeJob;$('selection-info').textContent=`已框选 ${Math.round(rect[2]*canvas.width)} × ${Math.round(rect[3]*canvas.height)} 像素。`;
}));
function draw() {if(!frameImage)return; context.drawImage(frameImage,0,0);if(rect){context.strokeStyle='#d3f58e';context.lineWidth=2;context.strokeRect(rect[0]*canvas.width,rect[1]*canvas.height,rect[2]*canvas.width,rect[3]*canvas.height);}}
function point(event) {const bounds=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-bounds.left)/bounds.width)),Math.max(0,Math.min(1,(event.clientY-bounds.top)/bounds.height))];}
canvas.addEventListener('pointerdown',event => {if(!frameImage)return;drag=point(event);canvas.setPointerCapture(event.pointerId);});
canvas.addEventListener('pointermove',event => {if(!drag)return;const p=point(event);rect=[Math.min(p[0],drag[0]),Math.min(p[1],drag[1]),Math.abs(p[0]-drag[0]),Math.abs(p[1]-drag[1])];draw();});
canvas.addEventListener('pointerup',() => {if(!drag)return;drag=null;const valid=rect&&rect[2]*canvas.width>=8&&rect[3]*canvas.height>=8;$('save-profile').disabled=!valid||!!activeJob;$('selection-info').textContent=valid?`已框选 ${Math.round(rect[2]*canvas.width)} × ${Math.round(rect[3]*canvas.height)} 像素。`:'区域太小，请重新框选。';});
$('save-profile').addEventListener('click',safe(async () => {
  if(!rect||!frameSource)throw new Error('请先读取画面并框选。');
  if(frameSource.video!==$('video').value||frameSource.time!==Number($('frame-time').value))throw new Error('录像或时间已改变，请重新读取画面再保存。');
  const profile=await api('/api/profile',{...frameSource,rect,name:$('profile-name').value});await refreshProfiles(profile.id);$('selection-info').textContent='配置已保存，可以生成混剪；首次使用请核对检测结果。';
}));
logStatus();
voiceSettings();
safe(async () => {const state=await refreshProfiles();if(state.active)startPolling(state.active);})();
