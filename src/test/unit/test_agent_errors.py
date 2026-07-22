from agent import errors


def test_not_found_shape():
    assert errors.not_found() == {"ok": False, "error": "not_found"}


def test_ambiguous_shape():
    assert errors.ambiguous() == {"ok": False, "error": "ambiguous"}


def test_profile_empty_shape():
    assert errors.profile_empty() == {"ok": False, "error": "profile_empty"}


def test_embedding_unavailable_shape():
    assert errors.embedding_unavailable() == {"ok": False, "error": "embedding_unavailable"}


def test_illegal_transition_carries_from_to_allowed():
    err = errors.illegal_transition("REJECTED", "OFFER", ["some_state"])
    assert err == {
        "ok": False,
        "error": "illegal_transition",
        "from": "REJECTED",
        "to": "OFFER",
        "allowed": ["some_state"],
    }


def test_guard_violation_carries_guard():
    err = errors.guard_violation("no_new_specifics")
    assert err == {"ok": False, "error": "guard_violation", "guard": "no_new_specifics"}


def test_invalid_patch_carries_detail():
    err = errors.invalid_patch("unknown field 'foo'")
    assert err == {"ok": False, "error": "invalid_patch", "detail": "unknown field 'foo'"}


def test_every_variant_has_ok_false():
    variants = [
        errors.not_found(),
        errors.ambiguous(),
        errors.profile_empty(),
        errors.embedding_unavailable(),
        errors.illegal_transition("A", "B", []),
        errors.guard_violation("g"),
        errors.invalid_patch("d"),
    ]
    assert all(v["ok"] is False for v in variants)
