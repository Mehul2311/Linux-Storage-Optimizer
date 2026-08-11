import os
import sys
import time
import json
import shutil
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5:3b"

# Windows doesn't have the pwd module
try:
    import pwd
    HAS_PWD = True
except ImportError:
    HAS_PWD = False


def get_disk_usage(path):
    """
    Real disk capacity info for whichever drive `path` lives on -
    this is what powers the "85% full" style warning banner, as
    opposed to individual file sizes.
    """
    try:
        total, used, free = shutil.disk_usage(path)
        percent_used = round((used / total) * 100, 1) if total else 0.0
        return {
            "total_gb": round(total / (1024 ** 3), 2),
            "used_gb": round(used / (1024 ** 3), 2),
            "free_gb": round(free / (1024 ** 3), 2),
            "percent_used": percent_used,
        }
    except OSError:
        return None


def get_file_size_mb(path):
    size_bytes = os.path.getsize(path)
    return round(size_bytes / (1024 * 1024), 2)


def get_file_age_days(path):
    modified_time = os.path.getmtime(path)
    age_seconds = time.time() - modified_time
    return round(age_seconds / 86400, 1)


def get_file_owner(path):
    if not HAS_PWD:
        return "unknown"

    try:
        uid = os.stat(path).st_uid
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        return "unknown"


def guess_file_type(path):
    # Normalize backslashes to forward slashes first - on Windows,
    # os.path.join / os.walk produce paths like "C:\...\Temp\file.tmp",
    # so a check for "/tmp" (or "/log", "/cache") would never match a
    # single backslash-separated path and every file would silently
    # fall through to "other". This is what made "Suggested Cleanup"
    # always come back empty on Windows even for files that clearly
    # sit in a log/cache/temp folder.
    lower = path.lower().replace("\\", "/")

    if "/log" in lower or lower.endswith(".log"):
        return "log"
    elif "/cache" in lower:
        return "cache"
    elif "backup" in lower:
        return "backup"
    elif lower.endswith(".iso"):
        return "iso"
    elif "/tmp" in lower or "/temp" in lower:
        return "tmp"

    return "other"


def ask_ai_reason(file_path, size_mb, age_days):
    prompt = (
        f"A file on a Linux server has these properties:\n"
        f"Path: {file_path}\n"
        f"Size: {size_mb} MB\n"
        f"Last modified: {age_days} days ago\n\n"
        f"In one short sentence, explain the likely reason this file is large. "
        f"Be specific and practical, no fluff."
    )

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL_NAME,
                "prompt": prompt,
                "stream": False
            },
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        print("OLLAMA RESPONSE:", data)

        return data.get("response", "").strip()

    except Exception as e:
        print("OLLAMA ERROR:", repr(e))
        return f"AI unavailable ({e})"


def scan_folder(folder_path):
    results = []

    for root, dirs, files in os.walk(folder_path):
        for file in files:
            full_path = os.path.join(root, file)

            try:
                size_mb = get_file_size_mb(full_path)
                age_days = get_file_age_days(full_path)

                results.append({
                    "path": full_path,
                    "size_mb": size_mb,
                    "age_days": age_days,
                    "owner": get_file_owner(full_path),
                    "type": guess_file_type(full_path),
                })

            except (OSError, FileNotFoundError):
                continue

    return results


if __name__ == "__main__":

    target_folder = sys.argv[1] if len(sys.argv) > 1 else "."

    files = scan_folder(target_folder)

    files.sort(key=lambda x: x["size_mb"], reverse=True)

    print(f"Scanned {len(files)} files in '{target_folder}'")

    reason = ""

    if files:
        biggest = files[0]

        print("\nAsking Ollama for AI reasoning...\n")

        reason = ask_ai_reason(
            biggest["path"],
            biggest["size_mb"],
            biggest["age_days"]
        )

        print("\n=== LARGEST FILE ===")
        print("Path :", biggest["path"])
        print("Size :", biggest["size_mb"], "MB")
        print("Age  :", biggest["age_days"], "days")
        print("Owner:", biggest["owner"])
        print("Type :", biggest["type"])
        print("AI   :", reason)
        print()

    print("=== TOP FILES ===")

    for f in files[:50]:
        print(
            f"{f['size_mb']} MB -> {f['path']} "
            f"[{f['owner']}, {f['type']}]"
        )

    output_data = {
        "folder_scanned": target_folder,
        "total_files": len(files),
        "disk_usage": get_disk_usage(target_folder),
        "largest_file": {
            **files[0],
            "ai_reason": reason
        } if files else None,
        "top_files": files[:50]
    }

    with open("scan_result.json", "w") as f:
        json.dump(output_data, f, indent=4)

    print("\nResults saved to scan_result.json")