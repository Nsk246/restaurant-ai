#!/usr/bin/env python3
"""Check that Gemini Live actually works, without a phone in the way.

A call that connects and then sits in silence gives you nothing to debug: the
websocket is open, no exception is raised, and the logs look healthy. This
isolates the model session so a failure has a message attached to it.

    python tools/probe_gemini.py
    python tools/probe_gemini.py --model gemini-2.5-flash-native-audio-preview-12-2025
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


def load_env() -> None:
    """Read the repo-root .env the same way the service does."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def speech_like(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A tone sweep. Not speech, but enough to make the model take a turn."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    sweep = np.sin(2 * np.pi * (200 + 400 * t) * t)
    return (sweep * 8000).astype(np.int16).tobytes()


async def probe(model: str, timeout: float) -> int:
    from app.providers.gemini import GeminiLiveProvider

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print("FAIL: GEMINI_API_KEY is not set")
        return 1
    print(f"key      : {key[:6]}...{key[-4:]} ({len(key)} chars)")
    print(f"model    : {model}")

    provider = GeminiLiveProvider(api_key=key, model=model)

    print("\n[1/3] opening a session...")
    started = time.time()
    try:
        await asyncio.wait_for(
            provider.connect(
                instructions="You are a test. Say 'hello' and nothing else.",
                tools=[],
            ),
            timeout=timeout,
        )
    except TimeoutError:
        print(f"FAIL: connect hung for {timeout}s with no error.")
        print("      Usually a model id the API accepts but never opens, or a")
        print("      key without Live access. Try --model with another id.")
        return 1
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(f"      connected in {time.time() - started:.2f}s")

    print("\n[2/3] sending audio...")
    await provider.send_audio(speech_like(1.0))
    await provider.send_text("Say hello now.")

    print("\n[3/3] waiting for a response...")
    audio_bytes = 0
    events = 0
    first_at: float | None = None
    sent_at = time.time()

    async def listen():
        nonlocal audio_bytes, events, first_at
        async for ev in provider.receive():
            events += 1
            if ev.kind == "audio":
                if first_at is None:
                    first_at = time.time()
                audio_bytes += len(ev.audio)
            elif ev.kind == "transcript":
                print(f"      transcript [{ev.role}]: {ev.text!r}")
            elif ev.kind == "error":
                print(f"      ERROR event: {ev.detail}")
                return
            if audio_bytes > 16000:
                return

    try:
        await asyncio.wait_for(listen(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        await provider.close()

    print()
    if audio_bytes:
        ttfa = int((first_at - sent_at) * 1000) if first_at else -1
        print(f"PASS: {audio_bytes} bytes of audio, {events} events")
        print(f"      time to first audio: {ttfa}ms")
        return 0

    print(f"FAIL: no audio after {timeout}s ({events} events)")
    print("      The session opened but produced nothing. Check that the key's")
    print("      project has billing enabled: native-audio Live models need it.")
    return 1


if __name__ == "__main__":
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--model",
        default=os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"),
    )
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()
    sys.exit(asyncio.run(probe(args.model, args.timeout)))
