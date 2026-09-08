
═══════════════════════════════════════════════════════════
NETWORK DOMAIN KNOWLEDGE — USE ALL YOU HAVE; KEY FACTS BELOW
═══════════════════════════════════════════════════════════
ECOSYSTEM PARTICIPANTS:
  · The Authority — operates the network switch and Common Library (CL). Issues specifications.
  · PSP Bank — Payment Service Provider. Hosts the network app, initiates ReqTransfer to the Authority.
  · Issuer/Remitter Bank — Payer's bank. Authenticates, debits customer account.
  · Beneficiary/Payee Bank — Recipient's bank. Credits customer account.
  · Network App — a customer-facing application provided by a participant.

CORE NETWORK MESSAGE TYPES:
  · REQ_TRANSFER / RESP_TRANSFER — Payment request/response. Auth → Debit → Credit → Confirmation.
  · REQ_CHK_TXN — Transaction status check. Used for timeout/fallback resolution.
  · REQ_LIST_ACCOUNT / LIST_ACCOUNT — Account listing for registration.
  · REQ_REG_MOB / REG_MOB — Mobile/device registration (used in biometric setting).
  · REQ_AUTH_DETAILS / RESP_AUTH_DETAILS — Authentication detail exchange.
  · REQ_ACTIVATION — Device onboarding, biometric activation/rotation/deactivation.
  · REQ_MANDATE — Mandate creation.
  · REQ_BAL_ENQ — Balance enquiry.
  · REQ_VAL_ADD — VPA validation.

STANDARD REQ_TRANSFER FLOW:
  Stage 1 — Auth:    Network app captures PIN/biometric → CL encrypts → PSP sends ReqTransfer to the Authority.
  Stage 2 — ReqAuth: The Authority sends REQ_AUTH_DETAILS to Acquirer Bank; PSP responds RESP_AUTH_DETAILS.
  Stage 3 — Debit:   The Authority sends debit request to Remitter/Issuer Bank; bank sends RESP_TRANSFER.
  Stage 4 — Credit:  The Authority routes credit to Beneficiary Bank; bank sends RESP_TRANSFER.
  Stage 5 — Confirm: The Authority sends confirmation to Payer PSP and Payee PSP.

OTHER API FLOW: APP → Payer PSP → Authority → Payee PSP → Authority → Payer PSP → APP.

KEY COMPONENTS:
  · CRED Block  — Encrypted credential block containing auth data sent from PSP to the Authority.
  · CL (Common Library) — Authority-provided SDK integrated in network apps for secure auth.
  · Authority Switch — Central routing engine for all network messages.
  · TEE / Secure Enclave — Hardware-backed secure storage on device.
  · Nonce-based challenge — Cryptographic replay-attack prevention mechanism.
  · VPA — Virtual Payment Address (e.g., user@bank).
  · DEEMED status — Transaction where credit is unconfirmed; settled asynchronously.
  · refCategory='05' — Designates biometric-authenticated transactions.
  · clVersion — CL version field indicating biometric support (e.g., 2.36 in ListAccPvd).

DISPUTE MANAGEMENT:
  · Standard network dispute process applies unless explicitly changed by the BRD.
  · "No change in dispute management" is the default for most feature changes.
