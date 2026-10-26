"""
Condition context: one condition plus every record linked to it.

Built for consumers that need the whole picture in a single call, such as an
AI agent reached through the MediKeep MCP server. Linked records are
serialized by CustomReportService so the shape matches custom reports.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.logging.config import get_logger
from app.core.utils.datetime_utils import get_utc_now
from app.models.associations import (
    ConditionMedication,
    EncounterCondition,
    InjuryCondition,
    LabResultCondition,
    SymptomCondition,
    TreatmentEquipment,
    TreatmentMedication,
)
from app.models.clinical import Allergy, Condition, Encounter, Medication, Vitals
from app.models.files import EntityFile
from app.models.patient import Patient
from app.models.procedures import MedicalEquipment, Procedure, Treatment
from app.services.custom_report_service import CustomReportService

logger = get_logger(__name__, "app")

# Columns that only add noise for a reader of the context
OMITTED_FIELDS = {"patient_id", "created_at", "updated_at"}

LATEST_VITALS_COUNT = 3
MARKDOWN_OCCURRENCES_LIMIT = 10

# Section key -> heading used by render_markdown, in output order
LINKED_SECTIONS = {
    "medications": "Medications",
    "treatments": "Treatments",
    "procedures": "Procedures",
    "lab_results": "Lab results",
    "encounters": "Encounters",
    "symptoms": "Symptoms",
    "injuries": "Injuries",
}


def _clean(record: Dict[str, Any]) -> Dict[str, Any]:
    """Drop noise fields and empty values, also in nested records such as
    lab test components and symptom occurrences."""
    cleaned = {}
    for key, value in record.items():
        if key in OMITTED_FIELDS or value in (None, "", []):
            continue
        if isinstance(value, list) and all(isinstance(v, dict) for v in value):
            value = [_clean(v) for v in value]
        cleaned[key] = value
    return cleaned


def _age(birth_date: Optional[date], today: date) -> Optional[int]:
    if not birth_date:
        return None
    return (
        today.year
        - birth_date.year
        - ((today.month, today.day) < (birth_date.month, birth_date.day))
    )


class ConditionContextService:
    """Collects a condition and everything linked to it for one patient."""

    def __init__(self, db: Session):
        self.db = db
        self.reports = CustomReportService(db)

    async def build(self, condition: Condition) -> Dict[str, Any]:
        patient_id = condition.patient_id
        linked_ids = self._linked_ids(condition)

        sections: Dict[str, List[Dict[str, Any]]] = {}
        for category, ids in linked_ids.items():
            sections[category] = await self.reports.get_records_as_dicts(
                patient_id, category, ids
            )

        self._attach_link_notes(condition.id, sections)
        self._attach_treatment_links(patient_id, sections["treatments"])

        condition_dict = (
            await self.reports.get_records_as_dicts(
                patient_id, "conditions", [condition.id]
            )
        )[0]
        if condition.practitioner:
            condition_dict["practitioner_name"] = condition.practitioner.name

        context = {
            "condition": _clean(condition_dict),
            "patient": self._patient_brief(patient_id),
            **{key: [_clean(r) for r in records] for key, records in sections.items()},
            "files": self._files(condition.id),
            "patient_background": self._patient_background(patient_id),
        }
        context["meta"] = {
            "generated_at": get_utc_now().isoformat(),
            "counts": {key: len(context[key]) for key in [*LINKED_SECTIONS, "files"]},
        }
        return context

    def _linked_ids(self, condition: Condition) -> Dict[str, List[int]]:
        def ids(query) -> List[int]:
            return sorted({row[0] for row in query.all()})

        cid = condition.id
        db = self.db
        encounter_ids = set(
            ids(
                db.query(EncounterCondition.encounter_id).filter(
                    EncounterCondition.condition_id == cid
                )
            )
        )
        # Legacy direct column, still populated on older records
        encounter_ids.update(
            ids(db.query(Encounter.id).filter(Encounter.condition_id == cid))
        )

        return {
            "medications": ids(
                db.query(ConditionMedication.medication_id).filter(
                    ConditionMedication.condition_id == cid
                )
            ),
            "treatments": ids(
                db.query(Treatment.id).filter(Treatment.condition_id == cid)
            ),
            "procedures": ids(
                db.query(Procedure.id).filter(Procedure.condition_id == cid)
            ),
            "lab_results": ids(
                db.query(LabResultCondition.lab_result_id).filter(
                    LabResultCondition.condition_id == cid
                )
            ),
            "encounters": sorted(encounter_ids),
            "symptoms": ids(
                db.query(SymptomCondition.symptom_id).filter(
                    SymptomCondition.condition_id == cid
                )
            ),
            "injuries": ids(
                db.query(InjuryCondition.injury_id).filter(
                    InjuryCondition.condition_id == cid
                )
            ),
        }

    def _attach_link_notes(
        self, condition_id: int, sections: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        """Add the junction table's relevance_note/purpose to each linked record."""
        links = {
            "medications": (ConditionMedication, "medication_id"),
            "lab_results": (LabResultCondition, "lab_result_id"),
            "symptoms": (SymptomCondition, "symptom_id"),
            "injuries": (InjuryCondition, "injury_id"),
            "encounters": (EncounterCondition, "encounter_id"),
        }
        for category, (model, fk) in links.items():
            rows = (
                self.db.query(model).filter(model.condition_id == condition_id).all()
            )
            by_record = {getattr(row, fk): row for row in rows}
            for record in sections[category]:
                row = by_record.get(record["id"])
                if row is None:
                    continue
                if row.relevance_note:
                    record["link_relevance_note"] = row.relevance_note
                purpose = getattr(row, "purpose", None)
                if purpose:
                    record["link_purpose"] = purpose

    def _attach_treatment_links(
        self, patient_id: int, treatments: List[Dict[str, Any]]
    ) -> None:
        """Second hop: the medications and equipment used by each treatment."""
        treatment_ids = [t["id"] for t in treatments]
        if not treatment_ids:
            return

        medications: Dict[int, List[Dict[str, Any]]] = {}
        for link, med in (
            self.db.query(TreatmentMedication, Medication)
            .join(Medication, Medication.id == TreatmentMedication.medication_id)
            .filter(TreatmentMedication.treatment_id.in_(treatment_ids))
            .filter(Medication.patient_id == patient_id)
            .all()
        ):
            medications.setdefault(link.treatment_id, []).append(
                _clean(
                    {
                        "medication_id": med.id,
                        "medication_name": med.medication_name,
                        "dosage": med.dosage,
                        "status": med.status,
                        "relevance_note": link.relevance_note,
                    }
                )
            )

        equipment: Dict[int, List[Dict[str, Any]]] = {}
        for link, item in (
            self.db.query(TreatmentEquipment, MedicalEquipment)
            .join(MedicalEquipment, MedicalEquipment.id == TreatmentEquipment.equipment_id)
            .filter(TreatmentEquipment.treatment_id.in_(treatment_ids))
            .filter(MedicalEquipment.patient_id == patient_id)
            .all()
        ):
            equipment.setdefault(link.treatment_id, []).append(
                _clean(
                    {
                        "equipment_id": item.id,
                        "equipment_name": item.equipment_name,
                        "relevance_note": link.relevance_note,
                    }
                )
            )

        for treatment in treatments:
            if treatment["id"] in medications:
                treatment["linked_medications"] = medications[treatment["id"]]
            if treatment["id"] in equipment:
                treatment["linked_equipment"] = equipment[treatment["id"]]

    def _patient_brief(self, patient_id: int) -> Dict[str, Any]:
        """Clinically relevant demographics only: no name, address or contacts."""
        patient = self.db.query(Patient).filter(Patient.id == patient_id).first()
        if not patient:
            return {}
        return _clean(
            {
                "age": _age(patient.birth_date, date.today()),
                "birth_date": patient.birth_date,
                "gender": patient.gender,
                "blood_type": patient.blood_type,
                "height_in": patient.height,
                "weight_lbs": patient.weight,
            }
        )

    def _files(self, condition_id: int) -> List[Dict[str, Any]]:
        files = (
            self.db.query(EntityFile)
            .filter(EntityFile.entity_type == "condition")
            .filter(EntityFile.entity_id == condition_id)
            .order_by(EntityFile.uploaded_at.desc())
            .all()
        )
        return [
            _clean(
                {
                    "id": f.id,
                    "file_name": f.file_name,
                    "file_type": f.file_type,
                    "category": f.category,
                    "description": f.description,
                    "uploaded_at": f.uploaded_at,
                }
            )
            for f in files
        ]

    def _patient_background(self, patient_id: int) -> Dict[str, Any]:
        """Patient-wide facts a consultant needs even though they aren't linked."""
        allergies = (
            self.db.query(Allergy)
            .filter(Allergy.patient_id == patient_id)
            .filter(Allergy.status == "active")
            .all()
        )
        active_medications = (
            self.db.query(Medication)
            .filter(Medication.patient_id == patient_id)
            .filter(Medication.status == "active")
            .order_by(Medication.medication_name)
            .all()
        )
        vitals = (
            self.db.query(Vitals)
            .filter(Vitals.patient_id == patient_id)
            .order_by(Vitals.recorded_date.desc())
            .limit(LATEST_VITALS_COUNT)
            .all()
        )
        to_dict = self.reports._model_to_dict
        return {
            "active_allergies": [
                _clean(
                    {
                        "allergen": a.allergen,
                        "reaction": a.reaction,
                        "severity": a.severity,
                    }
                )
                for a in allergies
            ],
            "active_medications": [
                _clean(
                    {
                        "id": m.id,
                        "medication_name": m.medication_name,
                        "dosage": m.dosage,
                        "frequency": m.frequency,
                    }
                )
                for m in active_medications
            ],
            "latest_vitals": [_clean(to_dict(v)) for v in vitals],
        }


