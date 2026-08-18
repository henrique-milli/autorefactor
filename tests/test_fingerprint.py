from __future__ import annotations

from autorefactor.fingerprint import (
    candidate_pairs,
    fingerprints_for_source,
    jaccard,
    normalize_tokens,
    winnow_fingerprints,
)


def test_renamed_functions_share_fingerprints() -> None:
    a = """
    public int computeTotal(List<Order> orders) {
        int sum = 0;
        for (Order order : orders) {
            if (order.isActive()) {
                sum += order.getAmount();
            }
        }
        return sum;
    }
    """
    b = """
    public int tallyAmounts(List<Purchase> purchases) {
        int acc = 0;
        for (Purchase purchase : purchases) {
            if (purchase.isActive()) {
                acc += purchase.getAmount();
            }
        }
        return acc;
    }
    """
    fa = fingerprints_for_source(a)
    fb = fingerprints_for_source(b)
    assert jaccard(fa, fb) >= 0.75


def test_unrelated_code_low_jaccard() -> None:
    a = "def add(x, y):\n    return x + y\n"
    b = "def render_html(page):\n    return f'<html>{page}</html>'\n"
    assert jaccard(fingerprints_for_source(a), fingerprints_for_source(b)) < 0.5


def test_winnow_and_candidates() -> None:
    tokens = normalize_tokens("for (int i = 0; i < n; i++) { sum += a[i]; }")
    fps = winnow_fingerprints(tokens, k=5, window=4)
    assert fps
    pairs = candidate_pairs({"u1": fps, "u2": set(fps)}, min_shared=1)
    assert pairs and pairs[0][2] == 1.0
