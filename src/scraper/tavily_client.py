import httpx
from loguru import logger

from app.config import settings

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


async def search(query: str, max_results: int | None = None) -> list[dict]:
    api_key = settings.tavily_api_key
    if not api_key:
        logger.warning("TAVILY_API_KEY is not set — returning empty results")
        return []

    payload = {
        "api_key":      api_key,
        "query":        query,
        "max_results":  max_results if max_results is not None else settings.scrape_max_results,
        "search_depth": "basic",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(TAVILY_SEARCH_URL, json=payload)
            response.raise_for_status()
            return response.json().get("results", [])

    except httpx.HTTPStatusError as e:
        logger.error(f"Tavily returned HTTP {e.response.status_code}: {e}")
        return []

    except httpx.RequestError as e:
        logger.error(f"Tavily request failed: {e}")
        return []

    except Exception as e:
        logger.error(f"Unexpected error calling Tavily: {e}")
        return []
