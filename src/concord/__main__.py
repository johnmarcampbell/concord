"""Enable ``python -m concord`` as an entry point.

Mirrors the ``concord`` console script (``concord.cli:main``). The daemon
(ADR 0026) spawns the CLI via ``sys.executable -m concord …`` so it runs
under whatever interpreter/venv launched the daemon, independent of whether
the ``concord`` script is on ``PATH``.
"""

from concord.cli import main

if __name__ == "__main__":  # pragma: no cover - entry point shim
    main()
