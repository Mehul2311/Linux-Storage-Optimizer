"""
server.py - FastAPI backend.

New in this version (on top of your original single-folder scanner):
  - GET /servers        -> list of configured servers (servers.json)
  - GET /scan           -> now takes a server_id, and supports optional
                            owner/type filters, matching the new
                            multi-server + user-based-filter UI

Kept backward compatible: /scan?folder=<path> still works exactly like
before if you don't pass a server_id, so nothing that already depends
on the old behavior breaks.
"""

import json
import os
import re
import subprocess
import sys
import time
from typing import List, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from cleanup_planner import build_cleanup_plan
from policy_validator import validate_plan, validate_action
from executor import execute_actions
from scoring import calculate_score
from ai_recommend import ask_recommendation

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS_PATH = os.path.join(BASE_DIR, "servers.json")
POLICY_PATH = os.path.join(BASE_DIR, "policy.json")
AGENTS_PATH = os.path.join(BASE_DIR, "agents.json")
REPORTS_DIR = os.path.join(BASE_DIR, "agent_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


def _load_policy() -> dict:
    with open(POLICY_PATH, "r") as f:
        return json.load(f)


@app.get("/dashboard")
def dashboard():
    """
    Serves the dashboard webpage directly from the backend, so you
    only ever need to run ONE command and visit ONE url:
    http://localhost:8080/dashboard - no separate double-clicking a
    file or dealing with Chrome misreading a bare filename as a website.
    """
    return FileResponse(os.path.join(BASE_DIR, "dashboard.html"))


@app.get("/manage")
def manage_page():
    """The Server Manager page - add/edit/delete servers via a form."""
    return FileResponse(os.path.join(BASE_DIR, "manage.html"))


class ServerCreate(BaseModel):
    name: str = Field(..., min_length=1)
    hostname: str = Field(..., min_length=1)
    base_path: str = Field(..., min_length=1)
    owner_team: Optional[str] = None
    enabled: bool = True


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    hostname: Optional[str] = None
    base_path: Optional[str] = None
    owner_team: Optional[str] = None
    enabled: Optional[bool] = None


def _save_servers(servers: list[dict]) -> None:
    with open(SERVERS_PATH, "w") as f:
        json.dump(servers, f, indent=2)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "server"


def _unique_id(base_slug: str, existing_ids: set[str]) -> str:
    candidate = base_slug
    n = 2
    while candidate in existing_ids:
        candidate = f"{base_slug}-{n}"
        n += 1
    return candidate


def _load_servers() -> list[dict]:
    if not os.path.exists(SERVERS_PATH):
        return []
    with open(SERVERS_PATH, "r") as f:
        servers = json.load(f)
    # Backward compatibility: older servers.json entries won't have
    # 'enabled' at all - treat those as enabled by default.
    for s in servers:
        s.setdefault("enabled", True)
    return servers


def _find_server(server_id: str) -> Optional[dict]:
    for s in _load_servers():
        if s["id"] == server_id:
            return s
    return None


@app.get("/")
def home():
    return {"status": "Disk Analyzer API is running"}


@app.get("/servers")
def list_servers(enabled_only: bool = False):
    """
    Powers the server-switcher dropdown in the dashboard. Teammates
    building their own module can also call this to know what
    servers/folders exist, instead of hard-coding a path.

    Pass ?enabled_only=true to get only enabled servers - the
    dashboard's scan dropdown uses this so a disabled server is
    skipped without needing to delete its configuration.
    """
    servers = _load_servers()
    if enabled_only:
        servers = [s for s in servers if s.get("enabled", True)]
    return servers


@app.post("/servers")
def create_server(payload: ServerCreate):
    """Adds a new server entry. Generates a unique id from the name."""
    servers = _load_servers()
    existing_ids = {s["id"] for s in servers}
    new_id = _unique_id(_slugify(payload.name), existing_ids)

    new_server = {
        "id": new_id,
        "name": payload.name,
        "hostname": payload.hostname,
        "base_path": payload.base_path,
        "owner_team": payload.owner_team,
        "enabled": payload.enabled,
    }
    servers.append(new_server)
    _save_servers(servers)
    return new_server


@app.put("/servers/{server_id}")
def update_server(server_id: str, payload: ServerUpdate):
    """Updates an existing server's fields. Only provided fields change."""
    servers = _load_servers()
    for s in servers:
        if s["id"] == server_id:
            if payload.name is not None:
                s["name"] = payload.name
            if payload.hostname is not None:
                s["hostname"] = payload.hostname
            if payload.base_path is not None:
                s["base_path"] = payload.base_path
            if payload.owner_team is not None:
                s["owner_team"] = payload.owner_team
            if payload.enabled is not None:
                s["enabled"] = payload.enabled
            _save_servers(servers)
            return s
    raise HTTPException(status_code=404, detail=f"Unknown server_id '{server_id}'")


@app.delete("/servers/{server_id}")
def delete_server(server_id: str):
    """Removes a server entry."""
    servers = _load_servers()
    remaining = [s for s in servers if s["id"] != server_id]
    if len(remaining) == len(servers):
        raise HTTPException(status_code=404, detail=f"Unknown server_id '{server_id}'")
    _save_servers(remaining)
    return {"deleted": server_id}


def _resolve_target(folder: Optional[str], server_id: Optional[str]) -> tuple[str, str]:
    """Returns (target_folder, server_name), same resolution rule /scan
    has always used - shared with /cleanup-plan so both agree on
    exactly what folder is in scope."""
    if server_id:
        server = _find_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"Unknown server_id '{server_id}'")
        return server["base_path"], server["name"]
    return (folder or "/var/log"), "custom"


