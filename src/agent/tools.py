import inspect
import json

from app.db import repository as repo
from app.models.enums import ApplicationStatus, InvalidTransitionError
from app.models.job import JobCreate
from app.routes.jobs import make_fingerprint
from agent.profile import read_profile, write_profile


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "log_job",
            "description": (
                "Log a new job application to the tracker. "
                "Only call this after the user has explicitly confirmed they want to log it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company name"},
                    "role":    {"type": "string", "description": "Job title or role"},
                    "url":     {"type": "string", "description": "Direct URL to the listing"},
                    "source":  {"type": "string", "description": "Where the listing was found"},
                    "notes":   {"type": "string", "description": "Optional notes"},
                },
                "required": ["company", "role", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_status",
            "description": "Move a job application to a new status. Respects valid state transitions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer", "description": "ID of the job record"},
                    "status": {
                        "type": "string",
                        "enum": ["applied", "screening", "interview", "offer", "rejected"],
                        "description": "Target status",
                    },
                    "notes": {"type": "string", "description": "Optional notes on this update"},
                },
                "required": ["job_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_jobs",
            "description": "Retrieve job applications from the tracker, optionally filtered by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["found", "applied", "screening", "interview", "offer", "rejected"],
                        "description": "Filter by this status. Omit to return all.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": (
                "Merge new information into the user profile. "
                "Call this when the user shares preferences, skills, or personal details. "
                "Always confirm the change with the user before calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "object",
                        "description": "Key-value pairs to merge into the existing profile",
                    },
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": "Search for live job listings matching a query string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search string, e.g. 'data engineer Singapore fintech'",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


async def execute_tool(tool_name: str, arguments: dict, db) -> str:
    handlers = {
        "log_job":        lambda: _log_job(arguments, db),
        "update_status":  lambda: _update_status(arguments, db),
        "query_jobs":     lambda: _query_jobs(arguments, db),
        "update_profile": lambda: _update_profile(arguments),
        "search_jobs":    lambda: _search_jobs(arguments),
    }
    handler = handlers.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    result = handler()
    if inspect.isawaitable(result):
        result = await result
    return result


async def _log_job(args: dict, db) -> str:
    job = JobCreate(**args)
    fp = make_fingerprint(job)
    record, created = await repo.insert_job(db, job, fp)
    return json.dumps({"created": created, "job": record})


async def _update_status(args: dict, db) -> str:
    try:
        job = await repo.update_job_status(
            db,
            args["job_id"],
            ApplicationStatus(args["status"]),
            args.get("notes"),
        )
        if not job:
            return json.dumps({"error": "Job not found"})
        return json.dumps(job)
    except InvalidTransitionError as e:
        return json.dumps({"error": str(e)})


async def _query_jobs(args: dict, db) -> str:
    jobs = await repo.get_all_jobs(db, status_filter=args.get("status"))
    return json.dumps(jobs)


def _update_profile(args: dict) -> str:
    current = read_profile()
    updated = {**current, **args["updates"]}
    write_profile(updated)
    return json.dumps({"status": "profile updated", "profile": updated})


def _search_jobs(args: dict) -> str:
    return json.dumps({
        "results": [],
        "note": "Search not yet available. Scraping layer coming in Week 3.",
    })
