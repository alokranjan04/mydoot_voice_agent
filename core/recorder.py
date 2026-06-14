# -*- coding: utf-8 -*-
"""
_TimelineRecorder — simple mono call recorder @ 8 kHz.

Both caller and agent audio are appended to a single list with
timestamps. At save time, frames are sorted by timestamp and
written as a mono WAV. No complex wall-clock placement — just
a list.append() that cannot fail.
"""
import time, wave


class _TimelineRecorder:
    __slots__ = ("_frames",)

    def __init__(self):
        self._frames = []  # list of (timestamp, pcm_bytes)

    def write_caller(self, pcm: bytes) -> None:
        """Record caller audio."""
        self._frames.append((time.perf_counter(), pcm))

    def write_priya(self, pcm: bytes) -> None:
        """Record agent (AI) audio."""
        self._frames.append((time.perf_counter(), pcm))

    def write(self, pcm: bytes) -> None:
        """Backward-compatibility alias."""
        self.write_caller(pcm)

    def save(self, path: str) -> None:
        """Sort frames by time and write a mono WAV."""
        self._frames.sort(key=lambda x: x[0])
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            for _, pcm in self._frames:
                wf.writeframes(pcm)

    def frame_count(self) -> int:
        return len(self._frames)

    def __bool__(self) -> bool:
        return len(self._frames) > 0
