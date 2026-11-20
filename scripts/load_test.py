"""
Concurrent load generator for Ollama.

Fires N concurrent streaming chat requests and reports, per request, the
time-to-first-token and the total latency, plus aggregate p95/p99 and the
overall generation rate.

This is the command-line counterpart of the Prometheus exporter: it measures
the same thing over a fixed burst instead of continuously, which makes it a
useful cross-check of what the Grafana dashboard displays.

Usage:
    pip install aiohttp
    python scripts/load_test.py --concurrency 4 --requests 20
"""

import argparse
import asyncio
import json
import statistics
import time

import aiohttp

PROMPTS = [
    "Explain in one sentence what a container is.",
    "What is a load balancer?",
    "Name two benefits of cloud computing.",
    "What is the purpose of a firewall?",
    "Define the term 'latency' in computer science.",
]


async def one_request(session, url: str, model: str, idx: int, max_tokens: int) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPTS[idx % len(PROMPTS)]}],
        "stream": True,
        "options": {"num_predict": max_tokens},
    }

    start = time.perf_counter()
    first_token_at = None
    final = {}

    async with session.post(f"{url}/api/chat", json=payload) as resp:
        resp.raise_for_status()
        async for raw in resp.content:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if first_token_at is None and chunk.get("message", {}).get("content"):
                first_token_at = time.perf_counter()
            if chunk.get("done"):
                final = chunk
                break

    end = time.perf_counter()
    return {
        "ttft": (first_token_at - start) if first_token_at else None,
        "latency": end - start,
        "tokens": final.get("eval_count", 0),
        "eval_seconds": final.get("eval_duration", 0) / 1e9,
    }


async def run(url: str, model: str, concurrency: int, total: int, max_tokens: int) -> None:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(idx, session):
        async with semaphore:
            return await one_request(session, url, model, idx, max_tokens)

    timeout = aiohttp.ClientTimeout(total=1800)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        wall_start = time.perf_counter()
        results = await asyncio.gather(*(bounded(i, session) for i in range(total)))
        wall = time.perf_counter() - wall_start

    ttfts = [r["ttft"] for r in results if r["ttft"] is not None]
    latencies = [r["latency"] for r in results]
    tokens = sum(r["tokens"] for r in results)

    def pct(values, p):
        if not values:
            return float("nan")
        if len(values) == 1:
            return values[0]
        return statistics.quantiles(values, n=100)[p - 1]

    print()
    print(f"Model: {model}")
    print(f"Requests: {total}   Concurrency: {concurrency}   Total duration: {wall:.1f}s")
    print("-" * 62)
    print(f"TTFT avg={statistics.mean(ttfts):6.2f}s  "
          f"p95={pct(ttfts, 95):6.2f}s  p99={pct(ttfts, 99):6.2f}s")
    print(f"E2E Latency avg={statistics.mean(latencies):6.2f}s  "
          f"p95={pct(latencies, 95):6.2f}s  p99={pct(latencies, 99):6.2f}s")
    print(f"Throughput {tokens / wall:6.2f} tokens/s aggregate ({tokens} total tokens)")
    print()
    


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--model", default="gemma3:1b")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args()

    asyncio.run(run(args.url, args.model, args.concurrency, args.requests, args.max_tokens))
