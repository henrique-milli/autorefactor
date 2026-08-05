from alpha.util import ScoreAggregator, merge_scores, normalize_user_id


def demo() -> None:
    agg = ScoreAggregator()
    agg.add(normalize_user_id("User: Alice"), 1.5)
    print(merge_scores(agg.snapshot(), {"bob": 2.0}))


if __name__ == "__main__":
    demo()
