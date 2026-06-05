# -*- coding: utf-8 -*-
"""
core/local_tts.py — Local TTS synthesis for deterministic workflow states.

Converts Hinglish text → μ-law 8 kHz mono PCM using the Sarvam TTS API.
Output is ready for direct forwarding to Vobiz via the playAudio event.

Usage:
  from core.local_tts import synthesize, build_confirmation_text, PROMPTS

  mulaw = await synthesize("Kripya apna naam batayein.", api_key)
  if mulaw:
      await ws.send_str(json.dumps({"event": "playAudio", "media": {
          "contentType": "audio/x-mulaw",
          "sampleRate": 8000,
          "payload": base64.b64encode(mulaw).decode(),
      }}))
"""
from __future__ import annotations

import audioop
import base64
import io
import wave
from typing import Optional

import aiohttp

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# ── Fixed Hinglish prompt templates ───────────────────────────────────────────
# Keyed by stage name; "_retry" variants are used after validation failure.

PROMPTS: dict[str, str] = {
    # Address
    "address": (
        "Kripya apna poora pata batayein — "
        "society ya building ka naam aur area ya locality zaroori hai."
    ),
    "address_retry": (
        "Kripya sector ya locality bhi batayein, "
        "taaki hamara technician sahi jagah pahunch sake."
    ),
    # Preferred time
    "preferred_time": (
        "Technician kab visit kare? "
        "Koi bhi din aur samay batayein — jaise kal subah 10 baje."
    ),
    "preferred_time_retry": (
        "Kripya ek specific din ya samay batayein — "
        "jaise kal ya parso, subah ya shaam."
    ),
    # Customer name
    "customer_name": "Kripya apna poora naam batayein.",
    "customer_name_retry": "Kripya sirf apna poora naam batayein.",
}

# Echo templates: "Value, sahi hai?" confirmation prompt
_ECHO_TEMPLATES: dict[str, str] = {
    "address":        "Maine {value} suna hai. Kya sahi hai?",
    "preferred_time": "{value}, theek hai?",
    "customer_name":  "{value} ji, sahi hai?",
}


def build_echo_text(field: str, value: str) -> str:
    """Return the echo-confirmation text for a field value."""
    tmpl = _ECHO_TEMPLATES.get(field, "{value}, sahi hai?")
    return tmpl.format(value=value)


def build_confirmation_text(state: dict) -> str:
    """
    Generate the final service-request confirmation message from collected state.

    Example: "Alok ji, aapki request register ho gayi hai.
              Hamari team jald hi aapse sampark karegi.
              MyDoot ko call karne ke liye dhanyavaad."
    """
    name = (state.get("customer_name") or "aap").strip()
    # Strip trailing "ji" if already appended by mistake
    if name.lower().endswith(" ji"):
        name = name[:-3].strip()
    return (
        f"{name} ji, aapki request register ho gayi hai. "
        f"Hamari team jald hi aapse sampark karegi. "
        f"MyDoot ko call karne ke liye dhanyavaad."
    )


# ── Synthesis ─────────────────────────────────────────────────────────────────

async def synthesize(
    text:    str,
    api_key: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[bytes]:
    """
    Synthesize Hinglish text to μ-law 8 kHz mono PCM.

    Returns raw μ-law bytes ready to base64-encode for Vobiz playAudio.
    Returns None on failure — caller should fall back to Gemini-generated speech.

    The Sarvam TTS API returns a base64-encoded WAV; we convert it to
    8 kHz μ-law so it matches the Vobiz audio format exactly.
    """
    if not text or not api_key:
        return None
    try:
        payload = {
            "inputs":                [text],
            "target_language_code":  "hi-IN",
            "speaker":               "ananya",
            "pitch":                 0,
            "pace":                  1.05,
            "loudness":              1.5,
            "speech_sample_rate":    8000,
            "enable_preprocessing":  True,
            "model":                 "bulbul:v1",
        }
        headers = {
            "api-subscription-key": api_key,
            "Content-Type":         "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=8.0)

        async def _post(sess: aiohttp.ClientSession) -> Optional[bytes]:
            async with sess.post(
                SARVAM_TTS_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    print(f"[LOCAL TTS] Sarvam TTS {r.status}: {body[:200]}")
                    return None
                result = await r.json()
                audios = result.get("audios", [])
                if not audios:
                    print("[LOCAL TTS] No audio in response")
                    return None
                wav_b64 = audios[0]
                wav_bytes = base64.b64decode(wav_b64)
                return _wav_to_mulaw8k(wav_bytes)

        if session is not None:
            return await _post(session)
        async with aiohttp.ClientSession() as s:
            return await _post(s)

    except Exception as exc:
        print(f"[LOCAL TTS] synthesis error: {exc}")
        return None


def _wav_to_mulaw8k(wav_bytes: bytes) -> Optional[bytes]:
    """Convert any-rate WAV bytes to μ-law 8 kHz mono PCM."""
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            src_rate   = wf.getframerate()
            n_channels = wf.getnchannels()
            sampwidth  = wf.getsampwidth()
            raw_pcm    = wf.readframes(wf.getnframes())

        # Stereo → mono
        if n_channels == 2:
            raw_pcm = audioop.tomono(raw_pcm, sampwidth, 0.5, 0.5)

        # 8-bit → 16-bit
        if sampwidth == 1:
            raw_pcm   = audioop.lin2lin(raw_pcm, 1, 2)
            sampwidth = 2

        # Resample to 8 kHz
        if src_rate != 8000:
            raw_pcm, _ = audioop.ratecv(raw_pcm, sampwidth, 1, src_rate, 8000, None)

        return audioop.lin2ulaw(raw_pcm, 2)

    except Exception as exc:
        print(f"[LOCAL TTS] WAV conversion error: {exc}")
        return None
