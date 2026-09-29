"""Audio selection, local voice reduction, caching, and final mixing."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import wave

from .audio_model import ENGINE_VERSION, MODEL_SHA256, SAMPLE_RATE, require_model
from .media import ROOT, LoggedProcess, ffmpeg_args, run

VOICE_MODES = ('keep','game_track','reduce')
MUSIC_VOICE_MODES = ('keep','instrumental')
CACHE = ROOT/'work'/'audio-cache'


def validate_modes(spec):
    voice = spec.get('voice_mode','keep')
    music = spec.get('music_voice_mode','keep')
    if voice not in VOICE_MODES or music not in MUSIC_VOICE_MODES:
        raise ValueError('人声处理方式无效。')
    track = None
    if voice == 'game_track':
        value = spec.get('game_audio_track')
        try:
            track = int(value)
            if isinstance(value,bool) or float(value) != track or not 0 <= track <= 63:
                raise ValueError()
        except (TypeError,ValueError,OverflowError):
            raise ValueError('请选择 OBS 单独录制的游戏音轨。') from None
    if voice == 'reduce' or music == 'instrumental':
        require_model()
    return {'voice_mode':voice,'music_voice_mode':music,'game_audio_track':track}


def validate_tracks(cfg, meta):
    tracks = meta['audio_tracks']
    if cfg['voice_mode'] == 'game_track':
        if len(tracks)<2:
            raise ValueError('录像没有多条音轨。若唯一音轨已经是纯游戏声，请选择保留原声；否则可尝试 AI 减弱人声。')
        track = cfg['game_audio_track']
    else:
        track = cfg['audio_track']
    if tracks and not 0 <= track < len(tracks):
        raise ValueError('所选游戏音轨不存在，请重新读取录像并选择音轨。')
    return track


def pcm_segment(path, start, duration, track, ctx):
    count = round(duration*SAMPLE_RATE)*4
    raw = run(ffmpeg_args()+['-ss',str(start),'-i',path,'-map',f'0:a:{track}','-vn','-af',f'apad,atrim=duration={duration},asetpts=PTS-STARTPTS','-t',str(duration),'-ar',str(SAMPLE_RATE),'-ac','2','-f','s16le','-'],ctx,120)
    return raw[:count].ljust(count,b'\0')


def write_pcm(path, chunks):
    with wave.open(str(path),'wb') as dest:
        dest.setparams((2,2,SAMPLE_RATE,0,'NONE','not compressed'))
        for chunk in chunks:
            dest.writeframesraw(chunk)


def cached_audio_valid(path, expected_frames):
    try:
        with wave.open(str(path),'rb') as audio:
            return audio.getnchannels()==2 and audio.getsampwidth()==2 and audio.getframerate()==SAMPLE_RATE and audio.getnframes()==expected_frames and len(audio.readframes(expected_frames))==expected_frames*4
    except (OSError,EOFError,wave.Error):
        return False


def reduce_voice(source, destination, ctx, label, low, high):
    require_model(verify=True)
    with source.open('rb') as file:
        digest = hashlib.file_digest(file,'sha256').hexdigest()
    key = hashlib.sha256(f'{digest}:{MODEL_SHA256}:{ENGINE_VERSION}'.encode()).hexdigest()
    cached = CACHE/f'{key}.wav'
    with wave.open(str(source),'rb') as audio:
        frames = audio.getnframes()
    if cached_audio_valid(cached,frames):
        ctx.update(label,high,'复用已处理的音频')
        ctx.record('audio_cache_hit',source=str(source),cache_key=key)
        return cached, {'mode':'ai','model_sha256':MODEL_SHA256,'device':'CPU','cache_hit':True}
    progress = destination.with_suffix('.progress.json')
    ctx.update(label,low,'CPU 正在加载人声模型，可随时取消')
    started = time.monotonic()
    command = [sys.executable,'-m','videocut.separator',source,destination,progress]
    with LoggedProcess(command,ctx) as child:
        last = None
        while True:
            ctx.check()
            try:
                child.process.wait(timeout=.25)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic()-started > 1800:
                    child.outcome='timeout'
                    raise RuntimeError('AI 人声处理超过 30 分钟，请缩短成片或使用独立游戏音轨。')
                try:
                    state = json.loads(progress.read_text(encoding='utf-8'))
                    done,total=state['done'],max(1,state['total'])
                    if (done,total) != last:
                        ctx.update(label,low+(high-low)*done/total,f'CPU · {done} / {total} 段')
                        last=(done,total)
                except (OSError,ValueError,KeyError):
                    pass
        child.check_result()
    ctx.check()
    if not cached_audio_valid(destination,frames):
        raise RuntimeError('人声处理输出损坏或时长不符，未继续混音。')
    details=json.loads(progress.read_text(encoding='utf-8'))
    CACHE.mkdir(parents=True,exist_ok=True)
    # Atomic cache publication; partial or cancelled output never becomes a hit.
    destination.replace(cached)
    ctx.update(label,high,'处理完成')
    record={'mode':'ai','model_sha256':MODEL_SHA256,'device':'CPU','cache_hit':False,'gain':details['gain'],'elapsed_seconds':details['elapsed_seconds']}
    ctx.record('audio_separated',source=str(source),cache_key=key,**record)
    return cached,record


def prepare_audio(cfg, meta, clips, duration, scratch, ctx):
    track = validate_tracks(cfg,meta)
    ctx.update('提取入选片段声音',51)
    game, music = scratch/'game-original.wav', scratch/'music-original.wav'
    def game_chunks():
        for i,clip in enumerate(clips):
            ctx.update('提取入选片段声音',51+3*i/len(clips),f'{i+1} / {len(clips)}')
            yield pcm_segment(cfg['video'],clip['start'],clip['duration'],track,ctx) if meta['audio_tracks'] else b'\0'*(round(clip['duration']*SAMPLE_RATE)*4)
    write_pcm(game,game_chunks())
    write_pcm(music,[pcm_segment(cfg['music'],cfg['music_start'],duration,0,ctx)])
    report={'game':{'mode':cfg['voice_mode'],'audio_track':track if meta['audio_tracks'] else None},'music':{'mode':cfg['music_voice_mode']}}
    warnings=[]
    if cfg['voice_mode']=='reduce' and meta['audio_tracks']:
        game,details=reduce_voice(game,scratch/'game-clean.wav',ctx,'AI 减弱录像人声',55,65)
        report['game'].update(details)
        warnings.append('已对录像音轨进行 AI 人声分离；说话声可能残留，枪声、环境声或游戏角色语音可能受影响，请试听。')
    elif cfg['voice_mode']=='game_track':
        warnings.append(f'已使用原片第 {track+1} 条音轨；请确认该轨只包含游戏声，工具不会自动判断音轨内容。')
    elif cfg['voice_mode']=='reduce':
        report['game']['skipped']='no_audio'
    if cfg['music_voice_mode']=='instrumental':
        music,details=reduce_voice(music,scratch/'music-clean.wav',ctx,'AI 提取 BGM 伴奏',65,74)
        report['music'].update(details)
        warnings.append('BGM 已经 AI 提取伴奏，可能仍有少量人声或乐器损失。')
    ctx.record('audio_ready',audio_processing=report)
    return game,music,report,warnings


def mix_and_mux(concat, game, music, impacts, duration, cfg, destination, ctx):
    fade_start=max(0,duration-.5)
    graph=f'[1:a]volume={cfg["game_volume"]}[game];[2:a]atrim=duration={duration},asetpts=PTS-STARTPTS,volume={cfg["music_volume"]},afade=t=in:d=0.15,afade=t=out:st={fade_start}:d=0.5[music];[game][music][3:a]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95:level=0:latency=1[a]'
    run(ffmpeg_args()+['-f','concat','-safe','0','-i',concat,'-i',game,'-i',music,'-i',impacts,'-filter_complex',graph,'-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-ac','2','-t',str(duration),'-movflags','+faststart',destination],ctx,900)
