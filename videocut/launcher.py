"""Standard-library-only entry point: log before loading media dependencies."""
from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import sys
import uuid

from . import __version__, logs


def main(argv=None):
    with logs.scope(run_id=uuid.uuid4().hex[:12]):
        logs.event('startup', 'startup_started', version=__version__, python=sys.version, executable=sys.executable, platform=platform.platform(), pid=os.getpid())
        try:
            server = importlib.import_module('videocut.server')
            versions = {name: importlib.metadata.version(name) for name in ('numpy', 'opencv-python-headless')}
            from .media import run, tool_path
            tools = {}
            for name in ('ffmpeg', 'ffprobe'):
                path = tool_path(name)
                version = run([path, '-version'], timeout=10).decode('utf-8', errors='replace').splitlines()[0]
                tools[name] = {'path': str(path), 'version': version}
            logs.event('startup', 'environment_ready', dependencies=versions, tools=tools)
            server.main(argv)
            return 0
        except KeyboardInterrupt:
            logs.event('startup', 'startup_interrupted')
            return 0
        except (Exception, SystemExit) as exc:
            if isinstance(exc, SystemExit) and not exc.code:
                return 0
            logs.failure('startup', 'startup_failed', exc)
            print(f'启动失败：{logs.store().clean(str(exc))}\n日志目录：{logs.store().directory}', file=sys.stderr, flush=True)
            return 1
        finally:
            logs.store().close()


if __name__ == '__main__':
    raise SystemExit(main())
