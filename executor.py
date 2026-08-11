"""
executor.py - the only module in this project allowed to actually
move or modify a file. Everything upstream (planner, validator) only
produces proposals - nothing is real until it reaches here.

Safety design:
  - Re-validates every action against policy.json right before
    running it (never trusts a plan just because it was approved
    earlier - see policy_validator.py's docstring for why).
  - Never permanently deletes. "Delete" actions are sent to the
    OS Recycle Bin / Trash via send2trash, so anything can be
    manually recovered if a mistake happens.
  - "Compress" actions gzip the file next to the original, then
    send the original to the Recycle Bin (not delete it directly).
  - Every action is attempted independently and wrapped in
    try/except, so one failure doesn't stop the rest of the batch.
"""

import gzip
import os
import shutil
from typing import Dict, List

from send2trash import send2trash

from policy_validator import validate_action


def _compress_file(path: str) -> str:
    gz_path = path + ".gz"
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return gz_path


def execute_actions(actions: List[Dict], policy: Dict, scanned_base_path: str) -> Dict[str, List[Dict]]:
    """
    Runs each action (already meant to be user-approved) through a
    fresh policy check, then performs it. Returns
    {"executed": [...], "failed": [...]} - failed entries include
    an 'error' field explaining what went wrong (rejected by policy,
    filesystem error, etc).
    """
    executed, failed = [], []

    for action in actions:
        ok, reason = validate_action(action, policy, scanned_base_path)
        if not ok:
            failed.append({**action, "error": f"Rejected at execution time: {reason}"})
            continue

        path = action["path"]
        try:
            if action["action"] == "compress":
                gz_path = _compress_file(path)
                send2trash(path)
                executed.append({**action, "result": f"Compressed to {gz_path}, original sent to trash"})

            elif action["action"] == "delete":
                send2trash(path)
                executed.append({**action, "result": "Sent to Recycle Bin / Trash"})

            else:
                failed.append({**action, "error": f"Unknown action type '{action['action']}'"})

        except Exception as e:
            failed.append({**action, "error": str(e)})

    return {"executed": executed, "failed": failed}
