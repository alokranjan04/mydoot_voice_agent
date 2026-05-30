# -*- coding: utf-8 -*-
"""
_TimelineRecorder — stereo PCM-16 LE @ 8 kHz call recorder.

Left  channel = caller audio
Right channel = Priya TTS audio

Audio is placed at max(wall_clock_offset, channel_head) so natural silence
gaps are preserved and no chunk overwrites already-written audio.
"""
import time, wave


class _TimelineRecorder:
    __slots__ = ("_caller", "_priya", "_start", "_caller_head", "_priya_head")

    def __init__(self):
        self._caller      = bytearray()
        self._priya       = bytearray()
        self._start       = time.perf_counter()
        self._caller_head = 0
        self._priya_head  = 0

    # ── Internal placement ────────────────────────────────────────────────────

    def _place(self, buf: bytearray, pcm: bytes, head: int) -> int:
        """Write pcm at max(wall_clock_byte_offset, head). Returns new head."""
        wc  = int((time.perf_counter() - self._start) * 8000) * 2
        pos = max(wc, head)
        end = pos + len(pcm)
        if len(buf) < end:
            buf.extend(b"\x00" * (end - len(buf)))
        buf[pos:end] = pcm
        return end

    # ── Public write API ──────────────────────────────────────────────────────

    def write_caller(self, pcm: bytes) -> None:
        """Record caller audio (left channel)."""
        self._caller_head = self._place(self._caller, pcm, self._caller_head)

    def write_priya(self, pcm: bytes) -> None:
        """Record Priya TTS audio (right channel).
        Must be called BEFORE the WebSocket send so the last chunk is always
        captured even if the WS closes mid-stream.
        """
        self._priya_head = self._place(self._priya, pcm, self._priya_head)

    def write(self, pcm: bytes) -> None:
        """Backward-compatibility alias → caller channel."""
        self.write_caller(pcm)

    # ── Save ──────────────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Interleave channels and write a stereo WAV file."""
        import array as _array
        n = max(len(self._caller), len(self._priya))
        n = (n + 1) & ~1                          # round up to whole 2-byte sample
        caller_b = bytes(self._caller) + b"\x00" * (n - len(self._caller))
        priya_b  = bytes(self._priya)  + b"\x00" * (n - len(self._priya))
        caller_s = _array.array("h", caller_b)
        priya_s  = _array.array("h", priya_b)
        stereo   = _array.array("h")
        for c, p in zip(caller_s, priya_s):
            stereo.append(c)   # left  = caller
            stereo.append(p)   # right = Priya
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(stereo.tobytes())

    def __bool__(self) -> bool:
        return bool(self._caller) or bool(self._priya)
