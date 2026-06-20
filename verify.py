import time
import httpx

API_URL = "http://localhost:8000/api"

def test_pipeline():
    print("==================================================")
    print("Starting Distributed Agent Orchestrator Verification")
    print("==================================================")
    
    # 1. Submit pipeline prompt
    prompt = "Create a report about electric vehicle sales projections in 2026 and render a comparison bar chart."
    print(f"1. Sending request prompt: '{prompt}'")
    
    try:
        response = httpx.post(
            f"{API_URL}/projects",
            json={"prompt": prompt},
            timeout=10.0
        )
    except httpx.ConnectError:
        print("[FAIL] Connection to FastAPI Gateway failed. Is Docker Compose up?")
        return False

    if response.status_code != 201:
        print(f"[FAIL] Server returned error: {response.status_code}")
        print(response.text)
        return False
        
    project = response.json()
    project_id = project["id"]
    print(f"[SUCCESS] Pipeline created. Project ID: {project_id}")
    print(f"Parsed DAG Tasks: {[t['id'].split('_')[-2] + '_' + t['id'].split('_')[-1] for t in project['tasks']]}")
    
    # 2. Poll status until completion
    print("\n2. Polling task status updates from Postgres Gateway...")
    max_attempts = 30
    attempt = 0
    completed = False
    
    while attempt < max_attempts:
        status_resp = httpx.get(f"{API_URL}/projects/{project_id}")
        if status_resp.status_code != 200:
            print(f"[FAIL] Failed to fetch project state: {status_resp.status_code}")
            return False
            
        proj_state = status_resp.json()
        status = proj_state["status"]
        
        # Display current statuses
        task_states = [f"{t['id'].split('_')[-2]}_{t['id'].split('_')[-1]}: {t['status']}" for t in proj_state["tasks"]]
        print(f"[{attempt+1:02d}/{max_attempts}] Project Status: {status} | Tasks -> {', '.join(task_states)}")
        
        if status == "COMPLETED":
            completed = True
            break
        elif status == "FAILED":
            print("[FAIL] Pipeline execution failed on one of the agents. Check worker logs.")
            return False
            
        time.sleep(3.0)
        attempt += 1

    if not completed:
        print("[FAIL] Pipeline execution timed out.")
        return False

    # 3. Verify final artifacts are served
    print("\n3. Verifying output artifacts on shared volume mount...")
    report_url = f"http://localhost:8000/shared/{project_id}_report.md"
    plot_url = f"http://localhost:8000/shared/{project_id}_plot.png"
    
    report_resp = httpx.get(report_url)
    plot_resp = httpx.get(plot_url)
    
    if report_resp.status_code == 200 and plot_resp.status_code == 200:
        print("[SUCCESS] Report Markdown file is accessible!")
        print("[SUCCESS] Matplotlib chart image file is accessible!")
        print("\n==================================================")
        print("VERIFICATION COMPLETED: All systems operational!")
        print("==================================================")
        return True
    else:
        print(f"[FAIL] Missing static output files. Report Code: {report_resp.status_code}, Plot Code: {plot_resp.status_code}")
        return False

if __name__ == "__main__":
    test_pipeline()
