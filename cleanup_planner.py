"""
cleanup_planner.py - proposes what COULD be cleaned up.

Important: this module only PROPOSES actions. It never deletes or
touches anything itself - see policy_validator.py (which checks each
proposal against policy.json) and executor.py (which is the only
module allowed to actually move a file).

Currently rule-based (age + file type) rather than LLM-driven, so it
works with zero setup. If Ollama is available, ask_ai_reason() in
scanner.py already explains individual files - a natural next step
for a teammate would be to also ask the AI to justify *cleanup*
decisions specifically. That's intentionally left as an extension
point rather than built in, so the rule-based path always works even
with no AI running.
"""

from typing import List, Dict


def build_cleanup_plan(files: List[Dict], policy: Dict) -> List[Dict]:
    """
    files: the 'top_files' list from a scan_result (each has path,
    size_mb, age_days, owner, type).
    policy: the loaded policy.json contents.

    Returns a list of candidate actions - NOT yet validated or safe
    to run. Every candidate must still pass policy_validator.py.
    """
    min_age = policy.get("min_age_days", 30)
    cleanup_types = set(policy.get("cleanup_types", []))
    compress_types = set(policy.get("compress_instead_of_delete", []))
    max_actions = policy.get("max_actions_per_plan", 25)

    candidates = []

    for f in files:
        file_type = f.get("type", "other")
        age_days = f.get("age_days", 0)

        if file_type not in cleanup_types:
            continue
        if age_days < min_age:
            continue

        action = "compress" if file_type in compress_types else "delete"

        candidates.append({
            "action": action,
            "path": f["path"],
            "size_mb": f.get("size_mb", 0),
            "age_days": age_days,
            "type": file_type,
            "owner": f.get("owner", "unknown"),
            "reason": f"{file_type} file, {age_days} days old (policy threshold: {min_age} days)",
        })

    # Biggest potential space savings first.
    candidates.sort(key=lambda c: c["size_mb"], reverse=True)

    return candidates[:max_actions]
