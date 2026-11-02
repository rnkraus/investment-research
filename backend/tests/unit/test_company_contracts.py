"""Verify bounded company identity before persistence or HTTP activation."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.companies.contracts import CompanySummary, CreateCompanyCommand
from investment_research.modules.companies.domain import (
    COMPANY_NAME_KEY_MAX_LENGTH,
    COMPANY_NAME_MAX_LENGTH,
    Company,
    company_name_key,
    normalize_company_name,
)

COMPANY_ID = UUID("10000000-0000-4000-8000-000000000010")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 11, 2, 12, tzinfo=UTC)


def company_payload() -> dict[str, object]:
    """Return synthetic server-owned identity and provenance."""

    return {
        "id": COMPANY_ID,
        "workspace_id": WORKSPACE_ID,
        "name": "Northstar Grid Analytics",
        "created_by": USER_ID,
        "created_at": CREATED_AT,
    }


@pytest.mark.parametrize(
    ("submitted", "display", "key"),
    [
        ("  Northstar   Grid  ", "Northstar Grid", "northstar grid"),
        ("Northstar\u00a0\u2003Grid", "Northstar Grid", "northstar grid"),
        ("Cafe\u0301 Research", "Café Research", "café research"),
        ("Straße AG", "Straße AG", "strasse ag"),
        ("A&B Holdings, Ltd.", "A&B Holdings, Ltd.", "a&b holdings, ltd."),
        ("\u0390" * 200, "\u0390" * 200, "\u03b9\u0308\u0301" * 200),
    ],
)
def test_company_name_has_stable_display_and_comparison_forms(
    submitted: str, display: str, key: str
) -> None:
    command = CreateCompanyCommand(name=submitted)

    assert command.name == display
    assert company_name_key(command.name) == key
    assert normalize_company_name(display) == display
    assert len(key) <= COMPANY_NAME_KEY_MAX_LENGTH


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        123,
        1.5,
        b"Northstar",
        [],
        {},
        "",
        " \u00a0\u2003 ",
        "N" * (COMPANY_NAME_MAX_LENGTH + 1),
        "Northstar\x00Grid",
        "Northstar\tGrid",
        "Northstar\nGrid",
        "Northstar\rGrid",
        "Northstar\x7fGrid",
        "Northstar\u200bGrid",
        "Northstar\u202eGrid",
        "Northstar\ud800Grid",
    ],
)
def test_company_name_rejects_invalid_or_hidden_input(value: object) -> None:
    with pytest.raises(ValidationError):
        CreateCompanyCommand.model_validate({"name": value})


def test_name_length_is_checked_after_display_normalization() -> None:
    name = "N" * COMPANY_NAME_MAX_LENGTH

    assert CreateCompanyCommand(name=f"  {name}  ").name == name
    assert len(company_name_key("\u0390" * COMPANY_NAME_MAX_LENGTH)) == COMPANY_NAME_KEY_MAX_LENGTH


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Northstar Grid", "  NORTHSTAR   GRID "),
        ("Cafe\u0301 Research", "CAFÉ Research"),
        ("Straße AG", "STRASSE AG"),
    ],
)
def test_equivalent_names_have_the_same_comparison_key(first: str, second: str) -> None:
    assert company_name_key(first) == company_name_key(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [("Café Research", "Cafe Research"), ("A-B", "AB"), ("A&B", "A and B")],
)
def test_name_keys_do_not_guess_company_equivalence(first: str, second: str) -> None:
    assert company_name_key(first) != company_name_key(second)


@pytest.mark.parametrize("field", ["workspace_id", "created_by", "id", "normalized_name"])
def test_create_command_does_not_accept_server_owned_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CreateCompanyCommand.model_validate({"name": "Northstar Grid", field: str(COMPANY_ID)})


def test_company_records_explicit_scope_actor_and_utc_creation_time() -> None:
    payload = company_payload()
    payload["name"] = " Northstar   Grid "
    payload["created_at"] = datetime.fromisoformat("2026-11-02T14:00:00+02:00")

    company = Company.model_validate(payload)

    assert company.workspace_id == WORKSPACE_ID
    assert company.created_by == USER_ID
    assert company.created_at == CREATED_AT
    assert company.created_at.tzinfo is UTC
    assert company.name == "Northstar Grid"
    assert company.normalized_name == "northstar grid"
    assert "normalized_name" not in company.model_dump()


@pytest.mark.parametrize("field", ["workspace_id", "created_by", "created_at"])
def test_company_requires_provenance(field: str) -> None:
    payload = company_payload()
    del payload[field]

    with pytest.raises(ValidationError, match="Field required"):
        Company.model_validate(payload)


def test_company_does_not_accept_a_forged_comparison_key() -> None:
    payload = company_payload()
    payload["normalized_name"] = "different entity"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Company.model_validate(payload)


def test_summary_exposes_only_company_identity_name_and_creation_time() -> None:
    summary = CompanySummary(
        company_id=COMPANY_ID,
        name=" Northstar   Grid ",
        created_at=datetime.fromisoformat("2026-11-02T14:00:00+02:00"),
    )

    assert summary.model_dump(mode="json") == {
        "company_id": str(COMPANY_ID),
        "name": "Northstar Grid",
        "created_at": "2026-11-02T12:00:00Z",
    }
    assert summary.created_at.tzinfo is UTC


@pytest.mark.parametrize("field", ["workspace_id", "created_by", "normalized_name"])
def test_summary_rejects_internal_fields(field: str) -> None:
    payload: dict[str, object] = {
        "company_id": COMPANY_ID,
        "name": "Northstar Grid",
        "created_at": CREATED_AT,
        field: "untrusted",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CompanySummary.model_validate(payload)


@pytest.mark.parametrize("summary", [False, True])
def test_creation_times_must_be_timezone_aware(summary: bool) -> None:
    naive_time = datetime(2026, 11, 2, 12)
    if summary:
        with pytest.raises(ValidationError, match="timezone info"):
            CompanySummary(company_id=COMPANY_ID, name="Northstar", created_at=naive_time)
    else:
        payload = company_payload()
        payload["created_at"] = naive_time
        with pytest.raises(ValidationError, match="timezone info"):
            Company.model_validate(payload)


def test_company_models_are_frozen() -> None:
    models: tuple[Company | CreateCompanyCommand | CompanySummary, ...] = (
        Company.model_validate(company_payload()),
        CreateCompanyCommand(name="Northstar Grid"),
        CompanySummary(company_id=COMPANY_ID, name="Northstar Grid", created_at=CREATED_AT),
    )
    for model in models:
        with pytest.raises(ValidationError, match="frozen"):
            model.name = "Changed"


def test_company_validation_errors_do_not_render_submitted_name() -> None:
    submitted = "untrusted-company-marker\x00"

    with pytest.raises(ValidationError) as error:
        CreateCompanyCommand(name=submitted)

    assert "untrusted-company-marker" not in str(error.value)
