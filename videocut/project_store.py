"""Durable recording sessions, markers and non-destructive edit selections."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid

from .paths import DATA_ROOT
from .timeline import RecordingSource, file_signature, seconds_to_us


_ACTIVE_STATES = ('created', 'starting', 'arming', 'recording', 'stopping', 'finalizing', 'recovering')
_STATES = {*_ACTIVE_STATES, 'ready', 'interrupted', 'failed'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _padding(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError('片段前后时长无效。') from None
    if not math.isfinite(value) or not 0 <= value <= 60:
        raise ValueError('片段前后时长须在 0–60 秒之间。')
    return value


def _marker_values(marker, previous=None):
    previous = previous or {}
    merged = {**previous, **marker}
    # Explicit seconds must replace an old integer timestamp in a partial edit.
    for integer, seconds in (('time_us', 'time'), ('end_us', 'end')):
        if seconds in marker and integer not in marker:
            merged.pop(integer, None)
    def timestamp(key, alternate, default=None):
        if key in merged:
            value = merged[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63-1:
                raise ValueError('事件时间戳须为非负整数微秒。')
            return value
        value = seconds_to_us(merged.get(alternate, default))
        if value > 2**63-1:
            raise ValueError('事件时间戳超出范围。')
        return value
    begin = timestamp('time_us', 'time')
    end = timestamp('end_us', 'end', begin/1_000_000)
    if end < begin:
        raise ValueError('事件结束时间不能早于开始时间。')
    payload = dict(merged)
    for key in ('id', 'marker_id', 'event_id', 'time_us', 'end_us', 'time', 'end'):
        payload.pop(key, None)
    payload.setdefault('confidence', 1. if payload.get('source') == 'manual' else 0.)
    payload.setdefault('classification', 'unknown')
    payload.setdefault('event_types', ['kill'])
    return begin, end, payload


class ProjectStore:
    """Each mutation is one FULL synchronous SQLite transaction.

    Call recover_interrupted once at application startup, never when a second
    client simply connects. The capture writer is independent of this store.
    """
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DATA_ROOT / 'data' / 'projects.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect(initialize=True) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, source_path TEXT, options_json TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
                    path TEXT NOT NULL, revision TEXT NOT NULL, signature TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, sealed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS markers (
                    id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
                    time_us INTEGER NOT NULL, end_us INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(session_id,id));
                CREATE INDEX IF NOT EXISTS markers_time ON markers(session_id,time_us);
                CREATE TABLE IF NOT EXISTS coverage (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    start_us INTEGER NOT NULL, end_us INTEGER NOT NULL, kind TEXT NOT NULL,
                    reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS edits (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id), revision INTEGER NOT NULL,
                    selection_json TEXT, settings_json TEXT NOT NULL DEFAULT '{}');
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def _connect(self, *, write=False, initialize=False):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            try:
                with db:
                    if not initialize:
                        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                    yield db
            finally:
                db.close()

    @staticmethod
    def _require(db, session_id):
        row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is None:
            raise ValueError('录制工程不存在。')
        return row

    @staticmethod
    def _marker(row):
        result = json.loads(row['payload_json'])
        result.update(id=row['id'], marker_id=row['id'], event_id=row['id'],
                      time_us=row['time_us'], end_us=row['end_us'],
                      time=row['time_us']/1_000_000, end=row['end_us']/1_000_000)
        return result

    def _snapshot(self, db, session_id):
        row = self._require(db, session_id)
        result = json.loads(row['details_json'])
        result.update(id=row['id'], session_id=row['id'], state=row['state'],
                      created_at=row['created_at'], updated_at=row['updated_at'],
                      source_path=row['source_path'], options=json.loads(row['options_json']))
        result['markers'] = [self._marker(item) for item in db.execute(
            'SELECT * FROM markers WHERE session_id=? ORDER BY time_us,id', (session_id,))]
        result['coverage'] = [dict(item, start=item['start_us']/1_000_000,
                                   end=item['end_us']/1_000_000) for item in db.execute(
            'SELECT * FROM coverage WHERE session_id=? ORDER BY start_us,id', (session_id,))]
        edit = db.execute('SELECT * FROM edits WHERE session_id=?', (session_id,)).fetchone()
        result['revision'] = edit['revision']
        result['settings'] = json.loads(edit['settings_json'])
        selected = json.loads(edit['selection_json']) if edit['selection_json'] is not None else None
        result['selection_explicit'] = selected is not None
        rejected = {event['event_id'] for event in result['markers'] if event.get('state') == 'rejected'}
        result['selection'] = [ident for ident in selected if ident not in rejected] if selected is not None else [
            event['event_id'] for event in result['markers'] if event.get('classification') != 'npc'
            and event.get('state') not in ('candidate', 'rejected')]
        asset = db.execute('SELECT * FROM assets WHERE session_id=?', (session_id,)).fetchone()
        result['source'] = None
        if asset:
            result['source'] = {'id': asset['id'], 'path': asset['path'], 'revision': asset['revision'],
                                'signature': asset['signature'], 'sealed_at': asset['sealed_at'],
                                'metadata': json.loads(asset['metadata_json']), 'state': 'sealed'}
        return result

    def create_session(self, options=None, source_path=None, session_id=None):
        ident = session_id or uuid.uuid4().hex
        if not isinstance(ident, str) or not ident or len(ident) > 128:
            raise ValueError('录制工程标识无效。')
        if options is not None and not isinstance(options, dict):
            raise ValueError('录制设置无效。')
        now = _now()
        with self._connect(write=True) as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',
                       (ident, 'created', now, now, str(Path(source_path).resolve()) if source_path else None,
                        _json(options or {}), '{}'))
            db.execute('INSERT INTO edits (session_id,revision) VALUES (?,0)', (ident,))
            return self._snapshot(db, ident)

    def update_session(self, session_id, state=None, **fields):
        if state is not None and state not in _STATES:
            raise ValueError('录制状态无效。')
        with self._connect(write=True) as db:
            row = self._require(db, session_id)
            if state == 'ready' and not db.execute('SELECT 1 FROM assets WHERE session_id=?', (session_id,)).fetchone():
                raise ValueError('录像尚未封存，不能标记为可剪辑。')
            details = json.loads(row['details_json'])
            source_path = fields.pop('source_path', row['source_path'])
            details.update(fields)
            db.execute('UPDATE sessions SET state=?,updated_at=?,source_path=?,details_json=? WHERE id=?',
                       (state or row['state'], _now(), str(Path(source_path).resolve()) if source_path else None,
                        _json(details), session_id))
            return self._snapshot(db, session_id)

    def get_session(self, session_id):
        with self._connect() as db:
            return self._snapshot(db, session_id)

    def list_sessions(self, limit=50):
        limit = max(1, min(500, int(limit)))
        with self._connect() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM sessions ORDER BY created_at DESC LIMIT ?', (limit,))]
            return [self._snapshot(db, ident) for ident in ids]

    def upsert_marker(self, session_id, marker, marker_id=None):
        if not isinstance(marker, dict):
            raise ValueError('事件标记无效。')
        ident = marker_id or marker.get('marker_id') or marker.get('event_id') or marker.get('id') or uuid.uuid4().hex
        if not isinstance(ident, str) or not ident or len(ident) > 128:
            raise ValueError('事件标记标识无效。')
        with self._connect(write=True) as db:
            self._require(db, session_id)
            old = db.execute('SELECT * FROM markers WHERE session_id=? AND id=?', (session_id, ident)).fetchone()
            begin, end, payload = _marker_values(marker, self._marker(old) if old else None)
            db.execute('''INSERT INTO markers VALUES (?,?,?,?,?) ON CONFLICT(session_id,id)
                          DO UPDATE SET time_us=excluded.time_us,end_us=excluded.end_us,payload_json=excluded.payload_json''',
                       (ident, session_id, begin, end, _json(payload)))
            if old is None and payload.get('source') == 'manual':
                edit = db.execute('SELECT selection_json FROM edits WHERE session_id=?', (session_id,)).fetchone()
                if edit['selection_json'] is not None:
                    selected = json.loads(edit['selection_json'])
                    if ident not in selected:
                        selected.append(ident)
                    db.execute('UPDATE edits SET selection_json=? WHERE session_id=?', (_json(selected), session_id))
            db.execute('UPDATE edits SET revision=revision+1 WHERE session_id=?', (session_id,))
            db.execute('UPDATE sessions SET updated_at=? WHERE id=?', (_now(), session_id))
            row = db.execute('SELECT * FROM markers WHERE session_id=? AND id=?', (session_id, ident)).fetchone()
            return self._marker(row)

    def add_gap(self, session_id, start, end, reason, kind='detection'):
        begin, finish = seconds_to_us(start), seconds_to_us(end)
        if finish < begin:
            raise ValueError('覆盖缺口结束时间不能早于开始时间。')
        ident = uuid.uuid4().hex
        with self._connect(write=True) as db:
            self._require(db, session_id)
            db.execute('INSERT INTO coverage VALUES (?,?,?,?,?,?)',
                       (ident, session_id, begin, finish, str(kind), str(reason)))
        return {'id': ident, 'start_us': begin, 'end_us': finish,
                'start': begin/1_000_000, 'end': finish/1_000_000, 'kind': str(kind), 'reason': str(reason)}

    def finalize_asset(self, session_id, path=None, metadata=None):
        """Seal an already closed, recorder-validated file; never probe a live file."""
        with self._connect(write=True) as db:
            row = self._require(db, session_id)
            selected_path = path or row['source_path']
            if not selected_path:
                raise ValueError('录制工程没有原始录像。')
            source = Path(selected_path).resolve()
            metadata = dict(metadata or {})
            duration = float(metadata.get('duration', 0))
            if not math.isfinite(duration) or duration <= 0 or not metadata.get('width'):
                raise ValueError('封存录像需要有效的视频检查结果。')
            signature = file_signature(source)
            prior = db.execute('SELECT * FROM assets WHERE session_id=?', (session_id,)).fetchone()
            ident = prior['id'] if prior else uuid.uuid4().hex
            revision = prior['revision'] if prior and prior['signature'] == signature else uuid.uuid4().hex
            now = _now()
            db.execute('''INSERT INTO assets VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id)
                          DO UPDATE SET path=excluded.path,revision=excluded.revision,signature=excluded.signature,
                          metadata_json=excluded.metadata_json,sealed_at=excluded.sealed_at''',
                       (ident, session_id, str(source), revision, signature, _json(metadata), now))
            db.execute('UPDATE sessions SET state=?,source_path=?,updated_at=? WHERE id=?',
                       ('ready', str(source), now, session_id))
            return self._snapshot(db, session_id)

    def recover_interrupted(self):
        """Only change metadata. Keep incomplete media for the recorder's recovery."""
        with self._connect(write=True) as db:
            rows = db.execute('SELECT id,state,details_json FROM sessions WHERE state IN (' +
                              ','.join('?' for _ in _ACTIVE_STATES) + ')', _ACTIVE_STATES).fetchall()
            for row in rows:
                details = json.loads(row['details_json'])
                details.update(interrupted_from=row['state'], recovery_required=True)
                db.execute('UPDATE sessions SET state=?,updated_at=?,details_json=? WHERE id=?',
                           ('interrupted', _now(), _json(details), row['id']))
            return [self._snapshot(db, row['id']) for row in rows]

    def recover_pending(self):
        """Alias used after the recording manager acquires its process lock."""
        return self.recover_interrupted()

    def update_selection(self, session_id, selected_ids, expected_revision=None, pre=None, post=None, **settings):
        if not isinstance(selected_ids, (list, tuple)) or any(not isinstance(item, str) for item in selected_ids):
            raise ValueError('选中事件清单无效。')
        selected_ids = list(dict.fromkeys(selected_ids))
        with self._connect(write=True) as db:
            current = self._snapshot(db, session_id)
            pre = _padding(current['settings'].get('pre', 5) if pre is None else pre)
            post = _padding(current['settings'].get('post', 2) if post is None else post)
            if expected_revision is not None and int(expected_revision) != current['revision']:
                raise ValueError('工程已更新，请刷新后重试。')
            if not set(selected_ids) <= {event['event_id'] for event in current['markers']}:
                raise ValueError('选中的事件标记不属于此录制工程。')
            saved = {**current['settings'], **settings, 'pre': pre, 'post': post}
            db.execute('UPDATE edits SET revision=revision+1,selection_json=?,settings_json=? WHERE session_id=?',
                       (_json(selected_ids), _json(saved), session_id))
            db.execute('UPDATE sessions SET updated_at=? WHERE id=?', (_now(), session_id))
            return self._snapshot(db, session_id)

    def replace_automatic_markers(self, session_id, markers):
        """Commit a completed rescan, preserving manual markers and review intent.

        One-to-one nearby matches of the same event type retain their UUID.
        Ambiguous matches receive fresh IDs; explicitly reviewed projects never
        silently select new detections. Partial/failed rescans must not call us.
        """
        if not isinstance(markers, list) or any(not isinstance(item, dict) for item in markers):
            raise ValueError('补扫事件清单无效。')
        with self._connect(write=True) as db:
            current = self._snapshot(db, session_id)
            asset = current['source']
            if current['state'] != 'ready' or not asset:
                raise ValueError('请先封存录像，再补扫事件。')
            if file_signature(asset['path']) != asset['signature']:
                raise ValueError('原始录制文件已改变，请重新检查录制工程。')
            old = [item for item in current['markers'] if item.get('source') != 'manual']
            available = {item['event_id']: item for item in old}
            replacements, matched = [], 0
            for incoming in sorted(markers, key=lambda item: _marker_values(item)[0]):
                begin, end, payload = _marker_values(incoming)
                types = set(payload['event_types'])
                choices = sorted((abs(begin-item['time_us']), ident)
                                 for ident, item in available.items()
                                 if types == set(item.get('event_types', ['kill']))
                                 and abs(begin-item['time_us']) <= 750_000)
                # Refuse uncertain same-time identities instead of transferring
                # an exclusion to a different player's/target's notification.
                reliable = choices and (len(choices) == 1 or choices[1][0]-choices[0][0] > 50_000)
                if reliable:
                    ident = choices[0][1]
                    available.pop(ident)
                    matched += 1
                else:
                    ident = uuid.uuid4().hex
                payload['source'] = 'automatic'
                payload.setdefault('state', 'confirmed')
                replacements.append((ident, session_id, begin, end, _json(payload)))
            automatic_ids = {item['event_id'] for item in old}
            for ident in automatic_ids:
                db.execute('DELETE FROM markers WHERE session_id=? AND id=?', (session_id, ident))
            db.executemany('INSERT INTO markers VALUES (?,?,?,?,?)', replacements)
            surviving = {item[0] for item in replacements} | {
                item['event_id'] for item in current['markers'] if item.get('source') == 'manual'}
            selection = (_json([ident for ident in current['selection'] if ident in surviving])
                         if current['selection_explicit'] else None)
            db.execute('UPDATE edits SET revision=revision+1,selection_json=? WHERE session_id=?', (selection, session_id))
            details = json.loads(self._require(db, session_id)['details_json'])
            added = len(replacements)-matched
            details['rescan_summary'] = {'matched': matched, 'new': added,
                                         'removed': len(available),
                                         'new_unselected': added if current['selection_explicit'] else 0,
                                         'selection_required': bool(added and current['selection_explicit'])}
            db.execute('UPDATE sessions SET updated_at=?,details_json=? WHERE id=?', (_now(), _json(details), session_id))
            return self._snapshot(db, session_id)

    def build_generation_spec(self, session_id, options=None, mode='collection'):
        if mode not in ('collection', 'montage'):
            raise ValueError('导出模式须为基础集锦或混剪。')
        if options is not None and not isinstance(options, dict):
            raise ValueError('混剪设置无效。')
        current = self.get_session(session_id)
        if current['state'] != 'ready' or current['source'] is None:
            raise ValueError('请先停止录制并等待录像封存完成。')
        asset = current['source']
        selected = set(current['selection'])
        duration = float(asset['metadata']['duration'])
        if any(marker['event_id'] in selected and marker['time'] >= duration for marker in current['markers']):
            raise ValueError('选中标记超出已保存录像范围，可能位于未恢复的片尾，请取消这些标记后重试。')
        settings = {**current['settings'], **(options or {})}
        context = RecordingSource(session_id, asset['id'], asset['revision'], current['revision'],
                                  asset['path'], asset['signature'], _json(current['markers']),
                                  tuple(current['selection']), mode,
                                  _padding(settings.get('pre', 5)), _padding(settings.get('post', 2)),
                                  _json(current['coverage']))
        context.validate()
        allowed = ('music', 'target', 'music_start', 'strength', 'game_volume', 'music_volume',
                   'audio_track', 'encoder', 'voice_mode', 'music_voice_mode', 'game_audio_track',
                   'effect_preset')
        spec = {key: settings[key] for key in allowed if key in settings}
        spec.update(video=asset['path'], profile='delta-auto')
        if mode == 'collection':
            spec.update(music='', target=90, strength=0, game_volume=1, voice_mode='keep',
                        music_voice_mode='keep')
        return spec, context
