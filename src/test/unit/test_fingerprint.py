from scoring.fingerprint import fingerprint_job


def test_same_inputs_produce_same_fingerprint():
    assert fingerprint_job("Google", "Engineer", "https://google.com/jobs/1") == \
           fingerprint_job("Google", "Engineer", "https://google.com/jobs/1")


def test_different_company_produces_different_fingerprint():
    a = fingerprint_job("Google", "Engineer", "https://example.com")
    b = fingerprint_job("Meta", "Engineer", "https://example.com")
    assert a != b


def test_different_role_produces_different_fingerprint():
    a = fingerprint_job("Google", "Engineer", "https://example.com")
    b = fingerprint_job("Google", "Designer", "https://example.com")
    assert a != b


def test_different_url_produces_different_fingerprint():
    a = fingerprint_job("Google", "Engineer", "https://example.com/1")
    b = fingerprint_job("Google", "Engineer", "https://example.com/2")
    assert a != b


def test_case_difference_only_produces_same_fingerprint():
    a = fingerprint_job("Google", "Engineer", "https://example.com")
    b = fingerprint_job("google", "engineer", "https://example.com")
    assert a == b


def test_leading_trailing_whitespace_produces_same_fingerprint():
    a = fingerprint_job("Google", "Engineer", "https://example.com")
    b = fingerprint_job("  Google  ", "  Engineer  ", "  https://example.com  ")
    assert a == b


def test_output_is_64_character_hex_string():
    result = fingerprint_job("Google", "Engineer", "https://example.com")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)
