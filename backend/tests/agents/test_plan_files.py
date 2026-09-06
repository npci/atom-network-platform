# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""plan_files — shared plan readers feeding the party-flow gate and schema guards."""
from app.agents import plan_files as PF


def test_touched_message_stems_skips_java_artefacts_named_after_messages():
    # Any Req/Resp-prefixed Java artefact (Processor, Router, Handler…) matches the
    # token pattern but is NOT a wire message — a phantom stem here bounces a valid
    # plan at propose_plan until the agent invents a bogus party_flows entry.
    ta = {"files_to_modify": [
        {"path": "x/ReqTransferProcessor.java", "intent": "modify"},
        {"path": "x/ReqTransferRouter.java", "intent": "modify"},
        {"path": "x/RespBalEnqConverter.java", "intent": "modify"},
        {"path": "x/ReqValAddRequest.java", "intent": "modify"},
        {"path": "x/RespBalEnq.xsd", "intent": "add odLimit to RespBalEnq"},
    ]}
    assert PF.touched_message_stems(ta) == ["RespBalEnq"]


def test_touched_message_stems_reads_schema_inventory_and_flow():
    ta = {"schema_inventory": "network-meta.xsd carries ReqTransfer"}
    flow = {"steps": "ReqTransfer then RespTransfer ack"}
    assert PF.touched_message_stems(ta, flow) == ["ReqTransfer", "RespTransfer"]
