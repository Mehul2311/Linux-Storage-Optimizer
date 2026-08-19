"""
ssh_scanner.py - connects OUT to a real remote server via SSH and
runs a scan there, pulling results back. This is the "pull" model:
you give this server's IP/username/credentials once in the dashboard,
and the central server initiates the connection whenever you click
Scan - unlike agents (agent/ folder), which "push" reports in on
their own schedule and need software pre-installed there.

Requires only that the remote machine has:
  - SSH access enabled (works for Linux, macOS, and Windows with
    OpenSSH Server turned on)
  - python3 available on its PATH

Nothing extra is installed on the remote machine - the scan logic is
sent over as a single self-contained script and run inline via
`python3 -c`, using only the Python standard library, so it works on
a bare remote machine with no pip installs needed there.
"""

import base64
import json
from typing import Dict, Optional

import paramiko

# Runs on the REMOTE machine, not here - cannot import anything from
# this project. Deliberately stdlib-only, and produces the exact same
# JSON shape scanner.py's local scan does, so the dashboard can
# render results from either source with no extra frontend branching.
_REMOTE_SCAN_SCRIPT = r'''
import os, sys, time, json, shutil

target = sys.argv[1] if len(sys.argv) > 1 else "."

try:
    import pwd
    HAS_PWD = True
except ImportError:
    HAS_PWD = False

def get_owner(path):
    if not HAS_PWD:
        return "unknown"
    try:
        return pwd.getpwuid(os.stat(path).st_uid).pw_name
    except Exception:
        return "unknown"

def guess_type(path):
    lower = path.lower().replace("\\", "/")
    if "/log" in lower or lower.endswith(".log"):
        return "log"
    if "/cache" in lower:
        return "cache"
    if "backup" in lower:
        return "backup"
    if lower.endswith(".iso"):
        return "iso"
    if "/tmp" in lower or "/temp" in lower:
        return "tmp"
    return "other"

def disk_usage(path):
    try:
        total, used, free = shutil.disk_usage(path)
        return {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "percent_used": round((used/total)*100, 1) if total else 0.0,
        }
    except Exception:
        return None

results = []
now = time.time()
for root, dirs, files in os.walk(target):
    for name in files:
        full = os.path.join(root, name)
        try:
            size_mb = round(os.path.getsize(full) / (1024*1024), 2)
            age_days = round((now - os.path.getmtime(full)) / 86400, 1)
            results.append({
                "path": full, "size_mb": size_mb, "age_days": age_days,
                "owner": get_owner(full), "type": guess_type(full),
            })
        except OSError:
            continue

results.sort(key=lambda x: x["size_mb"], reverse=True)
top = results[:50]

output = {
    "folder_scanned": target,
    "total_files": len(results),
    "disk_usage": disk_usage(target),
    "largest_file": top[0] if top else None,
    "top_files": top,
}
print(json.dumps(output))
'''


