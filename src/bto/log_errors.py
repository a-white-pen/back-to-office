"""Log failure locations without exposing private exception messages or data."""

import logging
import traceback


def log_failure(logger, message, *args, error, level=logging.ERROR):
    """Add error types and chained call frames to an operational log message."""
    if not logger.isEnabledFor(level):
        return
    sections = []
    seen = set()
    failure = error
    while failure is not None and id(failure) not in seen:
        seen.add(id(failure))
        lines = ["Traceback (most recent call last):"]
        # Source lines and exception text can contain private SQL or provider data.
        for frame, line in traceback.walk_tb(failure.__traceback__):
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {line}, in {code.co_name}')
        lines.append(f"{type(failure).__name__} (message omitted)")
        sections.append("\n".join(lines))
        failure = failure.__cause__ or (
            failure.__context__ if not failure.__suppress_context__ else None
        )
    logger.log(level, message + " error_type=%s\n%s", *args,
               type(error).__name__, "\nCaused by:\n".join(sections), stacklevel=2)
