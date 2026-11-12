"""Record only our generated test window; never falls back to a display."""
from pathlib import Path
import argparse
import json
import sys
import time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from videocut.capture_backend import CaptureBackend, list_sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--encoder", default="auto")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    source = next((item for item in list_sources() if item["kind"] == "window" and "FenghuoClip Capture Validation" in item["name"]), None)
    if not source:
        raise SystemExit("Start native/test_capture_window.py first; no private windows or displays will be captured")
    output = Path(args.output or f"work/capture-validation-{time.time_ns()}.mkv").resolve()
    backend, samples, anchor = CaptureBackend(), [], {}

    def sample(frame, pts, metadata):
        samples.append({"pts": pts, "mean": float(frame.mean()), "std": float(frame.std()), "shape": list(frame.shape), "metadata": metadata})
        if len(samples) == 30:
            anchor.update(frame=frame.copy(), pts=pts, roi=metadata["roi"])

    try:
        ack = backend.start({"source": source, "width": args.width, "height": args.height,
                             "fps": 30, "audio": args.audio, "encoder": args.encoder}, output,
                            on_sample=sample)
        print("START " + json.dumps(ack), flush=True)
        time.sleep(args.seconds)
        stop = backend.stop()
        print("STOP " + json.dumps(stop), flush=True)
        result = {"start": ack, "stop": stop, "samples": len(samples),
                  "first_sample": samples[0] if samples else None, "last_sample": samples[-1] if samples else None}
        output.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        output.with_suffix(".log").write_text(backend.diagnostics(), encoding="utf-8")
        if anchor:
            np.savez_compressed(output.with_suffix(".sample.npz"), **anchor)
        print("RESULT " + json.dumps(result), flush=True)
        if not samples or not stop.get("graceful") or stop.get("error"):
            raise RuntimeError("Capture validation did not complete cleanly")
    finally:
        backend.stop(timeout=5)
        if backend.diagnostics():
            output.with_suffix(".log").write_text(backend.diagnostics(), encoding="utf-8")


if __name__ == "__main__":
    main()
