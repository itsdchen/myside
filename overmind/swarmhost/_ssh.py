"""Strict SSH/rsync wrappers for the remote-sim toolchain.

Why this module exists rather than reusing `trade_manager.ssh_run`:
    `trade_manager.ssh_run` returns `[]` both when the command succeeds with
    no output and when it fails — we lose the success/failure signal. The
    dispatcher and bootstrap-adjacent code need that signal to write
    accurate failure manifests, so this module raises on non-zero exits.

ControlMaster reuse pattern mirrors `trade_manager._ssh_opts` so a single
socket per host is shared.
"""

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Default retry budget for transport-level failures (not app-level non-zero
# exits). Each call gets DEFAULT_RETRIES additional attempts beyond the
# first. Backoff is exponential with this base (seconds).
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_S = 1.0

# Exit codes we treat as transport failures worth retrying.
# - ssh: 255 is "ssh itself failed" (connection refused, timeout, unreachable host).
#   Any other non-zero is the remote command's exit code; we don't retry those.
# - rsync: 10/11/12 are I/O / protocol errors, 23 is partial transfer due to
#   error, 30 is read timeout, 35 is connection timeout. None of these are
#   "the data is wrong"; all are "network/disk hiccupped."
_RETRYABLE_SSH_CODES = {255}
_RETRYABLE_RSYNC_CODES = {10, 11, 12, 23, 30, 35}


def _run_with_retry(argv, *, timeout, kind, retries=DEFAULT_RETRIES,
                    backoff=DEFAULT_BACKOFF_S):
    """subprocess.run with retries on transport-level failures.

    Only retries on:
      - subprocess.TimeoutExpired
      - ssh exit 255 (ssh-layer failure)
      - rsync transport-error codes

    Does NOT retry app-level non-zero exits (e.g. pktrade returning 1) —
    those are deterministic failures the caller should see.

    Returns CompletedProcess (caller inspects returncode). Raises
    TimeoutExpired only if every attempt times out.
    """
    retryable_codes = _RETRYABLE_SSH_CODES if kind == "ssh" else _RETRYABLE_RSYNC_CODES
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt >= retries:
                raise
            time.sleep(backoff * (2 ** attempt))
            continue
        if proc.returncode == 0 or proc.returncode not in retryable_codes or attempt >= retries:
            return proc
        time.sleep(backoff * (2 ** attempt))
    return proc  # pragma: no cover (unreachable)


def _ssh_opts(alias: str) -> list:
    """Per-host ControlMaster opts — first call handshakes, subsequent calls reuse.

    Same socket pattern as trade_manager.py so we share the connection with
    any in-process consumer of that module.
    """
    ctl = f"/tmp/trademan-ssh-ctl-{alias}.sock"
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={ctl}",
        "-o", "ControlPersist=60s",
    ]


class SshError(RuntimeError):
    """Raised when an SSH/rsync invocation returns non-zero.

    Carries the original cmd, returncode, stdout, and stderr so callers can
    embed the failing cmd in a manifest for local reproduction.
    """

    def __init__(self, alias: str, cmd: str, returncode: int, stdout: str, stderr: str):
        self.alias = alias
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"ssh {alias!r}: exit {returncode} for cmd: {cmd!r}\n"
            f"stderr: {stderr.strip()}"
        )


@dataclass
class SshResult:
    """Return value of run() — distinguishes 'empty stdout' from 'no output'."""
    returncode: int
    stdout: str
    stderr: str


def run(alias: str, cmd: str, timeout: float = 60.0, check: bool = True) -> SshResult:
    """Run `cmd` on the remote host via SSH.

    Args:
        alias: ssh config alias to connect to.
        cmd: shell command string to execute on remote (passed as a single arg).
        timeout: seconds to wait for completion before raising.
        check: if True (default), raise SshError on non-zero exit. Set False
            when the caller wants to inspect failure without an exception
            (e.g. probing for state).

    Returns: SshResult with returncode, stdout, stderr.

    Raises:
        SshError: when check=True and the command exits non-zero, or any
            ssh-layer failure (timeout, connection refused, etc.).
    """
    argv = ["ssh"] + _ssh_opts(alias) + [alias, cmd]
    try:
        proc = _run_with_retry(argv, timeout=timeout, kind="ssh")
    except subprocess.TimeoutExpired as e:
        raise SshError(alias, cmd, -1, "", f"timed out after {timeout}s (all retries)") from e
    result = SshResult(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise SshError(alias, cmd, proc.returncode, proc.stdout, proc.stderr)
    return result


def rsync_push(alias: str, local_path: Path, remote_path: str, timeout: float = 600.0) -> None:
    """Rsync a file or directory from local to remote alias.

    Uses --inplace --partial so resumed transfers don't waste bandwidth, and
    --archive so perms/timestamps survive. Trailing slash on `local_path`
    matters for rsync — caller controls.

    Args:
        alias: ssh config alias to push to.
        local_path: source on the local machine.
        remote_path: destination path on the remote (absolute or relative to $HOME).
        timeout: seconds to wait for transfer before raising.

    Raises:
        SshError: on non-zero rsync exit.
    """
    local_str = str(local_path)
    ssh_cmd = "ssh " + " ".join(shlex.quote(x) for x in _ssh_opts(alias))
    argv = [
        "rsync",
        "--archive",
        "--inplace",
        "--partial",
        "--compress",
        "-e", ssh_cmd,
        local_str,
        f"{alias}:{remote_path}",
    ]
    try:
        proc = _run_with_retry(argv, timeout=timeout, kind="rsync")
    except subprocess.TimeoutExpired as e:
        raise SshError(alias, " ".join(argv), -1, "", f"timed out after {timeout}s (all retries)") from e
    if proc.returncode != 0:
        raise SshError(alias, " ".join(argv), proc.returncode, proc.stdout, proc.stderr)


def rsync_pull(alias: str, remote_path: str, local_path: Path, timeout: float = 600.0) -> None:
    """Rsync a file or directory from remote alias to local.

    Mirror of rsync_push. Same flags. Trailing slash semantics same as rsync.
    """
    local_str = str(local_path)
    ssh_cmd = "ssh " + " ".join(shlex.quote(x) for x in _ssh_opts(alias))
    argv = [
        "rsync",
        "--archive",
        "--inplace",
        "--partial",
        "--compress",
        "-e", ssh_cmd,
        f"{alias}:{remote_path}",
        local_str,
    ]
    try:
        proc = _run_with_retry(argv, timeout=timeout, kind="rsync")
    except subprocess.TimeoutExpired as e:
        raise SshError(alias, " ".join(argv), -1, "", f"timed out after {timeout}s (all retries)") from e
    if proc.returncode != 0:
        raise SshError(alias, " ".join(argv), proc.returncode, proc.stdout, proc.stderr)
