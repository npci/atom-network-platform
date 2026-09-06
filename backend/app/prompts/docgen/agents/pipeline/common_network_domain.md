
═══════════════════════════════════════════════════════════
UPI DOMAIN KNOWLEDGE — USE ALL YOU HAVE; KEY FACTS BELOW
═══════════════════════════════════════════════════════════
ECOSYSTEM PARTICIPANTS:
  · NPCI — operates the UPI switch and Common Library (CL). Issues specifications.
  · PSP Bank — Payment Service Provider. Hosts UPI App, initiates ReqTransfer to NPCI.
  · Issuer/Remitter Bank — Payer's bank. Authenticates, debits customer account.
  · Beneficiary/Payee Bank — Recipient's bank. Credits customer account.
  · UPI App — Customer-facing mobile application (e.g., PhonePe, GPay, BHIM).

CORE UPI MESSAGE TYPES:
  · REQ_PAY / RESP_PAY — Payment request/response. Auth → Debit → Credit → Confirmation.
  · REQ_CHK_TXN — Transaction status check. Used for timeout/fallback resolution.
  · REQ_LIST_ACCOUNT / LIST_ACCOUNT — Account listing for registration.
  · REQ_REG_MOB / REG_MOB — Mobile/device registration (used in biometric setting).
  · REQ_AUTH_DETAILS / RESP_AUTH_DETAILS — Authentication detail exchange.
  · REQ_ACTIVATION — Device onboarding, biometric activation/rotation/deactivation.
  · REQ_MANDATE — Mandate creation.
  · REQ_BAL_ENQ — Balance enquiry.
  · REQ_VAL_ADD — VPA validation.

STANDARD REQ_PAY FLOW:
  Stage 1 — Auth:    UPI App captures PIN/biometric → CL encrypts → PSP sends ReqTransfer to NPCI.
  Stage 2 — ReqAuth: NPCI sends REQ_AUTH_DETAILS to Acquirer Bank; PSP responds RESP_AUTH_DETAILS.
  Stage 3 — Debit:   NPCI sends debit request to Remitter/Issuer Bank; bank sends RESP_PAY.
  Stage 4 — Credit:  NPCI routes credit to Beneficiary Bank; bank sends RESP_PAY.
  Stage 5 — Confirm: NPCI sends confirmation to Payer PSP and Payee PSP.

OTHER API FLOW: APP → Payer PSP → NPCI → Payee PSP → NPCI → Payer PSP → APP.

KEY COMPONENTS:
  · CRED Block  — Encrypted credential block containing auth data sent from PSP to NPCI.
  · CL (Common Library) — NPCI-provided SDK integrated in UPI Apps for secure auth.
  · NPCI Switch — Central routing engine for all UPI messages.
  · TEE / Secure Enclave — Hardware-backed secure storage on device.
  · Nonce-based challenge — Cryptographic replay-attack prevention mechanism.
  · VPA — Virtual Payment Address (e.g., user@bank).
  · DEEMED status — Transaction where credit is unconfirmed; settled asynchronously.
  · refCategory='05' — Designates biometric-authenticated transactions.
  · clVersion — CL version field indicating biometric support (e.g., 2.36 in ListAccPvd).

DISPUTE MANAGEMENT:
  · Standard UPI dispute process applies unless explicitly changed by the BRD.
  · "No change in dispute management" is the default for most feature changes.

