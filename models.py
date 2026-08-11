"""
models.py - the shared "integration contract" for this project.

Why this file exists: you have 5 teammates, each probably generating
code with Claude separately. The fastest way for 5 separately-built
pieces to actually fit together is for everyone to agree on ONE shared
definition of "what a scanned file looks like" and "what a server
looks like" - instead of every module inventing its own field names.

Rule of thumb for your team: if a teammate's module produces or
consumes file/server data, it should import these Pydantic models
(or match this exact JSON shape) rather than defining its own.
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class ServerInfo(BaseModel):
    """One entry in servers.json - a machine/folder this tool can scan."""
    id: str = Field(..., description="Short unique id, e.g. 'srv-01'")
    name: str = Field(..., description="Human-readable label shown in the UI")
    hostname: str = Field(..., description="Display hostname, e.g. 'web01.local'")
    base_path: str = Field(..., description="Folder this server scans by default")
    owner_team: Optional[str] = Field(None, description="Which team/person owns this server")


class ScannedFile(BaseModel):
    """One file found during a scan - the atomic unit everything else builds on."""
    path: str
    size_mb: float
    age_days: float
    owner: str = Field("unknown", description="OS username that owns the file, if determinable")
    type: str = Field("other", description="Guessed category: log, cache, backup, iso, tmp, other")
    ai_reason: Optional[str] = Field(None, description="AI-generated explanation, only set for the largest file today")


class DiskUsage(BaseModel):
    """Real drive capacity info, powering the '85% full' warning banner."""
    total_gb: float
    used_gb: float
    free_gb: float
    percent_used: float


class ScanResult(BaseModel):
    """What the backend returns from /scan - the full response contract."""
    server_id: str
    server_name: str
    folder_scanned: str
    total_files: int
    disk_usage: Optional[DiskUsage] = None
    largest_file: Optional[ScannedFile] = None
    top_files: List[ScannedFile] = Field(default_factory=list)
    available_owners: List[str] = Field(default_factory=list, description="Distinct owners found, for building the filter dropdown")
    available_types: List[str] = Field(default_factory=list, description="Distinct file types found, for building the filter dropdown")
