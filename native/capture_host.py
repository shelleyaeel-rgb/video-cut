"""Isolated libobs host. Only stdlib is used by the portable Python runtime.

Wire format (stdout): uint32 LE JSON length, JSON, uint32 LE payload length,
payload. Commands arrive as one JSON object per stdin line. All native media
callbacks only copy a bounded ROI or update counters; IPC runs on another thread.
ABI declarations are pinned to OBS Studio 32.2.2 headers.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import math
import os
from pathlib import Path
import queue
import shutil
import struct
import sys
import tempfile
import threading
import time
import traceback

P = C.c_void_p
S = C.c_char_p
U32 = C.c_uint32
U64 = C.c_uint64
I64 = C.c_int64
B = C.c_bool


class VideoInfo(C.Structure):
    _fields_ = [("graphics_module", S), ("fps_num", U32), ("fps_den", U32),
                ("base_width", U32), ("base_height", U32), ("output_width", U32),
                ("output_height", U32), ("output_format", C.c_int), ("adapter", U32),
                ("gpu_conversion", B), ("colorspace", C.c_int), ("range", C.c_int),
                ("scale_type", C.c_int)]


class AudioInfo(C.Structure):
    _fields_ = [("samples_per_sec", U32), ("speakers", C.c_int)]


class VideoData(C.Structure):
    _fields_ = [("data", P * 8), ("linesize", U32 * 8), ("timestamp", U64)]


class ScaleInfo(C.Structure):
    _fields_ = [("format", C.c_int), ("width", U32), ("height", U32),
                ("range", C.c_int), ("colorspace", C.c_int)]


class Packet(C.Structure):
    _fields_ = [("data", P), ("size", C.c_size_t), ("pts", I64), ("dts", I64),
                ("timebase_num", C.c_int32), ("timebase_den", C.c_int32),
                ("type", C.c_int), ("keyframe", B), ("dts_usec", I64),
                ("sys_dts_usec", I64), ("priority", C.c_int), ("drop_priority", C.c_int),
                ("track_idx", C.c_size_t), ("encoder", P)]


class PacketTime(C.Structure):
    _fields_ = [("pts", I64), ("cts", U64), ("fer", U64), ("ferc", U64), ("pir", U64)]


class Vec2(C.Structure):
    _fields_ = [("x", C.c_float), ("y", C.c_float)]


VIDEO_CB = C.CFUNCTYPE(None, P, C.POINTER(VideoData))
PACKET_CB = C.CFUNCTYPE(None, P, C.POINTER(Packet), C.POINTER(PacketTime), P)
LOG_CB = C.CFUNCTYPE(None, C.c_int, S, P, P)
METER_CB = C.CFUNCTYPE(None, P, C.POINTER(C.c_float), C.POINTER(C.c_float), C.POINTER(C.c_float))
TASK_CB = C.CFUNCTYPE(None, P)
TASK_HANDLER = C.CFUNCTYPE(None, TASK_CB, P, B)


class Wire:
    def __init__(self):
        self.control = queue.Queue()
        self.samples = queue.Queue(maxsize=2)
        self.closed = threading.Event()
        self.dropped = 0
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def send(self, message, payload=b"", sample=False):
        target = self.samples if sample else self.control
        if sample:
            try:
                target.put_nowait((message, payload))
            except queue.Full:
                try:
                    target.get_nowait()
                except queue.Empty:
                    pass
                self.dropped += 1
                try:
                    target.put_nowait((message, payload))
                except queue.Full:
                    self.dropped += 1
        else:
            target.put((message, payload))

    def _write(self):
        try:
            while not self.closed.is_set() or not self.control.empty() or not self.samples.empty():
                try:
                    item = self.control.get_nowait()
                except queue.Empty:
                    try:
                        item = self.samples.get(timeout=0.05)
                    except queue.Empty:
                        continue
                message, payload = item
                header = json.dumps(message, ensure_ascii=False).encode("utf-8")
                sys.stdout.buffer.write(struct.pack("<I", len(header)) + header + struct.pack("<I", len(payload)))
                if payload:
                    sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            self.closed.set()

    def close(self):
        self.closed.set()
        self.thread.join(timeout=3)


def utf8(value):
    return (value.as_posix() if isinstance(value, Path) else str(value)).encode("utf-8")


def decode(value):
    return value.decode("utf-8", "replace") if value else ""


class Host:
    def __init__(self, runtime: Path, wire: Wire):
        self.runtime = runtime.resolve()
        self.wire = wire
        self.bin = self.runtime / "bin" / "64bit"
        self.dll_dirs = [os.add_dll_directory(str(self.bin)),
                         os.add_dll_directory(str(self.runtime / "obs-plugins" / "64bit"))]
        os.chdir(self.bin)
        self.obs = C.CDLL(str(self.bin / "obs.dll"))
        self.refs = []
        self.ui_tasks = queue.Queue()
        self.sources = []
        self.encoders = []
        self.scene = None
        self.output = None
        self.meter = None
        self.origin_ns = None
        self.latest_pts = 0.0
        self.last_packet_ns = 0
        self.packet_count = 0
        self.audio_packets = 0
        self.audio_peak = None
        self.audio_state = "disabled"
        self.compat_update_blocked = False
        self.geometry_revision = 0
        self.capture_geometry = None
        self.raw_installed = False
        self.width, self.height, self.fps = 1920, 1080, 60
        self.stopping = False
        self.first_packet = threading.Event()
        self._bind()
        if decode(self.obs.obs_get_version_string()) != "32.2.2":
            raise RuntimeError("CaptureHost requires the pinned OBS 32.2.2 runtime ABI")
        self._install_log()
        C.windll.ole32.CoInitializeEx(None, 2)
        try:
            C.windll.user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
        except (AttributeError, OSError):
            pass
        self.config_temp = tempfile.TemporaryDirectory(prefix="fenghuo-capture-")
        config = Path(self.config_temp.name)
        # OBS 32.2.2 has no runtime switch for win-capture's online compatibility
        # updater. Its file-updater returns BEFORE creating a network thread
        # when cache/.temp cannot be created. Make the module cache path a file
        # in this private temporary directory: no permissions, binaries, user
        # caches or system firewall settings are changed. Local bundled capture
        # data remains readable. The exact ABI version guard above is required;
        # this is a pinned-source adaptation, not a general OBS configuration API.
        (config / "win-capture").write_text("Optional online compatibility updates are disabled.\n", encoding="utf-8")
        if not self.obs.obs_startup(b"en-US", utf8(config), None):
            raise RuntimeError("libobs initialization failed")
        self.obs.obs_add_data_path(utf8(self.runtime / "data" / "libobs"))
        @TASK_HANDLER
        def ui_task(task, param, wait):
            # Queue OBS UI tasks onto our host thread; no Qt is involved.
            if threading.current_thread() is threading.main_thread():
                task(param)
                return
            done = threading.Event()
            self.ui_tasks.put((task, param, done))
            if wait:
                done.wait(10)
        self.refs.append(ui_task)
        self.obs.obs_set_ui_task_handler(ui_task)
        self.initialized = True

    def fn(self, name, result, *args):
        function = getattr(self.obs, name)
        function.restype, function.argtypes = result, list(args)
        return function

    def _bind(self):
        specs = [
            ("obs_startup", B, S, S, P), ("obs_shutdown", None), ("obs_add_data_path", None, S),
            ("obs_set_ui_task_handler", None, TASK_HANDLER),
            ("obs_reset_video", C.c_int, C.POINTER(VideoInfo)), ("obs_reset_audio", B, C.POINTER(AudioInfo)),
            ("obs_open_module", C.c_int, C.POINTER(P), S, S), ("obs_init_module", B, P),
            ("obs_post_load_modules", None), ("obs_get_version_string", S),
            ("obs_data_create_from_json", P, S), ("obs_data_release", None, P),
            ("obs_get_source_properties", P, S), ("obs_properties_get", P, P, S),
            ("obs_property_list_item_count", C.c_size_t, P),
            ("obs_property_list_item_name", S, P, C.c_size_t),
            ("obs_property_list_item_string", S, P, C.c_size_t),
            ("obs_properties_destroy", None, P),
            ("obs_source_create", P, S, S, P, P), ("obs_source_release", None, P),
            ("obs_source_get_width", U32, P), ("obs_source_get_height", U32, P),
            ("obs_set_output_source", None, U32, P), ("obs_source_set_audio_mixers", None, P, U32),
            ("obs_scene_create", P, S), ("obs_scene_get_source", P, P), ("obs_scene_add", P, P, P),
            ("obs_scene_release", None, P), ("obs_sceneitem_set_bounds_type", None, P, C.c_int),
            ("obs_sceneitem_set_bounds", None, P, C.POINTER(Vec2)),
            ("obs_enum_encoder_types", B, C.c_size_t, C.POINTER(S)),
            ("obs_get_encoder_type", C.c_int, S), ("obs_get_encoder_codec", S, S),
            ("obs_video_encoder_create", P, S, S, P, P),
            ("obs_audio_encoder_create", P, S, S, P, C.c_size_t, P),
            ("obs_encoder_set_video", None, P, P), ("obs_encoder_set_audio", None, P, P),
            ("obs_encoder_release", None, P), ("obs_get_video", P), ("obs_get_audio", P),
            ("obs_output_create", P, S, S, P, P), ("obs_output_set_video_encoder", None, P, P),
            ("obs_output_set_audio_encoder", None, P, P, C.c_size_t),
            ("obs_output_start", B, P), ("obs_output_stop", None, P), ("obs_output_force_stop", None, P),
            ("obs_output_active", B, P), ("obs_output_release", None, P),
            ("obs_output_get_last_error", S, P), ("obs_output_get_total_frames", C.c_int, P),
            ("obs_output_get_frames_dropped", C.c_int, P), ("obs_output_get_total_bytes", U64, P),
            ("obs_get_active_fps", C.c_double), ("obs_get_video_frame_time", U64),
            ("obs_output_add_packet_callback", None, P, PACKET_CB, P),
            ("obs_output_remove_packet_callback", None, P, PACKET_CB, P),
            ("obs_add_raw_video_callback2", None, C.POINTER(ScaleInfo), U32, VIDEO_CB, P),
            ("obs_remove_raw_video_callback", None, VIDEO_CB, P),
            ("obs_volmeter_create", P, C.c_int), ("obs_volmeter_attach_source", B, P, P),
            ("obs_volmeter_add_callback", None, P, METER_CB, P),
            ("obs_volmeter_remove_callback", None, P, METER_CB, P), ("obs_volmeter_destroy", None, P),
            ("os_gettime_ns", U64), ("base_set_log_handler", None, LOG_CB, P),
        ]
        for spec in specs:
            self.fn(*spec)

    def _install_log(self):
        runtime = C.CDLL("msvcrt")
        runtime._vsnprintf.argtypes = [P, C.c_size_t, S, P]
        runtime._vsnprintf.restype = C.c_int

        @LOG_CB
        def log(level, fmt, args, param):
            try:
                buffer = C.create_string_buffer(4096)
                runtime._vsnprintf(buffer, len(buffer) - 1, fmt, args)
                line = decode(buffer.value)
                if "[win-capture plugin] Could not create" in line and ("cache directory" in line or "temp directory" in line):
                    self.compat_update_blocked = True
                if "WASAPI:" in line and "initialized (source: System audio)" in line:
                    self.audio_state = "capturing"
                elif ("Device" in line and "invalidated" in line and "System audio" in line) or "WASAPI: Default output device changed" in line:
                    self.audio_state = "reconnecting"
                    self.wire.send({"type": "warning", "code": "audio_reconnecting", "message": "Audio device changed; reconnecting, with silence during the gap", "elapsed_seconds": self.latest_pts})
                elif "WASAPI:" in line and "failed to start" in line and "System audio" in line:
                    self.audio_state = "unavailable"
                    self.wire.send({"type": "warning", "code": "audio_unavailable", "message": "The selected audio device is unavailable", "elapsed_seconds": self.latest_pts})
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
            except Exception:
                pass
        self.refs.append(log)
        self.obs.base_set_log_handler(log, None)

    def initialize(self, width=1920, height=1080, fps=60):
        self.width, self.height, self.fps = int(width), int(height), int(fps)
        info = VideoInfo(b"libobs-d3d11", self.fps, 1, self.width, self.height,
                         self.width, self.height, 2, 0, True, 2, 1, 2)
        result = self.obs.obs_reset_video(C.byref(info))
        if result != 0:
            raise RuntimeError(f"libobs D3D11 initialization failed ({result})")
        if not self.obs.obs_reset_audio(C.byref(AudioInfo(48000, 2))):
            raise RuntimeError("libobs audio initialization failed")
        for name in ("win-capture", "win-wasapi", "obs-ffmpeg", "obs-x264", "obs-nvenc", "obs-qsv11", "obs-amf"):
            binary = self.runtime / "obs-plugins" / "64bit" / (name + ".dll")
            if not binary.exists():
                continue
            module = P()
            code = self.obs.obs_open_module(C.byref(module), utf8(binary), utf8(self.runtime / "data" / "obs-plugins" / name))
            if code == 0:
                self.obs.obs_init_module(module)
                if name == "win-capture" and not self.compat_update_blocked:
                    raise RuntimeError("The pinned win-capture updater disable guard was not confirmed; refusing to continue")
        self.obs.obs_post_load_modules()

    def data(self, values):
        ptr = self.obs.obs_data_create_from_json(json.dumps(values).encode())
        if not ptr:
            raise RuntimeError("Invalid native settings")
        return ptr

    def property_list(self, source_id, key):
        props = self.obs.obs_get_source_properties(utf8(source_id))
        if not props:
            return []
        result = []
        try:
            prop = self.obs.obs_properties_get(props, utf8(key))
            if prop:
                for index in range(self.obs.obs_property_list_item_count(prop)):
                    value = decode(self.obs.obs_property_list_item_string(prop, index))
                    name = decode(self.obs.obs_property_list_item_name(prop, index))
                    if value:
                        result.append({"id": value, "name": name})
        finally:
            self.obs.obs_properties_destroy(props)
        return result

    def list_sources(self):
        sources = []
        for kind, source_id, key in (("window", "window_capture", "window"),
                                    ("monitor", "monitor_capture", "monitor_id")):
            for item in self.property_list(source_id, key):
                item.update(kind=kind, width=0, height=0)
                sources.append(item)
        return sources

    def encoder_ids(self):
        ids = []
        index = 0
        value = S()
        while self.obs.obs_enum_encoder_types(index, C.byref(value)):
            ids.append(decode(value.value))
            index += 1
        return ids

    def source(self, source_id, name, settings):
        data = self.data(settings)
        try:
            result = self.obs.obs_source_create(utf8(source_id), utf8(name), data, None)
        finally:
            self.obs.obs_data_release(data)
        if not result:
            raise RuntimeError(f"Failed to create capture source: {source_id}")
        self.sources.append(result)
        return result

    def _setup_sources(self, options):
        source = options["source"]
        if source["kind"] == "window":
            settings = {"window": source["id"], "method": 2, "priority": 0,
                        "cursor": False, "client_area": True, "force_sdr": True}
            video = self.source("window_capture", "Game window", settings)
        else:
            video = self.source("monitor_capture", "Selected display",
                                {"monitor_id": source["id"], "method": 2, "capture_cursor": False, "force_sdr": True})
        self.video_source = video
        self.scene = self.obs.obs_scene_create(b"Recording")
        item = self.obs.obs_scene_add(self.scene, video)
        self.obs.obs_sceneitem_set_bounds_type(item, 2)
        self.obs.obs_sceneitem_set_bounds(item, C.byref(Vec2(self.width, self.height)))
        self.obs.obs_set_output_source(0, self.obs.obs_scene_get_source(self.scene))
        if options.get("audio", True):
            self.audio_state = "starting"
            audio = self.source("wasapi_output_capture", "System audio",
                                {"device_id": options.get("audio_device", "default"), "use_device_timing": True})
            self.obs.obs_source_set_audio_mixers(audio, 1)
            self.obs.obs_set_output_source(1, audio)

            @METER_CB
            def meter(param, magnitude, peak, input_peak):
                self.audio_peak = max(float(peak[0]), float(peak[1]))
            self.refs.append(meter)
            self.meter_callback = meter
            self.meter = self.obs.obs_volmeter_create(2)
            self.obs.obs_volmeter_add_callback(self.meter, meter, None)
            self.obs.obs_volmeter_attach_source(self.meter, audio)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.pump_messages()
            if self.obs.obs_source_get_width(video) > 0 and self.obs.obs_source_get_height(video) > 0:
                if options.get("audio", True) and self.audio_state != "capturing":
                    time.sleep(0.05)
                    continue
                self.capture_geometry = (self.obs.obs_source_get_width(video), self.obs.obs_source_get_height(video))
                time.sleep(0.2)
                return
            time.sleep(0.05)
        if options.get("audio", True) and self.audio_state != "capturing":
            raise RuntimeError("The selected WASAPI audio device did not initialize; choose an available output device")
        raise RuntimeError("Capture target did not produce a frame; keep the selected window visible and unminimized")

    def pump_messages(self):
        while not self.ui_tasks.empty():
            try:
                task, param, done = self.ui_tasks.get_nowait()
                task(param)
                done.set()
            except queue.Empty:
                break
        from ctypes import wintypes
        message = wintypes.MSG()
        while C.windll.user32.PeekMessageW(C.byref(message), None, 0, 0, 1):
            C.windll.user32.TranslateMessage(C.byref(message))
            C.windll.user32.DispatchMessageW(C.byref(message))

    def _video_encoder(self, encoder_id, options):
        settings = {"rate_control": "CBR", "bitrate": options.get("bitrate_kbps", 16000),
                    "keyint_sec": 2, "bf": 0, "bframes": 0, "profile": "high",
                    "preset": "veryfast" if encoder_id == "obs_x264" else "p5",
                    "x264opts": "bframes=0", "psycho_aq": False, "lookahead": False}
        if encoder_id == "obs_x264":
            settings["tune"] = "zerolatency"
        data = self.data(settings)
        try:
            encoder = self.obs.obs_video_encoder_create(utf8(encoder_id), b"Video", data, None)
        finally:
            self.obs.obs_data_release(data)
        if encoder:
            self.obs.obs_encoder_set_video(encoder, self.obs.obs_get_video())
        return encoder

    def start(self, options, output_path):
        self.output_path = str(Path(output_path).resolve())
        self._setup_sources(options)
        ids = self.encoder_ids()
        requested = options.get("encoder", "auto")
        candidates = [x for x in ("obs_nvenc_h264_tex", "obs_nvenc_h264", "jim_nvenc", "obs_qsv11_v2", "h264_texture_amf", "h264_fallback_amf", "obs_x264") if x in ids]
        if requested != "auto":
            candidates = [requested] if requested in ids else []
        if not candidates:
            raise RuntimeError("No compatible H.264 encoder is available")
        # ffmpeg_muxer is an AV output; disabling source capture leaves a silent
        # AAC track, it never enables loopback or microphone capture implicitly.
        audio_id = next((x for x in ("ffmpeg_aac", "CoreAudio_AAC", "mf_aac") if x in ids), None)
        if not audio_id:
            raise RuntimeError("No AAC encoder is available")
        data = self.data({"bitrate": 192})
        try:
            self.audio_encoder = self.obs.obs_audio_encoder_create(utf8(audio_id), b"Audio", data, 0, None)
        finally:
            self.obs.obs_data_release(data)
        if not self.audio_encoder:
            raise RuntimeError("AAC encoder initialization failed")
        self.encoders.append(self.audio_encoder)
        self.obs.obs_encoder_set_audio(self.audio_encoder, self.obs.obs_get_audio())

        @PACKET_CB
        def packet(output, pointer, timing, param):
            value = pointer.contents
            if value.type == 1 and value.timebase_den:
                pts = value.pts * value.timebase_num / value.timebase_den
                if self.origin_ns is None:
                    if timing and timing.contents.cts:
                        self.origin_ns = int(timing.contents.cts - round(pts * 1e9))
                    else:
                        self.origin_ns = int(value.sys_dts_usec * 1000 - value.dts * value.timebase_num / value.timebase_den * 1e9)
                    self.first_packet.set()
                self.latest_pts = max(self.latest_pts, pts)
                self.last_packet_ns = self.obs.os_gettime_ns()
                self.packet_count += 1
            elif value.type == 0:
                self.audio_packets += 1
        self.packet_callback = packet
        self.refs.append(packet)
        errors = []
        for candidate in candidates:
            encoder = self._video_encoder(candidate, options)
            if not encoder:
                errors.append(candidate + ": create failed")
                continue
            settings = self.data({"path": self.output_path, "muxer_settings": "", "split_file": False})
            self.output = self.obs.obs_output_create(b"ffmpeg_muxer", b"Recording file", settings, None)
            self.obs.obs_data_release(settings)
            if not self.output:
                self.obs.obs_encoder_release(encoder)
                raise RuntimeError("FFmpeg MKV output module is unavailable")
            self.obs.obs_output_set_video_encoder(self.output, encoder)
            if self.audio_encoder:
                self.obs.obs_output_set_audio_encoder(self.output, self.audio_encoder, 0)
            self.obs.obs_output_add_packet_callback(self.output, packet, None)
            if self.obs.obs_output_start(self.output):
                self.encoders.append(encoder)
                self.encoder_id = candidate
                break
            errors.append(candidate + ": " + decode(self.obs.obs_output_get_last_error(self.output)))
            self.obs.obs_output_remove_packet_callback(self.output, packet, None)
            self.obs.obs_output_release(self.output)
            self.output = None
            self.obs.obs_encoder_release(encoder)
        else:
            raise RuntimeError("All video encoders failed: " + "; ".join(errors))

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.first_packet.is_set():
            self.pump_messages()
            time.sleep(0.02)
        if not self.first_packet.is_set() or not self.obs.obs_output_active(self.output):
            raise RuntimeError("Recorder did not acknowledge its first encoded video packet: " + decode(self.obs.obs_output_get_last_error(self.output)))
        if self.encoder_id == "obs_x264":
            errors.append("Using CPU H.264 encoding; lower recording frame rate if the game slows down")
        self._samples(options)
        self.wire.send({**self.stats(), "type": "started", "encoder": self.encoder_id,
                        "output_path": self.output_path, "clock_origin_ns": self.origin_ns,
                        "recording_pts_origin_ns": self.origin_ns, "video_timebase": [1, self.fps],
                        "width": self.width, "height": self.height, "fps": self.fps,
                        "audio": options.get("audio", True), "warnings": errors})

    def _samples(self, options):
        x0, y0 = math.floor(.35 * self.width), math.floor(.59 * self.height)
        x1, y1 = math.ceil(.66 * self.width), math.ceil(.78 * self.height)
        width, height = x1 - x0, y1 - y0
        self.sample_count = 0

        @VIDEO_CB
        def sample(param, pointer):
            if self.stopping or self.origin_ns is None:
                return
            try:
                frame = pointer.contents
                pts = (frame.timestamp - self.origin_ns) / 1e9
                if pts < 0 or not frame.data[0]:
                    return
                buffer = C.create_string_buffer(width * height * 4)
                for row in range(height):
                    C.memmove(C.addressof(buffer) + row * width * 4,
                              frame.data[0] + (y0 + row) * frame.linesize[0] + x0 * 4, width * 4)
                self.sample_count += 1
                metadata = {"source_width": self.width, "source_height": self.height,
                            "roi": [x0, y0, x1, y1], "geometry_revision": self.geometry_revision,
                            "capture_width": self.capture_geometry[0] if self.capture_geometry else 0,
                            "capture_height": self.capture_geometry[1] if self.capture_geometry else 0,
                            "capture_timestamp_ns": int(frame.timestamp),
                            "recording_pts_origin_ns": self.origin_ns, "pixel_format": "BGRA",
                            "width": width, "height": height, "frame_id": self.sample_count}
                self.wire.send({"type": "sample", "pts_seconds": pts, "metadata": metadata}, buffer.raw, sample=True)
            except Exception as exc:
                self.wire.send({"type": "warning", "message": "Analysis sample dropped: " + str(exc)})
        self.sample_callback = sample
        self.refs.append(sample)
        scale = ScaleInfo(7, self.width, self.height, 2, 2)
        divisor = max(1, round(self.fps / max(1, min(30, options.get("sample_fps", 15)))))
        self.obs.obs_add_raw_video_callback2(C.byref(scale), divisor, sample, None)
        self.raw_installed = True

    def stats(self):
        peak = self.audio_peak
        if hasattr(self, "video_source"):
            geometry = (self.obs.obs_source_get_width(self.video_source), self.obs.obs_source_get_height(self.video_source))
            if geometry != self.capture_geometry:
                self.geometry_revision += 1
                self.capture_geometry = geometry
        return {"type": "stats", "elapsed_seconds": self.latest_pts,
                "compatibility_updates_disabled": self.compat_update_blocked,
                "duration_seconds": self.latest_pts + 1 / self.fps if self.packet_count else 0,
                "frames": self.packet_count, "audio_packets": self.audio_packets,
                "dropped_frames": self.obs.obs_output_get_frames_dropped(self.output) if self.output else 0,
                "analysis_dropped_frames": self.wire.dropped,
                "audio_peak": peak if peak is not None and math.isfinite(peak) else None,
                "audio_state": self.audio_state, "geometry_revision": self.geometry_revision,
                "capture_width": self.capture_geometry[0] if self.capture_geometry else 0,
                "capture_height": self.capture_geometry[1] if self.capture_geometry else 0,
                "bytes": self.obs.obs_output_get_total_bytes(self.output) if self.output else 0,
                "active_fps": self.obs.obs_get_active_fps(), "clock_origin_ns": self.origin_ns,
                "recording_pts_origin_ns": self.origin_ns}

    def stop(self):
        self.stopping = True
        if self.raw_installed:
            self.obs.obs_remove_raw_video_callback(self.sample_callback, None)
            self.raw_installed = False
        graceful = True
        if self.output and self.obs.obs_output_active(self.output):
            self.obs.obs_output_stop(self.output)
            deadline = time.monotonic() + 12
            while self.obs.obs_output_active(self.output) and time.monotonic() < deadline:
                self.pump_messages()
                time.sleep(0.03)
            if self.obs.obs_output_active(self.output):
                graceful = False
                self.obs.obs_output_force_stop(self.output)
                deadline = time.monotonic() + 3
                while self.obs.obs_output_active(self.output) and time.monotonic() < deadline:
                    time.sleep(0.03)
        result = {**self.stats(), "type": "stopped", "stopped": True, "graceful": graceful,
                  "output_path": getattr(self, "output_path", None)}
        # The packet hook precedes muxer forwarding. A last packet can be seen
        # there but discarded at the stop timestamp, so use accepted frame count.
        if self.output:
            frames = self.obs.obs_output_get_total_frames(self.output)
            result.update(frames=frames, duration_seconds=frames / self.fps,
                          elapsed_seconds=max(0, frames - 1) / self.fps)
        self.wire.send(result)
        return result

    def close(self):
        if self.output:
            self.obs.obs_output_remove_packet_callback(self.output, self.packet_callback, None)
            self.obs.obs_output_release(self.output)
            self.output = None
        if self.meter:
            self.obs.obs_volmeter_remove_callback(self.meter, self.meter_callback, None)
            self.obs.obs_volmeter_destroy(self.meter)
        for encoder in self.encoders:
            self.obs.obs_encoder_release(encoder)
        self.obs.obs_set_output_source(0, None)
        self.obs.obs_set_output_source(1, None)
        if self.scene:
            self.obs.obs_scene_release(self.scene)
        for source in self.sources:
            self.obs.obs_source_release(source)
        if getattr(self, "initialized", False):
            self.obs.obs_shutdown()
        C.windll.ole32.CoUninitialize()
        self.config_temp.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--list", action="store_true")
    arguments = parser.parse_args()
    wire, host = Wire(), None
    commands = queue.Queue()

    def read_commands():
        try:
            for line in sys.stdin:
                commands.put(json.loads(line))
        except Exception:
            pass
        commands.put({"command": "stop", "reason": "parent-disconnected"})
    threading.Thread(target=read_commands, daemon=True).start()
    try:
        host = Host(arguments.runtime, wire)
        if arguments.list:
            host.initialize(640, 360, 30)
            wire.send({"type": "sources", "sources": host.list_sources(),
                       "audio_devices": host.property_list("wasapi_output_capture", "device_id"),
                       "encoders": host.encoder_ids(), "version": decode(host.obs.obs_get_version_string())})
            return
        command = commands.get(timeout=20)
        if command.get("command") != "start":
            return
        options = command["options"]
        host.initialize(options["width"], options["height"], options["fps"])
        host.start(options, command["output_path"])
        next_status = 0
        while not wire.closed.is_set():
            host.pump_messages()
            try:
                message = commands.get(timeout=0.03)
                if message.get("command") == "stop":
                    break
            except queue.Empty:
                pass
            now = time.monotonic()
            if now >= next_status:
                wire.send(host.stats())
                next_status = now + .25
                if not host.obs.obs_output_active(host.output):
                    raise RuntimeError("Recording output stopped: " + decode(host.obs.obs_output_get_last_error(host.output)))
                if shutil.disk_usage(Path(host.output_path).parent).free < 128 * 1024 * 1024:
                    raise RuntimeError("Disk space is low; stopping before the disk fills")
                if host.last_packet_ns and host.obs.os_gettime_ns() - host.last_packet_ns > 10_000_000_000:
                    raise RuntimeError("The video encoder has not produced a packet for 10 seconds")
        host.stop()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        wire.send({"type": "error", "message": str(exc)})
        if host and host.output:
            host.stop()
    finally:
        if host:
            host.close()
        wire.close()


if __name__ == "__main__":
    main()
