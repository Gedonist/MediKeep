"""
API tests for GET /api/v1/conditions/{condition_id}/context.

Covers:
- Every linked section is filled from its junction table or direct column
- Records of another patient never leak into the context
- Patient background (allergies, active medications, vitals) and file metadata
- Markdown rendering
- 404 for a missing condition, 403/404 for a stranger, 200 for a view share
"""

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from app.crud.patient import patient as patient_crud
from app.models.models import (
    Allergy,
    Condition,
    ConditionMedication,
    Encounter,
    EncounterCondition,
    EntityFile,
    Injury,
    InjuryCondition,
    LabResult,
    LabResultCondition,
    Medication,
    PatientShare,
    Procedure,
    Symptom,
    SymptomCondition,
    SymptomOccurrence,
    Treatment,
    TreatmentMedication,
    Vitals,
)
from app.schemas.patient import PatientCreate
from tests.utils.user import create_random_user, create_user_token_headers


def _user_with_patient(db_session: Session, first_name: str) -> dict:
    user_data = create_random_user(db_session)
    patient = patient_crud.create_for_user(
        db_session,
        user_id=user_data["user"].id,
        patient_data=PatientCreate(
            first_name=first_name,
            last_name="Tester",
            birth_date=date(1985, 6, 15),
            gender="F",
        ),
    )
    user_data["user"].active_patient_id = patient.id
    db_session.commit()
    return {
        **user_data,
        "patient": patient,
        "headers": create_user_token_headers(user_data["user"].username),
    }


def _url(condition_id: int, fmt: str = "json") -> str:
    return f"/api/v1/conditions/{condition_id}/context?format={fmt}"


@pytest.fixture
def owner(db_session: Session) -> dict:
    return _user_with_patient(db_session, "Alice")


@pytest.fixture
def stranger(db_session: Session) -> dict:
    return _user_with_patient(db_session, "Bob")


@pytest.fixture
def linked_condition(db_session: Session, owner: dict, stranger: dict) -> dict:
    """A condition with one record of every linked type, plus one foreign link."""
    pid = owner["patient"].id
    add = db_session.add

    cond = Condition(
        patient_id=pid, diagnosis="Type 2 diabetes", status="active", severity="moderate"
    )
    add(cond)
    db_session.flush()

    med = Medication(patient_id=pid, medication_name="Metformin", status="active")
    foreign_med = Medication(
        patient_id=stranger["patient"].id, medication_name="Foreign", status="active"
    )
    lab = LabResult(patient_id=pid, test_name="HbA1c", status="completed")
    symptom = Symptom(
        patient_id=pid, symptom_name="Thirst", first_occurrence_date=date(2024, 1, 1)
    )
    injury = Injury(patient_id=pid, injury_name="Foot ulcer", body_part="Foot")
    encounter = Encounter(patient_id=pid, reason="Checkup", date=date(2024, 2, 1))
    legacy_encounter = Encounter(
        patient_id=pid, reason="Old visit", date=date(2023, 5, 1), condition_id=cond.id
    )
    treatment = Treatment(patient_id=pid, treatment_name="Diet", condition_id=cond.id)
    procedure = Procedure(
        patient_id=pid,
        procedure_name="Eye exam",
        date=date(2024, 3, 1),
        condition_id=cond.id,
    )
    for row in (med, foreign_med, lab, symptom, injury, encounter, legacy_encounter,
                treatment, procedure):
        add(row)
    db_session.flush()

    add(ConditionMedication(condition_id=cond.id, medication_id=med.id,
                            relevance_note="first line"))
    add(ConditionMedication(condition_id=cond.id, medication_id=foreign_med.id))
    add(LabResultCondition(condition_id=cond.id, lab_result_id=lab.id,
                           purpose="monitoring"))
    add(SymptomCondition(condition_id=cond.id, symptom_id=symptom.id))
    add(SymptomOccurrence(symptom_id=symptom.id, occurrence_date=date(2024, 1, 5),
                          severity="mild"))
    add(InjuryCondition(condition_id=cond.id, injury_id=injury.id))
    add(EncounterCondition(condition_id=cond.id, encounter_id=encounter.id))
    add(TreatmentMedication(treatment_id=treatment.id, medication_id=med.id))
    add(Allergy(patient_id=pid, allergen="Penicillin", reaction="Rash",
                status="active"))
    add(Vitals(patient_id=pid, recorded_date=datetime(2024, 4, 1, 9, 0),
               systolic_bp=130, diastolic_bp=85))
    add(EntityFile(entity_type="condition", entity_id=cond.id, file_name="scan.pdf",
                   file_path="/uploads/scan.pdf", file_type="application/pdf",
                   uploaded_at=datetime(2024, 4, 2)))
    db_session.commit()
    return {"condition": cond, "medication": med, "foreign_medication": foreign_med}


