# Disk Usage Analyzer — Fleet + Cleanup + Live Agents + Auth

Read-only-by-default disk analysis tool with an AI-assisted cleanup
pipeline (delete/compress proposals, policy-validated, human-approved
before anything runs), a real multi-server agent architecture, simple
account auth, and an audit trail of every cleanup action.

## Three ways this monitors servers

1. **Local-scan servers** (`servers.json`, the dashboard's server
   dropdown) - folders the central server scans directly, on demand,
   when you click "Scan Now." Good for folders on the same machine
   the dashboard is running on.

2. **Remote SSH servers** (`remote_servers.json`, added from Manage
   Servers → "Remote SSH Servers") - point at a real nearby machine
   by IP/hostname + username/password (or an SSH key). The central
   server connects out over SSH whenever you click "Scan Now" and
   runs the scan there (pull model) - nothing needs to be
   pre-installed on that machine, just SSH access and `python3` on
   its PATH. See `ssh_scanner.py`. "Delete" actions on a remote
   machine move the file into a `.node_sanity_trash` folder under
   the scanned base_path instead of the OS Recycle Bin, since
   `send2trash` isn't guaranteed to be installed there.

3. **Live Agents** (`agent/` folder, run separately on any machine)
   - a real background process that checks its own disk usage on an
   interval and PUSHES a report to the central server once past a
   threshold, without you needing to click anything. This is the
   piece that matches the original project blueprint's "Central AI
   Server + Linux Agent nodes" design. See `agent/README.md`.

All three flows end up in the same place: a policy-validated cleanup
plan you review and approve from the dashboard, executed without ever
permanently deleting anything, and logged to the Activity panel.

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
  ssh_scanner.py                    - Connects out over SSH, runs a stdlib-only scan/execute script remotely
  remote_servers.json                - Remote SSH server registry (created automatically)
  agents.json                       - Registered live agents (created automatically)
  users.json, sessions.json          - Account auth store (created automatically)
  activity_log.json                   - Audit trail of signups/logins/cleanups (created automatically)
  agent_reports/                       - Latest report per agent (created automatically)
  dashboard.html                        - Main UI: scanning, filters, cleanup, live agents, activity log
  manage.html                            - Add/edit/delete local-scan servers
  login.html                              - Animated sign-in / sign-up page
  models.py                                - Shared data contract for the team
  static/favicon.ico, static/logo-mark.png  - Original terminal-themed icon
  agent/                                     - Separate deployable piece, see agent/README.md
    agent_app.py
    metadata.py
    local_executor.py
    policy_validator.py, policy_client.py
    registration.py, user_mapper.py
    config.yaml
```

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (central) - includes `send2trash` for safe local deletion and `paramiko` for SSH to remote servers
- Ollama with `qwen2.5:3b` pulled, for AI reasoning and cleanup recommendations (optional - both `scanner.py` and `ai_recommend.py` fall back gracefully if it's unreachable)

## How to run (central server)

```bash
pip install -r requirements.txt
uvicorn server:app --port 8080 --reload
```

Open `http://localhost:8080/login`, create an account, and you'll
land on the dashboard. From there, "Manage Servers" and "Live Agents"
are both one click away.

## How to run an agent

See `agent/README.md` - it's a separate `pip install` and a separate
`uvicorn` process, meant to run on whatever machine you want
monitored (can be the same PC for testing, or a real separate Linux
server).

## Accounts and activity

Sign-in is required to reach the dashboard or server manager -
`login.html` handles both sign-up and sign-in with a single form.
Passwords are stored as salted PBKDF2 hashes (`users.json`), never in
plain text, and sessions are opaque tokens (`sessions.json`) sent as
a Bearer header. This is intentionally simple - a real deployment
open to the internet would want a proper auth provider instead, but
this is more than enough for a team project or a small trusted
network.

Every sign-in, sign-up, and cleanup execution (local or agent-based)
is appended to `activity_log.json` and shown newest-first in the
dashboard's Activity Log panel, so it's always clear who ran what.

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
- Every executed cleanup is attributed to the signed-in user and
  recorded in the activity log, so actions are auditable after the
  fact.
