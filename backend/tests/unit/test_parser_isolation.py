"""Process-level tests for bounded and recoverable parser execution."""

from io import BytesIO
from multiprocessing.connection import Connection
from time import sleep

import pytest

from investment_research.infrastructure.parsing.isolation import (
    SUCCESS_PREFIX,
    IsolatedParseRunner,
    ParserExecutionTimeoutError,
    _parse_in_child,
)
from investment_research.infrastructure.parsing.media import MediaAwareDocumentParser
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.processing.contracts import (
    M1_PARSER_CONFIG_VERSION,
    ParsedTextDocument,
    ParserLimits,
    ParserRequestV2,
    ParserResponseV2,
)


def block_selected_body(
    connection: Connection,
    body: bytes,
    serialized_request: str,
) -> None:
    """Hang only the sentinel request and delegate all following requests normally."""

    if body == b"block":
        sleep(60)
        return
    if body == b"respond-and-block":
        request = ParserRequestV2.model_validate_json(serialized_request)
        document = MediaAwareDocumentParser().parse(
            BytesIO(body),
            limits=ParserLimits(max_pages=1, max_extracted_characters=1_000),
            media_type="text/plain",
        )
        response = ParserResponseV2(
            parser_version=request.parser_version,
            parser_config_version=request.parser_config_version,
            document=document,
        )
        connection.send_bytes(SUCCESS_PREFIX + response.model_dump_json().encode("utf-8"))
        sleep(60)
        return
    _parse_in_child(connection, body, serialized_request)


def text_request() -> ParserRequestV2:
    return ParserRequestV2(
        media_type="text/plain",
        parser_version=PypdfDocumentParser().parser_version,
        parser_config_version=M1_PARSER_CONFIG_VERSION,
        max_input_bytes=1_024,
        max_extracted_characters=1_000,
    )


def test_timeout_reaps_child_and_releases_capacity_for_next_request() -> None:
    runner = IsolatedParseRunner(
        timeout_seconds=1,
        max_concurrent_children=1,
        child_target=block_selected_body,
    )

    with pytest.raises(ParserExecutionTimeoutError):
        runner.parse(b"block", text_request())

    result = runner.parse(b"following request", text_request())

    assert isinstance(result, ParsedTextDocument)
    assert result.blocks[0].text == "following request"


def test_deadline_also_terminates_child_that_hangs_after_sending_response() -> None:
    runner = IsolatedParseRunner(
        timeout_seconds=1,
        max_concurrent_children=1,
        child_target=block_selected_body,
    )

    with pytest.raises(ParserExecutionTimeoutError):
        runner.parse(b"respond-and-block", text_request())

    result = runner.parse(b"capacity recovered", text_request())
    assert isinstance(result, ParsedTextDocument)
