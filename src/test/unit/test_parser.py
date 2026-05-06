import pytest
from scraper.parser import parse_results, _split_title


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_result(**kwargs):
    base = {
        "title":   "Data Engineer — GovTech",
        "url":     "https://careers.gov.sg/1",
        "content": "We are looking for a data engineer.",
    }
    return {**base, **kwargs}


# ── parse_results ─────────────────────────────────────────────────────────────

def test_parse_single_result_returns_correct_shape():
    parsed = parse_results([_make_result()])
    assert len(parsed) == 1
    record = parsed[0]
    assert record["url"]    == "https://careers.gov.sg/1"
    assert record["source"] == "tavily"
    assert "company"     in record
    assert "role"        in record
    assert "description" in record
    assert "notes"       not in record  # notes is a user field, not set by the parser


def test_parse_empty_list_returns_empty_list():
    assert parse_results([]) == []


def test_parse_drops_result_with_missing_url():
    results = [_make_result(url=None), _make_result()]
    assert len(parse_results(results)) == 1


def test_parse_drops_result_with_empty_string_url():
    results = [_make_result(url=""), _make_result()]
    assert len(parse_results(results)) == 1


def test_parse_sets_source_to_tavily():
    parsed = parse_results([_make_result()])
    assert parsed[0]["source"] == "tavily"


def test_parse_content_becomes_description():
    parsed = parse_results([_make_result(content="Some job description.")])
    assert parsed[0]["description"] == "Some job description."


def test_parse_truncates_long_content_to_500_chars():
    parsed = parse_results([_make_result(content="x" * 600)])
    assert len(parsed[0]["description"]) == 500


def test_parse_none_content_sets_description_to_none():
    parsed = parse_results([_make_result(content=None)])
    assert parsed[0]["description"] is None


def test_parse_empty_content_sets_description_to_none():
    parsed = parse_results([_make_result(content="")])
    assert parsed[0]["description"] is None


def test_parse_multiple_results_returns_correct_count():
    results = [
        _make_result(url="https://a.com/1"),
        _make_result(url="https://b.com/2"),
        _make_result(url="https://c.com/3"),
    ]
    assert len(parse_results(results)) == 3


def test_parse_preserves_url_exactly():
    url = "https://careers.gov.sg/jobs/456?ref=linkedin"
    parsed = parse_results([_make_result(url=url)])
    assert parsed[0]["url"] == url


def test_parse_missing_title_does_not_crash():
    result = {"url": "https://example.com/1", "content": "some content"}
    parsed = parse_results([result])
    assert len(parsed) == 1
    assert parsed[0]["company"] == "Unknown"


# ── _split_title ──────────────────────────────────────────────────────────────

def test_split_title_em_dash_separator():
    company, role = _split_title("Data Engineer — GovTech")
    assert company == "GovTech"
    assert role    == "Data Engineer"


def test_split_title_pipe_separator():
    company, role = _split_title("DBS | Backend Engineer")
    assert "DBS"              in (company, role)
    assert "Backend Engineer" in (company, role)


def test_split_title_hyphen_separator():
    company, role = _split_title("Software Engineer - Grab")
    assert "Grab"             in (company, role)
    assert "Software Engineer" in (company, role)


def test_split_title_at_separator():
    company, role = _split_title("Frontend Engineer at Shopee")
    assert company == "Shopee"
    assert role    == "Frontend Engineer"


def test_split_title_no_separator_returns_unknown_company():
    company, role = _split_title("Backend Engineer")
    assert company == "Unknown"
    assert role    == "Backend Engineer"


def test_split_title_empty_string_returns_unknown_company():
    company, role = _split_title("")
    assert company == "Unknown"


def test_split_title_shorter_part_is_company():
    # "GovTech" is shorter than "Senior Data Engineer" — should be company
    company, role = _split_title("GovTech — Senior Data Engineer")
    assert company == "GovTech"
    assert role    == "Senior Data Engineer"
