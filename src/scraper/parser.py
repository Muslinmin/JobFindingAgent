MAX_NOTES_LENGTH = 500

_SEPARATORS = [" — ", " | ", " - ", " at "]


def parse_results(raw_results: list[dict]) -> list[dict]:
    parsed = []
    for result in raw_results:
        url = result.get("url")
        if not url:
            continue

        title = result.get("title") or ""
        company, role = _split_title(title)
        raw_content = result.get("content") or ""
        notes = raw_content[:MAX_NOTES_LENGTH] or None

        parsed.append({
            "company": company,
            "role":    role,
            "url":     url,
            "source":  "tavily",
            "notes":   notes,
        })

    return parsed


def _split_title(title: str) -> tuple[str, str]:
    for sep in _SEPARATORS:
        if sep in title:
            left, right = title.split(sep, 1)
            left, right = left.strip(), right.strip()
            if len(left) < len(right):
                return left, right
            return right, left
    return "Unknown", title.strip()
