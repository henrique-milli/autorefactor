# Simulated user — retest after product update

You previously filed product feedback on this episode. The **product was patched and the engine rebuilt**.
Analyze has been re-run on a fresh copy of this subject.

## Mission

For **each finding id** in the prior `feedback.json` checklist, decide whether it is resolved, partial, unresolved, or unknown **from the new product surfaces**.

## Success

Write `retest.json` scoring those prior ids with notes grounded in `get_status`, `get_report`, and `sample_queue` (or `list_changesets` + `get_changeset`).

## Out of scope

This is a regression check, not new product critique and not a subject rebuild. Changing the queue or tree is not needed to score prior complaints.

## Output

Write `retest.json` (raw JSON):

```json
{
  "episode_id": "<id>",
  "summary": "<2-3 sentences>",
  "results": [
    {
      "id": "f1",
      "status": "resolved|partial|unresolved|unknown",
      "note": "what you checked in report/queue"
    }
  ]
}
```
