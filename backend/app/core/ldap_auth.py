# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Hybrid LDAP/AD authentication (InfoSec phase 3).

`ldap_authenticate` binds to the directory over LDAPS as a read-only service
account, finds the user, then re-binds AS the user with their password (the real
credential check — never stored). On success it returns the directory attributes
+ group memberships, which `map_role` turns into an app role.

`ldap3` is imported lazily so this module (and the auth router) load fine when
LDAP is disabled / the package isn't installed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LdapIdentity:
    username: str
    email: str
    full_name: str
    groups: list[str] = field(default_factory=list)   # group DNs (memberOf)


def map_role(groups: list[str]) -> str | None:
    """Map directory group DNs → an app role via settings.ldap_group_role_map.
    First configured group the user is a member of wins (dict insertion order).
    Case-insensitive DN match. None when no mapped group → login is denied."""
    role_map = settings.ldap_group_role_map or {}
    member = {g.strip().lower() for g in (groups or [])}
    for dn, role in role_map.items():
        if dn.strip().lower() in member:
            return role
    return None


def map_roles(groups: list[str]) -> list[str]:
    """Map directory group DNs → ALL matching app roles (multi-role support).
    Preserves ldap_group_role_map insertion order and dedupes. Empty list when
    the user is in no mapped group → login is denied. The FIRST entry is used as
    the default active role on provisioning."""
    role_map = settings.ldap_group_role_map or {}
    member = {g.strip().lower() for g in (groups or [])}
    out: list[str] = []
    for dn, role in role_map.items():
        if dn.strip().lower() in member and role not in out:
            out.append(role)
    return out


def ldap_authenticate(username: str, password: str) -> LdapIdentity | None:
    """Return an LdapIdentity iff `username`/`password` bind successfully against
    the directory; None on any failure (bad credentials, not found, LDAP down).
    Never raises to the caller — LDAP problems degrade to 'auth failed'."""
    if not (settings.ldap_enabled and settings.ldap_server_uri and username and password):
        return None
    try:
        from ldap3 import Server, Connection, Tls, ALL, SUBTREE
        from ldap3.core.exceptions import LDAPException
        import ssl
    except Exception as e:  # noqa: BLE001 — ldap3 missing / import error
        logger.error("LDAP: ldap3 unavailable (%s) — LDAP auth disabled", e)
        return None

    tls = None
    if settings.ldap_server_uri.lower().startswith("ldaps://") or settings.ldap_start_tls:
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=settings.ldap_ca_cert or None,
        )
    use_ssl = settings.ldap_server_uri.lower().startswith("ldaps://")

    try:
        server = Server(settings.ldap_server_uri, use_ssl=use_ssl, tls=tls, get_info=ALL)
        # 1) Service-account bind for the user search.
        svc = Connection(server, settings.ldap_bind_dn, settings.ldap_bind_password, auto_bind=True)
        if settings.ldap_start_tls and not use_ssl:
            svc.start_tls()
        search_filter = settings.ldap_user_filter.format(username=_escape(username))
        svc.search(
            settings.ldap_user_search_base, search_filter, search_scope=SUBTREE,
            attributes=["distinguishedName", "mail", "displayName", "cn", "memberOf"],
        )
        if not svc.entries:
            logger.info("LDAP: user %s not found", username)
            svc.unbind()
            return None
        entry = svc.entries[0]
        user_dn = str(entry.entry_dn)
        attrs = entry.entry_attributes_as_dict
        svc.unbind()

        # 2) Re-bind AS the user — this is the actual password check.
        user_conn = Connection(server, user_dn, password, auto_bind=True)
        if settings.ldap_start_tls and not use_ssl:
            user_conn.start_tls()
        user_conn.unbind()
    except LDAPException as e:
        logger.info("LDAP auth failed for %s: %s", username, e)
        return None
    except Exception as e:  # noqa: BLE001 — TLS / network / config
        logger.error("LDAP auth error for %s: %s", username, e)
        return None

    return LdapIdentity(
        username=username,
        email=_first(attrs.get("mail")) or "",
        full_name=_first(attrs.get("displayName")) or _first(attrs.get("cn")) or username,
        groups=[str(g) for g in (attrs.get("memberOf") or [])],
    )


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _escape(value: str) -> str:
    """Escape LDAP filter special chars (RFC 4515 §3) to prevent filter injection.

    Escapes every character that has special meaning in an LDAP filter string:
      *  ``\\``  — escape character
      *  ``*``   — wildcard
      *  ``(`` ``)`` — filter grouping
      *  ``\0``  — null byte
      *  ``&`` ``|`` ``!`` — filter operators (AND, OR, NOT)
      *  ``=`` ``~=`` ``>=`` ``<=`` — comparison operators
      *  ``:`` ``;`` — extensible match / rule identifiers
      *  ``/`` — used in some directory path attributes
      *  Whitespace (`` `` ``\\t`` ``\\n``) — significant in some filter positions

    Each special character is replaced with its \\XX hex escape per RFC 4515.
    """
    out = []
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code > 0x7E or ch in "\\*()&|!=~:;/ \t\n\r":
            out.append("\\%02x" % code)
        else:
            out.append(ch)
    return "".join(out)
