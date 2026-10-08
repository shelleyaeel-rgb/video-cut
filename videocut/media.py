from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from . import logs
from .paths import DATA_ROOT, RESOURCE_ROOT

ROOT = DATA_ROOT
VIDEO_SUFFIXES = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.ts'}
AUDIO_SUFFIXES = VIDEO_SUFFIXES | {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg'}


class Cancelled(RuntimeError):
    pass


class Context:
    def __init__(self, callback=None, task_id=None):
        self.cancelled = threading.Event()
        self.callback = callback or (lambda *args: None)
        self.lock = threading.Lock()
        self.processes = set()
        self.task_id = task_id or logs.context().get('task_id') or uuid.uuid4().hex
        self.stage = '准备中'

    def record(self, name, **fields):
        logs.event('tasks', name, task_id=self.task_id, **fields)

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled('任务已取消。')

    def update(self, stage, progress, detail=''):
        self.check()
        self.stage = stage
        self.record('progress', stage=stage, progress=round(progress, 2), detail=detail)
        self.callback(stage, min(100, max(0, progress)), detail)

    def attach(self, process):
        with self.lock:
            if self.cancelled.is_set():
                process.kill()
                process.wait()
                raise Cancelled('任务已取消。')
            self.processes.add(process)

    def detach(self, process):
        with self.lock:
            self.processes.discard(process)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass


def tool_path(name):
    override = os.environ.get('VIDEOCUT_' + name.upper())
    if override and Path(override).is_file():
        return Path(override)
    suffix = '.exe' if os.name == 'nt' else ''
    found = sorted((RESOURCE_ROOT / '.tools' / 'ffmpeg').glob(f'**/bin/{name}{suffix}'))
    if found:
        return found[-1]
    system = shutil.which(name)
    if system:
        return Path(system)
    raise RuntimeError(f'找不到 {name}，请先运行 scripts/setup.ps1。')


def popen(args, **kwargs):
    return subprocess.Popen([str(a) for a in args], creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, **kwargs)


class LoggedProcess:
    """Drain stderr concurrently for both normal commands and raw-video scans."""
    def __init__(self, args, ctx=None):
        self.args, self.ctx = args, ctx
        self.fields = {**logs.context(), 'command_id': uuid.uuid4().hex[:12]}
        if ctx and ctx.task_id:
            self.fields['task_id'] = ctx.task_id
        self.tail = ''
        self.outcome = None

    def __enter__(self):
        if self.ctx:
            self.ctx.check()
        self.started = time.perf_counter()
        logs.event('ffmpeg', 'command_started', argv=[str(a) for a in self.args], **self.fields)
        try:
            self.process = popen(self.args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            logs.failure('ffmpeg', 'spawn_failed', exc, **self.fields)
            raise

        def read_errors():
            try:
                for raw in iter(self.process.stderr.readline, b''):
                    text = raw.decode('utf-8', errors='replace')
                    logs.stderr_line(text, **self.fields)
                    self.tail = (self.tail + logs.store().clean(text))[-5000:]
            except (OSError, ValueError) as exc:
                logs.failure('ffmpeg', 'stderr_reader_failed', exc, **self.fields)
            finally:
                self.process.stderr.close()
        self.reader = threading.Thread(target=read_errors, daemon=True)
        self.reader.start()
        try:
            if self.ctx:
                self.ctx.attach(self.process)
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def check_result(self):
        self.reader.join()
        if self.ctx:
            self.ctx.check()
        if self.process.returncode:
            raise RuntimeError(self.tail[-2200:] or '媒体处理失败。')

    def __exit__(self, exc_type, exc, tb):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.reader.join()
        self.process.stdout.close()
        if self.ctx:
            self.ctx.detach(self.process)
        outcome = self.outcome or ('cancelled' if isinstance(exc, Cancelled) else 'failed' if exc or self.process.returncode else 'complete')
        logs.event('ffmpeg', 'command_finished', 'INFO' if outcome == 'complete' else 'WARNING', returncode=self.process.returncode, outcome=outcome, elapsed_seconds=round(time.perf_counter()-self.started, 3), **self.fields)


def run(args, ctx=None, timeout=600):
    with LoggedProcess(args, ctx) as child:
        output, read_errors = [], []
        def read_output():
            try:
                output.append(child.process.stdout.read())
            except OSError as exc:
                read_errors.append(exc)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            child.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.outcome = 'timeout'
            child.process.kill()
            child.process.wait()
            raise RuntimeError(f'处理超时（{timeout} 秒），请检查素材或缩短任务。')
        finally:
            if child.process.poll() is None:
                child.process.kill()
                child.process.wait()
            reader.join()
        child.check_result()
        if read_errors:
            raise read_errors[0]
        return output[0]


def ffmpeg_args():
    return [tool_path('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostats', '-nostdin', '-y', '-threads', '2', '-filter_threads', '2', '-filter_complex_threads', '2']


def local_file(value, audio=False):
    path = Path(str(value).strip().strip('"')).expanduser().resolve()
    if not path.is_file():
        raise ValueError('文件不存在，请填写本机完整文件路径。')
    if path.suffix.lower() not in (AUDIO_SUFFIXES if audio else VIDEO_SUFFIXES):
        raise ValueError('不支持这个媒体文件扩展名。')
    return path


def probe(path, ctx=None):
    raw = json.loads(run([tool_path('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path], ctx, 30))
    duration = float(raw.get('format', {}).get('duration', 0) or 0)
    videos = [s for s in raw['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in raw['streams'] if s['codec_type'] == 'audio']
    if duration <= 0:
        raise ValueError('无法读取有效媒体时长。')
    video = videos[0] if videos else {}
    return {'duration': duration, 'width': video.get('width', 0), 'height': video.get('height', 0), 'video_codec': video.get('codec_name'), 'color_transfer': video.get('color_transfer'), 'audio_tracks': [{'index': i, 'codec': s.get('codec_name'), 'channels': s.get('channels'), 'title': s.get('tags', {}).get('title', f'音轨 {i + 1}')} for i, s in enumerate(audios)]}


def preview_frame(path, time, ctx=None):
    meta = probe(path, ctx)
    if not meta['width']:
        raise ValueError('所选文件没有视频画面。')
    time = max(0, min(float(time), max(0, meta['duration'] - .1)))
    return run(ffmpeg_args() + ['-ss', f'{time:.4f}', '-i', path, '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=960:-1', '-f', 'image2pipe', '-vcodec', 'png', '-'], ctx, 60)
