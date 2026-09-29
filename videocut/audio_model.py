"""Pinned optional model; media processing never downloads files."""
import hashlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = 'Kim_Vocal_2.onnx'
MODEL_URL = 'https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/' + MODEL_NAME
MODEL_SHA256 = 'ce74ef3b6a6024ce44211a07be9cf8bc6d87728cc852a68ab34eb8e58cde9c8b'
MODEL_BYTES = 66759214
MODEL_PATH = ROOT / '.tools' / 'audio' / MODEL_NAME
SAMPLE_RATE = 44100
N_FFT, HOP, DIM_F, DIM_T = 7680, 1024, 3072, 256
COMPENSATE = 1.009
ENGINE_VERSION = 'mdx-opencv-cpu-1'


def status():
    runtime = importlib.util.find_spec('cv2') is not None
    model = MODEL_PATH.is_file() and MODEL_PATH.stat().st_size == MODEL_BYTES
    return {'ready': runtime and model, 'runtime_installed': runtime, 'model_installed': model, 'model': MODEL_NAME, 'device': 'CPU', 'setup_command': r'.\.venv\Scripts\python.exe scripts\setup_audio.py'}


def require_model(verify=False):
    if not status()['ready']:
        raise ValueError('AI 人声处理组件尚未安装。请先运行 .venv\\Scripts\\python.exe scripts\\setup_audio.py，或选择保留原声／独立音轨。')
    if verify:
        with MODEL_PATH.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if digest != MODEL_SHA256:
            raise RuntimeError('人声模型校验失败，请重新运行 scripts/setup_audio.py。')
    return MODEL_PATH
