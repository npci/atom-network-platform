# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Local subprocess runner for Phase B build+deploy.

Mirrors the API of :mod:`app.services.host_runner` so callers can swap
between SSH (remote host) and local subprocess execution behind a single
config switch (``settings.phase_b_runner_mode``).

Yields ``("stdout", line)`` / ``("stderr", line)`` events and a final
``("exit", returncode)`` tuple, in the same shape host_runner uses.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Tuple

logger = logging.getLogger(__name__)


HostEvent = Tuple[str, object]

# Grace period after the process is reaped, for output still buffered in the
# pipes. Only relevant when children keep the pipes open (see stream_local_command).
_POST_EXIT_FLUSH_SECONDS = 2.0
# How often to check whether the child has been reaped.
_EXIT_POLL_SECONDS = 0.25


async def stream_local_command(command: str) -> AsyncIterator[HostEvent]:
    """Spawn *command* as a shell process, stream output line-by-line.

    The command is run via ``/bin/bash -c`` so the operator's existing
    invocation (``bash <script> <args...>``) keeps working without
    further parsing on our side.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to spawn local build process")
        yield ("stderr", f"local spawn error: {e}")
        yield ("exit", -1)
        return

    async def _drain(reader, kind: str):
        if reader is None:
            return
        while True:
            line = await reader.readline()
            if not line:
                break
            yield (kind, line.decode("utf-8", errors="replace").rstrip("\r\n"))

    stdout_iter = _drain(process.stdout, "stdout")
    stderr_iter = _drain(process.stderr, "stderr")

    stdout_task = asyncio.create_task(stdout_iter.__anext__())
    stderr_task = asyncio.create_task(stderr_iter.__anext__())
    pending = {stdout_task, stderr_task}
    stream_map = {stdout_task: stdout_iter, stderr_task: stderr_iter}

    # WHY we poll `returncode` instead of draining to EOF or awaiting wait():
    # build_and_deploy.sh ends by starting long-lived services (starter.sh ->
    # `nohup java -jar ... &`). Those children inherit the stdout/stderr pipe
    # write ends — even with their own 0/1/2 redirected, copies survive on
    # higher fds — so the pipes never reach EOF and a drain-until-EOF loop
    # waits forever on a script that finished minutes ago.
    #
    # `await process.wait()` does NOT solve it: asyncio resolves its exit
    # waiters from _call_connection_lost, which _try_finish() only reaches once
    # every pipe is disconnected — so wait() blocks on the same condition.
    # `process.returncode` is set directly by _process_exited as soon as the
    # child is reaped, so polling it is the one signal the pipes can't stall.
    async def _await_exit():
        while process.returncode is None:
            await asyncio.sleep(_EXIT_POLL_SECONDS)
        return process.returncode

    exit_task = asyncio.create_task(_await_exit())
    rc = None

    try:
        while pending:
            wait_set = set(pending)
            if not exit_task.done():
                wait_set.add(exit_task)
            done, _ = await asyncio.wait(
                wait_set,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=None if rc is None else _POST_EXIT_FLUSH_SECONDS,
            )
            if not done:
                break  # flush window elapsed with no further output
            for task in done:
                if task is exit_task:
                    rc = task.result()
                    continue
                pending.discard(task)
                try:
                    yield task.result()
                except StopAsyncIteration:
                    stream_map.pop(task, None)
                    continue
                it = stream_map.get(task)
                if it is not None:
                    new_task = asyncio.create_task(it.__anext__())
                    pending.add(new_task)
                    stream_map[new_task] = it

        for task in pending:
            task.cancel()
        if not exit_task.done():
            exit_task.cancel()
        if rc is None:
            # Never `await process.wait()` here — see above, it can block
            # indefinitely on pipes held open by surviving grandchildren.
            rc = process.returncode if process.returncode is not None else -1
        yield ("exit", int(rc))
    except Exception as e:  # noqa: BLE001
        logger.exception("Local build stream error")
        yield ("stderr", f"local stream error: {e}")
        # Best-effort kill if still running.
        try:
            if process.returncode is None:
                process.kill()
                await process.wait()
        except Exception:  # noqa: BLE001
            pass
        yield ("exit", -1)
    finally:
        # SCR finding #10 (Improper Resource Shutdown or Release) — the two
        # branches above only kill a still-running process on the *exception*
        # path. If this async generator is instead abandoned early (caller
        # breaks out of the `async for` loop, or the enclosing task is
        # cancelled) neither branch runs: GeneratorExit is a BaseException,
        # not an Exception, so `except Exception` above never catches it, and
        # the immediate `bash -c command` process would be left running
        # un-tracked. This `finally` is the safety net that fires on every
        # exit path (normal completion, exception, or GeneratorExit) and only
        # kills the process if it is still alive — a no-op once the happy
        # path's own exit/kill handling has already run. It deliberately does
        # NOT touch any detached grandchildren (e.g. `nohup java -jar ... &`);
        # those surviving their parent's exit is the intended behaviour
        # documented above, not a leak.
        try:
            if process.returncode is None:
                process.kill()
                await process.wait()
        except Exception:  # noqa: BLE001
            pass
