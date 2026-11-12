"""Kill only our own synthetic-window recorder and verify recoverable media."""
from pathlib import Path
import json
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from videocut.capture_backend import CaptureBackend, list_sources


def main():
    root = Path(__file__).resolve().parents[1]
    source = next((x for x in list_sources() if x["kind"] == "window" and "FenghuoClip Capture Validation" in x["name"]), None)
    if not source:
        raise RuntimeError("Start the synthetic capture validation window first")
    path = root / "work" / f"capture-interrupted-{time.time_ns()}.mkv"
    backend = CaptureBackend()
    samples = []

    def slow_detector(frame, pts, metadata):
        samples.append(pts)
        time.sleep(.4)

    backend.start({"source": source, "width": 1280, "height": 720, "fps": 30, "audio": False},
                  path, on_sample=slow_detector)
    time.sleep(6)
    before = backend.snapshot()
    # The muxer is a private child of the recorder started immediately above.
    # Never enumerate or terminate another application's recording process.
    command = f"Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {backend.process.pid} -and $_.Name -eq 'obs-ffmpeg-mux.exe' }} | Select-Object -ExpandProperty ProcessId"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True,
                            check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    children = [int(x) for x in result.stdout.decode().split() if x.isdigit()]
    backend.process.kill()
    for pid in children:
        subprocess.run(["taskkill.exe", "/PID", str(pid), "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    backend.process.wait(timeout=5)
    backend._ended.wait(5)
    stopped = backend.stop(timeout=2)
    ffmpeg = next((root / ".tools" / "ffmpeg").glob("*/bin/ffmpeg.exe"))
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    recovered = path.with_name(path.stem + "-recovered.mkv")
    remux = subprocess.run([str(ffmpeg), "-v", "error", "-fflags", "+discardcorrupt", "-i", str(path),
                            "-map", "0", "-c", "copy", str(recovered)], capture_output=True)
    probe = subprocess.run([str(ffprobe), "-v", "error", "-show_entries", "format=duration:stream=codec_name",
                            "-of", "json", str(recovered)], capture_output=True, check=True)
    media = json.loads(probe.stdout)
    result = {"before_crash": before, "stop_result": stopped, "slow_callback_samples": len(samples),
              "original_path": str(path), "recovered_path": str(recovered), "remux_exit_code": remux.returncode,
              "media": media}
    path.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    path.with_suffix(".log").write_text(backend.diagnostics(), encoding="utf-8")
    print(json.dumps(result), flush=True)
    assert not stopped["graceful"] and stopped.get("error"), "A killed host must never report a clean stop"
    assert remux.returncode == 0 and float(media["format"]["duration"]) > 4
    assert before["dropped_frames"] == 0 and before["callback_dropped_frames"] > 0


if __name__ == "__main__":
    main()
