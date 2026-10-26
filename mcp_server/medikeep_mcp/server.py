"""
MediKeep MCP server (stdio).

Tools are read-only, apart from select_patient, which only changes the agent
account's own active patient. Configuration comes from the environment:
MEDIKEEP_URL, MEDIKEEP_USERNAME, MEDIKEEP_PASSWORD, MEDIKEEP_VERIFY_SSL.
"""

import json
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from medikeep_mcp.client import MediKeepClient, MediKeepError, Settings

mcp = FastMCP("medikeep")

_client: Optional[MediKeepClient] = None


def get_client() -> MediKeepClient:
    global _client
    if _client is None:
        _client = MediKeepClient(Settings.from_env())
    return _client


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def _patient_line(patient: dict) -> dict:
    """The minimum needed to pick a patient: no address or contacts."""
    return {
        "id": patient["id"],
        "name": f"{patient['first_name']} {patient['last_name']}",
        "birth_date": patient.get("birth_date"),
        "relationship_to_self": patient.get("relationship_to_self"),
        "permission_level": patient.get("permission_level"),
    }


async def _call(coro) -> str:
    """Run a tool body; a MediKeep refusal becomes a tool error (isError)."""
    try:
        return await coro
    except MediKeepError as error:
        raise ToolError(str(error)) from error


@mcp.tool()
async def list_patients() -> str:
    """List the patients this account can read, and which one is active.

    Most other tools work on the active patient; change it with select_patient.
    """

    async def run() -> str:
        client = get_client()
        patients = await client.list_patients()
        active = await client.active_patient()
        return _dump(
            {
                "active_patient_id": active["id"] if active else None,
                "patients": [_patient_line(p) for p in patients["patients"]],
            }
        )

    return await _call(run())


@mcp.tool()
async def select_patient(patient_id: int) -> str:
    """Make a patient the active one for this account.

    get_patient_overview and other patient-wide tools then read that patient.
    """

    async def run() -> str:
        patient = await get_client().switch_patient(patient_id)
        return _dump({"active_patient": _patient_line(patient)})

    return await _call(run())


@mcp.tool()
async def list_conditions(
    patient_id: Optional[int] = None, status: Optional[str] = None
) -> str:
    """List a patient's conditions (diagnoses) with their ids.

    Args:
        patient_id: Patient to read; defaults to the active patient.
        status: Optional filter such as "active", "resolved" or "chronic".
    """

    async def run() -> str:
        conditions = await get_client().list_conditions(patient_id, status)
        return _dump(
            [
                {
                    "id": c["id"],
                    "name": c.get("condition_name") or c.get("diagnosis"),
                    "diagnosis": c.get("diagnosis"),
                    "status": c.get("status"),
                    "severity": c.get("severity"),
                    "onset_date": c.get("onset_date"),
                    "icd10_code": c.get("icd10_code"),
                }
                for c in conditions
            ]
        )

    return await _call(run())


@mcp.tool()
async def get_condition_context(
    condition_id: int, format: Literal["markdown", "json"] = "markdown"
) -> str:
    """Get one condition with everything linked to it, in a single response.

    Includes linked medications, treatments (with their medications and
    equipment), procedures, lab results with values, encounters, symptoms with
    occurrences, injuries, attached file names, plus patient background:
    active allergies, all active medications and the latest vitals.

    Args:
        condition_id: Id from list_conditions.
        format: "markdown" (compact, default) or "json" (full structure).
    """

    async def run() -> str:
        result = await get_client().condition_context(condition_id, format)
        return result if isinstance(result, str) else _dump(result)

    return await _call(run())


@mcp.tool()
async def get_patient_overview() -> str:
    """Overview of all records of the active patient, grouped by category.

    Each record has a title, date, status and a short key_info line. Use it to
    see what exists before asking for details.
    """

    async def run() -> str:
        return _dump(await get_client().data_summary())

    return await _call(run())


@mcp.tool()
async def search_records(query: str, patient_id: Optional[int] = None) -> str:
    """Full-text search across all record types of a patient.

    Args:
        query: Text to look for, for example a drug or test name.
        patient_id: Patient to search; defaults to the active patient.
    """

    async def run() -> str:
        return _dump(await get_client().search(query, patient_id))

    return await _call(run())


@mcp.prompt()
def consult_on_condition(condition_id: int) -> str:
    """Ask for a second-opinion style review of one condition."""
    return f"""You are an AI medical consultant helping a patient prepare for a
conversation with their doctor. You are not a doctor and do not replace one.

1. Call get_condition_context with condition_id={condition_id}.
2. Base every statement only on that data. Never invent values, dates or
   diagnoses. When something you would need is missing, say so.
3. Answer in the user's language with these sections:
   - Summary: the condition and its course so far.
   - Noteworthy: trends in lab values and symptoms, possible interactions
     between the linked medications and all active medications, conflicts with
     allergies, gaps in monitoring.
   - Missing data: what is not recorded but would matter.
   - Questions for the doctor.
   - Tests or checks worth discussing.
4. Cite the record type and date for each finding.
5. End with: "This is not medical advice. Discuss any decision with your doctor."
"""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
