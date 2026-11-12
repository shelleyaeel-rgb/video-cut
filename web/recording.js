'use strict';

// Recording owns its lifecycle; export/preview jobs use the existing task API.
(() => {
  let capabilities = null, sources = [], current = {state:'idle',busy:false};
  let selected = null, checked = new Set(), projectJob = null, pending = false;
  let projectVersion = 0, previewVersion = 0, wasBusy = false, pollTimer = null;
  const states = {idle:'等待录制',starting:'正在准备',recording:'正在录制',stopping:'正在停止',finalizing:'保存与检查',ready:'已保存',interrupted:'录制中断',failed:'启动失败',created:'准备中'};
  const detectionStates = {idle:'等待录制',warming:'识别准备中',running:'自动标记已开启',ready:'自动标记已就绪',complete:'标记已保存',incomplete:'有未完成识别区间',needs_rescan:'有待补查区间',degraded:'自动标记有缺口',failed:'自动标记不可用',disabled:'仅手动标记',stopped:'识别已结束'};

  function workspace(recording) {
    $('recording-workspace').hidden = !recording;
    $('import-workspace').hidden = recording;
    $('show-recording').className = recording ? 'primary' : 'secondary';
    $('show-import').className = recording ? 'secondary' : 'primary';
  }
  $('show-recording').addEventListener('click', () => workspace(true));
  $('show-import').addEventListener('click', () => workspace(false));
  $('demo').addEventListener('click', () => workspace(false));

  function controls() {
    const busy = current.busy || !!projectJob || pending;
    recordingBusy = busy;
    setBusy(!!activeJob);
    $('start-recording').disabled = busy || !!activeJob || !capabilities?.available || !$('capture-source').value;
    $('stop-recording').disabled = !current.busy || ['stopping','finalizing'].includes(current.state);
    $('mark-recording').disabled = current.state !== 'recording';
    ['capture-source','capture-audio','capture-fps','capture-detect','refresh-capture-sources'].forEach(id => $(id).disabled=busy || !!activeJob);
    const editable = selected?.state==='ready' && !busy && !activeJob;
    ['save-project-selection','add-project-marker','export-collection','prepare-montage','rescan-recording','project-pre','project-post','project-marker-time'].forEach(id=>$(id).disabled=!editable);
    $('recover-recording').disabled=busy || !!activeJob;
    document.querySelectorAll('.project-marker input,.project-marker button').forEach(node=>node.disabled=!editable);
  }

  function renderStatus(status) {
    current = status;
    if(status.busy)wasBusy=true;
    $('recording-state').textContent = states[status.state] || status.state;
    $('recording-clock').textContent = clock(status.elapsed_seconds || 0);
    $('recording-marker-count').textContent = status.marker_count || 0;
    $('recording-detection-state').textContent = detectionStates[status.detection_state] || status.detection_state || '';
    $('recording-hotkey').textContent = status.busy ? (status.hotkey ? 'Ctrl + Alt + F8 · 手动标记' : '快捷键尚未就绪，可使用添加标记按钮') : '录制时可用 Ctrl + Alt + F8 标记';
    const stats=status.stats || {};
    $('recording-health').textContent = [stats.encoder ? `编码 ${stats.encoder}` : '',status.gap_count ? `${status.gap_count} 处检测缺口` : ''].filter(Boolean).join(' · ');
    const warning=[status.error,...(status.warnings || [])].filter(Boolean).join('\n');
    $('recording-warning').hidden=!warning;$('recording-warning').textContent=warning;
    controls();
  }

  async function refreshSources() {
    const oldIndex=$('capture-source').value;
    const previous=oldIndex===''?null:sources[Number(oldIndex)];
    const response=await api('/api/recording/sources');
    sources=response.sources || [];
    $('capture-source').replaceChildren(new Option('请选择游戏窗口', ''));
    sources.forEach((source,index)=>$('capture-source').add(new Option(`${source.kind==='monitor'?'显示器 · ':''}${source.name || source.title || source.id}`,String(index))));
    const matching=previous?sources.findIndex(source=>source.kind===previous.kind && source.id===previous.id):-1;
    $('capture-source').value=matching>=0?String(matching):'';
    controls();
  }

  async function refreshSessions() {
    const response=await api('/api/recordings');
    $('recording-sessions').replaceChildren();
    if(!response.sessions?.length){const p=document.createElement('p');p.className='hint';p.textContent='还没有录制记录。';$('recording-sessions').append(p);return;}
    response.sessions.forEach(session=>{
      const button=document.createElement('button');button.type='button';button.className='secondary'+(selected?.id===session.id?' selected':'');
      const date=new Date(session.created_at).toLocaleString('zh-CN',{hour12:false});
      button.textContent=`${date}\n${states[session.state] || session.state} · ${session.markers?.length ?? session.marker_count ?? 0} 处标记`;
      button.addEventListener('click',safe(()=>loadProject(session.id)));$('recording-sessions').append(button);
    });
  }

  async function loadProject(id) {
    const version=++projectVersion;
    const project=await api(`/api/recording/project?id=${encodeURIComponent(id)}`);
    if(version!==projectVersion)return;
    selected=project;checked=new Set(project.selection || []);previewVersion++;
    $('project-preview').pause();$('project-preview').hidden=true;
    $('collection-video').pause();$('collection-result').hidden=true;
    $('project-preview-label').textContent='点击事件预览前后片段；可补充标记或排除误检。';
    renderProject();
  }

  function renderProject() {
    $('recording-project').hidden=!selected;
    if(!selected)return;
    $('project-state').textContent=states[selected.state] || selected.state;
    $('project-source').textContent=selected.source?.path || selected.source_path || '';
    const duration=selected.source?.metadata?.duration;
    $('project-summary').textContent=`${duration ? `原片 ${clock(duration)} · `:''}${selected.markers.length} 处标记 · 已选 ${checked.size} 处${selected.coverage?.length ? `\n有 ${selected.coverage.length} 处检测缺口记录，可补查整段提示区。`:''}`;
    if(selected.rescan_summary?.selection_required)$('project-summary').textContent+=`\n补查新增 ${selected.rescan_summary.new_unselected} 处未勾选标记，请核对后选择。`;
    if(selected.warnings?.length)$('project-summary').textContent+='\n'+selected.warnings.join('\n');
    $('project-pre').value=selected.settings?.pre ?? 5;$('project-post').value=selected.settings?.post ?? 2;
    $('project-markers').replaceChildren();
    selected.markers.filter(marker=>marker.state!=='rejected').forEach(marker=>{
      const row=document.createElement('div');row.className='project-marker';
      const label=document.createElement('label'), checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=checked.has(marker.event_id);
      checkbox.addEventListener('change',()=>{if(checkbox.checked)checked.add(marker.event_id);else checked.delete(marker.event_id);});
      const text=document.createElement('span');text.textContent=`${clock(marker.time)} · ${(marker.event_types || []).map(type=>typeLabels[type] || type).join(' / ') || '提示'}${marker.source==='manual'?' · 手动':marker.state==='needs_review'?' · 待确认':marker.state==='candidate'?' · 候选':''}`;
      label.append(checkbox,text);row.append(label);
      const button=document.createElement('button');button.type='button';button.className='secondary';button.textContent='预览片段';
      button.addEventListener('click',safe(async()=>{
        const pre=Number($('project-pre').value),post=Number($('project-post').value),duration=selected.source.metadata.duration;
        const start=Math.max(0,marker.time-pre),end=Math.min(duration,Math.max(start+.2,marker.end+post));
        await runProjectJob('/api/recording/preview',{session_id:selected.id,start,end:Math.min(end,start+45)},'preview');
      }));row.append(button);$('project-markers').append(row);
    });
    if(!selected.markers.length){const p=document.createElement('p');p.className='hint';p.textContent='暂无标记。可以补查提示区，或输入原片时间手动添加。';$('project-markers').append(p);}
    $('recover-recording').hidden=!['interrupted','failed'].includes(selected.state);
    controls();
  }

  async function saveSelection() {
    if(!selected)return;
    const id=selected.id,version=projectVersion;
    const result=await api('/api/recording/selection',{session_id:selected.id,selected_ids:[...checked],revision:selected.revision,pre:Number($('project-pre').value),post:Number($('project-post').value)});
    if(version!==projectVersion || selected?.id!==id)throw new Error('已切换录制工程，选择已保存；请在当前工程重新操作。');
    selected=result;checked=new Set(result.selection);renderProject();
  }

  async function runProjectJob(route,body,kind) {
    if(projectJob || current.busy || activeJob)throw new Error('请先完成当前任务。');
    pending=true;controls();
    try {
      const job=await api(route,body);
      projectJob={id:job.id,kind,sessionId:body.session_id,previewVersion:++previewVersion};
      $('project-job').hidden=false;$('project-job-progress').value=0;
      void pollProjectJob();
    } finally {pending=false;controls();}
  }

  async function pollProjectJob() {
    const task=projectJob;if(!task)return;
    try {
      const job=await api(`/api/job?id=${encodeURIComponent(task.id)}`);
      if(projectJob!==task)return;
      $('project-job-stage').textContent=job.stage;$('project-job-progress').value=job.progress;
      $('project-job-detail').textContent=job.detail || '';
      if(job.state==='running'){setTimeout(pollProjectJob,700);return;}
      projectJob=null;controls();
      if(job.state==='failed'){const error=new Error(job.error);error.taskId=job.id;throw error;}
      if(job.state==='cancelled')return;
      if(selected?.id!==task.sessionId)return;
      if(task.kind==='preview' && task.previewVersion===previewVersion){
        $('project-preview').src=mediaUrl(job.result.media_id);$('project-preview').hidden=false;
        $('project-preview-label').textContent=`原片 ${clock(job.result.start)}–${clock(job.result.end)}；预览不修改原片。`;
      } else if(task.kind==='collection'){
        $('collection-video').src=mediaUrl(job.result.media_id);$('collection-result').hidden=false;
        $('collection-summary').textContent=`${clock(job.result.duration)} · ${job.result.clips.length} 个片段 · ${job.result.selected_event_count} 处标记`;
        $('download-collection').href=mediaUrl(job.result.media_id);
      } else if(['recover','rescan'].includes(task.kind)){await loadProject(task.sessionId);await refreshSessions();}
    } catch(error){if(projectJob===task)projectJob=null;controls();showError(error);}
  }

  $('refresh-capture-sources').addEventListener('click',safe(refreshSources));
  $('capture-source').addEventListener('change',controls);
  $('refresh-recordings').addEventListener('click',safe(refreshSessions));
  $('start-recording').addEventListener('click',safe(async()=>{
    const index=$('capture-source').value;if(index==='' || !sources[Number(index)])throw new Error('请先选择录制窗口。');
    pending=true;controls();
    try{await api('/api/recording/start',{source:sources[Number(index)],audio:$('capture-audio').value==='system',fps:Number($('capture-fps').value),detect:$('capture-detect').checked,encoder:'auto'});renderStatus(await api('/api/recording'));await refreshSessions();}
    finally{pending=false;controls();}
  }));
  $('stop-recording').addEventListener('click',safe(async()=>renderStatus(await api('/api/recording/stop',{}))));
  $('mark-recording').addEventListener('click',safe(async()=>{await api('/api/recording/mark',{});renderStatus(await api('/api/recording'));}));
  $('save-project-selection').addEventListener('click',safe(saveSelection));
  $('add-project-marker').addEventListener('click',safe(async()=>{await saveSelection();await api('/api/recording/add-marker',{session_id:selected.id,time:Number($('project-marker-time').value)});await loadProject(selected.id);}));
  $('export-collection').addEventListener('click',safe(async()=>{await saveSelection();await runProjectJob('/api/recording/generate',{session_id:selected.id,mode:'collection'},'collection');}));
  $('recover-recording').addEventListener('click',safe(()=>runProjectJob('/api/recording/recover',{session_id:selected.id},'recover')));
  $('rescan-recording').addEventListener('click',safe(async()=>{await saveSelection();await runProjectJob('/api/recording/rescan',{session_id:selected.id},'rescan');}));
  $('cancel-project-job').addEventListener('click',safe(async()=>{if(projectJob)await api('/api/cancel',{id:projectJob.id});}));
  $('prepare-montage').addEventListener('click',safe(async()=>{
    await saveSelection();recordingMontage=selected;
    $('video').value=selected.source.path;invalidateTest();await inspect('video');
    $('recording-montage-note').hidden=false;$('recording-montage-note').textContent=`使用录制工程的 ${checked.size} 处已选标记，可直接选择音乐和特效，不必重新扫描。`;
    $('profile-test-panel').hidden=true;$('generation-hint').textContent='从已选高光按目标时长精选，并直接读取原片生成混剪。';
    workspace(false);
    $('strength').value=({none:0,light:.3,impact:.7})[$('effect-preset').value];strengthLabel();
    $('strength').disabled=true;
  }));
  $('effect-preset').addEventListener('change',()=>{$('strength').value=({none:0,light:.3,impact:.7})[$('effect-preset').value];strengthLabel();});

  async function tick() {
    if(appClosing)return;
    try {
      const status=await api('/api/recording');renderStatus(status);
      if(wasBusy && !status.busy){await refreshSessions();if(status.session_id)await loadProject(status.session_id);}
      wasBusy=status.busy;
      if(!status.busy && !projectJob && activeJob)workspace(false);
    } catch(error){$('recording-warning').hidden=false;$('recording-warning').textContent=`录制状态暂时无法读取：${error.message}。请保持程序运行后重试。`;}
    finally{pollTimer=setTimeout(tick,1500);}
  }
  async function init() {
    try{
      capabilities=await api('/api/recording/capabilities');
      $('recording-availability').textContent=capabilities.available?'录制组件已就绪 · 原片与标记保存在本机':`录制组件未就绪：${capabilities.reason || ''}。安装命令：${capabilities.install_command || '.venv\\Scripts\\python.exe scripts\\download_capture.py'}。导入剪辑仍可使用。`;
      await refreshSessions();controls();
      const state=await api('/api/state');
      if(state.active && (state.active_kind?.startsWith('recording_') || state.active_recording_mode==='collection')){
        const job=await api(`/api/job?id=${encodeURIComponent(state.active)}`);
        if(job.recording_session_id){await loadProject(job.recording_session_id);projectJob={id:job.id,kind:job.recording_mode==='collection'?'collection':job.kind.replace('recording_',''),sessionId:job.recording_session_id,previewVersion:++previewVersion};$('project-job').hidden=false;void pollProjectJob();}
      }
    }catch(error){$('recording-availability').textContent=`录制组件检查失败：${error.message}`;}
    void tick();
  }
  void init();
})();
