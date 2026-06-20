import json
import redis
from celery import Celery
from app.config import settings
from app.database import SessionLocal
from app.models import Task, Project

# Initialize Celery app
celery_app = Celery(
    "tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

# Connect to Redis for WebSocket PubSub
pubsub_client = None
try:
    pubsub_client = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=0,
        socket_timeout=2.0
    )
except Exception as e:
    print(f"Worker: Redis PubSub client setup failed: {e}")

def publish_task_update(project_id: str, task_id: str, status: str, logs: str, thoughts: str = "", output_data: dict = None):
    """
    Publishes a JSON state update payload to the Redis project channel for real-time WebSocket streaming.
    """
    if pubsub_client:
        try:
            payload = {
                "type": "task_update",
                "task_id": task_id,
                "project_id": project_id,
                "status": status,
                "logs": logs,
                "thoughts": thoughts,
                "output_data": output_data or {}
            }
            pubsub_client.publish(f"channel:{project_id}", json.dumps(payload))
        except Exception as e:
            print(f"Worker: Failed to publish websocket updates: {e}")

@celery_app.task(name="app.worker.run_agent_task")
def run_agent_task(task_id: str):
    """
    Distributed Celery worker task.
    Resolves the task runner, executes the agent, and advances the project DAG state machine.
    """
    db = SessionLocal()
    
    # Import runners inside the function to avoid circular imports
    from app.agents.researcher import run_researcher_agent
    from app.agents.sandbox import run_sandbox_agent
    from app.agents.writer import run_writer_agent
    from app.orchestrator import check_and_advance_dag

    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            print(f"Worker: Task {task_id} not found in database.")
            return False

        print(f"Worker: Executing agent for task {task_id} [{task.type}]")
        
        # Publish start event
        publish_task_update(
            project_id=task.project_id,
            task_id=task.id,
            status="RUNNING",
            logs=f"[System] Worker picked up task. Starting execution of {task.type} agent..."
        )

        # Route task to specific agent
        if task.type == "research":
            run_researcher_agent(db, task.id)
        elif task.type == "code_execution":
            run_sandbox_agent(db, task.id)
        elif task.type == "writer":
            run_writer_agent(db, task.id)
        else:
            raise ValueError(f"Unknown task type: {task.type}")

        # Fetch updated task state
        db.refresh(task)
        
        # Publish completed event
        publish_task_update(
            project_id=task.project_id,
            task_id=task.id,
            status=task.status,
            logs=task.logs,
            thoughts=task.thoughts,
            output_data=task.output_data
        )

        # Advance DAG
        check_and_advance_dag(db, task.project_id)
        
        # Publish project status advance event
        project = db.query(Project).filter(Project.id == task.project_id).first()
        if project:
            if pubsub_client:
                project_payload = {
                    "type": "project_update",
                    "project_id": project.id,
                    "status": project.status
                }
                pubsub_client.publish(f"channel:{project.id}", json.dumps(project_payload))

        return True

    except Exception as e:
        import traceback
        error_logs = f"\n[Worker Exception Error]: {str(e)}\n{traceback.format_exc()}"
        print(f"Worker: Exception executing task {task_id}:{error_logs}")
        
        # Mark task as failed in database
        task_to_fail = db.query(Task).filter(Task.id == task_id).first()
        if task_to_fail:
            task_to_fail.status = "FAILED"
            task_to_fail.logs += error_logs
            db.commit()
            
            publish_task_update(
                project_id=task_to_fail.project_id,
                task_id=task_to_fail.id,
                status="FAILED",
                logs=task_to_fail.logs
            )
            
            # Trigger state machine to transition project to FAILED
            check_and_advance_dag(db, task_to_fail.project_id)
            
            # Publish project status failure
            project = db.query(Project).filter(Project.id == task_to_fail.project_id).first()
            if project and pubsub_client:
                project_payload = {
                    "type": "project_update",
                    "project_id": project.id,
                    "status": project.status
                }
                pubsub_client.publish(f"channel:{project.id}", json.dumps(project_payload))
                
        return False
    finally:
        db.close()
