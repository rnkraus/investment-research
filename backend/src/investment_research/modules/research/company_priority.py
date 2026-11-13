"""Derive bounded company lookup terms from the question."""

import re
import unicodedata
from collections.abc import Set
from typing import Protocol
from uuid import UUID

from investment_research.modules.companies.domain import (
    COMPANY_NAME_MAX_LENGTH,
    COMPANY_TICKER_MAX_LENGTH,
    company_name_key,
)
from investment_research.modules.identity.domain import WorkspaceContext

_NAME_WORD = re.compile(r"(?<!\w)[\w][\w.'\u2019&-]*")
_TICKER = re.compile(r"(?<![\w.-])\$?([A-Z0-9]+(?:[.-][A-Z0-9]+)*)(?!\w)")
_MAX_NAME_WORDS = 8


def company_lookup_terms(question: str) -> tuple[set[str], set[str]]:
    """Build exact name keys and uppercase ticker candidates without loading companies."""

    question = unicodedata.normalize("NFC", question)
    names: set[str] = set()
    words = tuple(_NAME_WORD.finditer(question))
    for start, word in enumerate(words):
        end = word.start()
        for following in words[start : start + _MAX_NAME_WORDS]:
            if end != word.start() and not question[end : following.start()].isspace():
                break
            end = following.end()
            name = " ".join(question[word.start() : end].split()).rstrip(".")
            if 0 < len(name) <= COMPANY_NAME_MAX_LENGTH:
                names.add(company_name_key(name))
    tickers = {
        ticker
        for match in _TICKER.finditer(question)
        if (ticker := match[1])
        and len(ticker) <= COMPANY_TICKER_MAX_LENGTH
        and (match[0].startswith("$") or (len(ticker) >= 3 and any(c.isalpha() for c in ticker)))
    }
    return names, tickers


class CompanyPriorityReader(Protocol):
    def preferred_documents(
        self, context: WorkspaceContext, question: str, document_ids: Set[UUID]
    ) -> frozenset[UUID]:
        """Return reviewed documents for an unambiguous company mention."""
