import hashlib


def fingerprint_job(company: str, role: str, url: str) -> str:
    """
    Returns a SHA-256 hex digest of normalised (company + role + url).
    Normalisation: strip whitespace, lowercase, concatenate with '|' separator.
    """
    raw = "|".join([company.strip().lower(), role.strip().lower(), url.strip().lower()])
    return hashlib.sha256(raw.encode()).hexdigest()
