# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""
Cassandra transaction status client.

Production-grade client + transaction status resolver for the network OLTP backup
keyspace. Single-file by design: also exposes a CLI tester at the bottom so you
can run it directly on a server against a live cluster without pulling in any
extra harness.

Tables:
  - upi_oltp_backup.upi_txn_backup_<YYYYMMDD>           (kind="fin")
  - upi_oltp_backup.upi_meta_txn_backup_<YYYYMMDD>      (kind="meta")

Usage (library):
    from cassandra_txn_client import CassandraConfig, TransactionStatusService

    cfg = CassandraConfig(
        contact_points=["10.0.0.1", "10.0.0.2"],
        port=9042,
        username=None,            # or "cassandra"
        password=None,            # only set if auth is enabled
    )
    with TransactionStatusService(cfg) as svc:
        result = svc.get_transaction_status(
            kind="fin",
            txn_id="null000000001vRVBCKsuDU30sD9UoAU",
            txn_date="20260507",
        )
        print(result.status, result.reason)

Usage (tester):
    python cassandra_txn_client.py \\
        --hosts 10.0.0.1,10.0.0.2 --port 9042 \\
        --kind fin --txn-id null000000001vRVBCKsuDU30sD9UoAU --date 20260507
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

try:
    from cassandra import ConsistencyLevel
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT, Session
    from cassandra.policies import (
        DCAwareRoundRobinPolicy,
        ExponentialReconnectionPolicy,
        RetryPolicy,
        TokenAwarePolicy,
    )
    from cassandra.query import SimpleStatement, dict_factory
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "cassandra-driver is required. Install with: pip install cassandra-driver"
    ) from e


log = logging.getLogger("cassandra_txn_client")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEYSPACE = "upi_oltp_backup"
FIN_TABLE_FMT = "upi_txn_backup_{date}"
META_TABLE_FMT = "upi_meta_txn_backup_{date}"

# network schema messages are stored as `org.npci.network.schema.XXX#{json}` — strip prefix.
_SCHEMA_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_.$]+#")

# Typical the network response codes that mean "deemed approved/credited later" — kept
# narrow on purpose; the resolver is structural, not code-driven.
_DEEMED_CODES = {"00"}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class TxnStatus:
    """String constants for the resolved transaction status.

    SUCCESS         — transaction completed successfully.
    FAILURE         — transaction failed (debit/credit declined or errored).
    PENDING         — no downstream response captured yet (in-flight or stuck).
    DEEMED_REVERSED — debit leg failed but the reversal succeeded; money is safe.
    NOT_FOUND       — no row exists for that txn_id on that date.
    UNKNOWN         — row exists but its state could not be confidently classified.
                      Inspect `reason` and the `--show-row` output to diagnose.
    """
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PENDING = "PENDING"
    DEEMED_REVERSED = "DEEMED_REVERSED"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


