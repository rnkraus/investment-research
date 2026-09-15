"""Shared constants for the private worker-to-parser HTTP transport."""

from typing import Final

PARSER_HTTP_PATH: Final = "/internal/v1/parse"
PARSER_REQUEST_CONTENT_TYPE: Final = "application/pdf"
PARSER_RESPONSE_CONTENT_TYPE: Final = "application/json"
PARSER_REQUEST_METADATA_HEADER: Final = "X-Investment-Research-Parser-Metadata"
MAX_PARSER_REQUEST_METADATA_BYTES: Final = 1_024
