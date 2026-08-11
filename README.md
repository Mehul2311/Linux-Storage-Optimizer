# Disk Usage Analyzer — Fleet + Cleanup + Live Agents

Read-only-by-default disk analysis tool with an AI-assisted cleanup
pipeline (delete/compress proposals, policy-validated, human-approved
before anything runs), plus a real multi-server agent architecture.

## Two ways this monitors servers

1. **Local-scan servers** (`servers.json`, the dashboard's server
   dropdown) - folders the central server scans directly, on demand,
   when you click "Scan Now." Good for folders on the same machine
   the dashboard is running on.

2. **Live Agents** (`agent/` folder, run separately on any machine)
   - a real background process that checks its own disk usage on an
   interval and PUSHES a report to the central server once past a
   threshold, without you needing to click anything. This is the
   piece that matches the original project blueprint's "Central AI
   Server + Linux Agent nodes" design. See `agent/README.md`.

Both flows end up in the same place: a policy-validated cleanup plan
you review and approve from the dashboard, executed via the OS trash
(never a permanent delete).

## Project structure

```
disk-analyzer/
  server.py              - Central FastAPI server, the only thing the dashboard talks to
  scanner.py              - Local-scan folder scanner + Ollama reasoning
  cleanup_planner.py       - Proposes delete/compress actions for local scans
  policy_validator.py       - Safety gate, single source of truth for what's allowed
  executor.py                 - Performs LOCAL cleanup (send2trash, never permanent)
  scoring.py                   - Risk score for agent-flagged files
  ai_recommend.py                - Ollama recommendation for agent-flagged files
  policy.json                     - The actual rules policy_validator enforces
  servers.json                     - Local-scan folder registry
  agents.json                       - Registered live agents (created automatically)
  agent_reports/                     - Latest report per agent (created automatically)
  dashboard.html                      - Main UI: scanning, filters, cleanup, live agents
  manage.html                          - Add/edit/delete local-scan servers
  models.py                             - Shared data contract for the team
  agent/                                 - Separate deployable piece, see agent/README.md
    agent_app.py
    scanner.py
    local_executor.py
    policy_validator.py, policy_client.py
    registration.py, metadata.py, user_mapper.py
    config.yaml
```

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (central) - includes `send2trash` for safe deletion
- Ollama with `qwen2.5:3b` pulled, for AI reasoning and cleanup recommendations (optional - both `scanner.py` and `ai_recommend.py` fall back gracefully if it's unreachable)

## How to run (central server)

```bash
pip install -r requirements.txt
uvicorn server:app --port 8080 --reload
```

Open `http://localhost:8080/dashboard`.

## How to run an agent

See `agent/README.md` - it's a separate `pip install` and a separate
`uvicorn` process, meant to run on whatever machine you want
monitored (can be the same PC for testing, or a real separate Linux
server).

## Safety model

- Nothing is ever permanently deleted - everything goes through the
  OS Recycle Bin / Trash (`send2trash`), whether it's a local-scan
  cleanup or a live-agent cleanup.
- Every proposed action is validated against `policy.json` at least
  twice: once when the plan is generated (so you only ever see
  allowed actions), and again independently right before execution
  (never trust a plan just because it was shown or approved earlier).
- Agent-sourced actions get a third check, on the agent's own
  machine, right before it touches the file - the central server's
  approval is treated as a proposal, never a guarantee.
- Protected system paths, protected file extensions, a minimum file
  age, and (for agent files) a minimum UID are all enforced from the
  same `policy.json` - there's no second, separately-maintained copy
  of "what's protected" anywhere in the project.