@dataclass
class TxnStatusResult:
    """Structured result returned by ``TransactionStatusService.get_transaction_status``.

    Fields:
      txn_id          — echoed input.
      kind            — "fin" or "meta".
      txn_date        — normalized YYYYMMDD.
      status          — one of ``TxnStatus.*``. The thing your caller cares about.
      reason          — short human-readable explanation of how status was decided.
      err_code        — network/authority error code if any (e.g. "U17", "U67"); None on success.
      payer_result    — raw "SUCCESS"/"FAILURE" from payer.respPay.respPayResp.result (fin).
      payee_result    — raw "SUCCESS"/"FAILURE" from payees[0].respPay.respPayResp.result (fin).
      reversal_result — raw result from reversalTransaction.respPayReversal.resp.result (fin).
      last_status     — raw value of fin table's `last_status` column (engine-maintained).
      api             — meta_api_name for meta txns (e.g. "ReqValAdd").
      crtn_ts         — fin row creation timestamp.
      last_upd_ts     — fin row last-update timestamp.
      table           — actual table queried, e.g. "upi_txn_backup_20260507".
      raw             — full row as a dict (only populated when callers ask for it).
    """
    txn_id: str
    kind: str
    txn_date: str
    status: str
    reason: str
    err_code: Optional[str] = None
    payer_result: Optional[str] = None
    payee_result: Optional[str] = None
    reversal_result: Optional[str] = None
    last_status: Optional[str] = None
    api: Optional[str] = None
    crtn_ts: Optional[str] = None
    last_upd_ts: Optional[str] = None
    table: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CassandraConfig:
    """All connection and query options for the Cassandra client.

    Required:
      contact_points    — list/tuple of host IPs or DNS names. One is enough; the
                          driver discovers the rest of the cluster automatically.
                          Pass two or three for resiliency on first connect.

    Common:
      port              — CQL native port. Default 9042.
      username/password — leave both as None when auth is disabled. If only
                          password is set, username defaults to "cassandra".
      keyspace          — keyspace to use as the session default. Default
                          "upi_oltp_backup".
      consistency_level — read consistency. LOCAL_ONE (default) is fastest;
                          bump to LOCAL_QUORUM if you need stronger reads.
      request_timeout   — per-query timeout in seconds (default 15s).
      connect_timeout   — initial TCP/handshake timeout in seconds (default 10s).

    Multi-DC routing:
      local_dc          — name of your local datacenter (e.g. "dc1"). When set,
                          the driver pins reads to local replicas. Leave None
                          for single-DC clusters.

    TLS (optional):
      use_ssl           — enable TLS to the cluster.
      ssl_ca_cert       — path to CA bundle (PEM) verifying the server cert.
      ssl_cert/ssl_key  — client cert + key for mutual TLS (rarely needed).

    Advanced:
      protocol_version  — CQL native protocol. 4 covers Cassandra 2.2-4.x. Bump
                          to 5 only if your cluster is ≥4.0 and you need v5.
    """
    contact_points: Sequence[str]
    port: int = 9042
    username: Optional[str] = None
    password: Optional[str] = None
    local_dc: Optional[str] = None
    consistency_level: int = ConsistencyLevel.LOCAL_ONE
    request_timeout: float = 15.0
    connect_timeout: float = 10.0
    protocol_version: int = 4
    use_ssl: bool = False
    ssl_ca_cert: Optional[str] = None
    ssl_cert: Optional[str] = None
    ssl_key: Optional[str] = None
    keyspace: str = KEYSPACE

    def auth_provider(self) -> Optional[PlainTextAuthProvider]:
        """Build the auth provider, or None if auth is disabled (no password)."""
        if self.password:
            return PlainTextAuthProvider(
                username=self.username or "cassandra",
                password=self.password,
            )
        return None

    @classmethod
    def from_env(cls, prefix: str = "CASSANDRA_") -> "CassandraConfig":
        """Build a CassandraConfig from environment variables.

        Reads (all optional except hosts):
          CASSANDRA_HOSTS               comma-separated, e.g. "10.0.0.1,10.0.0.2"
          CASSANDRA_PORT                default 9042
          CASSANDRA_USERNAME            default None
          CASSANDRA_PASSWORD            default None (auth off when blank)
          CASSANDRA_KEYSPACE            default "upi_oltp_backup"
          CASSANDRA_LOCAL_DC            default None
          CASSANDRA_CONSISTENCY         "ONE" / "LOCAL_ONE" / ...; default LOCAL_ONE
          CASSANDRA_REQUEST_TIMEOUT     seconds, default 15
          CASSANDRA_CONNECT_TIMEOUT     seconds, default 10
          CASSANDRA_USE_SSL             "true"/"false", default false
          CASSANDRA_SSL_CA_CERT         path to CA bundle (PEM)
          CASSANDRA_SSL_CERT            path to client cert (mTLS)
          CASSANDRA_SSL_KEY             path to client key  (mTLS)
          CASSANDRA_PROTOCOL_VERSION    int, default 4

        Env var names are formed as ``f"{prefix}{NAME}"`` so you can run
        multiple clients side-by-side with different prefixes if needed.

        Raises ValueError if CASSANDRA_HOSTS is missing.
        """
        def _get(name: str, default: Optional[str] = None) -> Optional[str]:
            v = os.environ.get(prefix + name)
            return v if v not in (None, "") else default

        def _bool(name: str, default: bool = False) -> bool:
            v = _get(name)
            if v is None:
                return default
            return v.strip().lower() in {"1", "true", "yes", "on"}

        hosts_raw = _get("HOSTS")
        if not hosts_raw:
            raise ValueError(f"{prefix}HOSTS is required (comma-separated host list)")
        contact_points = [h.strip() for h in hosts_raw.split(",") if h.strip()]

        consistency_name = _get("CONSISTENCY", "LOCAL_ONE")
        try:
            consistency_level = getattr(ConsistencyLevel, consistency_name)
        except AttributeError as e:
            raise ValueError(f"Invalid {prefix}CONSISTENCY={consistency_name!r}") from e

        return cls(
            contact_points=contact_points,
            port=int(_get("PORT", "9042")),
            username=_get("USERNAME"),
            password=_get("PASSWORD"),
            keyspace=_get("KEYSPACE", KEYSPACE),
            local_dc=_get("LOCAL_DC"),
            consistency_level=consistency_level,
            request_timeout=float(_get("REQUEST_TIMEOUT", "15")),
            connect_timeout=float(_get("CONNECT_TIMEOUT", "10")),
            use_ssl=_bool("USE_SSL", False),
            ssl_ca_cert=_get("SSL_CA_CERT"),
            ssl_cert=_get("SSL_CERT"),
            ssl_key=_get("SSL_KEY"),
            protocol_version=int(_get("PROTOCOL_VERSION", "4")),
        )

    def ssl_context(self) -> Optional[ssl.SSLContext]:
        if not self.use_ssl:
            return None
        ctx = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=self.ssl_ca_cert,
        )
        if self.ssl_cert and self.ssl_key:
            ctx.load_cert_chain(certfile=self.ssl_cert, keyfile=self.ssl_key)
        return ctx


