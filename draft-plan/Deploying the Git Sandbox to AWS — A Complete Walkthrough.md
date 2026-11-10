# Deploying the Git Sandbox to AWS — A Complete Walkthrough

Nov 8, 2026 · @P

## The mental map

By the end you will have the Git Sandbox running as a public HTTPS site: two containers on ECS Fargate in two data centres, behind a load balancer and a CDN, with sessions in Redis, autoscaling, alarms, and a GitHub Actions pipeline that ships every merge to `main`.

Every deployment, of any project, answers the same seven questions. Learn these and every cloud diagram you ever see becomes readable:

1. **Where does the code run?** Compute: a VM, a container service, or functions.
2. **How is it packaged?** A container image, stored in a registry.
3. **How do requests find it?** DNS → CDN → load balancer → a healthy copy of the app.
4. **How is it protected?** Private networks, firewall rules, least-privilege permissions, TLS, a web application firewall.
5. **Where does state live?** Outside the app process, in a cache or database, so copies of the app are interchangeable.
6. **How do I know it is healthy?** Logs, metrics, health checks and alarms.
7. **How do changes get there safely?** Infrastructure as code plus a CI/CD pipeline.

&#91;embedded content: target architecture · request path and deploy path\]

Requests flow down the left: only cache misses and `/api/*` calls enter the VPC, and only the load balancer is reachable from outside it. Deploys flow down the right: GitHub Actions pushes an image to ECR, then tells ECS to roll it out.

**How to work through it.** Each phase builds on the one before and ends with a checkpoint you can verify. Do Phases 0–10 by hand once, in the console and CLI, because clicking through each screen is how the concepts stick. Then Phase 11 rebuilds the same stack as code, and you delete the hand-built copy. Commands are bash; on Windows run them in WSL, Git Bash or AWS CloudShell.

## Know your app before you deploy it

The Git Sandbox will deploy fine as-is, but it will behave wrongly the moment a second person or a second server shows up. Finding that before you touch AWS is the most important lesson in this guide.

**What it is.** A FastAPI app served by uvicorn. `GET /` returns `index.html`; the browser then calls four JSON endpoints: `/api/exercises`, `/api/state`, `/api/command` and `/api/exercise/{id}`. No database, no secrets, no files written to disk.

**The catch is one line in `main.py`:**

```python
# One in-memory lab for this local, single-user app.
lab = Lab()
```

That global object holds the whole simulated repository. On the cloud it causes three failures:

| Situation | What happens | Why |
| --- | --- | --- |
| Two users open the site | They type into the same repo and see each other's commits | One `Lab` per process, shared by everyone |
| Two containers behind a load balancer | Your `git commit` lands on container A, the next `git log` hits container B and shows nothing | Each process has its own memory |
| A deploy or autoscaling event | Everyone's progress vanishes | Containers are replaced; memory dies with them |

**The fix is the core idea of cloud apps: make the process stateless.** Give each browser a session ID in a cookie, and keep that session's lab in a shared store (Redis) that every container can read. Then any copy of the app can answer any request, and you can add, remove or replace copies freely. Phase 1 makes that change.

