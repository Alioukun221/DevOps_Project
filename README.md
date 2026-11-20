<div align="center">

# Self-Hosted Open Source LLM Deployment

**From Docker Compose to Kubernetes with Real-Time Inference Performance Monitoring**

![Kubernetes](https://img.shields.io/badge/Kubernetes-1.35-326CE5?logo=kubernetes&logoColor=white)
![Docker](https://img.shields.io/badge/Docker%20Compose-v2-2496ED?logo=docker&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-F46800?logo=grafana&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)

</div>

---

Ollama serves open-weights large language models, and Open WebUI provides the chat interface.

The stack is initially deployed with Docker Compose and then reproduced on a Minikube Kubernetes cluster. A custom Prometheus exporter developed for this project actively probes the inference engine to measure real-world performance metrics.

## Architecture

```text
                                  Browser
                                     │
     ┌───────────────────────────────┼─────────────────────────────────┐
     │  namespace llm                ▼                                 │
     │                         Ingress nginx                           │
     │                           llm.local                             │
     │                               │                                 │
     │                               ▼                                 │
     │                      Service open-webui                         │
     │                               │                                 │
     │                               ▼                                 │
     │     PVC 2 Gi  ◄────────  Pod Open WebUI                         │
     │                               │                                 │
     │                               │  internal DNS resolution        │
     │                               ▼                                 │
     │                         Service ollama                          │
     │                               │                                 │
     │                               ▼                                 │
     │     PVC 8 Gi  ◄────────  Pod Ollama                             │
     │                                                                 │
     │     ConfigMap llm-config ──────► injected into both pods        │
     └─────────────────────────────────────────────────────────────────┘
```

Nine Kubernetes resources satisfy the requirements. Open WebUI connects to Ollama via its Service name, resolved through cluster-internal CoreDNS, without depending on pod IP addresses.

PersistentVolumeClaims (PVCs) ensure model weights and web interface state are preserved across pod restarts and recreations, provided the volume can be remounted on the targeted node.

## Quickstart (Docker Compose)

The `.env` file is not tracked by Git. Create it from the provided template:

```bash
cp .env.example .env

docker compose --profile core up -d

docker exec -it ollama ollama pull gemma3:1b
docker exec -it ollama ollama pull llama3.2:1b
```

The web interface is available at [http://localhost:3000](http://localhost:3000).

> **Note:** The initial startup may take a few minutes as Open WebUI downloads an embedding model before reporting healthy on its health endpoint.

## Kubernetes Deployment

```bash
minikube start -p llm --driver=docker --memory=3800 --cpus=2 --disk-size=20g

minikube -p llm addons enable ingress
```

> **Important:** Wait until `ingress-nginx-controller` is in `1/1 Running` state before applying manifests.
>
> Its admission webhook rejects Ingress resource creation until the controller is operational.

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/

kubectl -n llm get pods,svc,pvc,ingress

kubectl -n llm exec -it deploy/ollama -- ollama pull gemma3:1b
kubectl -n llm exec -it deploy/ollama -- ollama pull llama3.2:1b
```

### Accessing the Service

Port forwarding provides direct local access, replicating the mapping established by Docker Compose:

```bash
kubectl -n llm port-forward svc/open-webui 3000:8080
```

Ingress provides production-like routing.

Retrieve the cluster IP:

```bash
minikube -p llm ip
```

Then map `<minikube-ip> llm.local` in your `hosts` file:

- **Linux / macOS:** `/etc/hosts`
- **Windows:** `C:\Windows\System32\drivers\etc\hosts`

Access the UI at `http://llm.local`.

## Monitoring & Observability

Ollama does not expose a native Prometheus endpoint for real-time inference latency and token throughput.

The custom exporter in `exporter/` acts as an active probe (similar to `blackbox_exporter`), sending continuous streaming requests to Ollama and measuring exact timings.

```text
   ┌──────────────┐     streaming requests      ┌──────────────┐
   │   Exporter   │ ──────────────────────────► │    Ollama    │
   │    :9105     │ ◄────────────────────────── │    :11434    │
   └──────┬───────┘     tokens & counters       └──────────────┘
          │
          │  scrapes /metrics
          ▼
   ┌──────────────┐          PromQL             ┌──────────────┐
   │  Prometheus  │ ◄────────────────────────── │   Grafana    │
   │    :9090     │                             │    :3001     │
   └──────────────┘                             └──────────────┘
```

### Starting the Monitoring Stack

```bash
docker compose --profile core --profile monitoring up -d --build
```

| Service | URL | Credentials / Notes |
| :-- | :-- | :-- |
| Grafana | [http://localhost:3001](http://localhost:3001) | `admin` / `admin` |
| Prometheus | [http://localhost:9090/targets](http://localhost:9090/targets) | Scrapes `exporter:9105` |
| Raw Metrics | [http://localhost:9105/metrics](http://localhost:9105/metrics) | Prometheus metric format |

Worker concurrency can be dynamically reconfigured at runtime without restarting containers:

```bash
curl -X POST "http://localhost:9105/concurrency?value=3"
```

You can also run synthetic concurrent load benchmarks with the standalone script:

```bash
python scripts/load_test.py --concurrency 4 --requests 20
```

## Performance Benchmarks & Metrics

### Key Metrics Defined

Given:
- `t_send`: timestamp when request is sent
- `t_first`: timestamp when the first token is received
- `t_end`: timestamp when the stream closes
- `N`: total number of generated tokens

The evaluated metrics are:

```text
TTFT (Time-to-First-Token) = t_first - t_send
TTFT = T_queue + T_load + T_prefill

TPOT (Time-Per-Output-Token) = (t_end - t_first) / (N - 1)

E2E Latency = TTFT + TPOT × (N - 1)

Throughput = eval_count / (eval_duration × 1e-9)
```

Streaming responses are essential for measuring TTFT. In non-streaming mode, all tokens arrive together in a single batch, making TTFT impossible to measure.

Histogram buckets are spaced from 0.25s to 120s to accurately calculate p50/p95 percentiles across long LLM generations, rather than relying on default web service buckets.

### Benchmark Results

| Metric | 1 Sequential Request | 3 Concurrent Requests |
| :-- | --: | --: |
| TTFT p50 | 0.5 s | 35.0 s |
| TTFT p95 | — | 57.0 s |
| E2E Latency p95 | 17.0 s | 86.0 s |
| Generation Rate | 3.80 tokens/s | 3.28 tokens/s |

- **TTFT p50 increases 70x** (from 0.5s to 35.0s).
- **E2E Latency p95 increases ~5x** (from 17s to 86s).
- **Generation throughput remains roughly constant** (~3.8 to 3.28 tokens/s).

These results highlight a heavily CPU-constrained environment: under concurrency, queuing and prefill delays surge while aggregate computation capacity is already saturated.

### Interpretation via Little's Law

Little's Law states:

```text
N = X × R
```

Where:
- `N` is the average number of requests in the system;
- `X` is request throughput (requests/second);
- `R` is average response time (latency).

When arrival rate exceeds execution capacity on a single CPU core, requests accumulate in the wait queue, leading directly to inflated TTFT and end-to-end latency without boosting throughput.

### Cold Start Timings

- First request after cold startup: **~123 seconds**
- Subsequent warm requests: **~0.5 seconds**

The cold-start delay corresponds to reading model weights from disk and loading them into RAM. The PersistentVolumeClaim avoids re-downloading gigabytes of model weights over the network across pod recreations.

### Production Alternatives

Production inference engines like **vLLM** overcome Ollama's CPU concurrency limits by using **continuous batching** and **PagedAttention** for dynamic KV cache management.

## Repository Structure

```text
.
├── k8s/               # Kubernetes manifests (numbered in execution order)
├── exporter/          # Custom Prometheus active probe & Dockerfile
├── prometheus/        # Scraping targets configuration
├── grafana/           # Provisioned data sources and dashboard JSON
├── scripts/           # Concurrent CLI load generator (load_test.py)
├── report/            # LaTeX project report & PDF
├── images/            # Screenshots and architecture diagrams
├── docker-compose.yaml# Multi-service local definition
└── Dockerfile         # Base container definition
```

## Technical Decisions

### 1B Parameter Models vs Assignment Defaults
The evaluation rubric suggests `gemma3:4b`, requiring ~4 GB of memory. On a test machine with 7.7 GB of total RAM (~7 GB in use by system and desktop apps), the 4B model caused severe swap trashing (7.2 GB swap) and Docker daemon HTTP 500 errors.

Efficient 1B variants were selected:
- `gemma3:1b` (~815 MB): ~6.1 tokens/s
- `llama3.2:1b` (~1.3 GB): ~4.2 tokens/s

On systems with 16+ GB of RAM, larger models can be pulled directly without changing any deployment files.

### `imagePullPolicy: IfNotPresent`
When a tag is `:latest`, Kubernetes defaults to `Always`, triggering redundant ~9 GB pulls on every restart. Explicitly setting `IfNotPresent` caches images locally on the Minikube node.

### `strategy: Recreate` on Deployments
Volumes are bound as `ReadWriteOnce` (RWO). Rolling updates attempt to launch the new pod before terminating the old one, leading to volume mount lock conflicts. `Recreate` ensures the terminating pod releases the PVC before the new pod attempts to attach it.

### `OLLAMA_KEEP_ALIVE: "5m"`
Default Ollama configuration retains weights in memory for 24 hours. A 5-minute timeout unloads models shortly after usage, preventing dual model residency on memory-limited hosts.

## Incidents and Resolutions

### 1. Open WebUI Unhealthy Despite HTTP 200
- **Symptom:** Open WebUI remained `unhealthy` for 27 consecutive probes while responding 200 OK.
- **Root Cause:** Container lacked `wget` (`exec: "wget": executable file not found in $PATH`).
- **Fix:** Switched healthcheck to `curl` with a 600s `start_period` for initial embedding model setup.

### 2. Gemma Rejecting Prompts via WebUI ("does not support tools")
- **Symptom:** Direct Ollama API calls succeeded, but Open WebUI returned `gemma3:1b does not support tools`.
- **Root Cause:** Open WebUI injected function-calling tool schemas by default. Gemma template did not declare native tool support.
- **Fix:** Set Open WebUI Function Calling to **Default / Legacy**.

### 3. Open WebUI Pod OOMKilled in CrashLoopBackOff
- **Symptom:** Exit code 137 (`OOMKilled`) and startup probe failures on Minikube.
- **Root Cause:** 1 Gi memory limit was insufficient during embedding model initialization, and startup probe timeout was too aggressive (5 minutes).
- **Fix:** Increased memory limit to `2Gi` and extended startup failure threshold to 60 (10 minutes).

### 4. Ollama Pod Killed During Initial Model Load
- **Symptom:** Pod restarted with Exit Code 0 when receiving its first inference request.
- **Root Cause:** Heavy disk I/O while loading 1.3 GB weights caused the liveness probe to timeout (`context deadline exceeded`).
- **Fix:** Increased liveness probe `timeoutSeconds: 10` and `failureThreshold: 9` (~3 min grace period) while keeping readiness probe strict.

### 5. Ingress Admission Webhook Rejection
- **Symptom:** `dial tcp 10.106.248.201:443: connect: connection refused` on applying Ingress.
- **Root Cause:** `minikube addons enable ingress` returns before the webhook controller is ready to inspect objects.
- **Fix:** Scripted wait until `ingress-nginx-controller` is `1/1 Running` before applying Ingress manifest.

### 6. cAdvisor Container Scraping Incompatibility
- **Symptom:** cAdvisor reported `failed to identify the read-write layer ID` and missed container metrics.
- **Root Cause:** Docker daemon used `containerd.snapshotter.v1`, removing `/var/lib/docker/image/overlayfs/layerdb`.
- **Fix:** Replaced cAdvisor with targeted application probes in Prometheus, removing the only privileged container.

---

## Author

- **Mouhamadou Aliou BA** ([@Alioukun221](https://github.com/Alioukun221))