def _run_scanner(target_folder: str) -> dict:
    """Runs scanner.py against target_folder and returns the parsed
    scan_result.json. Raises HTTPException on failure."""
    # sys.executable = whatever python is running THIS server (python.exe on
    # Windows, python3 on Linux/Mac) - hardcoding "python3" breaks on Windows.
    result = subprocess.run(
        [sys.executable, "scanner.py", target_folder],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )

    result_path = os.path.join(BASE_DIR, "scan_result.json")
    if result.returncode != 0 or not os.path.exists(result_path):
        raise HTTPException(
            status_code=500,
            detail=f"Scanner failed: {result.stderr.strip() or 'unknown error - check the server terminal'}"
        )

    with open(result_path, "r") as f:
        return json.load(f)


@app.get("/scan")
def scan(
    folder: Optional[str] = None,
    server_id: Optional[str] = None,
    owner: Optional[str] = None,
    file_type: Optional[str] = None,
):
    """
    Runs a scan and returns results, same shape as before plus:
      - each file now includes 'owner' and 'type'
      - 'available_owners' / 'available_types' for building filter dropdowns
      - results can be filtered server-side via ?owner=... and ?file_type=...

    You can call this two ways:
      /scan?server_id=srv-01           (new - looks up base_path from servers.json)
      /scan?folder=/var/log            (old - still works exactly as before)
    """
    target_folder, server_name = _resolve_target(folder, server_id)
    data = _run_scanner(target_folder)

    all_files = data.get("top_files", [])

    # Build filter dropdown options from the FULL unfiltered result,
    # so the dropdown doesn't shrink as soon as you apply a filter.
    data["available_owners"] = sorted({f.get("owner", "unknown") for f in all_files})
    data["available_types"] = sorted({f.get("type", "other") for f in all_files})

    if owner:
        all_files = [f for f in all_files if f.get("owner") == owner]
    if file_type:
        all_files = [f for f in all_files if f.get("type") == file_type]

    data["top_files"] = all_files
    data["total_files_after_filter"] = len(all_files)
    data["server_id"] = server_id or "custom"
    data["server_name"] = server_name

    return data


class CleanupAction(BaseModel):
    action: str
    path: str
    size_mb: float
    age_days: float
    type: str
    owner: str = "unknown"
    reason: str = ""


class CleanupExecuteRequest(BaseModel):
    folder: Optional[str] = None
    server_id: Optional[str] = None
    actions: List[CleanupAction]