def _connect(host: str, port: int, username: str, auth_method: str,
             password: Optional[str], key_path: Optional[str]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if auth_method == "key":
        client.connect(host, port=port, username=username, key_filename=key_path, timeout=10)
    else:
        client.connect(host, port=port, username=username, password=password, timeout=10)

    return client


def _shell_quote(value: str) -> str:
    """POSIX-safe single-quoting for the remote path argument, so
    spaces/special characters in the path don't break the remote
    shell's parsing of the command."""
    return "'" + value.replace("'", "'\\''") + "'"


# Runs on the REMOTE machine. Reads a JSON list of actions from
# stdin, re-validates each one independently (never trusts what the
# central server already approved), then performs it. Since we can't
# assume send2trash - or any pip package at all - is installed on an
# arbitrary remote machine, "delete" moves the file into a
# .node_sanity_trash folder inside the scanned base_path instead of
# removing it, using only shutil (stdlib) - still fully reversible,
# just manually rather than via the OS trash UI.
_REMOTE_EXECUTE_SCRIPT = r'''
import os, sys, json, gzip, shutil

payload = json.loads(sys.stdin.read())
actions = payload["actions"]
base_path = payload["base_path"]
never_touch_paths = payload["never_touch_paths"]
never_touch_extensions = set(e.lower() for e in payload["never_touch_extensions"])
min_age_days = payload["min_age_days"]
cleanup_types = set(payload["cleanup_types"])

def normalize(p):
    return os.path.normpath(p).replace("\\", "/").lower()

def is_within(path, parent):
    p, par = normalize(path), normalize(parent)
    return p == par or p.startswith(par + "/")

def trash_path_for(path):
    rel = os.path.relpath(path, base_path)
    dest = os.path.join(base_path, ".node_sanity_trash", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest

executed, failed = [], []

for action in actions:
    path = action.get("path", "")
    reason = None

    if not path or not os.path.exists(path):
        reason = "File no longer exists"
    elif not is_within(path, base_path):
        reason = "Path is outside the allowed scan scope"
    elif any(is_within(path, blocked) for blocked in never_touch_paths):
        reason = "Path is inside a protected system location"
    elif os.path.splitext(path)[1].lower() in never_touch_extensions:
        reason = "Protected file extension"
    elif action.get("age_days", 0) < min_age_days:
        reason = "File is newer than the policy age threshold"
    elif action.get("type") not in cleanup_types:
        reason = "File type is not in the cleanup allow-list"

    if reason:
        failed.append({**action, "error": "Rejected at remote execution time: " + reason})
        continue

    try:
        if action["action"] == "compress":
            gz_path = path + ".gz"
            with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            shutil.move(path, trash_path_for(path))
            executed.append({**action, "result": "Compressed to " + gz_path + ", original moved to .node_sanity_trash"})
        elif action["action"] == "delete":
            shutil.move(path, trash_path_for(path))
            executed.append({**action, "result": "Moved to .node_sanity_trash under the scanned folder"})
        else:
            failed.append({**action, "error": "Unknown action type"})
    except Exception as e:
        failed.append({**action, "error": str(e)})

print(json.dumps({"executed": executed, "failed": failed}))
'''


def remote_execute(host: str, port: int, username: str, auth_method: str,
                    actions: list, base_path: str, policy: dict,
                    password: Optional[str] = None, key_path: Optional[str] = None,
                    timeout: int = 60) -> Dict:
    """
    Sends the approved actions + the relevant policy rules to the
    remote machine over stdin, and runs the embedded execute script
    there. The remote script re-validates every action independently
    before touching anything - the central server's earlier approval
    is treated as a proposal, never a guarantee, same principle as
    the agent flow.
    """
    client = _connect(host, port, username, auth_method, password, key_path)

    try:
        encoded = base64.b64encode(_REMOTE_EXECUTE_SCRIPT.encode()).decode()
        remote_command = f"python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode())\""

        stdin, stdout, stderr = client.exec_command(remote_command, timeout=timeout)

        payload = {
            "actions": actions,
            "base_path": base_path,
            "never_touch_paths": policy.get("never_touch_paths", []),
            "never_touch_extensions": policy.get("never_touch_extensions", []),
            "min_age_days": policy.get("min_age_days", 30),
            "cleanup_types": policy.get("cleanup_types", []),
        }
        stdin.write(json.dumps(payload))
        stdin.channel.shutdown_write()

        exit_status = stdout.channel.recv_exit_status()
        output = stdout.read().decode()
        error_output = stderr.read().decode().strip()

        if exit_status != 0:
            raise RuntimeError(f"Remote execute failed (exit {exit_status}): {error_output[:500]}")

        return json.loads(output.strip().splitlines()[-1])

    finally:
        client.close()


def test_connection(host: str, port: int, username: str, auth_method: str,
                     password: Optional[str] = None, key_path: Optional[str] = None) -> Dict:
    """Just checks that the credentials actually work - no scan, no
    file access. Used by the 'Test Connection' button when adding or
    editing a remote server."""
    try:
        client = _connect(host, port, username, auth_method, password, key_path)
        client.close()
        return {"status": "ok", "message": "Connected successfully"}
    except paramiko.AuthenticationException:
        return {"status": "error", "message": "Authentication failed - check username/password/key"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def remote_scan(host: str, port: int, username: str, auth_method: str,
                 base_path: str, password: Optional[str] = None,
                 key_path: Optional[str] = None, timeout: int = 60) -> Dict:
    """
    Connects, runs the embedded scan script remotely, returns the
    same JSON shape scanner.py produces locally. Raises RuntimeError
    on any connection/auth/execution failure - server.py turns that
    into an HTTP error the dashboard can show.
    """
    client = _connect(host, port, username, auth_method, password, key_path)

    try:
        encoded = base64.b64encode(_REMOTE_SCAN_SCRIPT.encode()).decode()
        remote_command = (
            f"python3 -c \"import base64; "
            f"exec(base64.b64decode('{encoded}').decode())\" "
            f"{_shell_quote(base_path)}"
        )

        stdin, stdout, stderr = client.exec_command(remote_command, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        output = stdout.read().decode()
        error_output = stderr.read().decode().strip()

        if exit_status == 127 or "not found" in error_output.lower():
            raise RuntimeError(
                "Remote command failed - is python3 installed and on PATH on that machine? "
                f"({error_output[:200]})"
            )
        if exit_status != 0:
            raise RuntimeError(f"Remote scan failed (exit {exit_status}): {error_output[:500]}")

        try:
            # Take the last line in case anything else got printed
            # (e.g. a shell login banner) before our JSON output.
            return json.loads(output.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            raise RuntimeError(f"Could not parse remote scan output: {output[:300]}")

    finally:
        client.close()
