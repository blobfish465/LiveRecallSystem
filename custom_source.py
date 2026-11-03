#!/usr/bin/env python3
"""Test-only caption source for hidden_sim.py: more revisions per segment and bursts.

The public caption_source.py always sends 3 revisions per segment at an even pace;
the hidden source "may not" (spec: do not assume a fixed number of revisions;
the evaluator varies revision rate and burstiness). This speaks the same protocol
(SOURCE_HELLO, CAPTION_UPDATE, SOURCE_DONE) with:
  * --revisions R: R revisions per segment, each a longer prefix of the sentence,
    the last one final;
  * --burst-every S --burst-len L --burst-factor F: for L seconds out of every S,
    all delays are divided by F (a burst of revisions and segments).
It is a measurement tool, not part of the submission path; caption_source.py stays
the source for every public test.

Usage:
  python custom_source.py --segments 60 --revisions 8 --revision-delay 0.1
  python custom_source.py --segments 80 --burst-every 10 --burst-len 3 --burst-factor 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

import websockets

from caption_source import DEFAULT_SENTENCES, load_sentences  # instructor file, imported unmodified


def revisions_of(sentence: str, n: int) -> list[str]:
    """n strictly growing prefixes of `sentence`, the last one the whole sentence; cuts
    snap back to a word boundary when that keeps them growing. Needs len(sentence) >= n."""
    if n > len(sentence):
        raise ValueError(f"sentence too short for {n} revisions: {sentence!r}")
    out, prev = [], 0
    for k in range(1, n):
        cut = max(prev + 1, math.ceil(len(sentence) * k / n))
        space = sentence.rfind(" ", prev + 1, cut + 1)
        if space > prev:
            cut = space
        out.append(sentence[:cut])
        prev = cut
    out.append(sentence)
    return out


def in_burst(elapsed: float, every: float, length: float) -> bool:
    return every > 0 and length > 0 and (elapsed % every) < length


async def run(args) -> None:
    sentences = load_sentences(args.transcript) if args.transcript else DEFAULT_SENTENCES
    start = time.monotonic()
    updates = 0

    async def pause(seconds: float) -> None:
        factor = args.burst_factor if in_burst(time.monotonic() - start, args.burst_every, args.burst_len) else 1.0
        await asyncio.sleep(seconds / factor)

    async with websockets.connect(args.uri, max_size=2**20) as ws:
        await ws.send(json.dumps({"type": "SOURCE_HELLO", "session": args.session}))
        for segment in range(1, args.segments + 1):
            revs = revisions_of(sentences[(segment - 1) % len(sentences)], args.revisions)
            for revision, text in enumerate(revs, start=1):
                await ws.send(json.dumps({
                    "type": "CAPTION_UPDATE", "session": args.session, "segment": segment,
                    "revision": revision, "source_ts": time.time(),
                    "final": revision == len(revs), "text": text,
                }))
                updates += 1
                if revision != len(revs):
                    await pause(args.revision_delay)
            await pause(args.segment_gap)
        await ws.send(json.dumps({"type": "SOURCE_DONE", "session": args.session,
                                  "source_ts": time.time(), "segments": args.segments}))
    print(f"SOURCE_DONE: emitted {args.segments} finalized segments, {updates} updates", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--uri", default="ws://127.0.0.1:8765")
    p.add_argument("--session", default="L1")
    p.add_argument("--transcript", help="One sentence per line (default: the public sentences)")
    p.add_argument("--segments", type=int, required=True)
    p.add_argument("--revisions", type=int, default=3)
    p.add_argument("--revision-delay", type=float, default=0.20)
    p.add_argument("--segment-gap", type=float, default=0.35)
    p.add_argument("--burst-every", type=float, default=0.0, help="seconds between burst starts (0: no bursts)")
    p.add_argument("--burst-len", type=float, default=0.0)
    p.add_argument("--burst-factor", type=float, default=1.0, help="delays are divided by this during a burst")
    p.add_argument("--quiet", action="store_true", help="accepted for symmetry with caption_source.py")
    args = p.parse_args()
    if args.revisions < 1 or args.segments < 1 or args.burst_factor <= 0:
        p.error("--revisions and --segments must be >= 1, --burst-factor > 0")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
