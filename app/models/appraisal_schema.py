from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class FacilityRecord(BaseModel):
    """One row of a borrower's or guarantor's existing CRIB facility history."""

    agreement_no: Optional[str] = None
    date_of_grant: Optional[str] = None
    facility_amount: Optional[float] = None
    period: Optional[int] = None
    product_type: Optional[str] = None
    facility_status: Optional[str] = None
    matured_rentals: Optional[int] = None
    paid_rentals: Optional[int] = None
    capital_outstanding: Optional[float] = None
    vehicle_no: Optional[str] = None
    asset_description: Optional[str] = None
    make_model: Optional[str] = None
    yom: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class CreditAppraisalData(BaseModel):
    """
    Vendor-facing credit appraisal request body (the JSON string carried in the
    multipart "data" field of POST /public/generate-credit-appraisal).

    Bank statement PDFs are uploaded alongside as repeated multipart "file" parts;
    `file_banks` maps each uploaded PDF's original filename to which bank issued it.
    """

    # --- Submission / routing ---
    callback_url: Optional[str] = Field(
        None, description="Client webhook to notify with the finished appraisal (async delivery)"
    )
    bank: Optional[str] = None
    file_banks: dict[str, str] = Field(default_factory=dict)

    # --- Loan / date info ---
    loan_number: str = Field(..., description="Loan Number e.g. LN/2026/009872")
    date: Optional[str] = None
    branch: Optional[str] = None
    hob: Optional[str] = None
    marketing_officer: Optional[str] = None

    # --- Applicant (character) ---
    applicant_name: Optional[str] = None
    age: Optional[int] = None
    address: Optional[str] = None
    marital_status: Optional[str] = None
    spouse_name: Optional[str] = None
    spouse_details: Optional[str] = None
    education: Optional[str] = None
    family_details: Optional[str] = None
    business: Optional[str] = None
    experience_years: Optional[int] = None
    residence_inspection_by: Optional[str] = None
    residence_inspection_date: Optional[str] = None

    # --- Facility terms ---
    product_name: Optional[str] = None
    scheme_name: Optional[str] = None
    facility_amount: Optional[float] = None
    tenor: Optional[str] = None
    rate: Optional[str] = None
    gross_rental: Optional[float] = None
    proposed_rental: Optional[float] = None
    purpose_of_facility: Optional[str] = None
    cumulative_exposure: Optional[float] = None
    purpose_sector: Optional[str] = None
    customer_sector: Optional[str] = None
    customer_sub_sector: Optional[str] = None

    # --- Applicant CRIB ---
    crib_score: Optional[str] = None
    crib_rating: Optional[str] = None
    crib_pd_percentage: Optional[str] = None
    internal_crib_score: Optional[float] = None
    crib_active_facilities_count: Optional[int] = None
    crib_active_facilities_total: Optional[float] = None
    crib_settled_facilities_count: Optional[int] = None
    crib_settled_facilities_total: Optional[float] = None
    crib_arrears: Optional[str] = None
    crib_cheque_returns: Optional[str] = None
    crib_recent_inquiries: Optional[int] = None
    borrower_facilities: List[FacilityRecord] = Field(default_factory=list)

    # --- MSME / capacity ---
    msme_classification: Optional[str] = None
    employee_count: Optional[int] = None
    annual_turnover: Optional[float] = None

    # --- Collateral / asset ---
    asset_type: Optional[str] = None
    vehicle: Optional[str] = None
    vehicle_number: Optional[str] = None
    registration_status: Optional[str] = None
    year_of_manufacture: Optional[str] = None
    fuel_type: Optional[str] = None
    engine_capacity: Optional[str] = None
    transmission: Optional[str] = None
    market_value: Optional[float] = None
    expected_market_value: Optional[float] = None
    valuer: Optional[str] = None
    ownership_type: Optional[str] = None
    valuation_remarks: Optional[str] = None
    supplier_name: Optional[str] = None
    supplier_contact: Optional[str] = None
    asset_inspection_by: Optional[str] = None
    asset_inspection_date: Optional[str] = None

    # --- Guarantor ---
    guarantor_name: Optional[str] = None
    guarantor_age: Optional[int] = None
    guarantor_address: Optional[str] = None
    guarantor_marital_status: Optional[str] = None
    guarantor_relationship: Optional[str] = None
    guarantor_contact_date: Optional[str] = None
    guarantor_contact_time: Optional[str] = None
    guarantor_employment: Optional[str] = None
    guarantor_employer: Optional[str] = None
    guarantor_experience_years: Optional[int] = None
    guarantor_internal_crib_score: Optional[float] = None
    guarantor_crib_score: Optional[str] = None
    guarantor_crib_rating: Optional[str] = None
    guarantor_pd_percentage: Optional[str] = None
    guarantor_active_facilities_count: Optional[int] = None
    guarantor_active_facilities_total: Optional[float] = None
    guarantor_settled_facilities_count: Optional[int] = None
    guarantor_settled_facilities_total: Optional[float] = None
    guarantor_arrears: Optional[str] = None
    guarantor_cheque_returns: Optional[str] = None
    guarantor_recent_inquiries: Optional[int] = None
    guarantor_facilities: List[FacilityRecord] = Field(default_factory=list)

    # --- Capacity / capital ---
    source_of_income: Optional[str] = None
    total_asset_value: Optional[float] = None
    monthly_commitments: Optional[float] = None
    industry_margin_percent: Optional[float] = None
    bank_statement_summary: Optional[str] = None
    outlier_details: Optional[str] = None
    customer_contribution: Optional[float] = None
    ltv: Optional[float] = None

    # --- ESMS / policy / AI decision ---
    esms_risk: Optional[str] = None
    esms_closure_statement: Optional[str] = None
    policy_exceptions: Optional[str] = None
    policy_exception_approval: Optional[str] = None
    ai_credit_decision: Optional[str] = None
    ai_credit_score: Optional[float] = None

    # --- Contact / misc notes ---
    customer_contact_date: Optional[str] = None
    customer_contact_time: Optional[str] = None
    contact_details: Optional[str] = None
    additional_pledge_note: Optional[str] = None
    covering_approval_note: Optional[str] = None
    customer_data_modification: Optional[str] = None
    settlement_note: Optional[str] = None
    auto_outlier_handling: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


__all__ = ["FacilityRecord", "CreditAppraisalData"]
