"""Threaded recorders: baseband IQ (WAV or raw .cs8), demodulated audio, decoded FPV video.
The DSP thread only enqueues; disk I/O happens on the recorder's own thread. WAV files are
split before 4 GiB."""

from __future__ import annotations

import datetime as _dt
import logging
import os
import queue
import subprocess
import threading

import numpy as np

from .wavio import RawIqWriter, WavWriter, iq_filename

log = logging.getLogger(__name__)

SPLIT_BYTES = 4_000_000_000


class Recorder:
    def __init__(self, path: str, sample_rate: float, channels: int, fmt: str,
                 center: float | None = None, split_bytes: int = SPLIT_BYTES):
        self.base_path = path
        self.sample_rate = float(sample_rate)
        self.channels = channels
        self.fmt = fmt
        self.center = center
        self.split_bytes = split_bytes
        self.paths: list[str] = []
        self.frames_written = 0
        self.bytes_written = 0
        self.dropped = 0
        self.error: str | None = None
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._writer: WavWriter | None = None
        self._thread: threading.Thread | None = None
        self.active = False

    @property
    def path(self) -> str:
        return self.paths[-1] if self.paths else self.base_path

    @property
    def elapsed(self) -> float:
        return self.frames_written / self.sample_rate

    def _open_next(self) -> None:
        if self._writer:
            self._writer.close()
        p = self.base_path
        if self.paths:
            root, ext = os.path.splitext(self.base_path)
            p = f"{root}_part{len(self.paths) + 1}{ext}"
        if self.fmt == "cs8":
            self._writer = RawIqWriter(p, "cs8")
        else:
            self._writer = WavWriter(p, self.sample_rate, self.channels, self.fmt, self.center)
        self.paths.append(p)

    def start(self) -> None:
        d = os.path.dirname(self.base_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._open_next()  # raises OSError to the caller if the file can't be created
        self.active = True
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def push(self, data: np.ndarray) -> None:
        if not self.active:
            return
        try:
            self._q.put_nowait(np.array(data, copy=True))
        except queue.Full:
            self.dropped += 1

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=10.0)
        self._thread = None

    def _run(self) -> None:
        last_flush = 0
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                w = self._writer
                frame_bytes = w.bytes_per_frame
                if self.fmt != "cs8" and w.data_bytes + len(item) * frame_bytes > self.split_bytes:
                    self._open_next()
                    w = self._writer
                if self.channels == 2 and not np.iscomplexobj(item) and item.ndim == 1:
                    item = np.repeat(item[:, None], 2, axis=1)
                if np.iscomplexobj(item) or item.ndim == 1:
                    n = len(item)
                else:
                    n = item.shape[0]
                self.bytes_written += w.write_samples(item.reshape(-1) if not np.iscomplexobj(item) else item)
                self.frames_written += n
                if self.bytes_written - last_flush > 8 * 1024 * 1024:
                    w.flush()
                    last_flush = self.bytes_written
        except Exception as e:
            self.error = str(e)
            log.error("recording failed: %s", e)
            self.active = False
        finally:
            if self._writer:
                try:
                    self._writer.close()
                except Exception as e:
                    log.error("closing recording: %s", e)
                self._writer = None


def iq_recorder(directory: str, center: float, rate: float, fmt: str = "cs16") -> Recorder:
    now = _dt.datetime.now(_dt.timezone.utc)
    ext = ".cs8" if fmt == "cs8" else ".wav"   # .cs8: raw 8-bit, same as hackrf_transfer -r
    path = iq_filename(directory, center, rate, now, ext, "IQ")
    return Recorder(path, rate, 2, fmt, center)


def audio_recorder(directory: str, center_vfo: float, rate: float, channels: int = 2) -> Recorder:
    now = _dt.datetime.now(_dt.timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%SZ")
    path = os.path.join(directory, f"specsdr_{stamp}_{int(round(center_vfo))}Hz_AF.wav")
    return Recorder(path, rate, channels, "cs16")


class VideoRecorder:
    """Decoded video frames (RGB24) to MP4 through the ffmpeg bundled with imageio-ffmpeg.
    Frames are queued and encoded on the recorder's own thread; a slow disk drops frames."""

    def __init__(self, path: str, width: int, height: int, fps: float):
        self.path = path
        self.width, self.height, self.fps = int(width), int(height), float(fps)
        self.frames_written = 0
        self.dropped = 0
        self.error: str | None = None
        self.active = False
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import imageio_ffmpeg
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{self.width}x{self.height}", "-r", f"{self.fps:g}", "-i", "-", "-an", "-c:v", "libx264",
               "-preset", "ultrafast", "-pix_fmt", "yuv420p", self.path]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.active = True
        self._thread = threading.Thread(target=self._run, name="fpv-video-rec", daemon=True)
        self._thread.start()

    def push(self, rgb: np.ndarray) -> None:
        if not self.active:
            return
        if rgb.shape[1] != self.width or rgb.shape[0] != self.height:
            return
        try:
            self._q.put_nowait(rgb.tobytes())
        except queue.Full:
            self.dropped += 1

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=15.0)
        self._thread = None
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._proc = None

    def _run(self) -> None:
        proc = self._proc
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                proc.stdin.write(item)
                self.frames_written += 1
        except Exception as e:
            self.error = str(e)
            self.active = False
            log.error("video recording failed: %s", e)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass


def video_filename(directory: str, freq: float, stamp=None) -> str:
    now = stamp or _dt.datetime.now(_dt.timezone.utc)
    return os.path.join(directory, f"specsdr_{now.strftime('%Y%m%d_%H%M%SZ')}_{int(round(freq))}Hz_FPV.mp4")


def snapshot_filename(directory: str, freq: float, stamp=None) -> str:
    now = stamp or _dt.datetime.now(_dt.timezone.utc)
    return os.path.join(directory, f"specsdr_{now.strftime('%Y%m%d_%H%M%SZ')}_{int(round(freq))}Hz_FPV.png")
