import pytest
from scoring.scorer import score_job


def test_all_keywords_present():
    assert score_job("Python FastAPI SQL", ["python", "fastapi", "sql"]) == 1.0


def test_no_keywords_present():
    assert score_job("Java Spring Hibernate", ["python", "fastapi", "sql"]) == 0.0


def test_half_keywords_present():
    assert score_job("Python FastAPI", ["python", "fastapi", "sql", "docker"]) == 0.5


def test_empty_keywords_list_returns_zero():
    assert score_job("Python FastAPI SQL", []) == 0.0


def test_empty_description_returns_zero():
    assert score_job("", ["python", "fastapi"]) == 0.0


def test_matching_is_case_insensitive():
    assert score_job("We use Python and FastAPI", ["python"]) == 1.0


def test_multiword_keyword_matches_as_substring():
    assert score_job("Experience with machine learning required", ["machine learning"]) == 1.0


def test_duplicate_keywords_counted_as_is():
    # ["python", "python"] has 2 total keywords; both match → 2/2 = 1.0
    assert score_job("Python developer", ["python", "python"]) == 1.0


def test_duplicate_keywords_partial_match():
    # ["python", "python", "sql"] has 3 total; "python" matches twice, "sql" absent → 2/3
    result = score_job("Python developer", ["python", "python", "sql"])
    assert abs(result - 2 / 3) < 1e-9


def test_result_is_float():
    result = score_job("Python FastAPI", ["python"])
    assert isinstance(result, float)


def test_result_bounded_between_zero_and_one():
    result = score_job("Python FastAPI SQL Docker", ["python", "fastapi"])
    assert 0.0 <= result <= 1.0
