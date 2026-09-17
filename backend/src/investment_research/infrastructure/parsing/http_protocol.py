"""Shared constants for the private worker-to-parser HTTP transport."""

from typing import Final

PARSER_HEALTH_PATH: Final = "/internal/health"
PARSER_HTTP_PATH: Final = "/internal/v1/parse"
PARSER_V2_HTTP_PATH: Final = "/internal/v2/parse"
PARSER_REQUEST_CONTENT_TYPE: Final = "application/pdf"
PARSER_TEXT_REQUEST_CONTENT_TYPE: Final = "text/plain"
PARSER_V2_REQUEST_CONTENT_TYPES: Final = frozenset(
    {PARSER_REQUEST_CONTENT_TYPE, PARSER_TEXT_REQUEST_CONTENT_TYPE}
)
PARSER_RESPONSE_CONTENT_TYPE: Final = "application/json"
PARSER_REQUEST_METADATA_HEADER: Final = "X-Investment-Research-Parser-Metadata"
MAX_PARSER_REQUEST_METADATA_BYTES: Final = 1_024
