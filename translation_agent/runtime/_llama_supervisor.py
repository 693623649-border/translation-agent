"""Supervisor that ties one llama-server child to its parent worker's lifetime.

Run as ``python -I _llama_supervisor.py --control-fd N -- BINARY ARGS...``.
Standard library only, because it runs with ``-I`` outside the package path.

Data flow:
* The parent holds the heavy-model ``flock`` and passes that descriptor to
  this process and to llama-server (``pass_fds``), so the lock stays held
  while any of them is alive.
* The parent keeps the write end of a pipe whose read end is ``control-fd``.
  A normal stop closes it; a crashed or killed parent closes it implicitly.
  Either way this process sees EOF, terminates llama-server, waits for it, and
  exits.  The model is therefore never left resident without an owner.
* The first stdout line reports the llama-server PID to the parent.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys

TERMINATE_GRACE_SECONDS = 10.0


def _stop(child: subprocess.Popen) -> int:
    if child.poll() is None:
        child.terminate()
        try:
            return child.wait(TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
    return child.wait()


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[0] != "--control-fd" or argv[2] != "--":
        print("usage: _llama_supervisor.py --control-fd N -- BINARY ARGS...", file=sys.stderr)
        return 2
    control_fd = int(argv[1])
    command = argv[3:]
    inherited = tuple(
        int(value) for value in os.environ.pop("TA_SUPERVISOR_PASS_FDS", "").split(",") if value
    )
    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, request_stop)

    # stdout is the PID channel to the parent; server output goes to stderr.
    child = subprocess.Popen(command, pass_fds=inherited, stdin=subprocess.DEVNULL, stdout=2)
    sys.stdout.write(f"{child.pid}\n")
    sys.stdout.flush()
    while not stop_requested:
        if child.poll() is not None:
            return child.returncode
        try:
            readable, _, _ = select.select([control_fd], [], [], 0.25)
        except InterruptedError:
            continue
        if readable and not os.read(control_fd, 64):
            break  # EOF: the parent stopped us or died.
    return _stop(child)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
