import pytest
from pydantic import ValidationError

from app.models.appraisal_schema import CreditAppraisalData, FacilityRecord


def test_loan_number_is_required():
    with pytest.raises(ValidationError):
        CreditAppraisalData.model_validate({})


def test_minimal_valid_payload():
    data = CreditAppraisalData.model_validate({"loan_number": "LN-1"})
    assert data.loan_number == "LN-1"
    assert data.file_banks == {}
    assert data.borrower_facilities == []


def test_extra_fields_are_allowed():
    data = CreditAppraisalData.model_validate({"loan_number": "LN-1", "some_future_field": "x"})
    assert data.model_dump()["some_future_field"] == "x"


def test_file_banks_and_bank_field():
    data = CreditAppraisalData.model_validate(
        {
            "loan_number": "LN-1",
            "bank": "hnb",
            "file_banks": {"statement_1.pdf": "hnb", "statement_2.pdf": "auto"},
        }
    )
    assert data.bank == "hnb"
    assert data.file_banks == {"statement_1.pdf": "hnb", "statement_2.pdf": "auto"}


def test_facility_record_parses_nested_list():
    data = CreditAppraisalData.model_validate(
        {
            "loan_number": "LN-1",
            "borrower_facilities": [
                {
                    "agreement_no": "123",
                    "facility_amount": 1000.0,
                    "period": 12,
                }
            ],
        }
    )
    assert len(data.borrower_facilities) == 1
    assert isinstance(data.borrower_facilities[0], FacilityRecord)
    assert data.borrower_facilities[0].agreement_no == "123"


def test_exclude_none_drops_unset_optionals():
    data = CreditAppraisalData.model_validate({"loan_number": "LN-1"})
    dumped = data.model_dump(mode="json", exclude_none=True)
    assert "applicant_name" not in dumped
    assert dumped["loan_number"] == "LN-1"
