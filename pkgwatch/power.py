"""Keep the Mac awake (system only; display may sleep) while monitoring."""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)


class SleepGuard:
    def __init__(self):
        self._proc: subprocess.Popen | None = None

    @property
    def held(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def set(self, want: bool):
        if want and not self.held:
            # -i: block idle system sleep (display can still sleep).
            # -w: caffeinate exits by itself if this process dies, so it never leaks.
            try:
                self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
                log.info("sleep prevention ON (caffeinate pid %s)", self._proc.pid)
            except OSError as e:
                log.error("caffeinate failed: %s", e)
        elif not want and self.held:
            self._proc.terminate()
            self._proc.wait(timeout=5)
            self._proc = None
            log.info("sleep prevention OFF")
