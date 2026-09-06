
═══════════════════════════════════════════════════════════
TSD RULES — wire-level technical specification
═══════════════════════════════════════════════════════════

A TSD translates the approved BRD's business intent into wire-level
technical specifications. The TSD is the authoritative source for engineers
implementing the change — it MUST include the implementation depth the BRD
intentionally omitted.

REQUIRED IN A TSD (these were intentionally absent from the BRD):
  · Sample XML / JSON payloads for every API touched. XML declares the
    namespace exactly as the domain's own schema binds it, plus any library-supplied
    head fields (ver, ts, orgId, msgId).
  · Field-level contract table for every new or changed field:
    [Field Name, dType, dLength, Mandatory (Y/N), Validation, Description].
  · Error-code mapping at code level: [Code, TD/BD, Owning Entity, Triggering
    Condition, API, Customer Message]. Use the domain's error-code format
    (U##/Z#/RB/XT/XD); NEVER HTTP status codes.
  · CL version numbers (e.g. clVersion=2.36), refCategory codes,
    credType / subType values, namespace URIs.
  · Switch routing rules, credential-block byte format, cryptographic
    algorithm selection (key wrap, AES mode, key validity windows).
  · Idempotency-key construction algorithm: input fields, hash function,
    TTL, replay-window, dedup behaviour on duplicate.
  · Database schema or ORM-equivalent: primary keys, indexes, foreign keys,
    storage class (transactional vs analytical).
  · Implementation-level retry-with-backoff: max attempts, jitter, base.

API NAMING — same canonical allowlist + extension rules as BRD:
  Use ReqXxx/RespXxx canonical names. Document each extension as
  "<ExistingApi> (subType=<NEW_VALUE>)" rather than inventing a new API name.
  NEVER use SCREAMING_SNAKE_CASE.

CITATIONS — same as BRD; cite [S#] for every regulatory claim.
QUANTITATIVE CLAIMS — same three escape hatches (cited / assumed / illustrative).
OWNERSHIP MODEL — same layer rules; the authority does not enforce business policy.
NEVER FABRICATE METADATA — same.

WHEN BRD IS SILENT ON A WIRE-LEVEL DETAIL:
  The BRD describes business intent; the TSD picks the implementation.
  Where the BRD is silent (e.g. business says "persist the disbursement
  intent" but doesn't specify schema), the TSD must define the wire-level
  shape. Use sound domain conventions and label inferred values as Assumption
  when not directly derivable from the BRD or corpus.
═══════════════════════════════════════════════════════════

