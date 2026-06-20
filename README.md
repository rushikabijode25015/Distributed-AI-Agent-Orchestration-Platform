# Distributed AI Agent Orchestration Platform

A high-performance, containerized, distributed orchestration platform that executes complex task workflows using Directed Acyclic Graphs (DAG). Multiple autonomous AI agents collaborate as independent Celery workers, sharing memory state via `pgvector` and communicating asynchronously using Redis Pub/Sub and FastAPI WebSockets.

---

## 🏗️ System Architecture

```
                           +------------------------+
                           |  Frontend (Client UI)  |
                           +-----------+------------+
                                       |
                               (WebSockets / Event Stream)
                                       v
                           +-----------+------------+
                           |   FastAPI API Gateway  |
                           +-----+-----------+------+
                                 |           |
                     (Read/Write |           | (Push Agent Job)
                          State) |           v
                                 |     +-----+------+
                                 |     |   Celery   |
                                 |     | Message DB | (Redis)
                                 |     +-----+------+
                                 |           |
                                 v           v
  +------------------+     +-----+-----------+------+     +------------------+
  |    PostgreSQL    +<----+  Distributed Agent     +---->+    pgvector      |
  | (Relation/State) |     |  Worker Pool (LLM)     |     | (Vector DB/Mem)  |
  +------------------+     +-----------+------------+     +------------------+
                                       |
                              (LLM Inference calls)
                                       v
                           +-----------+------------+
                           |   Gemini / OpenAI API  |
                           +------------------------+
```

### Core Architecture Components
1. **API Gateway & Orchestrator (FastAPI)**: Serves the REST API, manages client WebSocket connections, parses user prompts into a task graph, and runs a PostgreSQL-locked DAG state machine.
2. **Distributed Workers (Celery + Redis)**: Processors execute agents inside sandboxed containers. Supports horizontal scaling to process hundreds of parallel agent routines.
3. **Semantic Memory (pgvector)**: Encodes research findings and execution summaries into 768-dimensional vector embeddings, dynamically injected into prompts as agent working memory via cosine similarity.
4. **Semantic Cache (Redis)**: Caches prompt-response pairs. Before executing an LLM call, computes prompt embedding and queries Redis. If a duplicate query (similarity > 0.95) exists, serves the cached result instantly, reducing API latency and costs.
5. **Headless Execution Sandbox**: The Coder Agent executes python code in a subprocess. Integrates the Matplotlib `Agg` backend to output PNG data plots within headless Docker containers safely.

---

## 🛠️ Elite Engineering Highlights

- **Atomic State Transitions**: Uses PostgreSQL `SELECT FOR UPDATE` transaction locking in the orchestrator state machine to prevent race conditions when multi-agent processes complete concurrently.
- **WebSocket Reconnection & Recovery**: Clients WebSocket triggers an initial dump of the project's task states, allowing developers to close the tab and reconnect to resume log streaming from the exact point of connection loss.
- **Redis Pub/Sub Sync**: Decouples the API Gateway from Celery. Agents write logs directly to the database and publish status payloads over Redis channels, which are immediately pushed to client WebSockets.
- **Deterministic Mock Engine**: Runs locally out-of-the-box. If no `GEMINI_API_KEY` is provided, the platform boots in high-fidelity simulator mode (using hash-seeded mock embeddings and deterministic mock web scraping), allowing complete end-to-end functionality testing.

---

## 🚀 Quick Start (Local Docker Setup)

### 1. Configure Environment
Clone the repository, copy the template `.env` and optionally insert your API keys:
```bash
cp .env.example .env
```
*Note: If `GEMINI_API_KEY` is left empty, the orchestrator automatically activates simulated local agent mode.*

### 2. Launch Services
Spin up the PostgreSQL, Redis, API, and Celery Worker containers:
```bash
docker-compose up --build
```

### 3. Access Dashboard
Once building finishes, open your browser and navigate to:
```
http://localhost:8000/frontend/index.html
```

---

## 🧪 Integration Verification

To run the automated verification script:
1. Ensure the docker containers are active.
2. Execute the verification script:
```bash
pip install httpx
python verify.py
```
*The script will submit a prompt, poll progress, verify state machine transitions, and check if final PNG plots and markdown reports are accessible via the static mounts.*

---

## 📡 API Reference

### REST Endpoints
- **`POST /api/projects`**: Submit prompt instructions. Returns the initialized DAG.
  - Body: `{"prompt": "string"}`
- **`GET /api/projects/{project_id}`**: Returns overall pipeline status and all individual task state arrays.

### WebSocket Connection
- **`WS /ws/projects/{project_id}`**: Real-time channel. Streams log feeds:
  - `init_state`: Initial database recovery dump.
  - `task_update`: Live status updates and console logs from active agents.
  - `project_update`: Pipeline status transition changes (`COMPLETED`/`FAILED`).

---

## ☁️ Deployment on Render

This project contains a `render.yaml` infrastructure blueprint. To deploy:
1. Connect your GitHub repository to Render.
2. Go to **Render Dashboard** -> **Blueprints** -> **New Blueprint Instance**.
3. Select this repository. Render will automatically provision:
   - PostgreSQL (with `pgvector` enabled)
   - Redis Instance
   - FastAPI web gateway
   - Celery worker service
4. In the **api-gateway** and **worker-pool** configurations in Render, define the `GEMINI_API_KEY` environment variable.
