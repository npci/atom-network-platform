# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Minimal Eclipse JDT Language Server (jdtls) client for on-demand diagnostics.

The §8 "structural tier" advisory LSP backend. One short-lived jdtls session per
call: launch → initialize → didOpen the file → collect `publishDiagnostics` → exit.
Strictly bounded (wall-clock deadline) and fully degrading: ANY failure (jdtls not
installed, slow import, crash, timeout) returns an error string, never raises and
never hangs the agent loop. Gated by ``agentic_lsp_enabled`` — OFF by default since
jdtls is a 0.5–1 GB JVM server (fine on a high-RAM host, not on a small box).

Diagnostics are ADVISORY: the authoritative compile gate is still ``verify_change``
(real ``mvn``). This just gives faster, inline, type-aware hints when enabled.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("app.agentic")

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _launcher_jar(home: str) -> str | None:
    hits = glob.glob(os.path.join(home, "plugins", "org.eclipse.equinox.launcher_*.jar"))
    return hits[0] if hits else None


def _writable_config_dir(home: str, data_dir: str) -> str:
    """jdtls WRITES to its ``-configuration`` dir (OSGi runtime state + locks). The
    shipped copy under ``home`` (e.g. /opt/jdtls) is usually root-owned / read-only for
    the app user, so pointing jdtls straight at it makes startup fail with a permission
    error — "enabled" but silently dead. So copy the shipped config ONCE into a writable
    per-workspace dir (under ``data_dir``, which the agent owns) and use that. ``home``
    then only needs to be READABLE, never writable. Falls back to the shipped dir if the
    copy can't be made (jdtls may still work if home happens to be writable)."""
    shipped = None
    # Try the known per-OS config dir names; pick the first that exists under home.
    for name in ("config_linux", "config_linux_arm", "config_ss_linux", "config_mac", "config_win"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            shipped = p
            break
    dest = os.path.join(data_dir, "jdtls-config")
    try:
        if shipped and not os.path.isdir(dest):
            os.makedirs(data_dir, exist_ok=True)
            shutil.copytree(shipped, dest)
            # copytree PRESERVES the source mode bits — a read-only /opt install would
            # yield a read-only copy, defeating the purpose. Force owner-write back on
            # every copied dir + file so jdtls can write its OSGi state.
            for r, dirs, files in os.walk(dest):
                for n in (dirs + files):
                    p = os.path.join(r, n)
                    try:
                        os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
                    except OSError:
                        pass
            try:
                os.chmod(dest, os.stat(dest).st_mode | stat.S_IWUSR)
            except OSError:
                pass
        if os.path.isdir(dest):
            return dest
    except OSError as e:  # copy not possible — degrade to the shipped (read-only) dir
        logger.warning("jdtls: could not make a writable config copy (%s) — using %s", e, shipped)
    return shipped or os.path.join(home, "config_linux")


def _launch_cmd(home: str, data_dir: str) -> list[str] | None:
    jar = _launcher_jar(home)
    if not jar:
        return None
    return [
        "java",
        "-Declipse.application=org.eclipse.jdt.ls.core.id1",
        "-Dosgi.bundles.defaultStartLevel=4",
        "-Declipse.product=org.eclipse.jdt.ls.core.product",
        "-Dlog.level=ERROR",
        f"-Xmx{max(512, int(settings.agentic_lsp_heap_mb))}m",
        "--add-modules=ALL-SYSTEM",
        "--add-opens", "java.base/java.util=ALL-UNNAMED",
        "--add-opens", "java.base/java.lang=ALL-UNNAMED",
        "-jar", jar,
        # Use a WRITABLE config copy under the (agent-owned) data dir, so a read-only
        # jdtls install (e.g. root-owned /opt/jdtls) doesn't fail jdtls on startup.
        "-configuration", _writable_config_dir(home, data_dir),
        "-data", data_dir,
    ]


def _write(proc: subprocess.Popen, obj: dict) -> None:
    body = json.dumps(obj).encode("utf-8")
    proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    proc.stdin.flush()


def _reader(proc: subprocess.Popen, out: list, stop: threading.Event) -> None:
    """Background thread: parse Content-Length-framed JSON-RPC messages → out."""
    buf = b""
    try:
        while not stop.is_set():
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\r\n\r\n" in buf:
                header, rest = buf.split(b"\r\n\r\n", 1)
                length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if len(rest) < length:
                    break
                payload, buf = rest[:length], rest[length:]
                try:
                    out.append(json.loads(payload.decode("utf-8")))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


def diagnostics(workspace_dir: str, file_rel_path: str) -> list[dict] | str:
    """Return jdtls diagnostics for ``file_rel_path`` (repo-relative) in ``workspace_dir``,
    or a human-readable degraded string. Never raises; bounded by agentic_lsp_timeout_s."""
    home = settings.agentic_lsp_home
    cmd = _launch_cmd(home, str(Path(workspace_dir) / ".jdtls-data"))
    if cmd is None:
        return f"(LSP unavailable — jdtls not found at {home}; use verify_change/run_command)"

    target = Path(workspace_dir) / file_rel_path
    if not target.is_file():
        return f"(file not found for LSP: {file_rel_path})"
    uri = target.resolve().as_uri()
    deadline = time.monotonic() + max(15, int(settings.agentic_lsp_timeout_s))

    proc = None
    stop = threading.Event()
    msgs: list = []
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=workspace_dir)
        t = threading.Thread(target=_reader, args=(proc, msgs, stop), daemon=True)
        t.start()

        _write(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "processId": os.getpid(), "rootUri": Path(workspace_dir).resolve().as_uri(),
            "capabilities": {"textDocument": {"publishDiagnostics": {}}},
            "workspaceFolders": [{"uri": Path(workspace_dir).resolve().as_uri(), "name": "ws"}]}})

        # wait for the initialize response (id==1)
        while time.monotonic() < deadline and not any(m.get("id") == 1 for m in msgs):
            time.sleep(0.2)
        _write(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        content = target.read_text(encoding="utf-8", errors="replace")
        _write(proc, {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": uri, "languageId": "java", "version": 1, "text": content}}})

        # collect publishDiagnostics for our file until it settles or we hit the deadline
        found: list[dict] | None = None
        while time.monotonic() < deadline:
            for m in msgs:
                if m.get("method") == "textDocument/publishDiagnostics" \
                        and (m.get("params") or {}).get("uri") == uri:
                    found = m["params"].get("diagnostics", [])
            if found is not None:
                # jdtls often emits an empty set first then the real one — give a short settle window
                time.sleep(1.0)
                for m in msgs:
                    if m.get("method") == "textDocument/publishDiagnostics" \
                            and (m.get("params") or {}).get("uri") == uri:
                        found = m["params"].get("diagnostics", [])
                break
            time.sleep(0.3)

        if found is None:
            return "(LSP timed out importing the project — try again, or use verify_change)"
        out = []
        for d in found:
            rng = (d.get("range") or {}).get("start") or {}
            out.append({"line": (rng.get("line", 0) + 1), "col": (rng.get("character", 0) + 1),
                        "severity": _SEVERITY.get(d.get("severity"), "info"),
                        "message": (d.get("message") or "").strip()})
        return out
    except Exception as e:  # noqa: BLE001 — degrade, never raise into the loop
        logger.warning("lsp diagnostics failed for %s: %s", file_rel_path, e)
        return f"(LSP error: {type(e).__name__} — use verify_change/run_command)"
    finally:
        stop.set()
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
