import os
import json
import asyncio
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import redis.asyncio as aioredis

from app.config import settings
from app.database import get_db, init_db
from app.models import Project, Task
from app.schemas import ProjectCreate, ProjectResponse
from app.orchestrator import create_project_dag, start_project_execution

# Initialize FastAPI App
app = FastAPI(
    title="Distributed AI Agent Orchestration Gateway",
    description="REST & WebSocket API Gateway for high-performance multi-agent execution.",
    version="1.0.0"
)

# Configure CORS for developer testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup migrations and setup
@app.on_event("startup")
def startup_event():
    init_db()

# Mount Static Shared Volume for generated plots/markdown files
shared_dir = "/app/shared" if os.path.exists("/app/shared") else os.path.join(os.getcwd(), "shared")
os.makedirs(shared_dir, exist_ok=True)
app.mount("/shared", StaticFiles(directory=shared_dir), name="shared")

# REST Endpoints
@app.post("/api/projects", response_model=ProjectResponse, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    """
    Submits a new project prompt. Parses the prompt into a task DAG and enqueues initial jobs.
    """
    try:
        project = create_project_dag(db, payload.prompt)
        # Start executing the 0-dependency nodes
        start_project_execution(db, project.id)
        # Re-fetch project to return updated states
        db.refresh(project)
        return project
    except Exception as e:
        import traceback
        print(f"API Error creating project: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to create agent execution pipeline: {str(e)}")

@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, db: Session = Depends(get_db)):
    """
    Gets the detailed status of a project and all its corresponding task DAG nodes.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project

# WebSocket Gateway with Redis PubSub tracing
@app.websocket("/ws/projects/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    """
    WebSocket endpoint that yields real-time task progress, LLM thoughts, and logs.
    Re-establishes context on reconnection by dumping database state first.
    """
    await websocket.accept()
    print(f"WS Client connected for project: {project_id}")

    # Step 1: Send current state from database immediately (Handles reconnection resume)
    db = next(get_db())
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            tasks = db.query(Task).filter(Task.project_id == project_id).all()
            # Send initial dump
            init_payload = {
                "type": "init_state",
                "project_status": project.status,
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "type": t.type,
                        "status": t.status,
                        "depend_on_ids": t.depend_on_ids,
                        "input_data": t.input_data,
                        "output_data": t.output_data,
                        "logs": t.logs,
                        "thoughts": t.thoughts
                    } for t in tasks
                ]
            }
            await websocket.send_json(init_payload)
    except Exception as err:
        print(f"WS Initial state load error: {err}")
    finally:
        db.close()

    # Step 2: Listen to Redis PubSub for real-time Celery worker triggers
    async_redis = None
    pubsub = None
    try:
        async_redis = aioredis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=0,
            socket_timeout=5.0
        )
        pubsub = async_redis.pubsub()
        await pubsub.subscribe(f"channel:{project_id}")
        
        print(f"WS subscribed to Redis channel: channel:{project_id}")

        while True:
            try:
                # Wait for messages from Pub/Sub
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message:
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    
                    # Forward to WebSocket client
                    event_data = json.loads(data)
                    await websocket.send_json(event_data)
            except asyncio.TimeoutError:
                # Keep-alive ping to ensure connection doesn't drop
                await websocket.send_json({"type": "ping"})
            except WebSocketDisconnect:
                print(f"WS client disconnected during loop: {project_id}")
                break
            except Exception as loop_ex:
                print(f"WS publish loop error: {loop_ex}")
                # Try sending a heartbeat/ping, if client disconnected, it will raise WebSocketDisconnect
                await websocket.send_json({"type": "ping"})
                await asyncio.sleep(1.0)

    except WebSocketDisconnect:
        print(f"WS Client disconnected: {project_id}")
    except Exception as e:
        print(f"WS Connection Error for project {project_id}: {e}")
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(f"channel:{project_id}")
                await pubsub.close()
            except Exception:
                pass
        if async_redis:
            try:
                await async_redis.close()
            except Exception:
                pass
        print(f"WS Connection cleaned up: {project_id}")

# Mount UI Frontend (to be loaded via FastAPI static files)
# Place at the bottom of file to avoid routing collision with REST API
frontend_dir = os.path.join(os.getcwd(), "frontend")
os.makedirs(frontend_dir, exist_ok=True)
app.mount("/frontend", StaticFiles(directory=frontend_dir, html=True), name="frontend")
