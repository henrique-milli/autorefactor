export function normalizeUserId(raw: string): string {
  let value = (raw || "").trim().toLowerCase();
  if (value.startsWith("user:")) {
    value = value.slice(5);
  }
  return value.replace(/ /g, "-");
}

export class ScoreAggregator {
  private scores: Record<string, number> = {};

  add(key: string, score: number): void {
    this.scores[key] = (this.scores[key] || 0) + score;
  }

  snapshot(): Record<string, number> {
    return { ...this.scores };
  }
}
