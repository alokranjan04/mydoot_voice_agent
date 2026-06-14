# -*- coding: utf-8 -*-
"""
_TimelineRecorder — mono PCM-16 LE @ 8 kHz call recorder.

Both caller audio and agent audio are mixed into a single mono channel
with wall-clock timing so the conversation flows naturally.

Audio is placed at max(wall_clock_offset, channel_head) so natural silence
gaps are preserved and no chunk overwrites already-written audio.
"""
import time, wave


class _TimelineRecorder:
    __slots__ = ("_buf", "_start", "_caller_head", "_agent_head")

    def __init__(self):
        self._buf         = bytearray()
        self._start       = time.perf_counter()
        self._caller_head = 0
        self._agent_head  = 0

    # ── Internal placement ────────────────────────────────────────────────────

    def _place(self, pcm: bytes, head: int) -> int:
        """Mix pcm into the mono buffer at max(wall_clock_offset, head).
        Audio is ADDED (mixed) to existing data, not overwritten.
        Returns new head position."""
        import array as _array

        wc  = int((time.perf_counter() - self._start) * 8000) * 2
        pos = max(wc, head)
        end = pos + len(pcm)

        # Extend buffer if needed
        if len(self._buf) < end:
            self._buf.extend(b"\x00" * (end - len(self._buf)))

        # Mix (add) new audio into existing buffer instead of overwriting.
        # This lets caller + agent audio overlap naturally.
        existing = _array.array("h", self._buf[pos:end])
        incoming = _array.array("h", pcm)
        for i in range(len(incoming)):
            # Clip to int16 range to prevent overflow
            mixed = existing[i] + incoming[i]
            existing[i] = max(-32768, min(32767, mixed))
        self._buf[pos:end] = existing.tobytes()

        return end

    # ── Public write API ──────────────────────────────────────────────────────

    def write_caller(self, pcm: bytes) -> None:
        """Record caller audio."""
        self._caller_head = self._place(pcm, self._caller_head)

    def write_priya(self, pcm: bytes) -> None:
        """Record agent (AI) audio."""
        self._agent_head = self._place(pcm, self._agent_head)

    def write(self, pcm: bytes) -> None:
        """Backward-compatibility alias → caller channel."""
        self.write_caller(pcm)

    # ── Save ──────────────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Write a mono WAV file with both caller and agent audio mixed."""
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(bytes(self._buf))

    def __bool__(self) -> bool:
        return bool(self._buf)
