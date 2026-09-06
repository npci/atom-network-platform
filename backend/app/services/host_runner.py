# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Async SSH runner used by Phase B to invoke the host's build_and_deploy.sh.

Yields one event tuple per output line so callers can stream/parse without
buffering the whole run. Final tuple is always ``("exit", <returncode>)``.

Why asyncssh: the existing Phase B paths used `asyncio.create_subprocess_exec`
to a local script. The unified build+deploy runs on a separate host (the
operator's VM that owns the GitLab token + service tree), so we need an
async SSH stream — paramiko is sync-only and would block the event loop
during multi-minute builds.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Tuple

logger = logging.getLogger(__name__)


HostEvent = Tuple[str, object]
# ("stdout", "<line>") | ("stderr", "<line>") | ("exit", <int>)


async def stream_remote_command(
    host: str,
    user: str,
    private_key_path: str,
    command: str,
    *,
    connect_timeout: int = 30,
) -> AsyncIterator[HostEvent]:
    """Open an SSH session, run *command*, yield (kind, line) tuples.

    Stops yielding after the final ``("exit", returncode)`` event. Any
    asyncssh-level error is mapped to ``("stderr", "<msg>")`` followed by
    ``("exit", -1)`` so callers always see exactly one terminal event.
    """
    try:
        import asyncssh  # type: ignore
    except ImportError:
        msg = (
            "asyncssh is not installed in this backend image. Add "
            "`asyncssh>=2.14.0` to backend/requirements.txt and rebuild."
        )
        logger.error(msg)
        yield ("stderr", msg)
        yield ("exit", -1)
        return

    # Resolve known_hosts: prefer operator-configured path, then system/user
    # defaults. If no file exists, log a warning and proceed (graceful
    # degradation — the operator should provision known_hosts for production).
    import os as _os
    from app.core.config import settings as _settings
    _known_hosts_path = getattr(_settings, "build_host_known_hosts", "") or ""
    if not _known_hosts_path:
        for _candidate in ("/etc/ssh/ssh_known_hosts",
                           _os.path.expanduser("~/.ssh/known_hosts")):
            if _os.path.isfile(_candidate):
                _known_hosts_path = _candidate
                break
    _known_hosts_arg: str | None = None
    if _known_hosts_path and _os.path.isfile(_known_hosts_path):
        _known_hosts_arg = _known_hosts_path
    else:
        logger.warning(
            "No SSH known_hosts file found (checked BUILD_HOST_KNOWN_HOSTS, "
            "/etc/ssh/ssh_known_hosts, ~/.ssh/known_hosts). Host key "
            "verification is DISABLED — provision known_hosts for production."
        )

    try:
        async with asyncssh.connect(
            host,
            username=user,
            client_keys=[private_key_path],
            known_hosts=_known_hosts_arg,
            connect_timeout=connect_timeout,
        ) as conn:
            # `term_type` is required for some scripts that probe TTY (e.g. for
            # colour). Most build/deploy scripts emit ANSI sequences regardless;
            # the parser strips them.
            async with conn.create_process(command, term_type="xterm") as process:
                # Read both streams concurrently. asyncssh's SSHClientProcess
                # exposes line-buffered iterators; the simplest correct merge
                # is to alternate reads until both close. We use
                # asyncio.wait + tasks rather than a fancy interleaver because
                # build/deploy scripts mostly write to one stream at a time.
                import asyncio

                async def _drain(reader, kind: str):
                    async for line in reader:
                        yield (kind, line.rstrip("\r\n"))

                stdout_iter = _drain(process.stdout, "stdout")
                stderr_iter = _drain(process.stderr, "stderr")

                stdout_task = asyncio.create_task(stdout_iter.__anext__())
                stderr_task = asyncio.create_task(stderr_iter.__anext__())
                pending = {stdout_task, stderr_task}
                stream_map = {stdout_task: stdout_iter, stderr_task: stderr_iter}

                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        try:
                            yield task.result()
                        except StopAsyncIteration:
                            stream_map.pop(task, None)
                            continue
                        # Schedule the next line on the same iterator.
                        it = stream_map.get(task)
                        if it is not None:
                            new_task = asyncio.create_task(it.__anext__())
                            pending.add(new_task)
                            stream_map[new_task] = it

                rc = await process.wait()
                yield ("exit", int(getattr(rc, "exit_status", 0) or 0))
    except Exception as e:  # noqa: BLE001 — surfaced verbatim to caller
        logger.exception("Remote command failed: host=%s user=%s", host, user)
        yield ("stderr", f"SSH error: {e}")
        yield ("exit", -1)
