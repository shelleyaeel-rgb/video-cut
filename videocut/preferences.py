"""Small, validated user defaults; media paths and session state are excluded."""
import json
import math
from pathlib import Path
import threading
import uuid

from . import logs
from .paths import DATA_ROOT

RANGES = {'target': (5, 120), 'strength': (0, 1), 'game-volume': (0, 2),
          'music-volume': (0, 2), 'threshold': (.5, .99)}
CHOICES = {'encoder': ('cpu', 'nvenc'), 'voice-mode': ('keep', 'game_track', 'reduce'),
           'music-voice-mode': ('keep', 'instrumental')}


def validate(values):
    if not isinstance(values, dict) or set(values) - (RANGES.keys() | CHOICES.keys()):
        raise ValueError('保存的设置包含不支持的字段。')
    result = {}
    for name, value in values.items():
        if name in RANGES:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError('设置必须是有效数字。') from None
            low, high = RANGES[name]
            if isinstance(value, bool) or not math.isfinite(number) or not low <= number <= high:
                raise ValueError('设置数值超出范围。')
            result[name] = number
        elif value in CHOICES[name]:
            result[name] = value
        else:
            raise ValueError('设置选项无效。')
    return result


class Preferences:
    def __init__(self, path=None):
        self.path = Path(path) if path else DATA_ROOT / 'data' / 'preferences.json'
        self.lock = threading.RLock()
        self.values = {}
        try:
            if self.path.is_file():
                self.values = validate(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            logs.event('runtime', 'preferences_ignored', 'WARNING', path=str(self.path))

    def get(self):
        with self.lock:
            return dict(self.values)

    def save(self, values):
        changes = validate(values)
        with self.lock:
            merged = {**self.values, **changes}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f'.preferences-{uuid.uuid4().hex}.tmp')
            try:
                temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(self.path)
                self.values = merged
            finally:
                temporary.unlink(missing_ok=True)
            return dict(merged)
