#!/usr/bin/env python3
"""Sweep the settings that affect response latency, without a phone call.

A measured call on Railway came back at 1614ms total with 1633ms of that
being the model round trip, so the network is not the problem and there is
nothing to gain from tuning it. What remains is the model: how long it waits
before deciding the caller finished, how much it thinks, and how much prompt
it carries.

Each run costs a few seconds of audio, so a full sweep is pennies. That is
far cheaper than a phone call per hypothesis, which is how the last two
evenings went.

    python tools/sweep_latency.py
    python tools/sweep_latency.py --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from tools.probe_gemini import load_env, load_real_context


def speech(seconds: float = 0.6, rate: int = 16000) -> bytes:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    return (np.sin(2 * np.pi * (200 + 400 * t) * t) * 8000).astype(np.int16).tobytes()


async def one_run(model: str, silence_ms: int, thinking: str, tools, instructions):
    """Time to first audio for a single turn."""
    from app.providers.gemini import GeminiLiveProvider

    provider = GeminiLiveProvider(
        api_key=os.environ["GEMINI_API_KEY"],
        model=model,
        thinking_level=thinking,
        end_of_speech_silence_ms=silence_ms,
    )
    await asyncio.wait_for(
        provider.connect(instructions=instructions, tools=tools), timeout=20
    )
    await provider.send_audio(speech())
    await provider.send_text("A caller just said: what time do you close?")
    sent = time.time()

    first: float | None = None

    async def listen():
        nonlocal first
        async for ev in provider.receive():
            if ev.kind == "audio":
                first = time.time()
                return
            if ev.kind == "error":
                return

    try:
        await asyncio.wait_for(listen(), timeout=25)
    except TimeoutError:
        pass
    finally:
        await provider.close()
    return int((first - sent) * 1000) if first else None


async def main(runs: int) -> None:
    load_env()
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set")
        return

    instructions, tools = await load_real_context()
    model = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
    print(f"model  : {model}")
    print(f"prompt : {len(instructions)} chars, {len(tools)} tools")
    print()

    # One variable at a time. The bare row isolates how much the menu and
    # tool declarations cost, which is the term nobody can guess.
    cases = [
        ("baseline  500ms silence, minimal", 500, "minimal", tools, instructions),
        ("silence   300ms", 300, "minimal", tools, instructions),
        ("silence   200ms", 200, "minimal", tools, instructions),
        ("no tools, 300ms", 300, "minimal", [], instructions),
        ("no menu,  300ms", 300, "minimal", tools, "You answer a restaurant phone."),
        ("bare,     300ms", 300, "minimal", [], "You answer a restaurant phone."),
    ]

    print(f"{'case':38} {'p50':>7} {'runs':>18}")
    for label, silence, thinking, case_tools, case_prompt in cases:
        results = []
        for _ in range(runs):
            try:
                ms = await one_run(model, silence, thinking, case_tools, case_prompt)
            except Exception as exc:
                print(f"{label:38} failed: {type(exc).__name__}: {exc}")
                break
            if ms:
                results.append(ms)
            await asyncio.sleep(1)
        if results:
            p50 = int(statistics.median(results))
            print(f"{label:38} {p50:>5}ms {results!s:>18}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    asyncio.run(main(ap.parse_args().runs))
