"""Install optional pinned CPU voice separation without touching system Python."""
import argparse
import json
import shutil
import sys
from pathlib import Path

from download_tools import DOWNLOADS, ROOT, download

sys.path.insert(0,str(ROOT))
from videocut.audio_model import MODEL_BYTES, MODEL_NAME, MODEL_PATH, MODEL_SHA256, MODEL_URL, require_model


def main():
    parser=argparse.ArgumentParser()
    parser.parse_args()
    if Path(sys.prefix).resolve() != (ROOT/'.venv').resolve():
        raise RuntimeError('Run with the project .venv Python, not a system interpreter.')
    DOWNLOADS.mkdir(parents=True,exist_ok=True)
    MODEL_PATH.parent.mkdir(parents=True,exist_ok=True)
    cached=DOWNLOADS/MODEL_NAME
    print('Checking pinned UVR model (64 MiB)...',flush=True)
    download(MODEL_URL,cached,MODEL_SHA256)
    partial=MODEL_PATH.with_suffix('.part')
    shutil.copyfile(cached,partial)
    partial.replace(MODEL_PATH)
    require_model(verify=True)
    import cv2
    import numpy as np
    cv2.setNumThreads(2)
    network=cv2.dnn.readNetFromONNX(str(MODEL_PATH))
    network.setInput(np.zeros((1,4,3072,256),dtype=np.float32))
    output=network.forward()
    if output.shape != (1,4,3072,256) or not np.isfinite(output).all():
        raise RuntimeError('Model inference check failed')
    manifest={'model':MODEL_NAME,'url':MODEL_URL,'sha256':MODEL_SHA256,'bytes':MODEL_BYTES,'opencv':cv2.__version__,'device':'CPU','output_shape':list(output.shape)}
    MODEL_PATH.with_suffix('.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest),flush=True)
    print('Local AI audio is ready. Restart VideoCut to refresh the UI status.',flush=True)


if __name__=='__main__':
    main()
