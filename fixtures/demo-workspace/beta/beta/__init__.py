from beta.util import ScoreAggregator, merge_scores, normalize_user_id


def demo() -> None:
    agg = ScoreAggregator()
    agg.add(normalize_user_id("User: Bob"), 3.0)
    print(merge_scores(agg.snapshot(), {"alice": 1.0}))