# ---------------------------------------------------------------------------
# Cassandra client
# ---------------------------------------------------------------------------


class _BoundedRetryPolicy(RetryPolicy):
    """Retry on read timeout / unavailable up to N times; rethrow otherwise."""

    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries

    def on_read_timeout(self, query, consistency, required_responses,
                        received_responses, data_retrieved, retry_num):
        if retry_num >= self.max_retries:
            return self.RETHROW, None
        if received_responses >= required_responses and not data_retrieved:
            return self.RETRY, consistency
        return self.RETHROW, None

    def on_unavailable(self, query, consistency, required_replicas,
                       alive_replicas, retry_num):
        if retry_num >= self.max_retries:
            return self.RETHROW, None
        return self.RETRY_NEXT_HOST, None

    def on_write_timeout(self, *args, **kwargs):
        return self.RETHROW, None


class CassandraClient:
    """Reusable Cassandra client. Owns the Cluster + Session lifecycle.

    Typical usage:

        with CassandraClient(cfg) as c:
            row = c.fetch_one("upi_txn_backup_20260507", "txn_id", "abc...")

    Or as a long-lived singleton in a service: call ``connect()`` once at
    startup and ``close()`` at shutdown. The Cluster/Session are thread-safe,
    so you can share one instance across threads / request handlers.
    """

    def __init__(self, cfg: CassandraConfig):
        """Store config; does NOT open a connection (lazy)."""
        self.cfg = cfg
        self._cluster: Optional[Cluster] = None
        self._session: Optional[Session] = None
        self._prepared: dict[str, Any] = {}
        self._connect_lock = threading.Lock()

    def connect(self) -> Session:
        """Open the cluster connection if not already open, return the session.

        Safe to call multiple times — subsequent calls return the cached
        session without re-connecting. The first call performs cluster
        discovery, auth (if configured), and TLS handshake.
        """
        if self._session is not None:
            return self._session
        with self._connect_lock:
            if self._session is not None:
                return self._session

            lb_policy = (
                TokenAwarePolicy(DCAwareRoundRobinPolicy(local_dc=self.cfg.local_dc))
                if self.cfg.local_dc
                else TokenAwarePolicy(DCAwareRoundRobinPolicy())
            )

            profile = ExecutionProfile(
                load_balancing_policy=lb_policy,
                retry_policy=_BoundedRetryPolicy(max_retries=2),
                consistency_level=self.cfg.consistency_level,
                request_timeout=self.cfg.request_timeout,
                row_factory=dict_factory,
            )

            log.info(
                "Connecting to Cassandra contact_points=%s port=%s ssl=%s auth=%s",
                list(self.cfg.contact_points),
                self.cfg.port,
                self.cfg.use_ssl,
                bool(self.cfg.password),
            )

            cluster = Cluster(
                contact_points=list(self.cfg.contact_points),
                port=self.cfg.port,
                auth_provider=self.cfg.auth_provider(),
                ssl_context=self.cfg.ssl_context(),
                protocol_version=self.cfg.protocol_version,
                connect_timeout=self.cfg.connect_timeout,
                execution_profiles={EXEC_PROFILE_DEFAULT: profile},
                reconnection_policy=ExponentialReconnectionPolicy(1.0, 60.0),
                idle_heartbeat_interval=30,
            )
            try:
                session = cluster.connect(self.cfg.keyspace)
            except Exception:
                cluster.shutdown()
                raise

            self._cluster = cluster
            self._session = session
            log.info("Connected. Hosts=%s", [str(h) for h in self._cluster.metadata.all_hosts()])
            return self._session

    def close(self) -> None:
        """Shut down the cluster and release sockets/threads. Idempotent."""
        if self._cluster is not None:
            try:
                self._cluster.shutdown()
            finally:
                self._cluster = None
                self._session = None
                self._prepared.clear()

    def __enter__(self) -> "CassandraClient":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def execute(self, cql: str, params: Sequence[Any] = (), *, timeout: Optional[float] = None):
        """Run a CQL statement and return the driver's ResultSet.

        Args:
          cql:     CQL string. Use %s placeholders for bound parameters.
          params:  iterable of values to bind to the placeholders.
          timeout: optional per-call timeout in seconds; defaults to
                   ``cfg.request_timeout``.

        The session is opened lazily on first call. Use ``.current_rows``
        on the returned object to materialize all rows as dicts.
        """
        session = self.connect()
        stmt = SimpleStatement(cql, consistency_level=self.cfg.consistency_level)
        return session.execute(stmt, params, timeout=timeout or self.cfg.request_timeout)

    def fetch_one(self, table: str, where_col: str, value: Any) -> Optional[dict[str, Any]]:
        """Fetch a single row by equality on one column.

        Equivalent to: ``SELECT * FROM <keyspace>.<table> WHERE <where_col>=<value> LIMIT 1``.

        Args:
          table:     table name within the configured keyspace. Validated
                     against ``[A-Za-z_][A-Za-z0-9_]*`` because Cassandra
                     can't bind table names as parameters.
          where_col: column to filter on (must be a primary-key column for
                     this to be efficient — txn lookups always are).
          value:     value to match.

        Returns:
          The row as a dict (column → value), or None if nothing matched.
        """
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError(f"Unsafe table name: {table!r}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", where_col):
            raise ValueError(f"Unsafe column name: {where_col!r}")

        cql = f"SELECT * FROM {self.cfg.keyspace}.{table} WHERE {where_col}=%s LIMIT 1"
        rows = self.execute(cql, [value]).current_rows
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Status resolution
# ---------------------------------------------------------------------------


