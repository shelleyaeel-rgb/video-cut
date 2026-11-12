"""Synthetic, non-private window for explicit capture integration tests."""
import argparse
import ctypes
import math
import struct
import time
import tkinter as tk
import wave
from pathlib import Path

TITLE = "FenghuoClip Capture Validation"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--tone", action="store_true")
    args = parser.parse_args()
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("1280x720+20+20")
    canvas = tk.Canvas(root, width=1280, height=720, background="#172538", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(640, 120, fill="white", font=("Arial", 30), text="Synthetic recording validation")
    canvas.create_text(640, 175, fill="#62e7bf", font=("Arial", 18), text="This window contains generated test content only")
    clock = canvas.create_text(640, 260, fill="white", font=("Consolas", 28), text="0.00")
    event = canvas.create_text(640, 495, fill="white", font=("Microsoft YaHei", 28), text="")
    block = canvas.create_rectangle(100, 350, 160, 410, fill="#fcaf51", outline="")
    canvas.create_rectangle(448, 424, 845, 562, outline="#526d86", width=2)
    start = time.perf_counter()
    if args.tone:
        import winsound
        path = Path(__file__).resolve().parents[1] / "work" / "capture-test-tone.wav"
        path.parent.mkdir(exist_ok=True)
        with wave.open(str(path), "wb") as sound:
            sound.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            samples = []
            for index in range(48000):
                value = int(1500 * math.sin(2 * math.pi * 440 * index / 48000)) if index < 4800 else 0
                samples.append(struct.pack("<hh", value, value))
            sound.writeframes(b"".join(samples))
        winsound.PlaySound(str(path), winsound.SND_ASYNC | winsound.SND_LOOP)

    def draw():
        elapsed = time.perf_counter() - start
        canvas.itemconfigure(clock, text=f"FRAME TIME {elapsed:08.3f}")
        x = 100 + (elapsed * 170) % 1000
        canvas.coords(block, x, 350, x + 60, 410)
        canvas.itemconfigure(event, text="击败 验证目标 +100" if int(elapsed) % 8 in (3, 4) else "")
        if elapsed >= args.seconds:
            root.destroy()
        else:
            root.after(33, draw)
    root.after(0, draw)
    try:
        root.mainloop()
    finally:
        if args.tone:
            import winsound
            winsound.PlaySound(None, 0)


if __name__ == "__main__":
    main()
