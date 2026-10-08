"""Immutable app resources and persistent user files live in separate places."""
from __future__ import annotations

import os
from pathlib import Path
import sys

FROZEN = bool(getattr(sys, 'frozen', False))
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])).resolve()
INSTALL_ROOT = Path(sys.executable).resolve().parent if FROZEN else RESOURCE_ROOT


def data_root():
    override = os.environ.get('VIDEOCUT_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if FROZEN:
        local = os.environ.get('LOCALAPPDATA')
        return (Path(local) if local else Path.home() / 'AppData' / 'Local') / 'FenghuoClip'
    return RESOURCE_ROOT


DATA_ROOT = data_root()


def worker_command(name, *args):
    if name not in ('picker', 'separator'):
        raise ValueError('Unknown local worker')
    prefix = [sys.executable, '--worker', name] if FROZEN else [sys.executable, '-m', f'videocut.{name}']
    return [*prefix, *map(str, args)]