def _normalize_date(d: Any) -> str:
    """Accept str/date/datetime, return YYYYMMDD."""
    if isinstance(d, _dt.datetime):
        return d.strftime("%Y%m%d")
    if isinstance(d, _dt.date):
        return d.strftime("%Y%m%d")
    if isinstance(d, str):
        s = d.strip()
        if re.fullmatch(r"\d{8}", s):
            return s
        # try a few common formats
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return _dt.datetime.strptime(s, fmt).strftime("%Y%m%d")
            except ValueError:
                continue
    raise ValueError(f"Unrecognized date: {d!r}; expected YYYYMMDD or date/datetime")


def _strip_schema_prefix(s: str) -> str:
    if not isinstance(s, str):
        return s
    return _SCHEMA_PREFIX_RE.sub("", s, count=1)


def _try_parse_json(s: Any) -> Optional[dict]:
    if not isinstance(s, str) or not s:
        return None
    s = _strip_schema_prefix(s).strip()
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _dig(d: Any, *path: str) -> Any:
    """Safe nested dict navigation."""
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


# ---------- META resolver --------------------------------------------------


def _resolve_meta(row: dict[str, Any]) -> tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """Return (status, reason, err_code, payer_result, api_name) for a meta row."""
    api = row.get("meta_api_name")
    resp_rcvd = row.get("resp_rcvd")

    # Prefer downstream resp; fall back to upstream resp_msg.
    resp_msg = row.get("resp_msg") or row.get("downsteam_resp_msg")
    parsed = _try_parse_json(resp_msg) if resp_msg else None

    if not resp_rcvd and not parsed:
        return (
            TxnStatus.PENDING,
            "no downstream response captured (resp_rcvd=False, resp_msg empty)",
            None,
            None,
            api,
        )

    if not parsed:
        # We have a flag but couldn't parse — surface as UNKNOWN, not a hard fail.
        return (TxnStatus.UNKNOWN, "resp_msg present but unparseable", None, None, api)

    result = _dig(parsed, "resp", "result")
    err_code = _dig(parsed, "resp", "errCode") or None

    if result == "SUCCESS":
        return (TxnStatus.SUCCESS, "resp.result=SUCCESS", err_code, result, api)
    if result == "FAILURE":
        return (
            TxnStatus.FAILURE,
            f"resp.result=FAILURE errCode={err_code or '<none>'}",
            err_code,
            result,
            api,
        )
    return (TxnStatus.UNKNOWN, f"resp.result={result!r}", err_code, result, api)


