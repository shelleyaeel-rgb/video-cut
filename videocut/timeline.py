"""Persisted recording timestamps and server-owned source snapshots.

Capture timestamps, rather than detector completion times, define every event.
The render API never constructs a RecordingSource from a browser request.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path


def seconds_to_us(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError('事件时间必须是有效数值。') from None
    if not math.isfinite(number) or number < 0:
        raise ValueError('事件时间必须是非负有限数值。')
    return round(number * 1_000_000)


def file_signature(path):
    source = Path(path).resolve()
    stat = source.stat()
    if not source.is_file() or stat.st_size <= 0:
        raise ValueError('录制文件为空或不存在。')
    payload = [str(source), stat.st_size, stat.st_mtime_ns]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class RecordingSource:
    """A sealed source and edit revision, created only by ProjectStore.

    JSON makes event contents immutable as well as independent of later edits.
    A file signature is deliberately created only after capture has closed it.
    """
    session_id: str
    source_id: str
    source_revision: str
    project_revision: int
    video: str
    signature: str
    events_json: str
    selected_ids: tuple[str, ...]
    mode: str = 'collection'
    pre: float = 5.
    post: float = 2.
    coverage_json: str = '[]'

    def validate(self, video=None):
        if self.mode not in ('collection', 'montage'):
            raise ValueError('未知的录制导出模式。')
        if video is not None and Path(video).resolve() != Path(self.video).resolve():
            raise ValueError('导出录像与录制工程不一致。')
        if file_signature(self.video) != self.signature:
            raise ValueError('原始录制文件已改变，请重新检查录制工程。')

    def candidates(self):
        return json.loads(self.events_json)

    def manifest(self):
        return {'session_id': self.session_id, 'source_id': self.source_id,
                'source_revision': self.source_revision,
                'project_revision': self.project_revision, 'mode': self.mode,
                'selected_ids': list(self.selected_ids), 'pre': self.pre, 'post': self.post,
                'coverage': json.loads(self.coverage_json)}
