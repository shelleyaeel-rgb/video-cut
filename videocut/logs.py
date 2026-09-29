"""Local structured diagnostics with bounded retention and secret redaction."""
from __future__ import annotations

import contextlib
import contextvars
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import sys
import threading
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parents[1] / 'work' / 'logs'
MIB = 1024 * 1024
POLICY = {'startup': (2*MIB, 2), 'runtime': (5*MIB, 3), 'tasks': (5*MIB, 3), 'ffmpeg': (10*MIB, 4), 'frontend': (2*MIB, 2)}
_context = contextvars.ContextVar('diagnostic_context', default={})
_manager = None
_manager_lock = threading.RLock()
_sensitive = re.compile(r'(?i)(token|authorization|cookie|password|passwd|secret|api[_-]?key|credential)')
_assignments = re.compile(r'''(?i)(?<![\w-])((?:[\w-]{0,64}(?:token|password|passwd|secret|api[_-]?key)[\w-]{0,64}|authorization|cookie)["']?\s*[:=]\s*["']?)(\[REDACTED\]|[^\s&"'<>;,}\]]+)''')


class SafeRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # Never dump an unsanitized LogRecord or stop a render due to log I/O.
        self.failed = True
        if not getattr(self, 'warned', False):
            self.warned = True
            print('VideoCut: cannot write diagnostics; check free space and permissions.', file=sys.stderr)


class LogStore:
    def __init__(self, directory=DIRECTORY, policy=None):
        self.directory = Path(directory).resolve()
        self.policy = policy or POLICY
        self.lock = threading.RLock()
        self.secrets = set()
        self.handlers = {}
        self.failed = False

    def add_secret(self, secret):
        if secret:
            with self.lock:
                self.secrets.add(str(secret))

    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): '[REDACTED]' if _sensitive.search(str(k)) else self.clean(v) for k,v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if value is None or isinstance(value, (int, float, bool)):
            return value
        text = str(value)
        with self.lock:
            secrets = tuple(self.secrets)
        for secret in secrets:
            text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', text)
        text = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[REDACTED]@', text)
        return _assignments.sub(r'\1[REDACTED]', text)

    def write(self, category, event, level='INFO', **fields):
        if category not in self.policy:
            category = 'runtime'
        entry = {'time': datetime.now().astimezone().isoformat(timespec='milliseconds'), 'level': level, 'event': event, **_context.get(), **fields}
        # Redact before serializing or handing anything to the logging module.
        payload = json.dumps(self.clean(entry), ensure_ascii=False, default=str)
        try:
            with self.lock:
                if category not in self.handlers:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    size, backups = self.policy[category]
                    handler = SafeRotatingHandler(self.directory/f'{category}.log', maxBytes=size, backupCount=backups, encoding='utf-8')
                    handler.failed = False
                    handler.setFormatter(logging.Formatter('%(message)s'))
                    self.handlers[category] = handler
                handler = self.handlers[category]
                handler.handle(logging.LogRecord(category, getattr(logging, level, logging.INFO), '', 0, payload, (), None))
                self.failed = self.failed or handler.failed
        except OSError:
            self.failed = True
        return not self.failed

    def status(self):
        return {'directory': str(self.directory), 'write_error': self.failed, 'retention_bytes': sum(size*(backups+1) for size,backups in self.policy.values())}

    def close(self):
        with self.lock:
            for handler in self.handlers.values():
                handler.close()
            self.handlers.clear()

    def archive(self):
        """Export only our known log files; never accept a user-supplied path."""
        buffer = io.BytesIO()
        with self.lock, zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            names = ['bootstrap.log', 'bootstrap.log.1']
            for category, (_, backups) in self.policy.items():
                names += [f'{category}.log'] + [f'{category}.log.{i}' for i in range(1, backups+1)]
            for name in names:
                path = self.directory/name
                if path.is_file():
                    archive.write(path, arcname=name)
        return buffer.getvalue()


def store():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LogStore()
        return _manager


@contextlib.contextmanager
def scope(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def context():
    return dict(_context.get())


def event(category, name, level='INFO', **fields):
    return store().write(category, name, level, **fields)


def failure(category, name, exc, **fields):
    return event(category, name, 'ERROR', error_type=type(exc).__name__, error=str(exc), traceback=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)), **fields)


def stderr_line(text, **fields):
    # Preserve all emitted diagnostics, redacting before splitting a very long line.
    text = store().clean(text)
    for start in range(0, len(text), 16384):
        event('ffmpeg', 'stderr', text=text[start:start+16384], continuation=start > 0, **fields)
