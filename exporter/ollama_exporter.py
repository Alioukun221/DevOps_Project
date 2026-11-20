"""
Prometheus exporter for Ollama inference metrics.

Ollama exposes no Prometheus endpoint of its own, so this service measures the
engine the only way available from outside: it drives it with real streaming
requests and times them. It is an active probe, in the same spirit as
blackbox_exporter, not a passive scraper.

Two sources of truth are combined for each request:

  * client-side  -- wall-clock time until the first token arrives on the stream
                    (TTFT) and until the stream closes (end-to-end latency);
  * server-side  -- the counters Ollama reports in the final chunk
                    (eval_count, eval_duration, prompt_eval_count, ...), which
                    give an exact generation rate free of network jitter.

The worker pool that generates the load is resizable at runtime:

    curl -X POST "http://localhost:9105/concurrency?value=8"

so a Grafana dashboard can show latency degrading as concurrency rises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time

import aiohttp
from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# --------------------------------------------------------------- configuration

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
MODELS = [m.strip() for m in os.getenv("OLLAMA_MODELS", "gemma3:1b").split(",") if m.strip()]
PORT = int(os.getenv("EXPORTER_PORT", "9105"))
START_CONCURRENCY = int(os.getenv("START_CONCURRENCY", "1"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "64"))
# Pause between two requests of the same worker. 0 keeps the engine saturated.
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "600"))

PROMPTS = [
    "Explique en une phrase ce qu'est un conteneur.",
    "Qu'est-ce qu'un load balancer ?",
    "Cite deux avantages du cloud.",
    "A quoi sert un pare-feu ?",
    "Definis le mot 'latence' en informatique.",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ollama-exporter")

# ------------------------------------------------------------------- metriques
# Buckets are deliberately wide: CPU-only inference on a small machine produces
# latencies that a default web-oriented bucket set would collapse into +Inf.

TTFT_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 45, 60, 90, 120)
E2E_BUCKETS = (0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300)
TPS_BUCKETS = (0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50)

ttft_seconds = Histogram(
    "ollama_ttft_seconds",
    "Time to first token, measured client-side on the streaming response",
    ["model"],
    buckets=TTFT_BUCKETS,
)
request_duration_seconds = Histogram(
    "ollama_request_duration_seconds",
    "End-to-end request latency, from send to end of stream",
    ["model"],
    buckets=E2E_BUCKETS,
)
tokens_per_second = Histogram(
    "ollama_tokens_per_second",
    "Per-request generation rate reported by Ollama (eval_count / eval_duration)",
    ["model"],
    buckets=TPS_BUCKETS,
)
model_load_seconds = Histogram(
    "ollama_model_load_seconds",
    "Time Ollama spent loading the model into memory (0 when already resident)",
    ["model"],
    buckets=TTFT_BUCKETS,
)
prompt_eval_seconds = Histogram(
    "ollama_prompt_eval_seconds",
    "Time Ollama spent evaluating the prompt before generating",
    ["model"],
    buckets=TTFT_BUCKETS,
)

generated_tokens_total = Counter(
    "ollama_generated_tokens_total",
    "Total tokens generated; rate() over this gives throughput in tokens/s",
    ["model"],
)
prompt_tokens_total = Counter(
    "ollama_prompt_tokens_total",
    "Total prompt tokens submitted",
    ["model"],
)
requests_total = Counter(
    "ollama_requests_total",
    "Requests sent to Ollama by this exporter",
    ["model", "status"],
)

inflight_requests = Gauge(
    "ollama_inflight_requests",
    "Requests currently open against Ollama",
)
target_concurrency = Gauge(
    "ollama_target_concurrency",
    "Number of load-generating workers currently running",
)
ollama_up = Gauge(
    "ollama_up",
    "1 when the last request to Ollama succeeded, 0 otherwise",
)

# ------------------------------------------------------------------- sondage


async def probe_once(session: aiohttp.ClientSession, model: str) -> None:
    """Send one streaming chat request and record every metric it yields."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": random.choice(PROMPTS)}],
        "stream": True,
        "options": {"num_predict": MAX_TOKENS},
    }

    start = time.perf_counter()
    first_token_at: float | None = None
    final: dict = {}

    inflight_requests.inc()
    try:
        async with session.post(f"{OLLAMA_URL}/api/chat", json=payload) as resp:
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

        elapsed = time.perf_counter() - start

        # Client-side timings.
        if first_token_at is not None:
            ttft_seconds.labels(model).observe(first_token_at - start)
        request_duration_seconds.labels(model).observe(elapsed)

        # Server-side counters, reported in nanoseconds.
        eval_count = final.get("eval_count", 0)
        eval_ns = final.get("eval_duration", 0)
        if eval_count and eval_ns:
            tokens_per_second.labels(model).observe(eval_count / (eval_ns / 1e9))
        if eval_count:
            generated_tokens_total.labels(model).inc(eval_count)
        if final.get("prompt_eval_count"):
            prompt_tokens_total.labels(model).inc(final["prompt_eval_count"])
        if final.get("load_duration"):
            model_load_seconds.labels(model).observe(final["load_duration"] / 1e9)
        if final.get("prompt_eval_duration"):
            prompt_eval_seconds.labels(model).observe(final["prompt_eval_duration"] / 1e9)

        requests_total.labels(model, "success").inc()
        ollama_up.set(1)

    except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
        requests_total.labels(model, "error").inc()
        ollama_up.set(0)
        log.warning("probe failed on %s: %s", model, exc)
    finally:
        inflight_requests.dec()


async def worker(session: aiohttp.ClientSession, index: int) -> None:
    """One load-generating worker, cycling through the configured models."""
    while True:
        model = MODELS[index % len(MODELS)] if len(MODELS) > 1 else MODELS[0]
        await probe_once(session, model)
        if REQUEST_DELAY:
            await asyncio.sleep(REQUEST_DELAY)


# ------------------------------------------------------------------- pool


class WorkerPool:
    """Holds the running workers and lets their number change at runtime."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._tasks: list[asyncio.Task] = []

    @property
    def size(self) -> int:
        return len(self._tasks)

    async def resize(self, target: int) -> None:
        target = max(0, min(target, 64))
        while len(self._tasks) < target:
            self._tasks.append(
                asyncio.create_task(worker(self._session, len(self._tasks)))
            )
        while len(self._tasks) > target:
            task = self._tasks.pop()
            task.cancel()
        target_concurrency.set(len(self._tasks))
        log.info("concurrency set to %d", len(self._tasks))


# ------------------------------------------------------------------- http


async def handle_metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST.split(";")[0])


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_concurrency(request: web.Request) -> web.Response:
    pool: WorkerPool = request.app["pool"]

    if request.method == "GET":
        return web.json_response({"concurrency": pool.size})

    raw = request.query.get("value")
    if raw is None and request.can_read_body:
        body = await request.json()
        raw = body.get("value")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return web.json_response({"error": "expected an integer 'value'"}, status=400)

    await pool.resize(value)
    return web.json_response({"concurrency": pool.size})


async def build_app() -> web.Application:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    session = aiohttp.ClientSession(timeout=timeout)

    pool = WorkerPool(session)
    await pool.resize(START_CONCURRENCY)

    app = web.Application()
    app["pool"] = pool
    app["session"] = session
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/concurrency", handle_concurrency)
    app.router.add_post("/concurrency", handle_concurrency)

    async def cleanup(_: web.Application):
        await pool.resize(0)
        await session.close()

    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    log.info("target=%s models=%s concurrency=%d", OLLAMA_URL, MODELS, START_CONCURRENCY)
    web.run_app(build_app(), port=PORT, access_log=None)
