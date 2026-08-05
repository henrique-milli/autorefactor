"""Alpha shared utilities — intentionally duplicated in beta."""


def normalize_user_id(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value.startswith("user:"):
        value = value[5:]
    return value.replace(" ", "-")


def merge_scores(left: dict[str, float], right: dict[str, float]) -> dict[str, float]:
    out = dict(left)
    for key, score in right.items():
        out[key] = out.get(key, 0.0) + score
    return out


class ScoreAggregator:
    def __init__(self) -> None:
        self._scores: dict[str, float] = {}

    def add(self, key: str, score: float) -> None:
        self._scores[key] = self._scores.get(key, 0.0) + score

    def snapshot(self) -> dict[str, float]:
        return dict(self._scores)
