You map each APPROVED business decline to its exact technical realisation,
grounded ONLY in (a) the feature's XSD (the real response/error fields) and
(b) the canonical error-code catalog provided. Assign a code to every row; if
NONE fits the feature's condition, MINT a new one.

Rules:
1. For each row pick the catalog code whose category matches the row's
   failure_type AND whose when_to_use matches the condition. Set error_code.
2. If no catalog code fits, set is_new_code=true and write new_code_def: a
   one-line definition (code id with prefix 'F', category, when_to_use). Add the
   minted code to `new_codes`. Reuse an existing code before minting a near-duplicate.
3. Confirm the code corresponds to a real response/error field in the XSD. If the
   XSD has no field able to signal this decline, set schema_gap=true (do not drop
   the row — the gap is a finding for the schema authors).
4. Keep/refine condition, stage, required_behavior; set tsd_ref where known.
5. Preserve every input row (same id). Output the FULL spec.

# Output — STRICT JSON ONLY, no prose, no fences
{"rows": [ {"id":"...","api":"...","owning_entity":"...","observing_entity":"...",
            "stage":"...","failure_type":"...","condition":"...","error_code":"U30",
            "is_new_code":false,"new_code_def":"","required_behavior":"...",
            "schema_gap":false,"tsd_ref":"" } ],
 "new_codes": [ {"code":"F01","category":"decline","description":"...",
                 "when_to_use":"...","applies_to":["<api>"]} ]}
