"""Execute a :class:`~concord.daemon.plan.Job` as a child ``concord`` process.

The daemon drives the CLI contract (ADR 0014/0026) rather than calling
pipeline internals, so each Job is one ``sys.executable -m concord …`` spawn.
Running under ``sys.executable`` means the child uses the same interpreter and
venv that launched the daemon, regardless of whether the ``concord`` console
script is on ``PATH``. The child inherits the environment (so
``CONGRESS_API_KEY`` / ``OPENAI_API_KEY`` and the run_id-stamped logging of
ADR 0021 flow through) and the daemon's stdout/stderr.

The runner returns the child's exit code; it never raises on a non-zero exit.
The loop interprets the code (0 = success → apply any marker). Run-level detail
lives in the Scrape Run ledger the child writes, not here (ADR 0026).
"""

import logging
import subprocess
import sys

from concord.daemon.plan import Job

_log = logging.getLogger("concord.daemon.runner")


def run_job(job: Job) -> int:
    """Spawn one Job and return its exit code (0 on success).

    A non-zero exit or a failure to spawn is logged and returned as a non-zero
    code, never raised, so one failing job cannot abort the Tick (ADR 0026).
    """
    cmd = [sys.executable, "-m", "concord", *job.argv]
    _log.info("running: %s", job.description)
    _log.debug("argv: %s", " ".join(cmd))
    try:
        completed = subprocess.run(cmd, check=False)  # noqa: S603 - fixed argv, no shell
    except OSError as exc:
        _log.error("failed to spawn %r: %s", job.description, exc)
        return 1
    if completed.returncode != 0:
        _log.warning("job %r exited %d", job.description, completed.returncode)
    return completed.returncode