@app.post("/cleanup-plan")
def cleanup_plan(folder: Optional[str] = None, server_id: Optional[str] = None):
    """
    Proposes a cleanup plan for a folder/server - NOTHING is deleted
    or modified by this endpoint. It runs a fresh scan, generates
    candidate actions (cleanup_planner), then checks every single one
    against policy.json (policy_validator) before returning them.

    Response: { approved: [...], rejected: [...], scanned_folder, ... }
    'approved' actions are the only ones the dashboard should let the
    user select for /cleanup-execute. 'rejected' actions are shown
    too, with a reason, so nothing silently disappears.
    """
    target_folder, server_name = _resolve_target(folder, server_id)
    data = _run_scanner(target_folder)
    policy = _load_policy()

    candidates = build_cleanup_plan(data.get("top_files", []), policy)
    result = validate_plan(candidates, policy, target_folder)

    result["scanned_folder"] = target_folder
    result["server_id"] = server_id or "custom"
    result["server_name"] = server_name
    result["policy"] = policy
    return result


@app.post("/cleanup-execute")
def cleanup_execute(payload: CleanupExecuteRequest):
    """
    Actually performs cleanup actions - the ONLY endpoint in this
    project allowed to touch a file. Every action is re-validated
    against policy.json here, independently of whatever validation
    already happened when the plan was generated (see executor.py's
    docstring for why: never trust a plan just because it was shown
    to the user earlier).

    "Delete" actions go to the OS Recycle Bin / Trash, never
    permanently removed. Requires an explicit list of actions the
    user selected - nothing runs automatically.
    """
    target_folder, _ = _resolve_target(payload.folder, payload.server_id)
    policy = _load_policy()

    actions = [a.model_dump() for a in payload.actions]
    result = execute_actions(actions, policy, target_folder)
    return result


@app.get("/policy")
def get_policy():
    """Read-only view of the current cleanup policy, for transparency
    (and so a teammate's module can check the same rules instead of
    guessing). This is also what agents fetch (policy_client.py) so
    there is exactly ONE source of truth for policy, never a
    separately-maintained copy that could drift out of sync."""
    return _load_policy()


# ==================== Agent pipeline ====================
# An "agent" is a separate machine running agent/agent_app.py - NOT
# the same thing as a "server" entry in servers.json (which is just a
# local folder this central server scans directly by running
# scanner.py itself). Agents push data in on their own schedule;
# local-scan servers get pulled on demand by the dashboard.

def _load_agents() -> dict:
    if not os.path.exists(AGENTS_PATH):
        return {}
    with open(AGENTS_PATH, "r") as f:
        return json.load(f)


def _save_agents(agents: dict) -> None:
    with open(AGENTS_PATH, "w") as f:
        json.dump(agents, f, indent=2)


class AgentRegister(BaseModel):
    hostname: str
    callback_host: str
    agent_port: int
    disk_threshold: float


@app.post("/register")
def register_agent(payload: AgentRegister):
    """Called by an agent on startup. Just records how to reach it
    back later - registering does NOT trigger a scan by itself."""
    agents = _load_agents()
    agents[payload.hostname] = {
        "hostname": payload.hostname,
        "callback_host": payload.callback_host,
        "agent_port": payload.agent_port,
        "disk_threshold": payload.disk_threshold,
        "registered_at": time.time(),
        "last_seen": time.time(),
        "last_disk_usage": None,
    }
    _save_agents(agents)
    return {"status": "registered", "hostname": payload.hostname}


class AgentFile(BaseModel):
    path: str
    size_mb: float
    age_days: float
    owner: str = "unknown"
    uid: Optional[int] = None
    protected_user: bool = False
    modified: Optional[str] = None
    accessed: Optional[str] = None
    type: str = "other"


class AgentReport(BaseModel):
    hostname: str
    disk_usage_percent: float
    target_usage: float
    files: List[AgentFile]


