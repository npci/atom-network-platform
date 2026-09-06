"""Negative end-to-end: can the machinery DETECT a defect and report it?"""
import asyncio, sys
sys.path.insert(0, "/app")
from app.core.database import SessionLocal
from app.models.phase_c import CertRun, CertTestResult
from app.models.sim_pack import SimPackRecord
from app.services import cert_pack_run, cert_reporting
from app.services.simulator import resolver

CHG, PARTNER = "e2e-change-0001", "e2e-partner-001"
OK, FAIL = [], []
def check(n, c, d=""):
    (OK if c else FAIL).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  — {d}" if d and not c else ""))

db = SessionLocal()
CAT = [{"case_id": "TC1", "api": "ReqDispute", "initiator": "npci",
        "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}}]

print("\n[A] plant a DEFECT: the deployed contract answers ZM where the case expects 00")
# Round 3 will rebuild its pack; mutate the PUBLISHED r2 pack's scenario so the
# simulator misbehaves, then point round 3 at it by making the rebuild identical.
row = db.query(SimPackRecord).filter(SimPackRecord.pack_ref.like("%@r2")).one()
content = dict(row.content)
content["scenarios"] = [{**s, "respond": {**s["respond"], "rc": "ZM"}}
                        for s in content["scenarios"]]
row.content = content
db.commit()
resolver._CACHE.clear()          # content changed underneath the content-addressed cache
check("defect planted in the published pack",
      resolver.resolve(db, row.pack_ref).scenarios[0]["respond"]["rc"] == "ZM")

print("\n[B] re-grade round 2's variants against the defective contract")
run2 = db.query(CertRun).filter_by(change_request_id=CHG, run_number=2).one()
from app.services.cert_assertions import assertion_failures, evaluate_specs
from app.core.wire.registry import codec_for
from app.services.simulator import runtime
from app.models.phase_c import CertCaseSpec, CertRequestVariant
variant = db.query(CertRequestVariant).filter_by(cflow_id=run2.cflow_id, run_number=2).first()
specs = db.query(CertCaseSpec).filter_by(variant_id=variant.id).all()
entry = resolver.resolve(db, row.pack_ref).apis["reqdispute"]
reply = asyncio.run(runtime.handle(db, body=entry["request_template"],
                                   pack=row.pack_ref, tc_id="TC1",
                                   variant_id=variant.variant_id))
check("the defective contract answers ZM", reply.rc == "ZM", reply.rc)
outcomes = evaluate_specs(specs, request_body=entry["request_template"],
                          response_body=reply.content, actual_code=reply.rc,
                          codec=codec_for("xml"))
fails = assertion_failures(outcomes)
check("the grader CAUGHT the mismatch", len(fails) >= 1, str(fails))
check("the failure names response_code and both values",
      any(f["kind"] == "response_code" and "ZM" in (f["reason"] or "") for f in fails),
      str(fails))

print("\n[C] a genuinely failing round is recorded as failed, not certified")
# Force round 3 to grade against the defective pack by reusing its ref.
import app.services.cert_pack_run as cpr
_orig = cpr._round_pack
cpr._round_pack = lambda db, **kw: row          # graded_by = the defective pack
r3 = asyncio.run(cpr.run_round(db, change_id=CHG, partner_id=PARTNER,
                               test_data={"case_catalogue": CAT}))
cpr._round_pack = _orig
print("     summary:", {k: r3.get(k) for k in ("status","passed","total","pass","fail")})
check("round 3 did NOT certify", r3["passed"] is False, str(r3))
check("the failure is counted", r3["fail"] >= 1, str(r3))
run3 = db.query(CertRun).filter_by(change_request_id=CHG, run_number=3).one()
rows3 = db.query(CertTestResult).filter_by(cert_run_id=run3.id).all()
check("the result row carries assertion_failures",
      any((r.actual_response or {}).get("assertion_failures") for r in rows3))

print("\n[D] CERT-7 surfaces it as NEWLY FAILING (the column that earns its keep)")
d3 = cert_reporting.round_diff(db, run3.cflow_id, 3)
check("newly_failing names TC1", d3["newly_failing"] == ["TC1"], str(d3))
check("it is not miscounted as fixed", d3["fixed"] == [], str(d3))

db.close()
print(f"\n{'='*60}\nPASS {len(OK)}   FAIL {len(FAIL)}")
sys.exit(1 if FAIL else 0)
