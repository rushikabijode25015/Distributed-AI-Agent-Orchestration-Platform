// Application State Management
let activeProjectId = null;
let tasks = [];
let taskLogs = {
    research: "[Researcher Console] Awaiting execution...\n",
    code_execution: "[Sandbox Coder Console] Awaiting execution...\n",
    writer: "[Writer Console] Awaiting execution...\n"
};
let activeTab = "researcher"; // researcher, coder, writer
let ws = null;
let reconnectTimer = null;

// DOM Elements
const wsStatus = document.getElementById("websocket-status");
const pipelineForm = document.getElementById("pipeline-form");
const promptInput = document.getElementById("prompt-input");
const submitBtn = document.getElementById("submit-btn");
const projectMetaBox = document.getElementById("project-meta-box");
const projectIdDisplay = document.getElementById("project-id-display");
const projectStatusDisplay = document.getElementById("project-status-display");
const dagEmpty = document.getElementById("dag-empty");
const dagNodesContainer = document.getElementById("dag-nodes-container");
const dagEdgesSvg = document.getElementById("dag-edges-svg");
const consoleTerminal = document.getElementById("console-terminal-body");
const reportContentBody = document.getElementById("report-content-body");

// Console Tabs
const tabButtons = {
    researcher: document.getElementById("tab-btn-researcher"),
    coder: document.getElementById("tab-btn-coder"),
    writer: document.getElementById("tab-btn-writer")
};

// Setup Tab Event Listeners
Object.keys(tabButtons).forEach(key => {
    tabButtons[key].addEventListener("click", () => {
        // Toggle Active Tab class
        Object.keys(tabButtons).forEach(k => tabButtons[k].classList.remove("active"));
        tabButtons[key].classList.add("active");
        activeTab = key;
        updateConsoleDisplay();
    });
});

// Submit prompt to launch new pipeline run
pipelineForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = promptInput.value.trim();
    if (!prompt) return;

    // Reset console logs
    taskLogs = {
        research: "[Researcher Console] Initializing task...\n",
        code_execution: "[Sandbox Coder Console] Awaiting dependencies...\n",
        writer: "[Writer Console] Awaiting dependencies...\n"
    };
    updateConsoleDisplay();

    // Disable button & animate loading
    submitBtn.disabled = true;
    submitBtn.querySelector("span").textContent = "Parsing & Scheduling...";

    try {
        const response = await fetch("/api/projects", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ prompt: prompt })
        });

        if (!response.ok) {
            throw new Error(`Server returned error status: ${response.status}`);
        }

        const project = await response.json();
        
        // Load active project meta info
        activeProjectId = project.id;
        projectIdDisplay.textContent = project.id;
        projectStatusDisplay.textContent = project.status;
        projectStatusDisplay.className = `meta-value status-pill status-${project.status.toLowerCase()}`;
        projectMetaBox.style.display = "block";

        // Setup live listener WebSocket
        connectWebSocket(project.id);

        // Reset submit button state
        submitBtn.disabled = false;
        submitBtn.querySelector("span").textContent = "Decompose & Execute DAG";

    } catch (err) {
        alert(`Failed to launch agent pipeline: ${err.message}`);
        submitBtn.disabled = false;
        submitBtn.querySelector("span").textContent = "Decompose & Execute DAG";
    }
});

