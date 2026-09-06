# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""One row per tunnelled HTTP exchange (ITA I-9, migration 0135).

The bar this table exists for: **a failed exchange is diagnosable from the
row alone, without logs** — which alias, which method/path, how many bytes
each way, how long, and the §5.2 error code when it failed. `correlation_id`
carries the exchange id that also rode the A2A hop as the transport
correlation header, so one value threads the Simulator's call, the wire
message and this row (architecture review action A12).

Deliberately FK-free and domain-neutral: an exchange row is transport
telemetry that must outlive whatever business rows sit around it, and the
tunnel carries anyone's bytes, not one ecosystem's.
"""
from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class IntegrationExchange(TimestampMixin, Base):
    __tablename__ = "integration_exchanges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    exchange_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Which end of the tunnel this platform was for this hop.
    direction: Mapped[str] = mapped_column(String(10), nullable=False)   # ingress | egress
    alias: Mapped[str] = mapped_column(String(100), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Recorded VERBATIM, like the path — never re-encoded. Stage 5 contract
    # selection rides entirely on `?pack=`, and the failure it guards against
    # (a normalised or dropped query presenting as "certified against baseline"
    # rather than as an error) is invisible in a row without it. NULL on rows
    # written before 0137; "" means the hop genuinely carried no query — those
    # are different facts. NET-F21, found live against the partner platform.
    query: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # The HTTP status of the final response, when one came back at all.
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The §5.2 structured code when the exchange failed; NULL on success.
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    request_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Header NAMES this hop dropped (hop-by-hop / per-alias strip) — values
    # are deliberately not recorded; some of them are credentials.
    dropped_headers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
