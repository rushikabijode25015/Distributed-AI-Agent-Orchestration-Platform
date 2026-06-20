import json
import uuid
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.config import settings
from app.models import Project, Task
from app.schemas import TaskGraphDefinition, TaskBase
from app.worker import run_agent_task  # We will define this celery task shortly

# Initialize Gemini Client if API key is provided
client = None
if settings.GEMINI_API_KEY:
    try:
        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as e:
        print(f"Orchestrator: Failed to initialize Gemini Client: {e}")

def parse_prompt_to_dag(prompt: str) -> TaskGraphDefinition:
    """
    Parses a user prompt into a Directed Acyclic Graph (DAG) of subtasks.
    If GEMINI_API_KEY is available, queries Gemini for a structured JSON task graph.
    Otherwise, returns a predefined graph tailored to the request contents.
    """
    if client:
        try:
            # Generate structured JSON using Gemini API
            system_instruction = (
                "You are the Orchestrator for a Distributed AI Agent Platform.\n"
                "Decompose the user request into a Directed Acyclic Graph (DAG) of subtasks.\n"
                "Supported task types are:\n"
                "1. 'research' (for searching the web, crawling, extracting info)\n"
                "2. 'code_execution' (for generating and executing Python code, e.g., drawing graphs, calculations)\n"
                "3. 'writer' (for compiling research details and code outputs into a cohesive final Markdown report)\n"
                "Define dependencies ('depend_on_ids') such that a task only starts after its dependencies complete.\n"
                "Assign unique alphanumeric IDs like 'task_1', 'task_2', etc."
            )
            
            # Using GenerateContentConfig with response_schema for strict output parsing
            from google.genai import types
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=f"Decompose the following prompt:\n{prompt}",
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=TaskGraphDefinition,
                ),
            )
            
            # Parse the response
            if response.text:
                data = json.loads(response.text)
                return TaskGraphDefinition(**data)
        except Exception as e:
            print(f"Orchestrator: Gemini DAG parsing failed: {e}. Falling back to rule-based parsing.")

    # Rule-Based Fallback Parser (IIT-level high fidelity simulator)
    # Checks common keywords in prompt to generate realistic multi-agent workflows.
    tasks = []
    
    # Check if code execution (graph, plot, code, chart) is requested
    needs_code = any(kw in prompt.lower() for kw in ["code", "plot", "chart", "graph", "draw", "visualize", "matplotlib"])
    
    # 1. Research Task (Almost always first)
    tasks.append(
        TaskBase(
            id="task_1",
            title="Search and extract research information",
            type="research",
            depend_on_ids=[],
            input_data={"search_query": prompt, "depth": "detailed"}
        )
    )
    
    if needs_code:
        # 2. Coder Task (Depends on Research task to supply parameters)
        tasks.append(
            TaskBase(
                id="task_2",
                title="Execute Python code & generate visual data plots",
                type="code_execution",
                depend_on_ids=["task_1"],
                input_data={"code_description": f"Generate a visual data plot related to: {prompt}"}
            )
        )
        # 3. Writer Task (Depends on both Research and Coder)
        tasks.append(
            TaskBase(
                id="task_3",
                title="Write final report and compile analysis",
                type="writer",
                depend_on_ids=["task_1", "task_2"],
                input_data={"tone": "professional"}
            )
        )
    else:
        # 2. Writer Task (Depends on Research task directly)
        tasks.append(
            TaskBase(
                id="task_2",
                title="Write final report and compile analysis",
                type="writer",
                depend_on_ids=["task_1"],
                input_data={"tone": "professional"}
            )
        )
        
    return TaskGraphDefinition(tasks=tasks)

def create_project_dag(db: Session, prompt: str) -> Project:
    """
    Decomposes prompt into a task graph, saves it to PostgreSQL, and initializes tasks.
    """
    # 1. Parse prompt
    dag = parse_prompt_to_dag(prompt)
    
    # 2. Save Project
    project = Project(prompt=prompt, status="PENDING")
    db.add(project)
    db.commit()
    db.refresh(project)
    
    # 3. Save Tasks
    for task_def in dag.tasks:
        task = Task(
            id=f"{project.id}_{task_def.id}",
            project_id=project.id,
            title=task_def.title,
            type=task_def.type,
            status="PENDING",
            depend_on_ids=[f"{project.id}_{dep_id}" for dep_id in task_def.depend_on_ids],
            input_data=task_def.input_data,
            output_data={},
            logs="",
            thoughts=""
        )
        db.add(task)
        
    db.commit()
    db.refresh(project)
    return project

def start_project_execution(db: Session, project_id: str):
    """
    Launches execution of the project DAG by enqueuing independent (0-dependency) tasks.
    Uses PostgreSQL transaction locks for state safety.
    """
    # Select for update ensures thread safety
    project = db.query(Project).filter(Project.id == project_id).with_for_update().first()
    if not project or project.status != "PENDING":
        return

    project.status = "RUNNING"
    db.commit()
    
    # Find all tasks with no dependencies
    tasks = db.query(Task).filter(Task.project_id == project_id).all()
    for task in tasks:
        if not task.depend_on_ids:
            # Set to running and enqueue
            task_to_run = db.query(Task).filter(Task.id == task.id).with_for_update().first()
            if task_to_run.status == "PENDING":
                task_to_run.status = "RUNNING"
                db.commit()
                # Enqueue Celery agent job asynchronously
                run_agent_task.delay(task_to_run.id)

def check_and_advance_dag(db: Session, project_id: str):
    """
    State machine that runs whenever a task finishes.
    Checks for newly unlocked tasks or marks project as completed/failed.
    Uses PostgreSQL transaction locks to avoid race conditions.
    """
    project = db.query(Project).filter(Project.id == project_id).with_for_update().first()
    if not project or project.status not in ["RUNNING", "PENDING"]:
        return

    all_tasks = db.query(Task).filter(Task.project_id == project_id).all()
    
    # Check if any task is FAILED
    if any(t.status == "FAILED" for t in all_tasks):
        project.status = "FAILED"
        # Fail any pending/running tasks
        for t in all_tasks:
            if t.status in ["PENDING", "RUNNING"]:
                t_lock = db.query(Task).filter(Task.id == t.id).with_for_update().first()
                t_lock.status = "FAILED"
                t_lock.logs += "\n[System Error] Task terminated due to parent task failure."
        db.commit()
        return

    # Check if all tasks are COMPLETED
    if all(t.status == "COMPLETED" for t in all_tasks):
        project.status = "COMPLETED"
        db.commit()
        return

    # Find tasks that are PENDING and check if all their dependencies are COMPLETED
    completed_task_ids = {t.id for t in all_tasks if t.status == "COMPLETED"}
    
    for task in all_tasks:
        if task.status == "PENDING":
            # Check dependencies
            deps_satisfied = all(dep in completed_task_ids for dep in task.depend_on_ids)
            if deps_satisfied:
                # Lock row and transition status to avoid double execution
                t_lock = db.query(Task).filter(Task.id == task.id).with_for_update().first()
                if t_lock.status == "PENDING":
                    t_lock.status = "RUNNING"
                    db.commit()
                    # Trigger worker execution
                    run_agent_task.delay(t_lock.id)
