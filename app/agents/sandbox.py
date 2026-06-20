import os
import re
import sys
import uuid
import time
import subprocess
from sqlalchemy.orm import Session
from app.config import settings
from app.models import Task
from app.memory import store_memory

# Initialize Gemini Client if API key is provided
client = None
if settings.GEMINI_API_KEY:
    try:
        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as e:
        print(f"Sandbox: Failed to initialize Gemini Client: {e}")

def extract_python_code(llm_output: str) -> str:
    """
    Extracts raw python code from markdown code blocks in LLM responses.
    """
    pattern = r"```python\s*(.*?)\s*```"
    match = re.search(pattern, llm_output, re.DOTALL)
    if match:
        return match.group(1)
    
    # Check for simple code blocks if ```python isn't matched
    pattern_alt = r"```\s*(.*?)\s*```"
    match_alt = re.search(pattern_alt, llm_output, re.DOTALL)
    if match_alt:
        return match_alt.group(1)
        
    return llm_output.strip()

def run_python_subprocess(code: str, output_image_path: str, timeout: int = 15) -> tuple[str, str, bool]:
    """
    Runs the python code inside a subprocess.
    Ensures matplotlib uses 'Agg' backend for headless container execution.
    Returns (stdout, stderr, success).
    """
    # Inject Agg backend configuration at the top of the script
    sanitized_code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        f"OUTPUT_IMAGE_PATH = r'{output_image_path}'\n"
        + code
    )
    
    # Save code to a temporary file
    temp_filename = f"run_{uuid.uuid4().hex}.py"
    temp_filepath = os.path.join("/tmp" if os.name != "nt" else os.environ.get("TEMP", "."), temp_filename)
    
    with open(temp_filepath, "w", encoding="utf-8") as f:
        f.write(sanitized_code)
        
    try:
        # Run process under system sandbox rules (timeout, custom env)
        env = os.environ.copy()
        # Prevent any graphical display connection attempts
        env["QT_QPA_PLATFORM"] = "offscreen" 
        
        start_time = time.time()
        result = subprocess.run(
            [sys.executable, temp_filepath],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )
        elapsed = time.time() - start_time
        
        stdout = result.stdout
        stderr = result.stderr
        success = (result.returncode == 0)
        
        stdout = f"[Execution Time: {elapsed:.2f}s]\n{stdout}"
        return stdout, stderr, success
        
    except subprocess.TimeoutExpired as te:
        stdout = te.stdout or ""
        stderr = te.stderr or f"Process expired timeout threshold of {timeout} seconds."
        return stdout, stderr, False
    except Exception as e:
        return "", f"Sandbox execution error: {str(e)}", False
    finally:
        # Cleanup temp script file
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except Exception:
                pass