class TestConditionContext:
    def test_all_sections_are_filled(self, client, owner, linked_condition):
        response = client.get(_url(linked_condition["condition"].id),
                              headers=owner["headers"])
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["condition"]["diagnosis"] == "Type 2 diabetes"
        counts = body["meta"]["counts"]
        assert counts == {
            "medications": 1,
            "treatments": 1,
            "procedures": 1,
            "lab_results": 1,
            "encounters": 2,
            "symptoms": 1,
            "injuries": 1,
            "files": 1,
        }
        assert body["medications"][0]["link_relevance_note"] == "first line"
        assert body["lab_results"][0]["link_purpose"] == "monitoring"
        assert len(body["symptoms"][0]["occurrences"]) == 1
        treatment = body["treatments"][0]
        assert treatment["linked_medications"][0]["medication_name"] == "Metformin"

    def test_patient_section_has_no_identifying_details(
        self, client, owner, linked_condition
    ):
        body = client.get(_url(linked_condition["condition"].id),
                          headers=owner["headers"]).json()
        assert "first_name" not in body["patient"]
        assert "address" not in body["patient"]
        assert body["patient"]["gender"] == "F"

    def test_foreign_records_do_not_leak(self, client, owner, linked_condition):
        body = client.get(_url(linked_condition["condition"].id),
                          headers=owner["headers"]).json()
        names = [m["medication_name"] for m in body["medications"]]
        assert names == ["Metformin"]
        background_names = [
            m["medication_name"]
            for m in body["patient_background"]["active_medications"]
        ]
        assert "Foreign" not in background_names

    def test_patient_background(self, client, owner, linked_condition):
        background = client.get(_url(linked_condition["condition"].id),
                                headers=owner["headers"]).json()["patient_background"]
        assert background["active_allergies"][0]["allergen"] == "Penicillin"
        assert background["latest_vitals"][0]["systolic_bp"] == 130

    def test_markdown_format(self, client, owner, linked_condition):
        response = client.get(_url(linked_condition["condition"].id, "markdown"),
                              headers=owner["headers"])
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        text = response.text
        assert text.startswith("# Condition: Type 2 diabetes")
        assert "## Medications (1)" in text
        assert "Metformin" in text
        assert "Foreign" not in text
        assert "Penicillin" in text

    def test_invalid_format_is_rejected(self, client, owner, linked_condition):
        response = client.get(_url(linked_condition["condition"].id, "xml"),
                              headers=owner["headers"])
        assert response.status_code == 422

    def test_missing_condition_returns_404(self, client, owner):
        assert client.get(_url(999999), headers=owner["headers"]).status_code == 404

    def test_requires_authentication(self, client, linked_condition):
        assert client.get(_url(linked_condition["condition"].id)).status_code == 401

    def test_stranger_cannot_read(self, client, stranger, linked_condition):
        response = client.get(_url(linked_condition["condition"].id),
                              headers=stranger["headers"])
        assert response.status_code in (403, 404), response.text

    def test_view_share_can_read(self, client, db_session, owner, linked_condition):
        viewer = create_random_user(db_session)
        db_session.add(
            PatientShare(
                patient_id=owner["patient"].id,
                shared_by_user_id=owner["user"].id,
                shared_with_user_id=viewer["user"].id,
                permission_level="view",
                is_active=True,
            )
        )
        viewer["user"].active_patient_id = owner["patient"].id
        db_session.commit()

        response = client.get(
            _url(linked_condition["condition"].id),
            headers=create_user_token_headers(viewer["user"].username),
        )
        assert response.status_code == 200, response.text
        assert response.json()["meta"]["counts"]["medications"] == 1
