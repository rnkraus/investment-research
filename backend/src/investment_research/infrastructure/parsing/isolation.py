"""Terminable child-process boundary for one untrusted parser input."""

from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from multiprocessing import get_context
from multiprocessing.connection import Connection
from threading import BoundedSemaphore
from time import monotonic
from typing import Final

from pydantic import ValidationError

from investment_research.infrastructure.parsing.media import MediaAwareDocumentParser
from investment_research.modules.processing.contracts import (
    ParsedDocumentV2,
    ParserFailureCode,
    ParserFailureV2,
    ParserLimits,
    ParserRequestV2,
    ParserResponseV2,
)
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
    ParserProtocolError,
    ParserTransportError,
)

SUCCESS_PREFIX: Final = b"S"
FAILURE_PREFIX: Final = b"F"
type ParseChildTarget = Callable[[Connection, bytes, str], None]


class ParserExecutionTimeoutError(ParserTransportError):
    """Report that a child exceeded its server-owned wall-clock deadline."""


class ParserCapacityError(ParserTransportError):
    """Report that all bounded parser child slots are currently occupied."""


@dataclass(slots=True)
class IsolatedParseRunner:
    """Run each parse in a fresh spawn child with bounded time and capacity."""

    timeout_seconds: int
    max_concurrent_children: int
    child_target: ParseChildTarget = field(
        default_factory=lambda: _parse_in_child,
        repr=False,
    )
    _capacity: BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("parser child timeout must be positive")
        if self.max_concurrent_children < 1:
            raise ValueError("parser child capacity must be positive")
        self._capacity = BoundedSemaphore(self.max_concurrent_children)

    def parse(self, body: bytes, request: ParserRequestV2) -> ParsedDocumentV2:
        """Return validated child output or one safe application-owned error."""

        if not self._capacity.acquire(blocking=False):
            raise ParserCapacityError("isolated parser capacity is exhausted")

        context = get_context("spawn")
        receive_connection, send_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=self.child_target,
            args=(send_connection, body, request.model_dump_json()),
            daemon=True,
        )
        try:
            process.start()
            deadline = monotonic() + self.timeout_seconds
            send_connection.close()
            if not receive_connection.poll(max(0.0, deadline - monotonic())):
                _terminate_and_reap(process)
                raise ParserExecutionTimeoutError("isolated parser deadline exceeded")

            try:
                payload = receive_connection.recv_bytes()
            except EOFError as error:
                raise ParserTransportError("isolated parser exited without a result") from error
            process.join(timeout=max(0.0, deadline - monotonic()))
            if process.is_alive():
                _terminate_and_reap(process)
                raise ParserExecutionTimeoutError("isolated parser deadline exceeded")
            if process.exitcode != 0:
                raise ParserTransportError("isolated parser child failed")
            return _decode_child_result(payload)
        finally:
            if process.is_alive():
                _terminate_and_reap(process)
            receive_connection.close()
            send_connection.close()
            process.close()
            self._capacity.release()


def _parse_in_child(
    connection: Connection,
    body: bytes,
    serialized_request: str,
) -> None:
    """Parse one body without returning input text or implementation details on failure."""

    try:
        request = ParserRequestV2.model_validate_json(serialized_request)
        parser = MediaAwareDocumentParser()
        document = parser.parse(
            BytesIO(body),
            limits=ParserLimits(
                max_pages=request.max_pdf_pages or 1,
                max_extracted_characters=request.max_extracted_characters,
            ),
            media_type=request.media_type,
        )
        response = ParserResponseV2(
            parser_version=request.parser_version,
            parser_config_version=request.parser_config_version,
            document=document,
        )
        connection.send_bytes(SUCCESS_PREFIX + response.model_dump_json().encode("utf-8"))
    except ParserLimitExceededError:
        _send_failure(connection, "limit_exceeded")
    except DocumentParserError:
        _send_failure(connection, "parse_failed")
    except Exception:
        _send_failure(connection, "internal_error")
    finally:
        connection.close()


def _send_failure(connection: Connection, code: ParserFailureCode) -> None:
    """Serialize one bounded failure category across the private process pipe."""

    failure = ParserFailureV2(code=code)
    connection.send_bytes(FAILURE_PREFIX + failure.model_dump_json().encode("utf-8"))


def _decode_child_result(payload: bytes) -> ParsedDocumentV2:
    """Validate the complete child message before trusting its parsed content."""

    prefix, body = payload[:1], payload[1:]
    if prefix == SUCCESS_PREFIX:
        try:
            return ParserResponseV2.model_validate_json(body).document
        except ValidationError:
            raise ParserProtocolError("isolated parser child returned invalid output") from None
    if prefix == FAILURE_PREFIX:
        try:
            failure = ParserFailureV2.model_validate_json(body)
        except ValidationError:
            raise ParserProtocolError("isolated parser child returned invalid failure") from None
        if failure.code == "limit_exceeded":
            raise ParserLimitExceededError("isolated parser safety limit exceeded")
        if failure.code == "parse_failed":
            raise DocumentParserError("isolated parser rejected the document")
        raise ParserTransportError("isolated parser child failed internally")
    raise ParserProtocolError("isolated parser child returned an unknown message")


def _terminate_and_reap(process: object) -> None:
    """Terminate and synchronously reap one multiprocessing child."""

    if not hasattr(process, "terminate") or not hasattr(process, "join"):
        raise TypeError("parser process does not support termination")
    process.terminate()
    process.join()
