# NETWORK API / FLOW / TEST-CASE / ERROR-CODE DESIGN PRINCIPLES

Distilled, always-on design principles for Phase A (API design, flow design,
certification test-case design). These are the *rules* — the full catalogues
(API list, element/attribute reference, enumerations, error-code tables, worked
examples, end-to-end flows) live in RAG under the `api_design_knowledge` category
(`knowledge_base/api_design_knowledge/{01_api_and_flow,02_test_case,03_error_code}_design.md`)
and the machine spec corpus bundled with `backend/app/excel_testcase_engine/`. Retrieve
those for specifics; obey the principles below always. **Never invent API names,
fields, enum values, or error codes — ground them in the catalogue or mark them
low-confidence / requiring Authority allocation.**

---

## 1. API & naming conventions

- Every API is a `Req<Name>` + `Resp<Name>` pair (`ReqTransfer`/`RespTransfer`).
- Families by stem: mandate (`Mandate`, `AuthMandate`, `MandateConfirmation`),
  listing/meta (`List<Thing>` → `ReqListPsp`, `ReqListKeys`), confirmation
  (`<Thing>Confirmation`). Debit/credit legs are suffixed (`ReqTransfer_Debit`).
- Single namespace for all messages: `http://example.org/network/schema/`.
- Request carries full intent; response carries `result` + per-participant `Ref`.
- Synchronous `Ack` on receipt, ahead of the asynchronous `Resp`.

## 2. Message envelope (reuse it)

Reuse the shared envelope: `Head` / `Meta` / `Txn` / `Payer` / `Payees` →
`Resp` / `Ref`. Add new elements/attributes only where an existing one cannot
carry the data — justify and version-gate. Counterparties are addressed by
VPA / global identifier and resolved by the Authority (`ReqAuthDetails`); a feature never
requires the initiator to know the real account number. Credentials are captured
on-device by the Common Library and travel as an encrypted/signed `<Cred>` block —
never raw on the wire.

## 3. Core design principles

1. XML over HTTPS, UTF-8, stateless — each message carries its own context.
2. Idempotency via `msgId`/`Txn.id` — a duplicate returns the same response, never
   re-executes.
