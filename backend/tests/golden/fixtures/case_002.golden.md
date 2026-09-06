## XSD Change Assessment

### Decision
**REQUIRED**

### Rationale
The refund request message gains three new data elements (`originalTxnId`, `refundAmount`, `reasonCode`) that do not exist in the current refund schema, and `reasonCode` introduces a fixed enumerated value set that must be constrained at the schema level. Even though all three fields are optional and the change is designed to be backward-compatible, the XSD must be updated to formally define these new elements, their data types, and the enumeration constraint so that both the Authority's validation layer and PSP-side schema validation accept and correctly type the new fields.

### Affected Schemas
- `upi_refund_request.xsd` (governs the `ReqRefund` message)
  - New optional elements added inside the `Refund`/`Tran` complex type: `OriginalTxnId`, `RefundAmount`, `ReasonCode`.
  - New simple type `ReasonCodeType` (enumeration restriction) added.
- `upi_refund_response.xsd` (governs `RespRefund`) — *Assumption: no new fields are echoed back in the response per the tech spec; only referenced for completeness of validation error codes (e.g., BD/TD for exceeded refund amount or invalid reason code). No structural change required here unless the Authority mandates echoing `refundAmount`.*

### New Elements / Types to be Added

| Element/Type Name | Parent Element | Data Type | Min/MaxOccurs | Description |
|---|---|---|---|---|
| `OriginalTxnId` | `Refund` | `xs:string` (length per existing txnId convention, e.g. 35 chars) | 0..1 | Identifier of the completed original payment being partially refunded; required by application logic for partial refunds but optional on the wire for backward compatibility. |
| `RefundAmount` | `Refund` | `xs:decimal` (`totalDigits="16"`, `fractionDigits="2"`) | 0..1 | Amount to be refunded; absence indicates a full refund. |
| `ReasonCode` | `Refund` | `ReasonCodeType` (`xs:string` with `xs:enumeration` restricted to `DISPUTED_ITEM`, `GOODS_NOT_RECEIVED`, `PRICE_ADJUSTMENT`, `OTHER`) | 0..1 | Enumerated reason for the refund. |

### Impact Assessment
- **Backward compatibility:** Non-breaking — all three new elements are optional (`minOccurs="0"`); existing senders that omit them continue to be processed as full refunds per current behaviour.
- **Affected message types:** `ReqRefund` (primary change); `RespRefund` and downstream reconciliation/settlement messages may require review to confirm no new mandatory echo fields are needed, though the tech spec does not require this.
- **Partner re-certification required:** Yes — although the change is non-breaking, PSPs and acquiring banks implementing partial refund functionality must be re-certified to confirm correct population of `originalTxnId`, `refundAmount`, `reasonCode`, and correct handling of the new BD-classified rejection cases (refund amount exceeding unrefunded balance; invalid `reasonCode`) with the network-compliant error codes rather than free-form errors.