"""Loud, repeating local alarm that runs until acknowledged."""

from __future__ import annotations

import logging
import math
import struct
import subprocess
import threading
import wave
from pathlib import Path
from typing import Callable

from . import audio

log = logging.getLogger(__name__)


def make_siren(path: Path, seconds: float = 2.0, rate: int = 44100) -> Path:
    """Two-tone sweeping siren, near full scale, harsh harmonics so it cuts through noise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    phase = 0.0
    n = int(seconds * rate)
    for i in range(n):
        t = i / rate
        freq = 650 + 750 * (0.5 - 0.5 * math.cos(2 * math.pi * t / seconds))
        phase += 2 * math.pi * freq / rate
        s = math.sin(phase)
        s = max(-1.0, min(1.0, 1.8 * s))  # soft clip -> square-ish, louder perceived
        frames += struct.pack("<h", int(s * 32000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def _osascript(*lines: str, args: tuple = (), timeout: float = 10) -> str:
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    r = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


class Alarm:
    def __init__(self, sound_dir: Path, settings: Callable[[], dict], on_dialog_ack: Callable[[], None]):
        self._settings = settings
        self._on_dialog_ack = on_dialog_ack
        self._default_sound = sound_dir / "siren.wav"
        self._lock = threading.Lock()
        self._keys: dict[str, str] = {}  # key -> description
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._player: subprocess.Popen | None = None
        self._dialog: subprocess.Popen | None = None
        self._saved_volume: tuple[str, str] | None = None
        self._saved_output: int | None = None

    @property
    def active(self) -> dict[str, str]:
        with self._lock:
            return dict(self._keys)

    def sound_file(self) -> str:
        custom = self._settings().get("alarm_sound") or ""
        if custom and Path(custom).exists():
            return custom
        if not self._default_sound.exists():
            make_siren(self._default_sound)
        return str(self._default_sound)

    def start(self, key: str, title: str, message: str):
        with self._lock:
            self._keys[key] = f"{title}: {message}"
            old, stopping = self._thread, self._stop.is_set()
        if old and old.is_alive() and stopping:
            old.join(timeout=5)  # previous alarm is winding down; let it finish restoring volume
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="alarm", daemon=True)
                self._thread.start()
        if self._settings().get("mac_dialog", True):
            self._show_dialog(title, message)

    def stop(self, key: str | None = None) -> list[str]:
        """Stop one alarm key, or all. Returns the keys that were silenced."""
        with self._lock:
            keys = list(self._keys) if key is None else [k for k in (key,) if k in self._keys]
            for k in keys:
                self._keys.pop(k, None)
            if self._keys:
                return keys
            self._stop.set()
            player, dialog = self._player, self._dialog
            self._dialog = None
        for proc in (player, dialog):
            if proc and proc.poll() is None:
                proc.terminate()
        return keys

    def _run(self):
        self._route_output()
        self._boost_volume()
        try:
            while not self._stop.is_set():
                try:
                    self._player = subprocess.Popen(["afplay", "-v", "1", self.sound_file()],
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._player.wait()
                except OSError as e:
                    log.error("afplay failed: %s; falling back to system beep", e)
                    _osascript("beep 3")
                    self._stop.wait(1)
        finally:
            self._restore_volume()
            self._restore_output()

    def _route_output(self):
        """Play on the built-in speakers (configurable): a sleeping external monitor is silent."""
        choice = self._settings().get("alarm_output", "builtin")
        try:
            target = audio.resolve(choice)
            current = audio.default_output()
            if target is None:
                if choice.lower() != "default":
                    log.warning("alarm output '%s' not found; using system default output", choice)
                return
            if current != target["id"] and audio.set_default_output(target["id"]):
                self._saved_output = current
                log.info("alarm output switched to %s", target["name"])
        except OSError as e:
            log.warning("could not select alarm output: %s", e)

    def _restore_output(self):
        if self._saved_output is None:
            return
        prev, self._saved_output = self._saved_output, None
        try:
            audio.set_default_output(prev)
            log.info("audio output restored")
        except OSError as e:
            log.warning("could not restore audio output: %s", e)

    def _boost_volume(self):
        target = int(self._settings().get("alarm_volume", 100) or 0)
        if target <= 0:
            return
        try:
            vol = _osascript("output volume of (get volume settings)")
            muted = _osascript("output muted of (get volume settings)")
            if vol in ("", "missing value"):
                log.info("output device has no software volume control; playing at device volume")
                return
            self._saved_volume = (vol, muted)
            _osascript(f"set volume output volume {min(100, target)} without output muted")
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("could not raise volume: %s", e)

    def _restore_volume(self):
        if not self._saved_volume:
            return
        vol, muted = self._saved_volume
        self._saved_volume = None
        try:
            _osascript(f"set volume output volume {int(vol)}" + (" with output muted" if muted == "true" else ""))
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            log.warning("could not restore volume: %s", e)

    def _show_dialog(self, title: str, message: str):
        with self._lock:
            if self._dialog and self._dialog.poll() is None:
                return  # one dialog is enough; it acknowledges everything
            try:
                self._dialog = subprocess.Popen(
                    ["osascript", "-e", "on run argv",
                     "-e", 'display alert (item 1 of argv) message (item 2 of argv) as critical '
                           'buttons {"Acknowledge"} default button 1',
                     "-e", "end run", title, message],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as e:
                log.warning("dialog failed: %s", e)
                return
            proc = self._dialog

        def wait():
            out, _ = proc.communicate()
            if proc.returncode == 0 and "Acknowledge" in (out or ""):
                self._on_dialog_ack()

        threading.Thread(target=wait, name="alarm-dialog", daemon=True).start()
