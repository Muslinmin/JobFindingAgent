def score_job(description: str, keywords: list[str]) -> float:
    """
    Returns the fraction of keywords found in description.
    Case-insensitive. Returns 0.0 if keywords is empty.
    """
    if not keywords:
        return 0.0

    lowered = description.lower()
    matched = sum(1 for kw in keywords if kw.lower() in lowered)
    return matched / len(keywords)