def _format_value(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(_format_value(v) for v in value)
    # Multi-line notes would break out of their list item
    return " ".join(str(value).split())


def _fields_line(record: Dict[str, Any], skip: set) -> str:
    """One line of key: value pairs; nested records are rendered separately."""
    return "; ".join(
        f"{key}: {_format_value(value)}"
        for key, value in record.items()
        if key not in skip
        and (key == "tags" or not isinstance(value, (dict, list)))
    )


def render_markdown(context: Dict[str, Any]) -> str:
    """Compact markdown rendering of a condition context for an LLM reader."""
    condition = context["condition"]
    title = condition.get("condition_name") or condition.get("diagnosis")
    lines = [f"# Condition: {title}", "", _fields_line(condition, {"id"}), ""]

    if context["patient"]:
        lines += ["## Patient", _fields_line(context["patient"], set()), ""]

    for key, heading in LINKED_SECTIONS.items():
        records = context[key]
        lines.append(f"## {heading} ({len(records)})")
        if not records:
            lines.append("_none linked_")
        for record in records:
            lines.append(f"- {_fields_line(record, set())}")
            for component in record.get("test_components", []):
                lines.append(f"  - {_fields_line(component, {'id', 'lab_result_id'})}")
            occurrences = sorted(
                record.get("occurrences", []),
                key=lambda o: str(o.get("occurrence_date", "")),
                reverse=True,
            )
            for occurrence in occurrences[:MARKDOWN_OCCURRENCES_LIMIT]:
                lines.append(
                    f"  - occurrence: {_fields_line(occurrence, {'id', 'symptom_id'})}"
                )
            for nested in ("linked_medications", "linked_equipment"):
                for item in record.get(nested, []):
                    lines.append(f"  - {nested}: {_fields_line(item, set())}")
        lines.append("")

    if context["files"]:
        lines.append(f"## Files ({len(context['files'])})")
        lines += [f"- {_fields_line(f, {'id'})}" for f in context["files"]]
        lines.append("")

    background = context["patient_background"]
    lines.append("## Patient background (not linked to this condition)")
    for key, label in (
        ("active_allergies", "Active allergies"),
        ("active_medications", "All active medications"),
        ("latest_vitals", "Latest vitals"),
    ):
        lines.append(f"### {label}")
        items = background[key]
        lines += [f"- {_fields_line(item, {'id'})}" for item in items] or ["_none_"]
    lines += ["", f"_Generated at {context['meta']['generated_at']}_"]
    return "\n".join(lines)