**"Cloud-ready" in one checklist** (a condensed version of the [Twelve-Factor App](https://12factor.net/) rules):

- Listens on `0.0.0.0` and a port from config, not `127.0.0.1:8000` hard-coded
- Reads all config (Redis URL, flags) from environment variables
- Keeps no state in memory or on local disk that matters after a restart
- Logs to stdout; the platform collects it
- Exposes a cheap health endpoint
- Starts fast and shuts down cleanly on `SIGTERM`
- Ships as one immutable artifact (a container image) that runs the same everywhere

## Core concepts, in plain words

These twenty terms cover almost every deployment conversation; skim now, come back when a phase uses one.

| Concept | What it is | Why you care | On AWS |
| --- | --- | --- | --- |
| Region | A geographic cluster of data centres | Pick one near your users; most resources live in one region | `ap-south-1` (Mumbai), `us-east-1` (N. Virginia) |
| Availability Zone (AZ) | One or more isolated data centres inside a region | Run copies in 2+ AZs so one outage doesn't take you down | `ap-south-1a`, `ap-south-1b` |
| VPC | Your private network inside a region | Everything with an IP lives in one | Amazon VPC |
| Subnet | A slice of the VPC's IP range, pinned to one AZ | *Public* subnets route to the internet; *private* ones don't | Subnets + route tables |
| Internet Gateway | The VPC's door to the internet | Public subnets route `0.0.0.0/0` through it | IGW (free) |
| NAT Gateway | Lets private resources call *out* without being reachable *in* | Private containers need it (or VPC endpoints) to pull images | NAT Gateway |
| Security Group | A stateful firewall attached to a resource | "Only the load balancer may talk to the app on port 8000" | Security Groups |
| IAM | Who (person or service) may do what to which resource | Every AWS action is an IAM check; least privilege limits blast radius | IAM, IAM Identity Center |
| Container image | Your app plus its OS libraries, frozen into one artifact | Same bits on your laptop, in CI and in production | Docker / OCI image |
| Registry | A store for images | The platform pulls images from here | Amazon ECR |
| Orchestrator | Runs N copies of a container, restarts failures, rolls out new versions | You declare "2 copies of v42"; it makes it true | Amazon ECS (or EKS) |
| Serverless compute | You pick CPU/RAM per container; AWS owns the servers | No OS patching, pay per second | AWS Fargate |
| Load balancer | One stable entry point that spreads requests across healthy copies | Enables scaling, zero-downtime deploys, failover | Application Load Balancer (ALB) |
| Health check | A URL the load balancer polls to decide if a copy may get traffic | Broken copies are pulled out automatically | ALB target group health check |
| DNS | Turns `sandbox.example.com` into an address | Users never see AWS hostnames | Route 53 |
| TLS certificate | Proves your identity and encrypts traffic (HTTPS) | Browsers require it; free on AWS | AWS Certificate Manager (ACM) |
| CDN | Edge servers worldwide that cache responses near users | Lower latency, absorbs spikes and attacks | Amazon CloudFront |
| Cache / key-value store | Very fast in-memory storage | Shared state and repeated-work shortcuts | ElastiCache (Valkey / Redis OSS) |
| WAF | Filters malicious or abusive HTTP requests | Rate limits and blocks common attacks | AWS WAF |
| Observability | Logs (what happened), metrics (how much), traces (where time went) | You can't fix what you can't see | CloudWatch, X-Ray |
| Infrastructure as Code | Your infrastructure written as reviewable, versioned code | Rebuild any environment from scratch, identically | CloudFormation, CDK, Terraform |
| CI/CD | Automation that tests, builds and deploys every change | Deploys become boring and frequent | GitHub Actions, CodePipeline |

**One distinction trips up everyone:** a *stateless* app keeps nothing it needs between requests in its own memory. Stateless apps can be scaled, replaced and load-balanced freely. Stateful pieces (databases, caches, session stores) are run separately, usually as managed services.

## Choosing the architecture

We use **ECS on Fargate behind an Application Load Balancer, fronted by CloudFront, with ElastiCache Serverless (Valkey) for sessions**. It is the mainstream AWS answer for a containerised web app: no servers to patch, every concept in this guide shows up, and it scales from this toy to real products.

| Option | You manage | Good for | Why not the main path here |
| --- | --- | --- | --- |
| EC2 (a virtual machine) | OS, patches, Python, process manager, scaling | Learning Linux; odd workloads | You'd hand-build what ECS gives you; doesn't scale cleanly |
| Lightsail | A small VM with fixed monthly price | Simple blogs and side projects | Hides the concepts (VPC, IAM, ALB) you want to learn |
| App Runner | Almost nothing | Was the "just run my container" service | [Closed to new customers since 30 April 2026](https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html); AWS points to ECS Express Mode instead |
| **ECS Express Mode** | An image + two IAM roles | Fastest working HTTPS URL | Great first win (below), but it builds the network and load balancer for you, so you learn less |
| **ECS on Fargate, built by hand** | Task definitions, services, networking | Most containerised web apps | **Our choice** |
| EKS (Kubernetes) | A Kubernetes cluster plus everything above | Large teams, multi-cloud portability | Large learning curve and a cluster fee for one small app |
| Lambda | Functions, not servers | Spiky, event-driven APIs | Possible with an adapter, but the stateless-container lesson transfers to more projects |

**Optional first win: ECS Express Mode.** Launched in [November 2025](https://aws.amazon.com/about-aws/whats-new/2025/11/announcing-amazon-ecs-express-mode), it takes a container image plus a task execution role and an infrastructure role, and creates a Fargate service, an ALB with HTTPS, autoscaling and a generated URL, all as normal resources in your account ([AWS Containers blog](https://aws.amazon.com/blogs/containers/extending-amazon-ecs-express-mode-to-build-an-optimal-container-environment/)). There is no extra charge beyond those underlying resources. Once you finish Phase 2 (image in ECR), creating an Express service is a ten-click way to see the app live; then open each resource it made and compare it with what Phases 3–6 build by hand. Delete it before continuing.

**The target architecture:**

- **Region:** `ap-south-1` (Mumbai), the closest to you in India. Any region works; keep everything in one.
- **Network:** one VPC across 2 AZs. Public subnets hold only the load balancer and NAT gateway; private subnets hold the containers and Redis.
- **Compute:** an ECS cluster running 2–6 Fargate tasks (0.5 vCPU, 1 GB each) of one image from ECR.
- **Traffic:** Route 53 DNS → CloudFront (CDN, WAF, HTTPS at the edge) → ALB (HTTPS, health checks) → tasks on port 8000.
- **State:** ElastiCache Serverless for Valkey (a Redis-compatible engine) keyed by session cookie.
- **Operations:** CloudWatch logs, metrics and alarms; Application Auto Scaling; GitHub Actions with OIDC for deploys; AWS CDK for infrastructure as code.

## Phase 0 — Secure the AWS account

Before building anything, lock down the root login, create a day-to-day admin identity and set a budget alarm. These four steps prevent the two classic disasters: a stolen root password and a surprise bill.

1. **Protect root.** Sign in as root once, turn on MFA (an authenticator app or passkey), and never use root again except for billing emergencies. Delete any root access keys.
2. **Create your everyday identity with IAM Identity Center.** Enable Identity Center, create a user for yourself, and give it the `AdministratorAccess` permission set. You'll sign in through the Identity Center portal URL and get short-lived credentials, never long-lived access keys.
3. **Set a budget alarm.** Billing → Budgets → *Create budget* → monthly cost budget of, say, $50 with email alerts at 50%, 80% and 100% (actual) and 100% (forecasted). Also enable *Free Tier usage alerts* in Billing preferences.
4. **Pick and pin a region.** Choose `ap-south-1` in the console's region menu and stay there. Most "I can't find my resource" moments are the wrong region.

**Credits.** Accounts created after 15 July 2025 get [$100 in credits at sign-up and can earn up to $100 more](https://aws.amazon.com/free), usable for up to 6 months. On the *Free* account plan some services are restricted and the account closes when the plan ends; upgrade to the *Paid* plan (credits still apply) before starting, since this stack uses several of them.

**Install and configure the CLI:**

```bash
# Install AWS CLI v2 (see the AWS docs for your OS), then:
aws configure sso            # session name: sandbox, region: ap-south-1
aws sso login --profile sandbox
export AWS_PROFILE=sandbox   # PowerShell: $env:AWS_PROFILE="sandbox"
aws sts get-caller-identity  # prints your account ID and role — the checkpoint
```

Also install **Docker Desktop** (with WSL 2 on Windows) and **Node.js 20+** (needed later for the CDK CLI).

**Tag everything** you create with `project=git-sandbox`. Tags let you find every resource for teardown and split the bill by project in Cost Explorer.

**Checkpoint:** `aws sts get-caller-identity` works without any access keys stored on disk, and a budget alert email address is confirmed.

## Phase 1 — Make the app cloud-ready

This phase is all on your laptop: give every browser its own lab stored in Redis, add a health check and cache headers, package it as a container, and prove it survives a restart. Nothing here costs money.

### 1.1 Add the Redis client

```bash
uv add redis
```

### 1.2 Decide what to store per session

The `Sandbox` object is plain dicts, lists and dataclasses, so Python's `pickle` can serialise it. We store `{exercise_id, sandbox}` rather than the whole `Lab`, because `Lab` holds a thread lock and exercise objects hold check functions, neither of which should be serialised.

### 1.3 Replace `git_sandbox_app/main.py`

```python
import os
import pickle
import threading
import uuid
from functools import lru_cache
from pathlib import Path

import redis
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .exercises import EXERCISE_BY_ID, EXERCISES, Lab

app = FastAPI(title="Interactive Git Sandbox")
INDEX_HTML = Path(__file__).with_name("index.html")

# ---- configuration comes from the environment (12-factor) ----
REDIS_URL = os.getenv("REDIS_URL")                 # unset locally -> in-memory store
SESSION_TTL = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
APP_VERSION = os.getenv("APP_VERSION", "dev")      # CI sets this to the git SHA

store = redis.Redis.from_url(REDIS_URL, socket_timeout=2) if REDIS_URL else None
_memory: dict[str, bytes] = {}
_memory_lock = threading.Lock()


class CommandRequest(BaseModel):
    command: str = Field(max_length=500)           # cap input size


# ---- sessions: one cookie per browser, one lab per cookie ----
def session_id(request: Request, response: Response) -> str:
    sid = request.cookies.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        response.set_cookie("sid", sid, max_age=SESSION_TTL, httponly=True,
                            secure=COOKIE_SECURE, samesite="lax")
    return sid


def _key(sid: str) -> str:
    return f"lab:v1:{sid}"


def load_lab(sid: str) -> Lab:
    raw = store.get(_key(sid)) if store else _memory.get(sid)
    lab = Lab()
    if raw:
        try:
            state = pickle.loads(raw)               # only ever data this app wrote
            lab.sandbox = state["sandbox"]
            lab.exercise = EXERCISE_BY_ID[state["exercise_id"]]
        except Exception:
            pass                                    # unreadable old state -> fresh lab
    return lab


def save_lab(sid: str, lab: Lab) -> None:
    raw = pickle.dumps({"exercise_id": lab.exercise.id, "sandbox": lab.sandbox})
    if store:
        store.set(_key(sid), raw, ex=SESSION_TTL)  # TTL: abandoned sessions expire
    else:
        _memory[sid] = raw


def session_lock(sid: str):
    # A distributed lock: two tabs firing commands at once can't corrupt the lab,
    # even when the requests land on different containers.
    return store.lock(f"lock:{sid}", timeout=10, blocking_timeout=5) if store else _memory_lock


# ---- cache-aside: exercise starting states are identical for everyone ----
@lru_cache(maxsize=64)                              # layer 1: this process's memory
def exercise_template(exercise_id: str) -> bytes:
    key = f"template:{APP_VERSION}:{exercise_id}"  # versioned key = free invalidation
    if store and (hit := store.get(key)):           # layer 2: shared Redis
        return hit
    lab = Lab()
    lab.load(exercise_id)                           # miss: do the real work
    raw = pickle.dumps(lab.sandbox)
    if store:
        store.set(key, raw, ex=86400)
    return raw


# ---- HTTP caching: safe default is "never cache"; opt in per route ----
@app.middleware("http")
async def default_no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": APP_VERSION}   # cheap: no Redis call


@app.get("/", response_class=HTMLResponse)
def dashboard(response: Response) -> str:
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=300"
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/api/exercises")
def list_exercises(response: Response) -> list[dict]:
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=86400"
    return [{"id": e.id, "group": e.group, "title": e.title, "area": e.area} for e in EXERCISES]


@app.get("/api/state")
def get_state(sid: str = Depends(session_id)) -> dict:
    return load_lab(sid).payload()


@app.post("/api/command")
def run_command(request: CommandRequest, sid: str = Depends(session_id)) -> dict:
    with session_lock(sid):
        lab = load_lab(sid)
        output = lab.run(request.command)
        save_lab(sid, lab)
    return {"output": output, **lab.payload()}


@app.post("/api/exercise/{exercise_id}")
def load_exercise(exercise_id: str, sid: str = Depends(session_id)) -> dict:
    if exercise_id not in EXERCISE_BY_ID:
        raise HTTPException(status_code=404, detail=f"Unknown exercise '{exercise_id}'")
    lab = Lab()
    lab.sandbox = pickle.loads(exercise_template(exercise_id))
    lab.exercise = EXERCISE_BY_ID[exercise_id]
    with session_lock(sid):
        save_lab(sid, lab)
    return lab.payload()
```

What each new piece teaches:

- **Session cookie** — identity without login. `HttpOnly` hides it from JavaScript; `Secure` sends it only over HTTPS.
- **TTL on keys** — abandoned labs delete themselves after a day, so the store never fills up.
- **Distributed lock** — a per-process lock stops working once you have two processes.
- **`/healthz`** — a *liveness* check: "is this process up?" It deliberately skips Redis, so a Redis blip doesn't make the load balancer kill every healthy container at once.
- **Cache-aside** — check the cache, on a miss compute and store. Versioning the key with `APP_VERSION` means a new deploy never serves stale templates, with no invalidation code.
- **`Cache-Control`** — per-user responses say `no-store`; shared ones say how long browsers (`max-age`) and the CDN (`s-maxage`) may keep them. Phase 7 relies on this.

### 1.4 Add a `Dockerfile` at the repo root

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv, pinned to the same minor version as uv_build in pyproject.toml
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 1) dependencies first: this layer is cached until uv.lock changes
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# 2) then the code (changes often, rebuilds in seconds)
#    if your package lives under src/, copy src/git_sandbox_app instead
COPY git_sandbox_app ./git_sandbox_app

# 3) never run as root inside the container
RUN useradd --create-home --uid 10001 app
USER app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

# 0.0.0.0 = reachable from outside the container; proxy headers = trust the ALB's X-Forwarded-*
CMD ["uvicorn", "git_sandbox_app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "20"]
```

Match the `python:3.12` tag to your `.python-version`. Add a `.dockerignore` so local junk never enters the image:

```
.venv
.git
__pycache__/
*.pyc
tests/
assets/
```

### 1.5 Run the real topology locally with `compose.yaml`

```yaml
services:
  app:
    build: .
    ports: ["8000:8000"]
    environment:
      REDIS_URL: redis://cache:6379/0
      COOKIE_SECURE: "0"          # localhost is plain HTTP
    depends_on: [cache]
  cache:
    image: valkey/valkey:8
```

```bash
docker compose up --build
```

**Checkpoint (three tests):**

1. Open http://localhost:8000 in a normal window and an incognito window. Commit in one; the other stays empty. *Sessions are isolated.*
2. Run `docker compose restart app`, refresh. Your commits are still there. *State lives outside the process.*
3. `curl -i localhost:8000/api/state` shows `Cache-Control: no-store` and a `set-cookie: sid=…` header; `curl -i localhost:8000/api/exercises` shows the long `s-maxage`. *Cache rules are in place.*

Run your existing tests too (`python tests/test_engine.py`); the engine itself didn't change.

## Phase 2 — Push the image to Amazon ECR

ECR is a private Docker registry in your account; ECS will pull from it. Tag each image with the git commit it came from, so "what is running?" always has an exact answer.

```bash
export AWS_REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export REPO=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/git-sandbox

# 1. Create the repository: scan on push, and tags can never be overwritten
aws ecr create-repository --repository-name git-sandbox \
  --image-scanning-configuration scanOnPush=true \
  --image-tag-mutability IMMUTABLE \
  --tags Key=project,Value=git-sandbox

# 2. Let Docker log in to ECR (token lasts 12 hours)
aws ecr get-login-password | docker login --username AWS --password-stdin ${REPO%/*}

# 3. Build for the platform Fargate will run, tag with the commit, push
export TAG=$(git rev-parse --short HEAD)
docker buildx build --platform linux/amd64 -t $REPO:$TAG --push .
```

Add a **lifecycle policy** so old images don't pile up (ECR charges for storage): ECR → *git-sandbox* → Lifecycle policy → rule "keep the last 20 images".

Two habits worth forming now:

- **Immutable tags, no `latest` in production.** `latest` silently changes meaning; `a1b2c3d` never does, which makes rollbacks a one-line change.
- **Build once, deploy many.** The same image tag moves from staging to production. You never rebuild for production.

`linux/amd64` keeps the first pass simple. Later you can switch to `linux/arm64` (Graviton), which [costs about 20% less per vCPU-hour on Fargate](https://aws.amazon.com/fargate/pricing/); the task definition must then say `ARM64` too.

**Checkpoint:** ECR console shows `git-sandbox:<sha>` with a vulnerability scan result. Review any *Critical* findings (usually fixed by a newer base image).

## Phase 3 — Build the network and the session store

The network's job is to make the app reachable only through the load balancer, and Redis reachable only from the app. You get that with public and private subnets in two AZs and a chain of security groups that each trust only the layer in front.

### 3.1 Create the VPC

VPC console → *Create VPC* → **VPC and more**, then set:

- Name: `git-sandbox`; IPv4 CIDR `10.0.0.0/16` (65,536 addresses for the whole VPC)
- Availability Zones: **2**; public subnets: **2**; private subnets: **2**
- NAT gateways: **In 1 AZ** (production: 1 per AZ)
- VPC endpoints: **S3 Gateway** (free, and ECR image layers are served from S3, so image pulls skip the NAT's per-GB fee)

The wizard creates this layout:

| Subnet | AZ | CIDR | Default route `0.0.0.0/0` goes to | Holds |
| --- | --- | --- | --- | --- |
| public-a | ap-south-1a | 10.0.0.0/24 | Internet Gateway | ALB node, NAT gateway |
| public-b | ap-south-1b | 10.0.1.0/24 | Internet Gateway | ALB node |
| private-a | ap-south-1a | 10.0.128.0/20 | NAT gateway | Fargate tasks, Redis |
| private-b | ap-south-1b | 10.0.144.0/20 | NAT gateway | Fargate tasks, Redis |

(The wizard picks its own CIDRs; yours may differ. What matters is which route table each subnet uses.) **"Public" vs "private" is only a routing decision**: a subnet is public if its route table sends internet traffic to the Internet Gateway.

**Why private tasks still need a way out:** they must pull images from ECR and send logs to CloudWatch, both of which are internet-facing AWS endpoints. Three ways to give them that:

| Option | Rough monthly cost (us-east-1 rates) | Trade-off |
| --- | --- | --- |
| NAT gateway in 1 AZ | [$0.045/hour ≈ $33, plus $0.045/GB](https://aws.amazon.com/vpc/pricing/) | Standard; the one AZ is a single point of failure for outbound calls |
| Interface VPC endpoints for ECR (2), CloudWatch Logs | \~$0.01/hour per endpoint per AZ ≈ $44 for 3 endpoints × 2 AZs | Traffic never leaves AWS; worth it at scale or when compliance requires it |
| Tasks in public subnets with public IPs | [$0.005/hour per IPv4 address](https://aws.amazon.com/blogs/aws/new-aws-public-ipv4-address-charge-public-ip-insights) ≈ $3.65 per task | Cheapest; safe only because the security group still admits the ALB alone |

This guide uses the NAT gateway. If you're cost-sensitive, the public-subnet option is a legitimate learning shortcut: set *Public IP: on* when creating the service and put tasks in the public subnets.

### 3.2 Create three security groups

EC2 console → Security Groups, all in the `git-sandbox` VPC. Each one references the previous group's **ID**, not an IP range, so the rules keep working as tasks come and go.

| Security group | Inbound rule | Source |
| --- | --- | --- |
| `alb-sg` | TCP 443 and 80 | `0.0.0.0/0` for now; Phase 7 narrows this to CloudFront only |
| `app-sg` | TCP 8000 | `alb-sg` |
| `cache-sg` | TCP 6379 | `app-sg` |

Leave outbound as "all traffic". Security groups are *stateful*: replies to allowed requests are let back in automatically.

&#91;embedded content: network layout · 2 AZs, 4 subnets, 3 security groups\]

Each arrow is the only inbound path its security group allows. Private tasks reach ECR and CloudWatch outbound through the NAT gateway in AZ-a; nothing on the internet can open a connection to them.

### 3.3 Create the session store (ElastiCache Serverless for Valkey)

ElastiCache console → *Create cache* → engine **Valkey** → **Serverless** → name `git-sandbox` → VPC `git-sandbox`, the two **private** subnets, security group `cache-sg`. Valkey is the open-source fork of Redis; `redis-py` talks to it unchanged.

Serverless means no node sizes to pick; you pay for data stored and requests. Valkey's serverless [minimum is 100 MB of metered storage, about $6 a month](https://aws.amazon.com/blogs/database/amazon-elasticache-version-8-0-for-valkey-brings-faster-scaling-and-improved-memory-efficiency), versus a 1 GB minimum on the Redis OSS engine.

When it shows *Available*, copy the endpoint. Serverless caches **require TLS**, so the app's URL uses `rediss://` (two s's):

```
REDIS_URL=rediss://git-sandbox-xxxxxx.serverless.aps1.cache.amazonaws.com:6379/0
```

**Checkpoint:** the VPC resource map shows 4 subnets, 1 NAT gateway and 3 route tables; the cache is *Available*; the three security groups chain ALB → app → cache.

## Phase 4 — Run it on ECS Fargate

In this phase you describe the container to ECS and run one copy by hand to prove it starts; Phase 5 then wraps it in a long-running service behind the load balancer.

### 4.1 The four ECS nouns

| Noun | What it is | Analogy |
| --- | --- | --- |
| Cluster | A named grouping of services and tasks | A folder |
| Task definition | A versioned JSON blueprint: image, CPU/RAM, ports, env vars, roles, logging | A recipe (every edit creates revision `:2`, `:3`…) |
| Task | One running copy of a task definition | One dish made from the recipe |
| Service | Keeps *N* tasks running, replaces failures, rolls out new revisions, registers tasks with the load balancer | The kitchen manager |

### 4.2 Two IAM roles (the most common point of confusion)

- **Task execution role** — used by *ECS itself* before your code runs: pull the image from ECR, write logs, fetch secrets. The AWS managed policy `AmazonECSTaskExecutionRolePolicy` covers the first two.
- **Task role** — used by *your code* when it calls AWS APIs. This app calls none, so it starts empty. If the app later reads from S3, that permission goes here, never on the execution role.

```bash
cat > ecs-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
aws iam create-role --role-name ecsTaskExecutionRole --assume-role-policy-document file://ecs-trust.json
aws iam attach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam create-role --role-name gitSandboxTaskRole --assume-role-policy-document file://ecs-trust.json
```

The *trust policy* says who may assume the role (the ECS tasks service); the *permissions policy* says what the role may do.

### 4.3 Log group, cluster and task definition

```bash
aws logs create-log-group --log-group-name /ecs/git-sandbox
aws logs put-retention-policy --log-group-name /ecs/git-sandbox --retention-in-days 14
aws ecs create-cluster --cluster-name git-sandbox --tags key=project,value=git-sandbox
```

Set a retention period on every log group; the default is "keep forever" and you pay for storage forever. Save this as `taskdef.json`, replacing `ACCOUNT_ID`, `TAG` and the Redis endpoint:

```json
{
  "family": "git-sandbox",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "runtimePlatform": { "cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX" },
  "executionRoleArn": "arn:aws:iam::ACCOUNT_ID:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::ACCOUNT_ID:role/gitSandboxTaskRole",
  "containerDefinitions": [
    {
      "name": "web",
      "image": "ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/git-sandbox:TAG",
      "essential": true,
      "portMappings": [{ "containerPort": 8000, "protocol": "tcp" }],
      "environment": [
        { "name": "REDIS_URL", "value": "rediss://YOUR-CACHE-ENDPOINT:6379/0" },
        { "name": "APP_VERSION", "value": "TAG" }
      ],
      "readonlyRootFilesystem": true,
      "stopTimeout": 30,
      "healthCheck": {
        "command": ["CMD", "python", "-c",
          "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=2).status == 200 else 1)"],
        "interval": 15, "timeout": 5, "retries": 3, "startPeriod": 10
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/git-sandbox",
          "awslogs-region": "ap-south-1",
          "awslogs-stream-prefix": "web"
        }
      }
    }
  ]
}
```

```bash
aws ecs register-task-definition --cli-input-json file://taskdef.json
```

Why these settings:

- **`awsvpc` networking** gives each task its own network interface and private IP, so security groups apply per task. Fargate requires it.
- **0.5 vCPU / 1 GB** suits one uvicorn process. Scale *out* (more tasks) rather than *up* (bigger tasks or more workers); it's simpler and survives an AZ failure.
- **Container health check** uses Python because the slim image has no `curl`.
- **`readonlyRootFilesystem`** means an attacker who gets code execution can't drop files into the image. The app writes nothing to disk, so it costs nothing.
- **`stopTimeout: 30`** gives uvicorn time to finish in-flight requests after `SIGTERM` before ECS sends `SIGKILL`.

### 4.4 Smoke-test one task

```bash
aws ecs run-task --cluster git-sandbox --launch-type FARGATE --task-definition git-sandbox \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-PRIVATE_A],securityGroups=[sg-APP],assignPublicIp=DISABLED}"

aws logs tail /ecs/git-sandbox --follow     # wait for: Uvicorn running on http://0.0.0.0:8000
```

**Checkpoint:** the task reaches `RUNNING` and its health shows `HEALTHY` in the ECS console. Then stop it (`aws ecs stop-task --cluster git-sandbox --task <task-id>`). If it stops with `CannotPullContainerError`, the private subnet has no route out: see Troubleshooting.

## Phase 5 — Load balancing with an Application Load Balancer

The ALB gives your app one stable address and spreads requests across every healthy task in both AZs. Because Phase 1 made the app stateless, any task can serve any request, so no "sticky sessions" are needed.

### 5.1 Concepts

- **Layer 7 vs layer 4.** An *Application* Load Balancer understands HTTP: it can route by path or host, terminate TLS, and add `X-Forwarded-For`/`-Proto` headers. A *Network* Load Balancer works at TCP/UDP level: faster and with static IPs, but blind to HTTP. Web apps use ALBs.
- **The ALB's parts.** Load balancer (lives in the public subnets) → **listener** (a port + protocol, e.g. HTTPS:443) → **rules** (if path is `/api/*`…) → **target group** (a pool of task IPs plus a health check) → **targets** (your tasks, registered automatically by the ECS service).
- **Algorithm.** Round robin is the default. *Least outstanding requests* sends each request to the target with the fewest in flight, which suits uneven request costs like `git merge` vs `git status`.
- **Health checks.** The ALB polls `/healthz` on each task; 2 passes = healthy, 3 failures = removed from rotation. Combined with ECS, unhealthy tasks are also replaced.
- **Connection draining (deregistration delay).** When a task is being removed, the ALB stops sending new requests but lets in-flight ones finish for N seconds. The default is 300; 30 is plenty here and makes deploys faster.
- **Cross-zone load balancing** is on by default for ALBs: a node in AZ-a can send traffic to a task in AZ-b, so uneven task counts per AZ still balance.
- **Sticky sessions** pin a user to one task with a cookie. They are a workaround for stateful apps, and they break when that task dies or scales in. Externalised state (what you did in Phase 1) is the real fix.

### 5.2 Create the target group and the ALB

```bash
aws elbv2 create-target-group --name git-sandbox-tg --vpc-id vpc-XXXX \
  --protocol HTTP --port 8000 --target-type ip \
  --health-check-path /healthz --health-check-interval-seconds 15 \
  --healthy-threshold-count 2 --unhealthy-threshold-count 3 --matcher HttpCode=200

aws elbv2 modify-target-group-attributes --target-group-arn $TG_ARN --attributes \
  Key=deregistration_delay.timeout_seconds,Value=30 \
  Key=load_balancing.algorithm.type,Value=least_outstanding_requests

aws elbv2 create-load-balancer --name git-sandbox-alb --type application \
  --scheme internet-facing --subnets subnet-PUBLIC_A subnet-PUBLIC_B --security-groups sg-ALB

# Temporary plain-HTTP listener; Phase 6 replaces it with HTTPS
aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN
```

`--target-type ip` is required for Fargate, because tasks are network interfaces, not EC2 instances.

### 5.3 Create the ECS service

```bash
aws ecs create-service --cluster git-sandbox --service-name web \
  --task-definition git-sandbox --desired-count 2 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-PRIVATE_A,subnet-PRIVATE_B],securityGroups=[sg-APP],assignPublicIp=DISABLED}" \
  --load-balancers "targetGroupArn=$TG_ARN,containerName=web,containerPort=8000" \
  --health-check-grace-period-seconds 30 \
  --deployment-configuration "minimumHealthyPercent=100,maximumPercent=200,deploymentCircuitBreaker={enable=true,rollback=true}" \
  --propagate-tags SERVICE --tags key=project,value=git-sandbox
```

- **Two subnets in two AZs** → ECS spreads the two tasks across AZs.
- **100% / 200%** → during a deploy, ECS starts new tasks *before* stopping old ones: zero downtime, at the cost of briefly running double.
- **Circuit breaker with rollback** → if new tasks keep failing health checks, ECS stops the deploy and returns to the last working revision on its own.
- **Grace period** → ignore health-check failures for 30 s while a new task boots.

**Checkpoint and three experiments:**

1. Open `http://<ALB DNS name>`. The target group shows **2 healthy targets** in different AZs.
2. Refresh `/healthz` and `/api/state` many times. Different tasks answer, yet your lab is identical every time. *That's shared state working.*
3. In the ECS console, stop one task. The site stays up; the ALB routes around it, and the service launches a replacement within a minute or two. *That's self-healing.*

## Phase 6 — Domain, DNS and HTTPS

Users should reach `https://sandbox.example.com`, never an AWS hostname, over HTTPS only. You need a domain, a Route 53 hosted zone, free TLS certificates from ACM, and an HTTPS listener on the ALB. Swap `example.com` for your domain throughout.

### 6.1 How DNS and TLS fit together

- **DNS:** the browser asks a resolver for `sandbox.example.com`; the resolver walks root → `.com` → your domain's name servers (Route 53) and gets back an address. Each answer is cached for its **TTL**, which is why DNS changes take minutes to spread.
- **Hosted zone:** Route 53's copy of your domain's records. Its four **NS** records must be set at your registrar, or nobody will ask Route 53.
- **Alias records:** a Route 53 extension that points a name at an AWS resource (ALB, CloudFront). Unlike a CNAME it works on the bare domain (`example.com`), follows the resource's changing IPs, and queries to AWS targets are free.
- **TLS termination:** the ALB (and later CloudFront) holds the certificate and decrypts HTTPS. Traffic from ALB to tasks is plain HTTP inside your private subnets, the common and accepted default.

### 6.2 Get a domain and hosted zone

Either register one in Route 53 (*Registered domains → Register*; the hosted zone is created for you), or keep your existing registrar: create a public hosted zone for the domain, then paste its four NS values into the registrar's name-server settings.

No domain? You can still finish the guide: skip 6.3–6.5 and use CloudFront's own `https://dxxxx.cloudfront.net` address in Phase 7, with CloudFront talking to the ALB over HTTP.

### 6.3 Request certificates

ACM → *Request public certificate* → names `example.com` and `*.example.com` → **DNS validation** → *Create records in Route 53*. Do this **twice**:

| Region | Used by | Why there |
| --- | --- | --- |
| `ap-south-1` | The ALB | A load balancer only uses certificates from its own region |
| `us-east-1` | CloudFront | CloudFront is global and only reads certificates from N. Virginia |

Public ACM certificates are free and renew themselves as long as the validation CNAME stays in DNS.

### 6.4 Switch the ALB to HTTPS

```bash
# HTTPS listener with a modern TLS policy (TLS 1.3 and 1.2 only)
aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTPS --port 443 \
  --certificates CertificateArn=$CERT_ARN_APS1 \
  --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN

# Turn the old HTTP listener into a permanent redirect
aws elbv2 modify-listener --listener-arn $HTTP_LISTENER_ARN \
  --default-actions 'Type=redirect,RedirectConfig={Protocol=HTTPS,Port=443,StatusCode=HTTP_301}'
```

### 6.5 Point a name at the ALB

Route 53 → hosted zone → *Create record*: name `origin`, type **A**, **Alias** on → *Alias to Application Load Balancer* → `ap-south-1` → `git-sandbox-alb`. This `origin.` name is what CloudFront will call in Phase 7; users will get `sandbox.` instead.

**Checkpoint:** `https://origin.example.com` loads with a padlock; `http://origin.example.com` redirects to it; `curl -I https://origin.example.com/healthz` returns `200`. In browser dev tools, the `sid` cookie is now marked *Secure*.

## Phase 7 — Caching at every layer

Caching means keeping a copy of a result closer to whoever needs it, so the expensive trip is skipped. This stack caches at four layers, and the one rule that matters most is: **never let a shared cache store a per-user response**, or one user sees another's data.

### 7.1 Vocabulary

- **Hit / miss / hit ratio** — served from cache, or not; the share of hits is your main cache metric.
- **TTL** — how long a copy is considered fresh.
- **Cache key** — what makes two requests "the same" (path, some headers, some cookies, query string). Too narrow a key leaks data between users; too wide a key means nothing ever hits.
- **Invalidation** — throwing copies away early. Versioned keys (`template:<sha>:<id>`) and versioned URLs (`app.3f9a.js`) avoid needing it.
- **Cache-aside** — the app checks the cache, and on a miss computes and stores the result (Phase 1's `exercise_template`).
- **Cache vs store** — a *cache* holds data you can recompute, so losing it is harmless. Our session labs are a *store*: losing Valkey means users restart their exercise. That's acceptable for a practice app; for data that must survive, use a database such as DynamoDB, which also supports TTL.

| Layer | Where | What it caches here | Controlled by | Freshness handled by |
| --- | --- | --- | --- | --- |
| Browser | User's device | `/` for 60 s, `/api/exercises` for 5 min | `Cache-Control: max-age` from the app | TTL |
| CDN edge | CloudFront locations worldwide | `/` for 5 min, `/api/exercises` for 1 day | `s-maxage` + CloudFront cache policies | TTL + invalidation on deploy |
| In-process | Each task's memory | Exercise templates | `functools.lru_cache` | Process restart (every deploy) |
| Shared | ElastiCache Valkey | Templates (cache) and session labs (store) | App code, key TTLs | Versioned keys, TTL |

### 7.2 Put CloudFront in front

CloudFront console → *Create distribution*:

- **Pricing plan:** the [flat-rate *Free* plan](https://aws.amazon.com/cloudfront/pricing/) ($0/month; 1 million requests and 100 GB a month, with AWS WAF and DDoS protection included) is plenty for this app. Note that [accounts on the Free account plan can't use flat-rate plans](https://docs.aws.amazon.com/PricingPlanManager/latest/UserGuide/plans.html); use pay-as-you-go there.
- **Origin:** `origin.example.com`, protocol **HTTPS only**. Add a custom origin header `X-Origin-Verify` with a long random value (`openssl rand -hex 32`).
- **Viewer:** *Redirect HTTP to HTTPS*; allowed methods **GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE** (the API uses POST); compress objects: **yes** (gzip/Brotli).
- **Settings:** alternate domain `sandbox.example.com`, the **us-east-1** certificate.

Then add behaviors. CloudFront evaluates them top-down and uses the first path match:

| Order | Path pattern | Cache policy | Origin request policy | Reason |
| --- | --- | --- | --- | --- |
| 0 | `/api/exercises` | `CachingOptimized` | none | Identical for every user |
| 1 | `/api/*` | `CachingDisabled` | `AllViewerExceptHostHeader` | Per-user; the `sid` cookie must reach the app |
| Default | `*` | `CachingOptimized` | none | `/` HTML; CloudFront honours the app's `s-maxage` |

`AllViewerExceptHostHeader` forwards cookies, headers and query strings but sends the origin's own hostname, so the ALB's certificate matches. The cacheable routes never set cookies, which keeps them safe to share.

Finally, Route 53 → records `sandbox` type **A** and **AAAA**, Alias → *CloudFront distribution*.

### 7.3 Make CloudFront the only way in

Otherwise attackers can bypass the CDN and WAF by calling the ALB directly. Use two locks:

1. **Network:** edit `alb-sg` — remove `0.0.0.0/0`, and allow TCP 443 from the AWS-managed prefix list `com.amazonaws.global.cloudfront.origin-facing`.
2. **Proof it's *your* distribution:** on the ALB's HTTPS listener, add a rule "if header `X-Origin-Verify` equals the secret → forward to target group", and change the default action to *fixed response 403*. The prefix list admits every CloudFront distribution in the world; the header admits only yours.

### 7.4 Invalidate on deploy

After each deploy (Phase 12 automates this):

```bash
aws cloudfront create-invalidation --distribution-id $DIST_ID --paths "/" "/api/exercises"
```

**Checkpoint:** run `curl -sI https://sandbox.example.com/api/exercises | grep -i x-cache` twice. The first says `Miss from cloudfront`, the second `Hit from cloudfront`, with a growing `age` header. `/api/state` never hits. `https://origin.example.com` from your laptop now returns 403 or times out.

## Phase 8 — Auto scaling and resilience

The service should add tasks when busy, remove them when quiet, never drop below two (one per AZ), and survive any single task, deploy or AZ failing. ECS plus Application Auto Scaling does this with two commands.

### 8.1 Concepts

- **Horizontal vs vertical.** Horizontal = more copies (what we do). Vertical = bigger copies; it has a ceiling and needs a restart.
- **Target tracking** — "keep average CPU near 60%"; AWS works out how many tasks that takes. Simplest and usually best. *Step scaling* reacts to alarm thresholds; *scheduled scaling* pre-scales for known peaks (a class starting at 10:00).
- **Cooldowns** — scale out fast (60 s), scale in slowly (300 s) so a brief lull doesn't cause flapping.
- **Min and max** — min 2 keeps one task per AZ; max is your cost ceiling.

### 8.2 Turn it on

```bash
aws application-autoscaling register-scalable-target --service-namespace ecs \
  --resource-id service/git-sandbox/web --scalable-dimension ecs:service:DesiredCount \
  --min-capacity 2 --max-capacity 6

aws application-autoscaling put-scaling-policy --service-namespace ecs \
  --resource-id service/git-sandbox/web --scalable-dimension ecs:service:DesiredCount \
  --policy-name cpu-60 --policy-type TargetTrackingScaling \
  --target-tracking-scaling-policy-configuration '{
    "TargetValue": 60,
    "PredefinedMetricSpecification": {"PredefinedMetricType": "ECSServiceAverageCPUUtilization"},
    "ScaleOutCooldown": 60, "ScaleInCooldown": 300 }'
```

You can add a second policy on `ALBRequestCountPerTarget` (requests per task per minute); ECS scales out if *either* policy asks. Pick its target value from the load test below, not by guessing.

### 8.3 Watch it scale

Run a load test from your laptop (install [`hey`](https://github.com/rakyll/hey) or k6). Run it **before** enabling the WAF rate limit in Phase 9, or the WAF will block you.

```bash
hey -z 5m -c 50 -m POST -H "Content-Type: application/json" -H "Cookie: sid=loadtest" \
  -d '{"command":"git log --oneline --all"}' https://sandbox.example.com/api/command
```

In the ECS console's *Metrics* and *Events* tabs, watch CPU climb, desired count rise toward 6, and then fall back to 2 a few minutes after the test ends. Note the requests per second and p95 latency `hey` reports at each task count; that's your capacity per task.

### 8.4 What already protects you, and how

| Failure | What handles it | Configured in |
| --- | --- | --- |
| A task crashes or hangs | ALB health check removes it; ECS replaces it | Phases 4–5 |
| A whole AZ goes down | The other AZ's task keeps serving; ECS launches replacements there | 2 subnets in 2 AZs |
| A bad deploy | Circuit breaker rolls back to the last good revision | Phase 5 |
| Deploy interrupts requests | New tasks start first; ALB drains old ones for 30 s; uvicorn finishes in-flight work on `SIGTERM` | 200% max, deregistration delay, `stopTimeout` |
| Redis is slow or down | `socket_timeout=2` fails fast instead of hanging every request | Phase 1 |
| Traffic spike | Scale-out policy; CloudFront serves cached routes without touching the ALB | This phase, Phase 7 |

**Saving money with Fargate Spot.** Spot uses AWS's spare capacity at [roughly 70% below on-demand prices](https://aws.amazon.com/fargate/pricing/), but tasks can be reclaimed with a two-minute warning. A capacity provider strategy of *base 2 on `FARGATE`, extra tasks weighted to `FARGATE_SPOT`* keeps the baseline safe and puts only the burst on Spot. A stateless app is exactly the kind that can take this deal.

**Checkpoint:** the load test triggers at least one scale-out event and a later scale-in, with no `5xx` errors in the ALB metrics while tasks were added and removed.

## Phase 9 — Security hardening

Security on AWS is layered ("defence in depth") and shared: AWS secures the data centres, hypervisors and managed services; you secure your identities, network rules, code and data. Most of the layers are already in place; this phase adds the edge firewall, secrets handling, security headers and monitoring.

| Layer | Control | Status |
| --- | --- | --- |
| Account | Root MFA, no root keys, Identity Center, budget alerts | Phase 0 |
| Identity | Separate execution and task roles; CI role limited to one repo (Phase 12) | Phase 4 |
| Network | Tasks and Redis in private subnets; SG chain; ALB only reachable from CloudFront with a secret header | Phases 3, 7 |
| Transport | TLS 1.2+ at CloudFront and ALB; TLS to Redis; `Secure` + `HttpOnly` cookie | Phases 1, 3, 6 |
| Edge | AWS WAF managed rules and rate limiting | **This phase** |
| Application | Input length cap, non-root user, read-only filesystem, security headers | Phases 1, 4, **this phase** |
| Supply chain | ECR scan on push, immutable tags, locked dependencies (`uv.lock`) | Phase 2 |
| Detection | CloudTrail, GuardDuty, alarms | **This phase**, Phase 10 |

### 9.1 AWS WAF on CloudFront

CloudFront → your distribution → *Security* → enable protections (included in flat-rate plans; the Free plan allows 5 rules). Add:

1. `AWSManagedRulesCommonRuleSet` — the OWASP-style basics (bad bots, oversized bodies, common exploit patterns).
2. `AWSManagedRulesKnownBadInputsRuleSet` — known exploit payloads such as Log4Shell probes.
3. `AWSManagedRulesAmazonIpReputationList` — IPs AWS has seen attacking.
4. **A rate-based rule** scoped to `/api/command`: block an IP above, say, 1,000 requests per 5 minutes. Every command costs CPU, so this is your abuse brake.

Start new rules in *Count* mode for a day, check the WAF logs for false positives, then switch to *Block*.

### 9.2 Security headers at the edge

Attach CloudFront's managed response headers policy **`SecurityHeadersPolicy`** to every behavior. It adds `Strict-Transport-Security` (browsers refuse plain HTTP for your domain), `X-Content-Type-Options: nosniff`, `X-Frame-Options`, `Referrer-Policy` and more, with no app changes.

### 9.3 Secrets, the right way

This app has no secrets yet, but the pattern is worth learning. Never put them in the image, the git repo or plain `environment` variables. Store them in **Secrets Manager** and reference them from the task definition; ECS injects them at start-up using the execution role:

```json
"secrets": [
  { "name": "REDIS_PASSWORD",
    "valueFrom": "arn:aws:secretsmanager:ap-south-1:ACCOUNT_ID:secret:git-sandbox/redis-AbCdEf" }
]
```

Then grant `secretsmanager:GetSecretValue` on **that one ARN** to `ecsTaskExecutionRole`. The value never appears in the console's task definition view or in CloudTrail. A natural first use: enable Valkey user-based access control and inject its password this way.

### 9.4 Detection

- **CloudTrail** records every API call. Event history keeps 90 days by default; create a trail to an S3 bucket if you want longer.
- **GuardDuty** watches CloudTrail, DNS and network logs for compromise signs (crypto-mining, credential misuse). Enable it; it starts with a free trial.
- **IAM Access Analyzer** flags resources shared outside your account and unused permissions.
- **Dependabot** (or Renovate) on the GitHub repo keeps `uv.lock` and the base image patched.

**Checkpoint:** WAF metrics show requests being evaluated; `curl -sI https://sandbox.example.com/` includes `strict-transport-security`; a quick burst of 1,500 requests to `/api/command` from one IP starts receiving `403`s.

## Phase 10 — Observability

You should find out something is wrong from an alarm, not from a user. That needs three signals — **logs** (what happened), **metrics** (how much, how fast) and **traces** (where the time went) — plus alarms on the few metrics that mean users are hurting.

### 10.1 The four golden signals, mapped to this stack

| Signal | Question | Metric (namespace) | Alarm when |
| --- | --- | --- | --- |
| Errors | Are requests failing? | `HTTPCode_Target_5XX_Count` (AWS/ApplicationELB) | > 10 in 5 min |
| Latency | Are requests slow? | `TargetResponseTime`, p95 (AWS/ApplicationELB) | p95 > 1 s for 10 min |
| Traffic | How busy are we? | `RequestCount` (AWS/ApplicationELB), CloudFront `Requests` | No alarm; context for the others |
| Saturation | Are we near the limit? | `CPUUtilization`, `MemoryUtilization` (AWS/ECS); cache `ElastiCacheProcessingUnits` | Memory > 85% for 15 min |
| *Availability* | Can we serve at all? | `HealthyHostCount` (AWS/ApplicationELB) | < 2 for 5 min |

### 10.2 Alarms that email you

```bash
aws sns create-topic --name git-sandbox-alerts
aws sns subscribe --topic-arn $TOPIC_ARN --protocol email --notification-endpoint you@example.com

aws cloudwatch put-metric-alarm --alarm-name git-sandbox-5xx \
  --namespace AWS/ApplicationELB --metric-name HTTPCode_Target_5XX_Count \
  --dimensions Name=LoadBalancer,Value=app/git-sandbox-alb/0123456789abcdef \
  --statistic Sum --period 300 --evaluation-periods 1 --threshold 10 \
  --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
  --alarm-actions $TOPIC_ARN --ok-actions $TOPIC_ARN
```

Repeat for the other rows (latency uses `--extended-statistic p95`; `HealthyHostCount` also needs the `TargetGroup` dimension). `--treat-missing-data notBreaching` stops a quiet night from paging you. Alarm on symptoms users feel, not on every cause, or you'll learn to ignore the emails.

### 10.3 Logs

Uvicorn's access log and any Python tracebacks already go to stdout → the `awslogs` driver → log group `/ecs/git-sandbox`. Query them with **CloudWatch Logs Insights**:

```
fields @timestamp, @logStream, @message
| filter @message like /Traceback|ERROR/
| sort @timestamp desc
| limit 50
```

```
filter @message like /POST \/api\/command/
| stats count() as commands by bin(5m)
```

As apps grow, switch to **structured JSON logs** (one JSON object per line with fields like `session`, `route`, `duration_ms`) so you can filter and aggregate on fields instead of regexes.

### 10.4 An outside-in check

Internal metrics can look fine while DNS or CloudFront is broken. Add a **Route 53 health check** (or a CloudWatch Synthetics canary) that requests `https://sandbox.example.com/healthz` every minute from several regions, and alarm when it fails.

### 10.5 Tracing (when you need it)

For one service, logs plus metrics are enough. When requests cross several services, add **OpenTelemetry** instrumentation to the app and an AWS Distro for OpenTelemetry collector as a sidecar container; traces then appear in **X-Ray** / CloudWatch Application Signals, showing exactly which hop is slow.

**Checkpoint:** the SNS subscription is confirmed; stopping both tasks (set desired count 0 briefly) fires the `HealthyHostCount` alarm email, and restoring it sends an *OK* email.

## Phase 11 — Infrastructure as Code

You've now clicked and typed about forty resources into existence; nobody, including you in three months, could rebuild them identically. Infrastructure as code turns the whole stack into a reviewed, versioned file that can create, change or delete everything in one command.

### 11.1 Concepts

- **Declarative, desired state** — you describe *what* should exist; the tool works out the create/update/delete steps.
- **Plan before apply** — `cdk diff` / `terraform plan` shows exactly what will change. Review it like a pull request.
- **Drift** — someone edits a resource in the console and the code no longer matches reality. Rule of thumb: once a stack is in code, the console is read-only.
- **Environments** — the same code deployed as `staging` and `prod` stacks, ideally in separate AWS accounts.

| Tool | Language | State lives in | Best when |
| --- | --- | --- | --- |
| CloudFormation | YAML/JSON | AWS (stacks) | You want AWS-native and no extra tooling |
| **AWS CDK** | Python, TypeScript, … | CloudFormation | You're on AWS and prefer real code with loops and types — **our choice, in Python** |
| Terraform / OpenTofu | HCL | A state file (usually S3) | Multi-cloud or an existing Terraform shop; the most common in job listings |
| Pulumi | Python, TypeScript, … | Pulumi Cloud or S3 | Like CDK but multi-cloud |

### 11.2 The stack in CDK (Python)

```bash
npm install -g aws-cdk
mkdir infra && cd infra && cdk init app --language python
source .venv/bin/activate && pip install -r requirements.txt
```

Replace `infra/app.py` with:

```python
import aws_cdk as cdk
from aws_cdk import (Duration, Stack, aws_certificatemanager as acm, aws_ec2 as ec2,
                     aws_ecs as ecs, aws_ecs_patterns as patterns,
                     aws_elasticache as elasticache, aws_logs as logs, aws_route53 as route53)
from constructs import Construct

DOMAIN = "example.com"


class GitSandboxStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Phase 3: network (2 AZs, public + private subnets, 1 NAT, free S3 endpoint)
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=1, gateway_endpoints={
            "S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3)})

        app_sg = ec2.SecurityGroup(self, "AppSg", vpc=vpc)
        cache_sg = ec2.SecurityGroup(self, "CacheSg", vpc=vpc)
        cache_sg.add_ingress_rule(app_sg, ec2.Port.tcp(6379), "app to valkey")

        # Phase 3: session store
        private = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        cache = elasticache.CfnServerlessCache(
            self, "Cache", engine="valkey", serverless_cache_name="git-sandbox",
            subnet_ids=private.subnet_ids, security_group_ids=[cache_sg.security_group_id])

        # Phase 6: DNS + certificate for the ALB
        zone = route53.HostedZone.from_lookup(self, "Zone", domain_name=DOMAIN)
        cert = acm.Certificate(self, "Cert", domain_name=f"origin.{DOMAIN}",
                               validation=acm.CertificateValidation.from_dns(zone))

        # Phases 4-5: cluster, task definition, ALB, HTTPS, service, circuit breaker
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        web = patterns.ApplicationLoadBalancedFargateService(
            self, "Web", cluster=cluster, cpu=512, memory_limit_mib=1024, desired_count=2,
            security_groups=[app_sg],
            task_image_options=patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_asset(".."),      # builds ../Dockerfile
                container_port=8000,
                environment={"REDIS_URL": f"rediss://{cache.attr_endpoint_address}:6379/0"},
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="web", log_retention=logs.RetentionDays.TWO_WEEKS)),
            domain_name=f"origin.{DOMAIN}", domain_zone=zone, certificate=cert,
            redirect_http=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True))

        web.target_group.configure_health_check(path="/healthz", healthy_threshold_count=2)
        web.target_group.set_attribute("deregistration_delay.timeout_seconds", "30")

        # Phase 8: autoscaling
        scaling = web.service.auto_scale_task_count(min_capacity=2, max_capacity=6)
        scaling.scale_on_cpu_utilization("Cpu", target_utilization_percent=60,
                                         scale_out_cooldown=Duration.seconds(60),
                                         scale_in_cooldown=Duration.seconds(300))

        cdk.Tags.of(self).add("project", "git-sandbox")


app = cdk.App()
GitSandboxStack(app, "GitSandbox",
                env=cdk.Environment(account="ACCOUNT_ID", region="ap-south-1"))
app.synth()
```

Roughly 50 lines replace Phases 3–6 and 8. `ApplicationLoadBalancedFargateService` is a *pattern* construct: it creates the ALB, listeners, target group, security-group rules, task definition, roles and the Route 53 record with sensible defaults, and still lets you tune each piece.

**Left for you to add (good practice):** CloudFront with `aws_cloudfront.Distribution` and the three behaviors from Phase 7 (its certificate must live in `us-east-1`, so put it in a small second stack with `cross_region_references=True`); the WAF web ACL; the alarms from Phase 10.

```bash
cdk bootstrap aws://ACCOUNT_ID/ap-south-1    # one-time: creates CDK's asset bucket and roles
cdk diff                                      # what will change?
cdk deploy                                    # do it (shows IAM/security changes for approval)
cdk destroy                                   # remove everything the stack created
```

**Checkpoint:** `cdk deploy` brings up a second, identical environment from nothing; `cdk destroy` removes it cleanly. Once it works, tear down the hand-built resources (see *Cost, teardown and troubleshooting*) and treat the CDK app as the source of truth.

## Phase 12 — CI/CD with GitHub Actions

The end state: every pull request runs the tests, and every merge to `main` builds one image, pushes it to ECR, rolls it out to ECS with zero downtime and refreshes the CDN, all without a single AWS key stored in GitHub.

### 12.1 Concepts

- **CI** (continuous integration): every change is built and tested automatically. **CD** (continuous delivery/deployment): every passing change *can* ship (delivery) or *does* ship (deployment).
- **OIDC instead of access keys.** GitHub issues the workflow a short-lived signed token; AWS trusts GitHub as an identity provider and swaps that token for temporary role credentials. Nothing long-lived exists to leak.
- **Environments.** A GitHub *environment* (`staging`, `production`) can require a manual approval and holds its own variables. The AWS role can be restricted to one environment.
- **Build once, promote the same image** through staging to production.
- **Rollback = deploy the previous image.** On `main` that means `git revert` (a new commit that undoes one), never `git reset` plus force-push. Your sandbox's *Revert* exercises are exactly this skill.

| Strategy | How traffic moves | Rollback | Extra cost | Use for |
| --- | --- | --- | --- | --- |
| Rolling (default) | Tasks replaced a few at a time | Circuit breaker or redeploy old revision | Little | Most changes |
| Blue/green ([ECS built-in since July 2025](https://aws.amazon.com/blogs/aws/accelerate-safe-software-releases-with-new-built-in-blue-green-deployments-in-amazon-ecs)) | Full new set started, optionally tested, then all traffic switched | Instant switch back during the bake time | Double capacity during deploy | Risky changes |
| Canary / linear (ECS built-in since late 2025) | A slice (e.g. 10%) first, or equal steps | Stop the shift | Double capacity briefly | Busy services where a small blast radius matters |

### 12.2 Trust GitHub from AWS (one-time)

1. IAM → *Identity providers* → *Add provider* → OpenID Connect, URL `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
2. Create role `github-deploy` with this **trust policy** (replace `OWNER/REPO`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:OWNER/REPO:environment:production"
      }
    }
  }]
}
```

The `sub` condition means only jobs in *your* repo running in the `production` environment can assume it; a fork or another repo cannot.

3. Give it a **least-privilege permissions policy**: `ecr:GetAuthorizationToken` (on `*`); push actions (`ecr:BatchCheckLayerAvailability`, `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`, `BatchGetImage`) on the `git-sandbox` repository ARN only; `ecs:DescribeTaskDefinition`, `ecs:RegisterTaskDefinition`, `ecs:UpdateService`, `ecs:DescribeServices`; `iam:PassRole` on the two task roles only; `cloudfront:CreateInvalidation` on your distribution.

### 12.3 The workflow: `.github/workflows/deploy.yml`

```yaml
name: test-and-deploy
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read
  id-token: write          # allows the job to request an OIDC token

env:
  AWS_REGION: ap-south-1
  ECR_REPOSITORY: git-sandbox
  ECS_CLUSTER: git-sandbox
  ECS_SERVICE: web
  CONTAINER_NAME: web

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked
      - run: uv run python tests/test_engine.py

  deploy:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: production        # add a required reviewer here for manual approval
    steps:
      - uses: actions/checkout@v5

      - uses: aws-actions/configure-aws-credentials@v5
        with:
          role-to-assume: arn:aws:iam::${{ vars.AWS_ACCOUNT_ID }}:role/github-deploy
          aws-region: ${{ env.AWS_REGION }}

      - id: ecr
        uses: aws-actions/amazon-ecr-login@v2

      - id: build
        env:
          IMAGE: ${{ steps.ecr.outputs.registry }}/${{ env.ECR_REPOSITORY }}:${{ github.sha }}
        run: |
          docker build -t "$IMAGE" .
          docker push "$IMAGE"
          echo "image=$IMAGE" >> "$GITHUB_OUTPUT"

      - run: aws ecs describe-task-definition --task-definition git-sandbox --query taskDefinition > taskdef.json

      - id: render
        uses: aws-actions/amazon-ecs-render-task-definition@v1
        with:
          task-definition: taskdef.json
          container-name: ${{ env.CONTAINER_NAME }}
          image: ${{ steps.build.outputs.image }}
          environment-variables: APP_VERSION=${{ github.sha }}

      - uses: aws-actions/amazon-ecs-deploy-task-definition@v2
        with:
          task-definition: ${{ steps.render.outputs.task-definition }}
          cluster: ${{ env.ECS_CLUSTER }}
          service: ${{ env.ECS_SERVICE }}
          wait-for-service-stability: true

      - run: >
          aws cloudfront create-invalidation
          --distribution-id ${{ vars.CF_DISTRIBUTION_ID }} --paths "/" "/api/exercises"
```

Set `AWS_ACCOUNT_ID` and `CF_DISTRIBUTION_ID` as GitHub environment **variables** (not secrets; they aren't sensitive). Then protect `main`: require a pull request and a passing `test` job before merging.

**If you moved to CDK in Phase 11**, don't let CI edit the task definition behind CDK's back (that's drift). Instead, switch the stack to `ecs.ContainerImage.from_ecr_repository(repo, tag)` with the tag passed as context, and replace the render and deploy steps with `npx cdk deploy --require-approval never -c imageTag=${{ github.sha }}`.

**Adding staging:** deploy the same pipeline to a second cluster/service (ideally a second AWS account) in a `staging` job, run a smoke test against it (`curl -f https://staging.example.com/healthz`), and make the production job `needs: staging`.

**Checkpoint:** change a line in `index.html`, open a PR (tests run, no deploy), merge it (deploy runs), and within a few minutes `https://sandbox.example.com/healthz` reports the new commit SHA as `version`. Then practise a rollback: `git revert HEAD`, push, and watch the old version return.

## Cost, teardown and troubleshooting

The full stack costs roughly **$110 a month (about $3.60 a day)** while it runs, and your sign-up credits cover a learning sprint many times over. The surprise bills people get almost always come from NAT gateways and public IPs left behind after a half-finished teardown.

### Monthly estimate

US East (N. Virginia) list prices, 730 hours, light traffic. Mumbai rates run somewhat higher; check the [AWS Pricing Calculator](https://calculator.aws/) for your region.

| Item | Assumption | ≈ USD / month |
| --- | --- | --- |
| NAT gateway | 1 gateway + its public IP + a little data | 37 |
| Fargate tasks | 2 × (0.5 vCPU × [$0.04048/h](https://aws.amazon.com/fargate/pricing/) + 1 GB × $0.004445/h) | 36 |
| Application Load Balancer | [$0.0225/h + \~1 LCU at $0.008/h](https://aws.amazon.com/elasticloadbalancing/pricing/) | 22 |
| ALB public IPv4 addresses | 2 AZs × $0.005/h | 7 |
| ElastiCache Serverless (Valkey) | 100 MB minimum storage; requests negligible | 6 |
| CloudWatch logs and alarms | 14-day retention, \~6 alarms | 1–3 (approx.) |
| Route 53 hosted zone | 1 zone (domain registration extra) | 0.50 |
| ECR storage | \~20 small images | < 1 |
| CloudFront + WAF | Flat-rate Free plan | 0 |
| **Total** |  | **≈ 110** |

**Cheaper learning variant (≈ $60):** 1 task, tasks in public subnets with a public IP (no NAT gateway). **Cheapest of all:** build it, learn, and run `cdk destroy` the same evening. Switching tasks to ARM64 saves about 20% of the Fargate line; Fargate Spot saves more.

### Teardown order (hand-built version)

Delete in roughly reverse order of dependencies, then check the bill the next day:

1. CloudFront distribution (disable, wait, delete) and its WAF web ACL
2. Route 53 records you added; the hosted zone if you're done with the domain
3. ECS service (set desired count to 0, then delete), cluster, task definitions (deregister)
4. ALB, then the target group
5. ElastiCache serverless cache
6. **NAT gateway**, then **release its Elastic IP** (an unattached EIP keeps billing)
7. VPC (deleting it removes subnets, route tables, IGW, endpoints and security groups)
8. ECR repository, CloudWatch log groups and alarms, SNS topic, ACM certificates, IAM roles

Then open **Resource Groups → Tag Editor**, search all regions for `project=git-sandbox`, and delete anything still listed. With CDK, `cdk destroy` does steps 1–8 for what it created (ECR repos and log groups may be retained by default; delete those by hand).

### Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Task stops with `CannotPullContainerError` | Private subnet has no route to ECR (no NAT/endpoints), or a public-subnet task has no public IP | Check the subnet's route table; or enable *assign public IP* |
| Task stops with `exec format error` | Image built for ARM, task set to X86\_64 (or the reverse) | Rebuild with `--platform linux/amd64`, or set `cpuArchitecture: ARM64` |
| Targets stuck *unhealthy*, tasks keep restarting | Wrong health path or port; `app-sg` doesn't allow 8000 from `alb-sg`; app bound to `127.0.0.1` | Test `/healthz` locally in Docker; check the SG rule references the ALB's SG |
| ALB returns **502** | App crashed or closed the connection | Read the task's logs in `/ecs/git-sandbox` |
| ALB returns **503** | Zero healthy targets | See the unhealthy row above |
| ALB returns **504**, or requests hang \~2 s then 500 | App can't reach Valkey: `cache-sg` rule missing, or `redis://` used instead of `rediss://` | Fix the SG chain; serverless caches need TLS |
| Two users see each other's lab | `/api/*` is being cached at CloudFront, or the old global `lab` is still deployed | Check the `/api/*` behavior uses `CachingDisabled`; check `/healthz` version |
| Lab resets after every deploy | `REDIS_URL` not set in the task definition, so the in-memory fallback is used | Add the env var, redeploy |
| ACM certificate stuck *Pending validation* | Validation CNAME missing, or registrar still points at old name servers | Check the record exists; `dig NS example.com` must show Route 53 servers |
| CloudFront returns 502 | Origin TLS mismatch: origin name doesn't match the ALB certificate | Use `origin.example.com` (covered by the cert), protocol HTTPS only |
| Endless redirect loop | CloudFront → ALB over HTTP while the ALB redirects HTTP → HTTPS | Origin protocol policy: HTTPS only |
| GitHub Actions: `Not authorized to perform sts:AssumeRoleWithWebIdentity` | Trust policy `sub` doesn't match repo/environment, or `id-token: write` missing | Compare `sub` with `repo:OWNER/REPO:environment:production` |
| Need a shell inside a running task | — | Enable ECS Exec (`--enable-execute-command` on the service, `ssmmessages:*` permissions on the task role), then `aws ecs execute-command --interactive --command sh …` |

## Reusable checklist for any localhost project

Run through this list for the next app you deploy, on AWS or anywhere else; the services change names between clouds, the questions don't.

**Before the cloud**

- [ ] Where does state live? Move sessions, uploads and data out of process memory and local disk (cache, database, object storage).
- [ ] All config from environment variables; secrets from a secrets manager.
- [ ] Binds `0.0.0.0`, port from config; logs to stdout; handles `SIGTERM`.
- [ ] Cheap `/healthz`; `Cache-Control` set deliberately on every route (default `no-store`).
- [ ] Dockerfile: slim base, dependencies layer before code, non-root user, `.dockerignore`.
- [ ] Runs with its real dependencies via `docker compose`, and survives a container restart.

**Platform**

- [ ] Account: root MFA, SSO identity, budget alerts, one region, tags.
- [ ] Image in a private registry with immutable, commit-based tags and scanning.
- [ ] Network: public subnets for the load balancer only; app and data private; security groups chained by reference.
- [ ] Compute: at least 2 copies in 2 AZs; separate execution and task roles.
- [ ] Load balancer: health checks, short deregistration delay, HTTPS with HTTP redirect.
- [ ] DNS and certificates; CDN in front with cache behaviors split into shared vs per-user; origin locked to the CDN.
- [ ] Autoscaling with sane min/max, verified by a load test.
- [ ] WAF managed rules plus a rate limit on expensive endpoints; security headers.
- [ ] Alarms on errors, latency, healthy hosts and an outside-in check, routed to a human.
- [ ] Log retention set on every log group.

**Process**

- [ ] Infrastructure in code; the console is read-only.
- [ ] CI runs tests on every PR; CD deploys `main` with OIDC, never stored keys.
- [ ] Zero-downtime strategy and automatic rollback; you've practised a rollback.
- [ ] Staging environment that receives the same image first.
- [ ] A teardown path you've tested.

## Sources

- [AWS App Runner availability change](https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html) — closed to new customers from 30 April 2026
- [Announcing Amazon ECS Express Mode](https://aws.amazon.com/about-aws/whats-new/2025/11/announcing-amazon-ecs-express-mode) and [Extending ECS Express Mode](https://aws.amazon.com/blogs/containers/extending-amazon-ecs-express-mode-to-build-an-optimal-container-environment/)
- [Built-in blue/green deployments in Amazon ECS](https://aws.amazon.com/blogs/aws/accelerate-safe-software-releases-with-new-built-in-blue-green-deployments-in-amazon-ecs)
- [AWS Free Tier](https://aws.amazon.com/free) and [Free Tier credits announcement](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-free-tier-credits-month-free-plan)
- [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)
- [Elastic Load Balancing pricing](https://aws.amazon.com/elasticloadbalancing/pricing/)
- [Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/) and [Public IPv4 address charge](https://aws.amazon.com/blogs/aws/new-aws-public-ipv4-address-charge-public-ip-insights)
- [ElastiCache for Valkey 8.0 and serverless pricing](https://aws.amazon.com/blogs/database/amazon-elasticache-version-8-0-for-valkey-brings-faster-scaling-and-improved-memory-efficiency)
- [CloudFront pricing and flat-rate plans](https://aws.amazon.com/cloudfront/pricing/) and [Pricing Plan Manager: plans](https://docs.aws.amazon.com/PricingPlanManager/latest/UserGuide/plans.html)
- [aws-actions/amazon-ecs-deploy-task-definition](https://github.com/aws-actions/amazon-ecs-deploy-task-definition)
- [The Twelve-Factor App](https://12factor.net/)