// Establish WebSockets Channel with API Gateway
function connectWebSocket(projectId) {
    // Close existing WebSocket if active
    if (ws) {
        ws.close();
    }
    
    // Clear any pending reconnect timers
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
    }

    const loc = window.location;
    const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${loc.host}/ws/projects/${projectId}`;
    
    console.log(`Connecting to WebSocket: ${wsUrl}`);
    ws = new WebSocket(wsUrl);

    // Update status badge to active connected
    ws.onopen = () => {
        wsStatus.innerHTML = `
            <span class="status-dot status-connected"></span>
            <span class="status-text">Connected</span>
        `;
    };

    ws.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type === "ping") return; // Heartbeat ignore

        console.log("WebSocket event received: ", payload);

        if (payload.type === "init_state") {
            // Load tasks dump
            tasks = payload.tasks;
            
            // Extract logs for each agent type
            tasks.forEach(t => {
                const agentKey = mapTaskTypeToTabKey(t.type);
                if (agentKey) {
                    taskLogs[agentKey] = t.logs || `[${t.title}] Status: ${t.status}\n`;
                }
            });
            
            // Draw DAG Graph and update panels
            renderDAG();
            updateConsoleDisplay();
            checkAndRenderReport();
        } 
        else if (payload.type === "task_update") {
            // Update individual task in tasks state array
            const taskIndex = tasks.findIndex(t => t.id === payload.task_id);
            if (taskIndex !== -1) {
                tasks[taskIndex].status = payload.status;
                tasks[taskIndex].logs = payload.logs;
                tasks[taskIndex].thoughts = payload.thoughts;
                tasks[taskIndex].output_data = payload.output_data;
            } else {
                // If it's a new task dynamically added
                tasks.push({
                    id: payload.task_id,
                    project_id: payload.project_id,
                    status: payload.status,
                    logs: payload.logs,
                    thoughts: payload.thoughts,
                    output_data: payload.output_data
                });
            }

            // Sync console logs
            const taskObj = tasks.find(t => t.id === payload.task_id);
            if (taskObj) {
                const agentKey = mapTaskTypeToTabKey(taskObj.type);
                if (agentKey) {
                    taskLogs[agentKey] = payload.logs;
                }
            }

            renderDAG();
            updateConsoleDisplay();
            checkAndRenderReport();
        }
        else if (payload.type === "project_update") {
            projectStatusDisplay.textContent = payload.status;
            projectStatusDisplay.className = `meta-value status-pill status-${payload.status.toLowerCase()}`;
            
            if (payload.status === "COMPLETED") {
                // Trigger report lookup
                setTimeout(fetchFullReport, 500);
            }
        }
    };

    ws.onclose = () => {
        wsStatus.innerHTML = `
            <span class="status-dot status-disconnected"></span>
            <span class="status-text">Disconnected (Retrying)</span>
        `;
        
        // Exponential/Fixed retry handler (Automatic Reconnection)
        reconnectTimer = setTimeout(() => {
            connectWebSocket(projectId);
        }, 3000);
    };

    ws.onerror = (err) => {
        console.error("WS connection error occurred:", err);
    };
}

// Maps backend task type string to local UI tab string
function mapTaskTypeToTabKey(type) {
    if (type === "research") return "research";
    if (type === "code_execution") return "code_execution";
    if (type === "writer") return "writer";
    return null;
}

// Updates terminal logger view with current tab's content
function updateConsoleDisplay() {
    let activeLogs = "";
    if (activeTab === "researcher") {
        activeLogs = taskLogs.research;
    } else if (activeTab === "coder") {
        activeLogs = taskLogs.code_execution;
    } else if (activeTab === "writer") {
        activeLogs = taskLogs.writer;
    }

    // Clean up lines and render nicely
    consoleTerminal.innerHTML = "";
    const lines = activeLogs.split("\n");
    lines.forEach(line => {
        const lineDiv = document.createElement("div");
        lineDiv.className = "terminal-line";
        
        // Color tags
        if (line.startsWith("[Error]") || line.startsWith("[Worker Exception Error]")) {
            lineDiv.classList.add("error-line");
        } else if (line.startsWith("[System]") || line.startsWith("[Researcher Agent Started]") || line.startsWith("[Code Sandbox Agent Started]") || line.startsWith("[Writer Agent Started]")) {
            lineDiv.classList.add("info-line");
        } else if (line.includes("successfully") || line.includes("Success")) {
            lineDiv.classList.add("success-line");
        } else if (line.includes("Warning") || line.includes("Rate limit")) {
            lineDiv.classList.add("warn-line");
        } else {
            lineDiv.classList.add("system-line");
        }

        lineDiv.textContent = line;
        consoleTerminal.appendChild(lineDiv);
    });

    // Auto-scroll terminal to bottom
    consoleTerminal.scrollTop = consoleTerminal.scrollHeight;
}

// Draw DAG Nodes dynamically
function renderDAG() {
    // Hide empty state
    dagEmpty.style.display = "none";
    dagNodesContainer.innerHTML = "";
    dagEdgesSvg.innerHTML = "";

    if (!tasks || tasks.length === 0) {
        dagEmpty.style.display = "block";
        return;
    }

    // Sort tasks logically by ID
    const sortedTasks = [...tasks].sort((a, b) => a.id.localeCompare(b.id));

    // Render nodes
    sortedTasks.forEach(task => {
        const node = document.createElement("div");
        node.id = `node-${task.id}`;
        
        let statusClass = "";
        if (task.status === "RUNNING") statusClass = "active-run";
        else if (task.status === "COMPLETED") statusClass = "done";
        else if (task.status === "FAILED") statusClass = "error";

        node.className = `dag-node ${statusClass}`;
        node.innerHTML = `
            <div class="node-header">
                <span class="node-title">${task.title}</span>
                <span class="node-type">${task.type}</span>
            </div>
            <div class="node-desc">Inputs: ${JSON.stringify(task.input_data)}</div>
            <div class="node-status ${task.status.toLowerCase()}-text" style="color: ${getStatusColor(task.status)}">
                ● ${task.status}
            </div>
        `;
        dagNodesContainer.appendChild(node);
    });

    // Delay drawing SVGs to ensure node positions are rendered by browser
    setTimeout(drawConnectorLines, 100);
}

function getStatusColor(status) {
    if (status === "RUNNING") return "#3b82f6";
    if (status === "COMPLETED") return "#10b981";
    if (status === "FAILED") return "#ef4444";
    return "#64748b";
}

// Dynamic drawing of dependency lines
function drawConnectorLines() {
    dagEdgesSvg.innerHTML = "";
    const rect = dagNodesContainer.getBoundingClientRect();

    tasks.forEach(task => {
        if (task.depend_on_ids && task.depend_on_ids.length > 0) {
            const targetNode = document.getElementById(`node-${task.id}`);
            if (!targetNode) return;

            task.depend_on_ids.forEach(depId => {
                const sourceNode = document.getElementById(`node-${depId}`);
                if (!sourceNode) return;

                const srcRect = sourceNode.getBoundingClientRect();
                const trgRect = targetNode.getBoundingClientRect();

                // Compute coordinates relative to container
                const x1 = srcRect.left + srcRect.width / 2 - rect.left;
                const y1 = srcRect.bottom - rect.top;
                const x2 = trgRect.left + trgRect.width / 2 - rect.left;
                const y2 = trgRect.top - rect.top;

                // Draw line with glowing gradient
                const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
                
                // Draw curve or line
                const dx = x2 - x1;
                const dy = y2 - y1;
                const controlOffset = dy * 0.4;
                const d = `M ${x1} ${y1} C ${x1} ${y1 + controlOffset}, ${x2} ${y2 - controlOffset}, ${x2} ${y2}`;
                
                path.setAttribute("d", d);
                path.setAttribute("fill", "none");
                
                // Set color matching dependency path status
                const isCompleted = sourceNode.classList.contains("done") && targetNode.classList.contains("done");
                const color = isCompleted ? "#10b981" : "rgba(255, 255, 255, 0.15)";
                path.setAttribute("stroke", color);
                path.setAttribute("stroke-width", "2");
                
                if (sourceNode.classList.contains("done") && targetNode.classList.contains("active-run")) {
                    path.setAttribute("stroke", "#3b82f6");
                    path.setAttribute("stroke-dasharray", "5,5");
                    path.setAttribute("class", "flowing-dash");
                }

                dagEdgesSvg.appendChild(path);
            });
        }
    });
}

// Renders the report panel if writer finishes
function checkAndRenderReport() {
    const writerTask = tasks.find(t => t.type === "writer");
    if (writerTask && writerTask.status === "COMPLETED" && writerTask.output_data) {
        const finalReport = writerTask.output_data.final_report;
        if (finalReport) {
            reportContentBody.innerHTML = markdownToHTML(finalReport);
        }
    }
}

// Fetch complete report object directly via REST API (Alternative/Fallback)
async function fetchFullReport() {
    if (!activeProjectId) return;
    try {
        const response = await fetch(`/api/projects/${activeProjectId}`);
        if (response.ok) {
            const project = await response.json();
            const writerTask = project.tasks.find(t => t.type === "writer");
            if (writerTask && writerTask.status === "COMPLETED" && writerTask.output_data) {
                const finalReport = writerTask.output_data.final_report;
                if (finalReport) {
                    reportContentBody.innerHTML = markdownToHTML(finalReport);
                }
            }
        }
    } catch (e) {
        console.error("Failed fetching full report:", e);
    }
}

// IIT-level Markdown to HTML converter (no third party packages needed, keeps JS slim)
function markdownToHTML(markdown) {
    let html = markdown;

    // Headers
    html = html.replace(/### (.*?)\n/g, "<h3>$1</h3>");
    html = html.replace(/## (.*?)\n/g, "<h2>$1</h2>");
    html = html.replace(/# (.*?)\n/g, "<h1>$1</h1>");

    // Fenced Code blocks
    html = html.replace(/```python\s*([\s\S]*?)\s*```/g, "<pre><code class='language-python'>$1</code></pre>");
    html = html.replace(/```text\s*([\s\S]*?)\s*```/g, "<pre><code class='language-text'>$1</code></pre>");
    html = html.replace(/```\s*([\s\S]*?)\s*```/g, "<pre><code>$1</code></pre>");

    // Images
    html = html.replace(/!\[(.*?)\]\((.*?)\)/g, "<img src='$2' alt='$1'>");

    // Bold
    html = html.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");

    // Bullet Lists
    html = html.replace(/\n- (.*?)\n/g, "<li>$1</li>");
    // Wrap lists (very simple grouping)
    html = html.replace(/(<li>.*?<\/li>)/gs, "<ul>$1</ul>");
    
    // Line breaks
    html = html.replace(/\n\n/g, "<p></p>");

    return html;
}

// Handle window resizing to keep SVG coordinates correct
window.addEventListener("resize", () => {
    if (tasks && tasks.length > 0) {
        drawConnectorLines();
    }
});