3. Credential encryption (base64 + the Authority's public key); mandates digitally signed
   (`Cred type="Mandate" subType="DS"`).
4. Multi-party / multi-payee: `Payees` is a list; one `Ref` per participant;
   `PARTIAL` covers mixed outcomes.
5. Sync ack + async resp → timeout → `ReqChkTxn` → deemed handling.
6. Reversal (repairs a failed/timed-out leg, `orgTxnId` back-ref) ≠ refund
   (merchant-initiated return). Risk scores ride in-band.

## 4. Design ideology — reuse → extend → invent

A new top-level API is **rare** and carries a high bar: every API is one contract
the whole ecosystem must implement identically. Governing instincts:

- **Reuse before extend before invent.** Most features ride on an existing message
  (new `Txn.type`/`subType`/`purpose`, new `Rule`/`Tag`, new `Cred` type, new
  optional element).
- **One contract for everyone** — unambiguous, mandatory-vs-optional explicit.
- **Backward compatible & version-gated** — changes are *additive*; existing
  required semantics never change; new behaviour gated by `ver`; ecosystem rollout
  controlled via the capability directory (`<Version>` entry in
  `RespListPsp`/`RespListAccPvd` + `prodType`/format flags).
- Four-party model & role clarity (Payer PSP, Payee PSP, Remitter bank,
  Beneficiary bank, the Authority as switch). Standard two-factor auth via `Creds`.
- Full lifecycle (idempotency, retries, timeout/ChkTxn, reversal, deemed);
  stateful features get a state object + create/modify/revoke lifecycle (as
  mandates have the UMN). Define money movement & settlement for financial
  features. Align to RBI limits/consent. Minimal data, one unambiguous meaning
  per field.

### Decision procedure (given a feature)
1. Classify (financial / mandate-stateful / meta / auth / registration / lifecycle).
2. Map actors & flow — who initiates, who authorises, how money moves; sketch hops.
3. Decide reuse vs extend vs new (tree below).
4. Choose the carrier (existing enum value, or new `Rule`/`Tag`/optional/`Cred`, or
   — last resort — a new message).
5. Define the data delta (where each new field hangs, M/O, format, `ver` gating).
6. Define auth & consent (cred types; pre-approved/signed; what the customer sees).
7. Define lifecycle & failure handling (idempotency key, timeout/ChkTxn, reversal,
   deemed; state object + transitions for stateful features).
8. Define settlement & reversal (financial).
9. Set version & compatibility (confirm additive; older-participant behaviour).
10. Define response/error codes (§6) and certification scenarios (§5).

### Reuse → extend → invent tree (stop at first fit)
1. Existing API + existing/new `Txn.type`/`subType`/`purpose`? → use it.
2. Extra data, same interaction? → optional element/attribute or `Rule`/`Tag`,
   version-gated.
3. New funding instrument / account behaviour? → new `ACTYPE` on `Ac`, not a new API.
4. New auth method? → new `Cred type/subType`, not a new API.
5. New stateful object with its own lifecycle? → a new mandate-style API *family*
   with a UMN-like id.
6. Genuinely new interaction no existing message expresses? → new `Req/Resp` pair
   (high bar — justify why 1–5 don't fit; keep the shared envelope).

### New-API checklist
Decide reuse/extend/invent first → name `Req/Resp` by analogy → reuse envelope →
pick `type`/`subType` → specify credentials → define the flow (incl.
timeout→ChkTxn→deemed) → confirm backward compatibility → list response & error
codes → define certification scenarios → state confidence + source (leave XML
blank where ungrounded).

## 5. Certification test-case design

- Pack is one workbook organised **role → subset → test case**. Roles & id
  prefixes: Remitter `RE_*`, Beneficiary `BE_*` (Issuer); Payer PSP `PR_*`,
  Payee PSP `PE_*` (Acquirer); Meta `MT_*` (both); Mandate `MA_MT_PR_*` /
  `MA_MT_RE_*`. **Never renumber existing ids** (retired ids keep the slot).
- A new feature introduces a **new subset**: next free letter, one-line
  description, scope (acquirer/issuer/both), enumerated ids, count.
- `DETAILS` block fixed fields: API Involved, Type, Approval Type
  (Pre/Non-Pre-Approved), Payer Handle, Payee Handle (VPA | Aadhaar | A/c+IFSC |
  Mobile[+MMID]).

### Scenario discovery (goal: completeness — prune duplicates, never miss a failure mode)
1. Decompose the feature into its ordered API exchange (every hop, every actor).
2. For every hop enumerate failure modes: success, timeout, business decline,
   validation/malformed, system exception, negative-ACK. A hop with no failure
   case is almost always an omission.
3. Walk every scenario lens — most features touch 8–12: happy paths;
   auth & credentials; limits & caps; state lifecycle (incl. illegal transitions);
   idempotency/duplicate/replay; timeout/deemed/reversal/settlement; risk & fraud;
   validation/malformed; account & instrument types; channels & devices; roles
   under test; regulatory/compliance/account-status; disputes;
   identity/registration/resolution; concurrency/ordering; interoperability/partner
   permutations; feature eligibility/gating; principal↔dependent management;
   notifications.
4. Cross-product with coverage dimensions: Outcome (Success/Failure/Timeout/
   Deemed/neg-ACK) × Handle pair × Approval type × Role-under-test × Txn type/
   sub-type × Initiation (Bank/Authority) × Channel. Drop impossible combos (say why).
5. De-duplicate and tag each with a `coverage_tag`
   (`happy_path`/`timeout`/`decline`/`neg_ack`/`deemed`/`revoke`/`partial`) and the
   expected `respCode`/`errCode` (§6).
6. Completeness check: every hop has ≥1 success + ≥1 failure; every role appears as
   system-under-test in success and its own failure modes; every new field/enum/cred
   type has a positive case and a validation-failure case. List intentional
   exclusions with a one-line reason.

Emit the machine form the platform consumes (`TestCaseStub` + `FlowDefinition` per
`docs/cert_simulator_contract/` and
`excel_testcase_engine/schemas/workbook_plan.py`). The minimum variant count per API
is encoded in the machine corpus `coverage_matrix.json` — a new API declares its required
variants the same way.

## 6. Error / response code design

Three response attributes: `result` (`SUCCESS|FAILURE|PARTIAL|DEEMED`, on `Resp`),
`errCode` (network service layer or PSP, mandatory when `result=FAILURE`), `respCode`
(bank per-participant on `Ref`, `00`=success). The same failure can surface twice
(bank `respCode` → network `errCode`, e.g. `ZM`→`UM8`) — **design both ends.**

Prefix taxonomy (letter = validation/decline context): `00` success; `U..` network
service-layer; `T..`/`E..`/`R..`/`D..`/`G..`/`K..`/`BA..`/`IM..`/`OR..` validation
families; mandate validation (`M*`,`HV`,`PA/PR/PY`,`RM`); `Q..` query/mandate-auth;
`A2/A3/A4..` UIDAI; `V../X../Y../Z..` bank decline (ISO-8583-style, often
remitter/beneficiary pairs).

### Rules for choosing codes
1. **Never invent a code** — select from the catalogue
   (the machine corpus `error_codes.json` is the source of truth).
2. Respect `applies_to` — a code belongs to specific APIs (no mandate code on a Pay
   leg, no Meta code on a financial leg).
3. Match category to outcome: success→`00`; business decline→bank decline code
   (`Z*/Y*/X*/B*/V*/Q*`); timeout→`UP` or a `U`-timeout
   (`U10/U67/U68/U85/U87/U88`); malformed→a validation prefix (`T*/E*/R*/G*/BA*`);
   deemed→reversal `96` / settlement `R9/U9`.
4. Design both ends (bank `respCode`s + network `errCode`s + the forwarding mapping).
5. Pair remitter/beneficiary variants where the catalogue does (`XH`/`XI`,
   `YE`/`YF`, `ZX`/`ZY`).
6. A genuinely new code is proposed with a context-chosen prefix, marked **requiring
   Authority allocation**, and never used in a test case until allocated.