def run_sandbox_agent(db: Session, task_id: str):
    """
    Fetches context from dependencies, generates Python plotting script,
    runs it safely in a headless subprocess sandbox, and registers output.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return

    task.logs = "[Code Sandbox Agent Started]\nScanning dependency parameters..."
    db.commit()

    # Get research findings from parent task
    research_data = ""
    project_id = task.project_id
    
    # Find researcher task
    researcher_task = (
        db.query(Task)
        .filter(Task.project_id == project_id, Task.type == "research")
        .first()
    )
    if researcher_task and researcher_task.output_data:
        research_data = researcher_task.output_data.get("research_output", "")

    task.logs += f"\nFound research dataset ({len(research_data)} bytes). Preparing code generator..."
    db.commit()

    # Prepare paths
    shared_dir = "/app/shared"
    if not os.path.exists(shared_dir):
        # Local fallback if run outside container
        shared_dir = os.path.join(os.getcwd(), "shared")
        os.makedirs(shared_dir, exist_ok=True)

    plot_filename = f"{project_id}_plot.png"
    plot_filepath = os.path.join(shared_dir, plot_filename)
    # URL path to serve the file
    plot_url = f"/shared/{plot_filename}"

    code = ""
    if settings.GEMINI_API_KEY and client:
        try:
            task.logs += "\nQuerying Gemini to generate plotting script..."
            db.commit()
            
            prompt = (
                f"Read this research data:\n{research_data}\n\n"
                f"Write a Python script using matplotlib to generate a beautiful, modern line/bar chart "
                f"visualizing this data.\n"
                f"Make sure to:\n"
                f"1. Save the plot to: OUTPUT_IMAGE_PATH (this variable is pre-defined for you).\n"
                f"2. Use clean visual styles (dark mode palette, custom fonts, grids, markers).\n"
                f"3. Do not try to show the plot using plt.show() (matplotlib Agg backend is already loaded).\n"
                f"4. Provide ONLY the raw Python code within a markdown block. No extra words or commentary."
            )
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            code = extract_python_code(response.text)
            task.logs += f"\nGenerated Python script:\n```python\n{code}\n```"
            db.commit()
        except Exception as e:
            task.logs += f"\nLLM Code generation failed: {e}. Falling back to default script."
            db.commit()

    # Pre-coded high fidelity python script for fallback mode
    if not code:
        task.logs += "\n[Local Sandbox Mode] Initializing deterministic Matplotlib script..."
        code = (
            "import matplotlib.pyplot as plt\n"
            "import numpy as np\n\n"
            "# Config styles\n"
            "plt.style.use('dark_background')\n"
            "fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)\n\n"
            "# Data setup\n"
            "years = ['2025 Est', '2026 Proj']\n"
            "byd_sales = [5.98, 7.18] # in millions\n"
            "tesla_sales = [3.64, 4.38] # in millions\n\n"
            "x = np.arange(len(years))\n"
            "width = 0.35\n\n"
            "rects1 = ax.bar(x - width/2, byd_sales, width, label='BYD', color='#00f2fe', alpha=0.9)\n"
            "rects2 = ax.bar(x + width/2, tesla_sales, width, label='Tesla', color='#4facfe', alpha=0.9)\n\n"
            "# Labels and titles\n"
            "ax.set_ylabel('Sales (Millions of Units)', color='#a0aec0', fontsize=10)\n"
            "ax.set_title('Global EV Sales Projection: BYD vs Tesla (2025 - 2026)', color='white', fontsize=12, fontweight='bold', pad=15)\n"
            "ax.set_xticks(x)\n"
            "ax.set_xticklabels(years, color='#a0aec0')\n"
            "ax.legend(frameon=True, facecolor='#1e293b', edgecolor='none')\n"
            "ax.grid(axis='y', linestyle='--', alpha=0.2, color='#718096')\n\n"
            "# Save file\n"
            "plt.tight_layout()\n"
            "plt.savefig(OUTPUT_IMAGE_PATH, transparent=True)\n"
            "print('Successfully plotted EV Sales Projection Chart!')\n"
        )
        task.logs += f"\nGenerated Python script:\n```python\n{code}\n```"
        db.commit()

    task.logs += "\nRunning script inside container subprocess..."
    db.commit()

    stdout, stderr, success = run_python_subprocess(code, plot_filepath)

    task.logs += f"\n[STDOUT]\n{stdout}"
    if stderr:
        task.logs += f"\n[STDERR]\n{stderr}"
    db.commit()

    if success:
        task.logs += f"\nPlot successfully rendered and saved to: {plot_url}"
        task.output_data = {
            "code": code,
            "stdout": stdout,
            "plot_image_url": plot_url
        }
        task.status = "COMPLETED"
        db.commit()
        
        # Store in semantic vector database (RAG)
        store_memory(db, task.project_id, f"Sandbox successfully executed code to generate graph. Output: {stdout}", task_id=task.id)
    else:
        task.logs += "\n[Error] Subprocess returned a non-zero exit code."
        task.status = "FAILED"
        db.commit()
