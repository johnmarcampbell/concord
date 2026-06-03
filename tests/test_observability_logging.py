"""Central run_id-stamped logging — the formatter reads the run_id contextvar
and the handler install is idempotent (ADR 0021)."""

import logging

from concord.observability import (
    _HANDLER_FLAG,
    RunIdFormatter,
    _run_id,
    configure_logging,
)


def _make_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="concord.api",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="429 from %s",
        args=("/bill/119/hr",),
        exc_info=None,
    )


class TestRunIdFormatter:
    def test_stamps_active_run_id(self) -> None:
        formatter = RunIdFormatter("[%(run_id)s] %(message)s")
        token = _run_id.set("20260603T120000-deadbeef")
        try:
            out = formatter.format(_make_record())
        finally:
            _run_id.reset(token)
        assert out == "[20260603T120000-deadbeef] 429 from /bill/119/hr"

    def test_stamps_dash_outside_a_run(self) -> None:
        formatter = RunIdFormatter("[%(run_id)s] %(message)s")
        out = formatter.format(_make_record())
        assert out == "[-] 429 from /bill/119/hr"


class TestConfigureLogging:
    def _our_handlers(self) -> list[logging.Handler]:
        logger = logging.getLogger("concord")
        return [h for h in logger.handlers if getattr(h, _HANDLER_FLAG, False)]

    def test_installs_one_handler(self) -> None:
        logger = logging.getLogger("concord")
        # Tear down any handler left by a previous test/CLI invocation.
        for handler in self._our_handlers():
            logger.removeHandler(handler)
        configure_logging()
        assert len(self._our_handlers()) == 1

    def test_is_idempotent(self) -> None:
        configure_logging()
        configure_logging()
        configure_logging()
        assert len(self._our_handlers()) == 1

    def test_handler_uses_run_id_formatter(self) -> None:
        configure_logging()
        handler = self._our_handlers()[0]
        assert isinstance(handler.formatter, RunIdFormatter)