@app.post("/agent-report")
def agent_report(payload: AgentReport):
    """
    Called by an agent once its own disk_threshold is crossed. Scores
    each flagged file, asks Ollama for a recommendation (advisory
    only), then validates every candidate through the SAME policy
    engine local scans use - nothing here is trusted just because an
    agent sent it. 'ignore' recommendations are dropped entirely,
    never proposed as an action.
    """
    agents = _load_agents()
    if payload.hostname not in agents:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{payload.hostname}' - has it registered?")

    policy = _load_policy()
    candidates = []

    for f in payload.files:
        file_dict = f.model_dump()
        scored = {**file_dict, **calculate_score(file_dict)}
        recommendation = ask_recommendation(scored)

        if recommendation == "ignore":
            continue

        action = "compress" if recommendation in ("archive", "compress") else "delete"

        candidates.append({
            "action": action,
            "path": file_dict["path"],
            "size_mb": file_dict["size_mb"],
            "age_days": file_dict["age_days"],
            "type": file_dict["type"],
            "owner": file_dict["owner"],
            "uid": file_dict["uid"],
            "reason": f"AI recommended {recommendation} (risk: {scored['risk']}, score: {scored['score']})",
        })

    agent_config = agents[payload.hostname]
    # check_exists=False: the central server has no filesystem access
    # to this remote agent's disk - existence gets re-checked for
    # real on the agent itself, right before it actually executes.
    result = validate_plan(candidates, policy, list({os.path.dirname(c["path"]) for c in candidates}) or ["/"], check_exists=False)

    report = {
        "hostname": payload.hostname,
        "disk_usage_percent": payload.disk_usage_percent,
        "target_usage": payload.target_usage,
        "received_at": time.time(),
        "total_flagged": len(payload.files),
        "approved": result["approved"],
        "rejected": result["rejected"],
    }

    with open(os.path.join(REPORTS_DIR, f"{payload.hostname}.json"), "w") as f:
        json.dump(report, f, indent=2)

    agents[payload.hostname]["last_seen"] = time.time()
    agents[payload.hostname]["last_disk_usage"] = payload.disk_usage_percent
    _save_agents(agents)

    return {"status": "received", "approved_count": len(result["approved"]), "rejected_count": len(result["rejected"])}


@app.get("/agents")
def list_agents():
    """Powers the dashboard's 'Live Agents' panel - registered agents
    plus a summary of their latest report, if any."""
    agents = _load_agents()
    summaries = []
    for hostname, agent in agents.items():
        report_path = os.path.join(REPORTS_DIR, f"{hostname}.json")
        summary = {**agent}
        if os.path.exists(report_path):
            with open(report_path, "r") as f:
                report = json.load(f)
            summary["approved_count"] = len(report["approved"])
            summary["last_report_at"] = report["received_at"]
        else:
            summary["approved_count"] = 0
            summary["last_report_at"] = None
        summaries.append(summary)
    return summaries


@app.get("/agents/{hostname}")
def get_agent_report(hostname: str):
    """Full latest report for one agent - the approved/rejected
    action lists the dashboard renders as a checklist."""
    report_path = os.path.join(REPORTS_DIR, f"{hostname}.json")
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail=f"No report yet for '{hostname}'")
    with open(report_path, "r") as f:
        return json.load(f)


class AgentExecuteRequest(BaseModel):
    actions: List[CleanupAction]


@app.post("/agents/{hostname}/execute")
def agents_execute(hostname: str, payload: AgentExecuteRequest):
    """
    Re-validates the selected actions centrally (defense in depth),
    then relays ONLY the validated ones to that specific agent's own
    /execute endpoint - the agent re-validates AGAIN and is the only
    thing that actually touches the file, on its own disk.
    """
    agents = _load_agents()
    agent = agents.get(hostname)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{hostname}'")

    policy = _load_policy()
    actions = [a.model_dump() for a in payload.actions]
    scope = list({os.path.dirname(a["path"]) for a in actions}) or ["/"]
    validated = validate_plan(actions, policy, scope, check_exists=False)

    if not validated["approved"]:
        return {"executed": [], "failed": [], "rejected_centrally": validated["rejected"]}

    callback_url = f"http://{agent['callback_host']}:{agent['agent_port']}/execute"
    try:
        response = requests.post(callback_url, json={"actions": validated["approved"]}, timeout=60)
        response.raise_for_status()
        result = response.json()
        result["rejected_centrally"] = validated["rejected"]
        return result
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Could not reach agent '{hostname}' at {callback_url}: {e}")