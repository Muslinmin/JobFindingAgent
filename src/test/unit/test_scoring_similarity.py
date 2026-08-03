import pytest

from scoring.similarity import cosine_similarity, similarity_to_score


# ── cosine_similarity ──────────────────────────────────────────────────────────

def test_cosine_similarity_identical_vectors_is_one():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_similarity_opposite_vectors_is_negative_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0


def test_cosine_similarity_scale_invariant():
    assert cosine_similarity([1.0, 2.0], [2.0, 4.0]) == pytest.approx(1.0)


def test_cosine_similarity_zero_vector_a_returns_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_similarity_zero_vector_b_returns_zero():
    assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0


def test_cosine_similarity_both_zero_vectors_returns_zero():
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


# ── similarity_to_score ─────────────────────────────────────────────────────────

def test_similarity_to_score_converts_to_basis_points():
    assert similarity_to_score(0.73) == 7300


def test_similarity_to_score_zero_maps_to_zero():
    assert similarity_to_score(0.0) == 0


def test_similarity_to_score_one_maps_to_ten_thousand():
    assert similarity_to_score(1.0) == 10000


def test_similarity_to_score_rounds_half_up_boundary():
    assert similarity_to_score(0.99995) == 10000


def test_similarity_to_score_clamps_negative_similarity_to_zero():
    assert similarity_to_score(-0.5) == 0


def test_similarity_to_score_clamps_above_one_to_ten_thousand():
    assert similarity_to_score(1.5) == 10000


def test_similarity_to_score_returns_int_type():
    assert isinstance(similarity_to_score(0.5), int)
