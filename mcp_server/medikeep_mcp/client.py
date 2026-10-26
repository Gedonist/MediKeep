"""
Thin async client for the MediKeep REST API.

Logs in with username and password, keeps the JWT in memory and logs in again
once when the token expires. Every access check stays on the server.
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT = 30.0


class MediKeepError(Exception):
    """A request MediKeep refused, with a message fit to show the agent."""


@dataclass(frozen=True)
class Settings:
    url: str
    username: str
    password: str
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [
            name
            for name in ("MEDIKEEP_URL", "MEDIKEEP_USERNAME", "MEDIKEEP_PASSWORD")
            if not os.environ.get(name)
        ]
        if missing:
            raise MediKeepError(
                f"Missing environment variables: {', '.join(missing)}"
            )
        return cls(
            url=os.environ["MEDIKEEP_URL"].rstrip("/"),
            username=os.environ["MEDIKEEP_USERNAME"],
            password=os.environ["MEDIKEEP_PASSWORD"],
            verify_ssl=os.environ.get("MEDIKEEP_VERIFY_SSL", "true").lower()
            not in ("0", "false", "no"),
        )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or response.reason_phrase
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            if body.get(key):
                return str(body[key])
    return str(body)[:300]


class MediKeepClient:
    def __init__(
        self,
        settings: Settings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.settings = settings
        self._token: Optional[str] = None
        self._http = httpx.AsyncClient(
            base_url=settings.url + API_PREFIX,
            verify=settings.verify_ssl,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        response = await self._http.post(
            "/auth/login",
            data={
                "username": self.settings.username,
                "password": self.settings.password,
            },
        )
        if response.status_code != 200:
            raise MediKeepError(
                f"MediKeep login failed ({response.status_code}): "
                f"{_error_message(response)}"
            )
        body = response.json()
        if body.get("must_change_password"):
            raise MediKeepError(
                "The MediKeep account must change its password before it can be "
                "used. Sign in through the web UI once and set a new password."
            )
        self._token = body["access_token"]

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        if self._token is None:
            await self._login()

        for attempt in range(2):
            response = await self._http.request(
                method,
                path,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if response.status_code == 401 and attempt == 0:
                await self._login()
                continue
            break

        if response.status_code == 404:
            raise MediKeepError(f"Not found: {_error_message(response)}")
        if response.status_code == 403:
            raise MediKeepError(
                f"Access denied: {_error_message(response)}. The patient may not "
                "be shared with this account."
            )
        if response.status_code >= 400:
            raise MediKeepError(
                f"MediKeep returned {response.status_code}: {_error_message(response)}"
            )
        return response

    async def get_json(self, path: str, **params: Any) -> Any:
        return (await self.request("GET", path, params=params)).json()

    async def get_text(self, path: str, **params: Any) -> str:
        return (await self.request("GET", path, params=params)).text

    # Endpoint wrappers

    async def list_patients(self) -> Any:
        return await self.get_json("/patient-management/")

    async def active_patient(self) -> Any:
        return await self.get_json("/patient-management/active/current")

    async def switch_patient(self, patient_id: int) -> Any:
        response = await self.request(
            "POST", "/patient-management/switch", json={"patient_id": patient_id}
        )
        return response.json()

    async def list_conditions(
        self, patient_id: Optional[int] = None, status: Optional[str] = None
    ) -> Any:
        return await self.get_json(
            "/conditions/", patient_id=patient_id, status=status
        )

    async def condition_context(self, condition_id: int, fmt: str) -> Any:
        path = f"/conditions/{condition_id}/context"
        if fmt == "markdown":
            return await self.get_text(path, format="markdown")
        return await self.get_json(path, format="json")

    async def search(self, query: str, patient_id: Optional[int] = None) -> Any:
        return await self.get_json("/search/", q=query, patient_id=patient_id)

    async def data_summary(self) -> Any:
        return await self.get_json("/custom-reports/data-summary")
