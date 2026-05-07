import inspect
import json

from loguru import logger
from pydantic import ValidationError

from app.config import settings
from app.db import repository as repo
from app.models.enums import ApplicationStatus, InvalidTransitionError
from app.models.job import JobCreate
from scoring.fingerprint import fingerprint_job
from scoring.scorer import score_job
from agent.profile import read_profile, write_profile
from scraper.tavily_client import search as tavily_search
from scraper.parser import parse_results


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
                "Always confirm the change with the user before calling. "
                "All fields must be nested inside the 'updates' key. "
                "Example 1: {\"updates\": {\"target_roles\": [\"backend engineer\"], \"base_salary\": 4500}}. "
                "Example 2: {\"updates\": {\"location\": \"Singapore\", \"skills\": [\"Python\", \"FastAPI\"]}}."
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
        "search_jobs":    lambda: _search_jobs(arguments, db),
    }
    handler = handlers.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    result = handler()
    if inspect.isawaitable(result):
        result = await result
    return result


async def _log_job(args: dict, db) -> str:
    try:
        job = JobCreate(**args)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid job data: {e.errors()}"})
    fp = fingerprint_job(job.company, job.role, str(job.url))
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


async def _search_jobs(args: dict, db) -> str:
    query       = args.get("query") or settings.scrape_query
    raw_results = await tavily_search(query)
    parsed      = parse_results(raw_results)

    profile  = read_profile()
    keywords = profile.get("skills", [])

    inserted = []
    for record in parsed:
        try:
            job_data     = JobCreate(**record)
            fp           = fingerprint_job(job_data.company, job_data.role, str(job_data.url))
            score        = score_job(job_data.description or "", keywords)
            job, created = await repo.insert_job(db, job_data, fp, score)
            inserted.append({"id": job["id"], "company": job["company"],
                              "role": job["role"], "created": created})
            status = "NEW" if created else "duplicate"
            logger.info(f"[search_jobs] [{status}] {job['company']} — {job['role']} | {job['url']}")
        except Exception as e:
            logger.warning(f"search_jobs: failed to insert record: {e}")

    logger.info(f"[search_jobs] query='{query}' found={len(parsed)} inserted={len(inserted)}")
    return json.dumps({
        "query":    query,
        "found":    len(parsed),
        "inserted": inserted,
        "count":    len(inserted),
    })
