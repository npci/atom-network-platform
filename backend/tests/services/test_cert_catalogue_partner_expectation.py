# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""`_partner_own_response` — whose error code a partner-sheet case asserts.

A partner-sheet case narrates two actors: the partner answers, then the
authority reacts to that answer. The first error code in the block is therefore
routinely the AUTHORITY's rejection, and asserting it against the PARTNER's
response fails the partner for behaving correctly.

The trap these tests exist to hold shut: DESCRIPTION prose is copy-pasted
between cases with different outcomes, so a reader that scans the whole block
concludes SUCCESS for cases whose partner answers with an error. That reader
was written, dry-run against a real workbook, and withdrawn — it would have
broken two passing cases to fix one.
"""
import re

from app.services.cert_catalogue import _partner_own_response

CODES = re.compile(r"\b[EA]\d{3}\b")

# Trimmed verbatim from change 8ebc2b48's Operator sheet.

# The partner answers SUCCESS; the AUTHORITY then raises E008. The operator is
# never supposed to emit E008 — this is the case the fix exists for.
TC_4 = """
**DETAILS**
```
CAAB issues ReqAirworthinessCheck; Operator returns RespAirworthinessCheck with
an empty-string hangarLocation — E008 is returned and the submission rejected.
```
**DESCRIPTION**

ComplianceService.report() raises E008 per the field specification.

**TEST STEPS**

```
1. CAAB issues ReqAirworthinessCheck to Operator during the guard sequence.
2. Operator returns RespAirworthinessCheck with overall standing SUCCESS but with hangarLocation set to an empty string.
3. ComplianceService evaluates the hangarLocation field — empty string detected; RejectedException(E008) raised.
```
"""

# The partner itself answers FAILURE. Its DESCRIPTION is TC_1's happy-path
# prose, copied verbatim — the exact contamination that sank the first attempt.
TC_2 = """
**DETAILS**
```
Operator returns a SUCCESS standing, allowing the compliance submission to proceed.
```
**DESCRIPTION**

CAAB sends ReqAirworthinessCheck to the Operator for the submitted tailNumber;
the Operator responds with a SUCCESS status and cyclesRemaining greater than zero.

**TEST STEPS**

```
1. CAAB issues ReqAirworthinessCheck to Operator for the submitted tailNumber.
2. Operator processes the check and returns RespAirworthinessCheck with a FAILURE standing status or cyclesRemaining equal to zero or null.
3. ComplianceService detects the failed airworthiness result; RejectedException(E004) raised.
```
"""

# Double timeout: the partner sends nothing at all. Also carries TC_1's
# happy-path DESCRIPTION.
TC_3 = """
**DETAILS**
```
First attempt times out, CAAB retries once, second attempt also times out.
```
**DESCRIPTION**

CAAB sends ReqAirworthinessCheck to the Operator; the Operator responds with a
SUCCESS status and cyclesRemaining greater than zero.

**TEST STEPS**

```
1. CAAB issues ReqAirworthinessCheck to Operator during the guard sequence.
2. First ReqAirworthinessCheck attempt times out — no RespAirworthinessCheck received.
3. CAAB retries the outbound ReqAirworthinessCheck exactly once.
4. ComplianceService raises A16; fail-closed posture applied.
```
"""

# The partner answers with its OWN error code, stated in its own clause.
TC_5 = """
**TEST STEPS**

```
1. CAAB issues ReqAirworthinessCheck to Operator.
2. Operator returns suspended standing status; RejectedException(E002) raised.
```
"""


def _own(block):
    return _partner_own_response(block, "Operator", CODES)


def test_partner_answering_success_is_recognised():
    """TC_4: the operator answers SUCCESS and the authority raises E008."""
    assert _own(TC_4) == "SUCCESS"


def test_copied_happy_path_description_does_not_leak_in():
    """TC_2 and TC_3 both carry TC_1's 'responds with a SUCCESS status' prose in
    DESCRIPTION while their own steps say otherwise. Reading the whole block
    returns SUCCESS for both; reading TEST STEPS does not."""
    assert _own(TC_2) is None
    assert _own(TC_3) is None


def test_partner_that_never_answers_asserts_nothing():
    """A double timeout has no partner response to grade. None leaves the
    caller's existing derivation in place rather than inventing SUCCESS."""
    assert _own(TC_3) is None


def test_code_in_the_partners_own_clause_is_left_alone():
    """E002 sits in the operator's own clause, so it IS the operator's to emit
    and must not be relaxed to SUCCESS."""
    assert _own(TC_5) is None


def test_block_without_test_steps_asserts_nothing():
    assert _own("**DETAILS**\n```\nOperator returns SUCCESS.\n```\n") is None


def test_unknown_partner_label_asserts_nothing():
    assert _partner_own_response(TC_4, None, CODES) is None
    assert _partner_own_response(TC_4, "", CODES) is None
