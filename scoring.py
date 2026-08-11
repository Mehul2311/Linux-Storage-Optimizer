"""
scoring.py - numeric risk/priority score for agent-flagged files.

Ported from the teammates' server/scoring.py. Purely informational -
this score does NOT grant any file permission to be cleaned up; that
is policy_validator.py's job alone. This just helps the dashboard
sort "most worth looking at first" for a human reviewing a long list.
"""

from typing import Dict


def calculate_score(file: Dict) -> Dict:
    score = 0

    size_mb = file.get("size_mb", 0)
    age_days = file.get("age_days", 0)
    file_type = file.get("type", "other")

    if size_mb > 100:
        score += 40
    elif size_mb > 10:
        score += 20

    if age_days > 180:
        score += 30
    elif age_days > 90:
        score += 15

    if file_type in ("log", "tmp", "iso", "backup", "cache"):
        score += 30

    if score >= 70:
        risk = "high"
    elif score >= 40:
        risk = "medium"
    else:
        risk = "low"

    return {"score": score, "risk": risk}
