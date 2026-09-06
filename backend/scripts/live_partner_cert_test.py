"""Live cross-repo test: the Gate-3 two-sided sim_pack run against a REAL
partner backend (atom-partner-platform, running separately), not a mock.

Drives cert_pack_run.run_round with one authority-initiated case (executes
locally against the pack simulator) and one bank-initiated case (must
trigger `_announce()` -> a real, HMAC-signed, TLS-verified
`cert_setup_notification` POST to the live partner over the network).

Prints exactly what happened so a human can tell live-send success from a
silent local-only fallback (which is the honest-degradation path this code
takes when the send fails — indistinguishable from success unless you look
at the logs, which is why this script exists).
"""
import asyncio
import sys

sys.path.insert(0, "/app")

from app.core.database import SessionLocal
from app.models.phase_c import CertRun, CertTestResult, PartnerAgent
from app.services import cert_pack_run

CHG = "demo-change-0001"

db = SessionLocal()
partner = db.query(PartnerAgent).filter_by(name="Live Test Bank").first()
if partner is None:
    print("FAIL: 'Live Test Bank' partner not found — register it via the "
          "admin API first")
    sys.exit(1)
print(f"partner: {partner.id}  endpoint={partner.endpoint_url}")

CATALOGUE = [
    {"case_id": "TC_LOCAL_01", "api": "ReqDispute", "initiator": "npci",
     "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}},
    {"case_id": "TC_BANK_01", "api": "ReqDispute", "initiator": "bank",
     "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}},
]

print("\nrunning cert_pack_run.run_round against the LIVE partner...")
summary = asyncio.run(cert_pack_run.run_round(
    db, change_id=CHG, partner_id=partner.id,
    test_data={"case_catalogue": CATALOGUE}))
print("\nsummary:", {k: summary.get(k) for k in
                     ("status", "passed", "total", "pass", "fail",
                      "awaiting_partner_cases", "npci_mode", "partner_mode")})

run = (db.query(CertRun).filter_by(change_request_id=CHG, partner_id=partner.id)
       .order_by(CertRun.run_number.desc()).first())
if run is None:
    print("\nFAIL: no CertRun was persisted")
    sys.exit(1)

print(f"\nrun {run.id}  status={run.status.value if hasattr(run.status,'value') else run.status}"
      f"  completed_at={run.completed_at}")

bank_row = (db.query(CertTestResult)
            .filter_by(cert_run_id=run.id, test_case_id="TC_BANK_01").first())
if bank_row is None:
    print("FAIL: no result row for the bank-initiated case")
    sys.exit(1)

triggered = (bank_row.actual_response or {}).get("not_reported")
print(f"\nTC_BANK_01 result row: {bank_row.actual_response}")

if summary.get("status") == "awaiting_partner":
    print("\nRound is 'awaiting_partner' — this means a partner-owned case "
          "existed, NOT that delivery succeeded (send_task_to_partner can "
          "swallow the transport error and still return). Check the "
          "backend container logs for this run's timestamp and the "
          "partner's own access log for the actual HTTP status of the "
          "POST /a2a-rpc/rpc — that is the only real signal.")
else:
    print("\n*** LIVE SEND DID NOT HAPPEN — honest local-only fallback ***")
    print("The round executed the whole scope locally instead of awaiting "
          "the partner. This is cert_pack_run._announce()'s deliberate "
          "degradation path (tunnel off / partner unreachable / send raised) "
          "— check the backend container logs around this run for why.")

db.close()