# ---------- FIN resolver ---------------------------------------------------


_LAST_STATUS_MAP = {
    # Map raw values seen in `last_status` to our taxonomy. Keep loose — the
    # column is engine-maintained and may use any of these spellings.
    "SUCCESS": TxnStatus.SUCCESS,
    "S": TxnStatus.SUCCESS,
    "COMPLETED": TxnStatus.SUCCESS,
    "COMPLETE": TxnStatus.SUCCESS,
    "FAILURE": TxnStatus.FAILURE,
    "FAILED": TxnStatus.FAILURE,
    "F": TxnStatus.FAILURE,
    "DECLINED": TxnStatus.FAILURE,
    "PENDING": TxnStatus.PENDING,
    "INITIATED": TxnStatus.PENDING,
    "IN_PROGRESS": TxnStatus.PENDING,
    "INPROGRESS": TxnStatus.PENDING,
    "P": TxnStatus.PENDING,
    "DEEMED": TxnStatus.DEEMED_REVERSED,
    "DEEMED_APPROVED": TxnStatus.DEEMED_REVERSED,
    "DEEMED_REVERSED": TxnStatus.DEEMED_REVERSED,
    "REVERSED": TxnStatus.DEEMED_REVERSED,
}


def _normalize_last_status(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _LAST_STATUS_MAP.get(raw.strip().upper())


def _resolve_fin(row: dict[str, Any]) -> tuple[
    str, str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]
]:
    """
    Return (status, reason, err_code, payer_result, payee_result, reversal_result, last_status_raw).

    FIN table layout:
      txn_id, crtn_ts, data_integrity_checksum, last_status, last_upd_host,
      last_upd_port, last_upd_site_cd, lst_upd_ts, lst_version, next_action_due,
      product_type, txn_data

    `last_status` is engine-maintained and authoritative. `txn_data` carries the
    full JSON we use to derive details (errCode, payer/payee/reversal results).
    """
    last_status_raw = row.get("last_status")
    doc = _try_parse_json(row.get("txn_data"))

    payer_result = _dig(doc, "payer", "respPay", "respPayResp", "result") if doc else None
    payer_err = _dig(doc, "payer", "respPay", "respPayResp", "errCode") if doc else None
    payee_result = None
    payee_err = None
    if doc:
        payees = _dig(doc, "payees", "payees")
        if isinstance(payees, list) and payees:
            payee_result = _dig(payees[0], "respPay", "respPayResp", "result")
            payee_err = _dig(payees[0], "respPay", "respPayResp", "errCode")

    reversal_result = (
        _dig(doc, "payer", "reqPayDebit", "reversalTransaction", "respPayReversal", "resp", "result")
        if doc else None
    )
    debit_status = _dig(doc, "payer", "reqPayDebit", "reqStatusType", "status") if doc else None
    debit_err = (
        _dig(doc, "payer", "reqPayDebit", "reqStatusType", "errCode")
        or _dig(doc, "payer", "reqPayDebit", "reqStatusType", "respCode")
    ) if doc else None

    err_code = payer_err or payee_err or debit_err

    # Prefer last_status if it maps cleanly.
    mapped = _normalize_last_status(last_status_raw)
    if mapped:
        return (
            mapped,
            f"last_status={last_status_raw!r}"
            + (f" errCode={err_code}" if err_code and mapped == TxnStatus.FAILURE else ""),
            err_code,
            payer_result,
            payee_result,
            reversal_result,
            last_status_raw if isinstance(last_status_raw, str) else None,
        )

    # Fall back to txn_data structural analysis.
    if not doc:
        return (
            TxnStatus.UNKNOWN,
            f"last_status={last_status_raw!r} and txn_data missing/unparseable",
            err_code, payer_result, payee_result, reversal_result,
            last_status_raw if isinstance(last_status_raw, str) else None,
        )

    if payer_result == "SUCCESS" and (payee_result is None or payee_result == "SUCCESS"):
        return (TxnStatus.SUCCESS, "txn_data.payer.respPay.result=SUCCESS",
                err_code, payer_result, payee_result, reversal_result,
                last_status_raw if isinstance(last_status_raw, str) else None)

    if payer_result == "FAILURE" or debit_status == "FAILURE":
        if reversal_result == "SUCCESS":
            return (TxnStatus.DEEMED_REVERSED, "debit failed and reversal succeeded",
                    err_code, payer_result, payee_result, reversal_result,
                    last_status_raw if isinstance(last_status_raw, str) else None)
        return (TxnStatus.FAILURE, f"debit/payer failed errCode={err_code or '<none>'}",
                err_code, payer_result, payee_result, reversal_result,
                last_status_raw if isinstance(last_status_raw, str) else None)

    if payer_result is None and payee_result is None:
        return (TxnStatus.PENDING, "no respPay captured for payer or payee",
                err_code, payer_result, payee_result, reversal_result,
                last_status_raw if isinstance(last_status_raw, str) else None)

    return (TxnStatus.UNKNOWN, f"payer={payer_result!r} payee={payee_result!r}",
            err_code, payer_result, payee_result, reversal_result,
            last_status_raw if isinstance(last_status_raw, str) else None)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TransactionStatusService:
    """High-level API: resolve a network transaction's status from the OLTP backup keyspace.

    This is the class your friend will use. Two ways to instantiate:

      # 1) one-shot script: open + close around a single block
      with TransactionStatusService(cfg) as svc:
          result = svc.get_transaction_status("fin", "<txn_id>", "20260507")
          print(result.status)

      # 2) long-lived service: build once, reuse across many calls
      svc = TransactionStatusService(cfg)
      ...
      result = svc.get_transaction_status(...)
      ...
      svc.client.close()  # at shutdown

    Pass ``client=existing_client`` if you already have a ``CassandraClient``
    you want to reuse — the service will not close it on ``__exit__`` in that
    case.
    """

    def __init__(self, cfg: CassandraConfig, *, client: Optional[CassandraClient] = None):
        """Build a service. If ``client`` is given, it is reused (and not
        owned — caller is responsible for closing it). Otherwise a new
        ``CassandraClient`` is created internally."""
        self._owned = client is None
        self.client = client or CassandraClient(cfg)

    def __enter__(self) -> "TransactionStatusService":
        self.client.connect()
        return self

    def __exit__(self, *_exc) -> None:
        if self._owned:
            self.client.close()

    def get_transaction_status(
        self,
        kind: str,
        txn_id: str,
        txn_date: Any,
    ) -> TxnStatusResult:
        """Look up one transaction and return its resolved status.

        Args:
          kind:     "fin" for financial txns (upi_txn_backup_<date> table) or
                    "meta" for non-financial APIs like ReqValAdd, ReqAuthDetails,
                    etc. (upi_meta_txn_backup_<date> table). Case-insensitive.
          txn_id:   the network transaction id, e.g. "null000000001vRVBCKsuDU30sD9UoAU".
          txn_date: the date the transaction occurred, used to pick the daily
                    partition table. Accepts:
                      - "YYYYMMDD"   ("20260507")
                      - "YYYY-MM-DD" ("2026-05-07")
                      - "DD-MM-YYYY", "DD/MM/YYYY", "YYYY/MM/DD"
                      - datetime.date or datetime.datetime objects

        Returns:
          A ``TxnStatusResult``. The ``.status`` field is the headline value.
          This method NEVER raises on Cassandra errors — they are caught and
          surfaced as ``status=UNKNOWN`` with a ``reason`` string. It DOES
          raise ``ValueError`` for bad inputs (unknown kind, blank txn_id,
          unparseable date) — those are programmer errors, not runtime ones.

        Examples:
          >>> svc.get_transaction_status("fin", "null000000001vRVBCKsuDU30sD9UoAU", "20260507")
          TxnStatusResult(status='SUCCESS', reason="last_status='SUCCESS'", ...)

          >>> svc.get_transaction_status("meta", "null000...", date(2026,5,8))
          TxnStatusResult(status='FAILURE', err_code='U17', api='ReqValAdd', ...)
        """
        kind_norm = (kind or "").strip().lower()
        if kind_norm not in {"fin", "meta"}:
            raise ValueError(f"kind must be 'fin' or 'meta', got {kind!r}")
        if not txn_id or not isinstance(txn_id, str):
            raise ValueError("txn_id must be a non-empty string")

        date_str = _normalize_date(txn_date)
        table = (FIN_TABLE_FMT if kind_norm == "fin" else META_TABLE_FMT).format(date=date_str)

        t0 = time.perf_counter()
        try:
            row = self.client.fetch_one(table, "txn_id", txn_id)
        except Exception as e:
            log.exception("Cassandra query failed for table=%s txn_id=%s", table, txn_id)
            return TxnStatusResult(
                txn_id=txn_id,
                kind=kind_norm,
                txn_date=date_str,
                status=TxnStatus.UNKNOWN,
                reason=f"query error: {e.__class__.__name__}: {e}",
                table=table,
            )
        finally:
            log.debug("Query took %.1f ms", (time.perf_counter() - t0) * 1000)

        if row is None:
            return TxnStatusResult(
                txn_id=txn_id,
                kind=kind_norm,
                txn_date=date_str,
                status=TxnStatus.NOT_FOUND,
                reason=f"no row in {KEYSPACE}.{table}",
                table=table,
            )

        if kind_norm == "meta":
            status, reason, err_code, payer_result, api = _resolve_meta(row)
            return TxnStatusResult(
                txn_id=txn_id,
                kind=kind_norm,
                txn_date=date_str,
                status=status,
                reason=reason,
                err_code=err_code,
                payer_result=payer_result,
                api=api,
                table=table,
                raw={k: v for k, v in row.items() if not isinstance(v, (bytes, bytearray))},
            )

        (status, reason, err_code, payer_result, payee_result,
         reversal_result, last_status_raw) = _resolve_fin(row)
        return TxnStatusResult(
            txn_id=txn_id,
            kind=kind_norm,
            txn_date=date_str,
            status=status,
            reason=reason,
            err_code=err_code,
            payer_result=payer_result,
            payee_result=payee_result,
            reversal_result=reversal_result,
            last_status=last_status_raw,
            crtn_ts=str(row.get("crtn_ts")) if row.get("crtn_ts") is not None else None,
            last_upd_ts=str(row.get("lst_upd_ts")) if row.get("lst_upd_ts") is not None else None,
            table=table,
            raw={k: v for k, v in row.items() if not isinstance(v, (bytes, bytearray))},
        )


