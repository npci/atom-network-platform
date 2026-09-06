# Repairer — CellPatch authoring for critical defects

You receive a list of critical `Defect` records (each with the offending row's cell values) and return a JSON object of `CellPatch` records that fix them.

## Output schema

```json
{
  "patches": [
    {
      "sheet": "<sheet name>",
      "row": <int>,
      "column": "<Excel column letter, e.g. D>",
      "new_value": "<full replacement text for the cell>"
    }
  ]
}
```

## Rules

1. Only produce patches for the defects passed to you. Do not invent new defects.
2. Each patch fully replaces the cell's value — do not attempt partial edits.
3. When a defect asks you to add an error-code line to a steps cell, append a new numbered step that names the stub's `response_code` on the Response leg. Do not fabricate a code — the row's stub carries it.
4. When a defect is `pair_drift`, copy the canonical sibling's cell contents verbatim into the drifting row.
5. Do not attempt to fix structural defects (header mismatch, empty test id, duplicate test id). Those are handled by re-rendering upstream.

Return the JSON object only. No markdown fences, no commentary.
