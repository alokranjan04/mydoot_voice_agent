#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_asr_compare.py — Compare ASR services on a recorded PSTN call WAV.

Tests in parallel:
  1. Azure OpenAI Whisper   (AZURE_KEY1 + ENDPINTS env vars)
  2. Sarvam Saaras V3       (SARVAM_API_KEY)
  3. Deepgram Nova-2        (DEEPGRAM_API_KEY)

Usage:
    python test_asr_compare.py [path/to/recording.wav]

    If no path is given, picks the most recent WAV in ./recordings/

    Run a real call first with RECORD_CALLS=1 in your .env to generate a file.
"""

import os, sys, time, glob, json, wave, struct, io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; set env vars manually if needed

import urllib.request
import urllib.parse

# ── Credentials (from .env or environment) ────────────────────────────────────
AZURE_KEY        = os.getenv("AZURE_KEY1", "")
AZURE_ENDPOINT   = os.getenv("ENDPINTS", "").rstrip("/")   # typo in .env is intentional
AZURE_DEPLOYMENT = os.getenv("AZURE_DEPLOYMENT", "whisper")
SARVAM_KEY       = os.getenv("SARVAM_API_KEY", "")
DEEPGRAM_KEY     = os.getenv("DEEPGRAM_API_KEY", "")


# ── Find WAV file ──────────────────────────────────────────────────────────────
def find_wav(path_arg):
    if path_arg and os.path.isfile(path_arg):
        return path_arg
    recordings_dir = os.getenv("RECORDINGS_DIR", "recordings")
    wavs = sorted(glob.glob(os.path.join(recordings_dir, "*.wav")), key=os.path.getmtime)
    if not wavs:
        print(f"[ERROR] No WAV files found in {recordings_dir}/")
        print("        Make a test call with RECORD_CALLS=1 in your .env, then re-run.")
        sys.exit(1)
    return wavs[-1]


# ── WAV info ───────────────────────────────────────────────────────────────────
def wav_info(path):
    with wave.open(path, "rb") as wf:
        frames    = wf.getnframes()
        rate      = wf.getframerate()
        channels  = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        duration  = frames / rate
    size_kb = os.path.getsize(path) / 1024
    return {
        "duration_s": duration,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": sampwidth,
        "size_kb": size_kb,
    }


# ── HTTP helper (stdlib only, no requests) ────────────────────────────────────
def _multipart_body(fields: dict, files: dict, boundary: str) -> bytes:
    """Build a multipart/form-data body without external deps."""
    body = b""
    for name, value in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    for name, (filename, data, content_type) in files.items():
        body += (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body


def _post(url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


# ── Azure OpenAI Whisper ───────────────────────────────────────────────────────
def transcribe_azure(wav_bytes: bytes, wav_filename: str) -> dict:
    if not AZURE_KEY:
        return {"service": "Azure Whisper", "error": "AZURE_KEY1 not set", "text": None, "latency_ms": 0}
    if not AZURE_ENDPOINT:
        return {"service": "Azure Whisper", "error": "ENDPINTS not set", "text": None, "latency_ms": 0}

    url = (f"{AZURE_ENDPOINT}/openai/deployments/{AZURE_DEPLOYMENT}"
           f"/audio/transcriptions?api-version=2024-06-01")

    boundary = "----AzureBoundary"
    body = _multipart_body(
        fields={"language": "hi", "response_format": "json"},
        files={"file": (wav_filename, wav_bytes, "audio/wav")},
        boundary=boundary,
    )
    headers = {
        "api-key": AZURE_KEY,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    t0 = time.time()
    try:
        raw = _post(url, headers, body)
        latency_ms = int((time.time() - t0) * 1000)
        result = json.loads(raw)
        text = result.get("text", "").strip()
        return {"service": "Azure Whisper", "text": text, "latency_ms": latency_ms, "error": None}
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {"service": "Azure Whisper", "text": None, "latency_ms": latency_ms, "error": str(e)}


# ── Sarvam AI ──────────────────────────────────────────────────────────────────
def transcribe_sarvam(wav_bytes: bytes, wav_filename: str) -> dict:
    if not SARVAM_KEY:
        return {"service": "Sarvam V3", "error": "SARVAM_API_KEY not set", "text": None, "latency_ms": 0}

    url = "https://api.sarvam.ai/speech-to-text"
    boundary = "----SarvamBoundary"
    body = _multipart_body(
        fields={"model": "saaras:v2", "language_code": "hi-IN", "with_timestamps": "false"},
        files={"file": (wav_filename, wav_bytes, "audio/wav")},
        boundary=boundary,
    )
    headers = {
        "api-subscription-key": SARVAM_KEY,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    t0 = time.time()
    try:
        raw = _post(url, headers, body)
        latency_ms = int((time.time() - t0) * 1000)
        result = json.loads(raw)
        # Sarvam returns { "transcript": "...", ... }
        text = (result.get("transcript") or result.get("text") or "").strip()
        return {"service": "Sarvam V3", "text": text, "latency_ms": latency_ms, "error": None}
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {"service": "Sarvam V3", "text": None, "latency_ms": latency_ms, "error": str(e)}


# ── Deepgram Nova-2 Phonecall ──────────────────────────────────────────────────
def transcribe_deepgram(wav_bytes: bytes) -> dict:
    if not DEEPGRAM_KEY:
        return {"service": "Deepgram Nova-2", "error": "DEEPGRAM_API_KEY not set", "text": None, "latency_ms": 0}

    params = urllib.parse.urlencode({
        "model": "nova-2-phonecall",
        "language": "hi",
        "detect_language": "true",
        "punctuate": "true",
        "smart_format": "true",
    })
    url = f"https://api.deepgram.com/v1/listen?{params}"
    headers = {
        "Authorization": f"Token {DEEPGRAM_KEY}",
        "Content-Type": "audio/wav",
    }

    t0 = time.time()
    try:
        raw = _post(url, headers, wav_bytes)
        latency_ms = int((time.time() - t0) * 1000)
        result = json.loads(raw)
        channels = result.get("results", {}).get("channels", [{}])
        alts = channels[0].get("alternatives", [{}]) if channels else [{}]
        text = (alts[0].get("transcript") or "").strip()
        detected_lang = channels[0].get("detected_language", "") if channels else ""
        return {
            "service": "Deepgram Nova-2",
            "text": text,
            "latency_ms": latency_ms,
            "error": None,
            "detected_language": detected_lang,
        }
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {"service": "Deepgram Nova-2", "text": None, "latency_ms": latency_ms, "error": str(e)}


# ── Print helpers ──────────────────────────────────────────────────────────────
def _bar(label, value, max_val, width=30, unit=""):
    filled = int(width * value / max_val) if max_val else 0
    bar = "█" * filled + "░" * (width - filled)
    return f"  {label:<18} [{bar}] {value}{unit}"


def print_results(results, wav_path, info):
    print()
    print("=" * 70)
    print(f"  ASR COMPARISON — {os.path.basename(wav_path)}")
    print(f"  Audio: {info['duration_s']:.1f}s | {info['sample_rate']}Hz | "
          f"{info['channels']}ch | {info['size_kb']:.0f}KB")
    print("=" * 70)

    max_latency = max((r["latency_ms"] for r in results if r["latency_ms"]), default=1)

    for r in results:
        print()
        svc = r["service"]
        if r.get("error"):
            print(f"  ❌  {svc}")
            print(f"       Error: {r['error']}")
            continue

        text = r.get("text") or "(empty)"
        lat  = r["latency_ms"]
        print(f"  ✅  {svc}  ({lat}ms)")
        if r.get("detected_language"):
            print(f"       Detected language: {r['detected_language']}")
        print(f"       Transcript: {text}")
        print(_bar("Latency", lat, max_latency, unit="ms"))

    print()
    print("-" * 70)
    # Simple winner: longest non-empty transcript (more words = more was understood)
    ok = [r for r in results if r.get("text")]
    if ok:
        best = max(ok, key=lambda r: len((r.get("text") or "").split()))
        print(f"  Most detailed transcript: {best['service']}")
        fastest = min(ok, key=lambda r: r["latency_ms"])
        print(f"  Fastest response:         {fastest['service']} ({fastest['latency_ms']}ms)")
    print("=" * 70)
    print()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    wav_path = find_wav(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"\n[ASR Compare] Loading: {wav_path}")

    info = wav_info(wav_path)
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    wav_filename = os.path.basename(wav_path)

    print(f"[ASR Compare] Audio: {info['duration_s']:.1f}s | "
          f"{info['sample_rate']}Hz | {info['size_kb']:.0f}KB")
    print("[ASR Compare] Sending to 3 services in parallel...\n")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(transcribe_azure,   wav_bytes, wav_filename): "azure",
            pool.submit(transcribe_sarvam,  wav_bytes, wav_filename): "sarvam",
            pool.submit(transcribe_deepgram, wav_bytes):              "deepgram",
        }
        results = []
        for future in as_completed(futures):
            res = future.result()
            svc = res["service"]
            if res.get("error"):
                print(f"  [{svc}] ❌ {res['error']}")
            else:
                preview = (res.get("text") or "")[:80]
                print(f"  [{svc}] ✅ {res['latency_ms']}ms — \"{preview}\"")
            results.append(res)

    # Sort by service name for consistent display
    results.sort(key=lambda r: r["service"])
    print_results(results, wav_path, info)


if __name__ == "__main__":
    main()