# ---------------------------------------------------------------------------
# CLI tester  (run on server: `python cassandra_txn_client.py ...`)
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cassandra_txn_client",
        description="Resolve network transaction status from upi_oltp_backup keyspace.",
    )
    p.add_argument("--hosts", required=True,
                   help="Comma-separated contact points, e.g. 10.0.0.1,10.0.0.2")
    p.add_argument("--port", type=int, default=9042)
    p.add_argument("--username", default=None,
                   help="Optional. Defaults to 'cassandra' if --password is set.")
    p.add_argument("--password", default=None,
                   help="Optional. Omit if Cassandra has no auth configured.")
    p.add_argument("--local-dc", default=None,
                   help="Optional. Local DC name for token-aware routing.")
    p.add_argument("--keyspace", default=KEYSPACE)
    p.add_argument("--ssl", action="store_true", help="Enable TLS")
    p.add_argument("--ssl-ca", default=None, help="Path to CA cert (PEM)")
    p.add_argument("--consistency", default="LOCAL_ONE",
                   choices=["ONE", "LOCAL_ONE", "QUORUM", "LOCAL_QUORUM", "ALL"])
    p.add_argument("--timeout", type=float, default=15.0)

    p.add_argument("--kind", choices=["fin", "meta"],
                   help="Required unless --ping is set")
    p.add_argument("--txn-id", help="Required unless --ping is set")
    p.add_argument("--date",
                   help="YYYYMMDD or YYYY-MM-DD. Required unless --ping is set")

    p.add_argument("--show-row", action="store_true",
                   help="Also print the raw row (truncated)")
    p.add_argument("--ping", action="store_true",
                   help="Just verify connectivity and exit")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def _truncate(v: Any, n: int = 400) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"... <+{len(v) - n} chars>"
    return v


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else (logging.INFO if args.verbose else logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    consistency = getattr(ConsistencyLevel, args.consistency)
    cfg = CassandraConfig(
        contact_points=[h.strip() for h in args.hosts.split(",") if h.strip()],
        port=args.port,
        username=args.username,
        password=args.password,
        local_dc=args.local_dc,
        consistency_level=consistency,
        request_timeout=args.timeout,
        use_ssl=args.ssl,
        ssl_ca_cert=args.ssl_ca,
        keyspace=args.keyspace,
    )

    if args.ping:
        with CassandraClient(cfg) as c:
            rows = list(c.execute("SELECT release_version FROM system.local").current_rows)
            release = rows[0].get("release_version") if rows else None
            print(json.dumps({"ok": True, "release_version": release}))
        return 0

    missing = [n for n, v in (("--kind", args.kind), ("--txn-id", args.txn_id), ("--date", args.date)) if not v]
    if missing:
        print(f"error: required for query mode: {', '.join(missing)} (omit them only with --ping)", file=sys.stderr)
        return 2

    try:
        with TransactionStatusService(cfg) as svc:
            result = svc.get_transaction_status(
                kind=args.kind, txn_id=args.txn_id, txn_date=args.date,
            )
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{e.__class__.__name__}: {e}"}, indent=2))
        return 2

    payload = result.to_dict()
    if args.show_row:
        payload["raw"] = {k: _truncate(v) for k, v in result.raw.items()}

    print(json.dumps(payload, indent=2, default=str))
    return 0 if result.status not in (TxnStatus.UNKNOWN,) else 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
