import os
from sqlalchemy.orm import Session
from app.config import settings
from app.models import Task, Project
from app.memory import retrieve_memories, store_memory

# Initialize Gemini Client if API key is provided
client = None
if settings.GEMINI_API_KEY:
    try:
        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as e:
        print(f"Writer: Failed to initialize Gemini Client: {e}")

def run_writer_agent(db: Session, task_id: str):
    """
    Writer Agent: Ingests research, sandbox charts, queries pgvector RAG memory,
    and drafts the final comprehensive Markdown report.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return

    task.logs = "[Writer Agent Started]\nCompiling research summaries and code outcomes..."
    db.commit()

    project_id = task.project_id
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        task.logs += "\n[Error] Project parent entity not found."
        task.status = "FAILED"
        db.commit()
        return

    # 1. Gather outputs from dependencies
    research_notes = ""
    sandbox_logs = ""
    plot_image_url = ""

    all_tasks = db.query(Task).filter(Task.project_id == project_id).all()
    for t in all_tasks:
        if t.type == "research" and t.output_data:
            research_notes = t.output_data.get("research_output", "")
        elif t.type == "code_execution" and t.output_data:
            sandbox_logs = t.output_data.get("stdout", "")
            plot_image_url = t.output_data.get("plot_image_url", "")

    task.logs += f"\nLoaded research ({len(research_notes)} bytes) and sandbox logs ({len(sandbox_logs)} bytes)."
    db.commit()

    # 2. RAG retrieval step: query pgvector
    task.logs += "\nQuerying pgvector vector database for similar historical context..."
    db.commit()
    
    memories = retrieve_memories(db, project_id, project.prompt, limit=3)
    memory_context = ""
    if memories:
        memory_context = "\n=== SEMANTIC RAG MEMORY CONTEXT ===\n"
        for i, (mem, score) in enumerate(memories):
            memory_context += f"Memory [{i+1}] (Similarity Score: {score:.4f}):\n{mem.content}\n\n"
        task.logs += f"\nInjected {len(memories)} semantic memory contexts into system prompt."
    else:
        task.logs += "\nNo vector memories found for this project yet. Continuing with empty context."
    db.commit()

    report_content = ""
    if settings.GEMINI_API_KEY and client:
        try:
            task.logs += "\nQuerying Gemini to compile final report..."
            db.commit()
            
            prompt = (
                f"You are the Writer Agent for a Distributed Multi-Agent Platform.\n"
                f"You must draft a comprehensive, beautiful Markdown report answering the original user request:\n"
                f"'{project.prompt}'\n\n"
                f"Incorporate the following information:\n"
                f"1. Research Notes:\n{research_notes}\n\n"
                f"2. Sandbox Logs:\n{sandbox_logs}\n\n"
                f"3. Historical RAG Memories (if any):\n{memory_context}\n\n"
                f"If a chart was generated, embed it in the markdown. The chart URL is: '{plot_image_url}'\n"
                f"Write a highly detailed, professional, executive report. Do not use placeholders."
            )
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            report_content = response.text or "Error generating report content."
        except Exception as e:
            task.logs += f"\nLLM Report generation failed: {e}. Falling back to default report compilation."
            db.commit()

    if not report_content:
        # Fallback compilation logic
        task.logs += "\n[Local Writer Mode] Compiling markdown documentation locally..."
        
        # Build a beautiful report linking to the chart
        chart_snippet = ""
        if plot_image_url:
            chart_snippet = f"### Data Visualization\n\n![Global EV Sales Projections]({plot_image_url})\n"

        report_content = (
            f"# Executive Report: Distributed Agent Collaboration Summary\n\n"
            f"**Objective**: Analytical breakdown answering: *\"{project.prompt}\"*\n\n"
            f"## 1. Grounded Research Summaries\n"
            f"{research_notes or 'No research notes found.'}\n\n"
            f"## 2. Sandbox Execution Output\n"
            f"The Python sandbox executed data modeling scripts and compiled the following terminal output:\n"
            f"```text\n{sandbox_logs or 'No code output found.'}\n```\n\n"
            f"{chart_snippet}\n"
            f"## 3. RAG Semantic Search Trace\n"
            f"Prior to compilation, the system queried pgvector memories with the query embeddings. "
            f"The similarity scoring retrieved these semantically linked events:\n"
            f"```text\n{memory_context or 'No memory trace available.'}\n```\n\n"
            f"---  \n"
            f"*Report compiled by Autonomous Writer Agent at 2026-06-20.*"
        )

    # Save to file
    shared_dir = "/app/shared"
    if not os.path.exists(shared_dir):
        shared_dir = os.path.join(os.getcwd(), "shared")
        os.makedirs(shared_dir, exist_ok=True)

    report_filename = f"{project_id}_report.md"
    report_filepath = os.path.join(shared_dir, report_filename)
    report_url = f"/shared/{report_filename}"

    with open(report_filepath, "w", encoding="utf-8") as f:
        f.write(report_content)

    task.logs += f"\nReport compiled and written to shared volume: {report_url}"
    task.output_data = {
        "final_report": report_content,
        "report_file_url": report_url
    }
    task.status = "COMPLETED"
    db.commit()

    # Store report itself in memory
    store_memory(db, project_id, f"Project report generated: {report_content[:300]}...", task_id=task.id)
    task.logs += "\nUpdated final memory bank. Executing exit status..."
    db.commit()
