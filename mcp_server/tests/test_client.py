import httpx
import pytest

from medikeep_mcp import server
from medikeep_mcp.client import MediKeepClient, MediKeepError, Settings

SETTINGS = Settings(url="http://medikeep.test", username="agent", password="secret")


class FakeApi:
    """Records requests and answers them like the MediKeep API would."""

    def __init__(self):
        self.requests = []
        self.logins = 0
        self.expire_next = False
        self.must_change_password = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/v1/auth/login":
            self.logins += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{self.logins}",
                    "token_type": "bearer",
                    "must_change_password": self.must_change_password,
                },
            )

        if request.headers.get("Authorization") != f"Bearer token-{self.logins}":
            return httpx.Response(401, json={"message": "Invalid token"})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"message": "Token expired"})

        if path == "/api/v1/conditions/7/context":
            if request.url.params["format"] == "markdown":
                return httpx.Response(200, text="# Condition: Asthma")
            return httpx.Response(200, json={"condition": {"id": 7}})
        if path == "/api/v1/conditions/8/context":
            return httpx.Response(403, json={"message": "Access denied"})
        if path == "/api/v1/conditions/":
            return httpx.Response(
                200,
                json=[{"id": 7, "diagnosis": "Asthma", "status": "active",
                       "notes": "long text"}],
            )
        return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def client(api):
    return MediKeepClient(SETTINGS, transport=httpx.MockTransport(api))


async def test_logs_in_once_and_sends_bearer_token(client, api):
    assert await client.condition_context(7, "markdown") == "# Condition: Asthma"
    assert await client.condition_context(7, "json") == {"condition": {"id": 7}}
    assert api.logins == 1
    login = api.requests[0]
    assert login.method == "POST"
    assert b"username=agent" in login.content


async def test_logs_in_again_after_expiry(client, api):
    await client.condition_context(7, "json")
    api.expire_next = True
    assert await client.condition_context(7, "json") == {"condition": {"id": 7}}
    assert api.logins == 2


async def test_drops_unset_query_params(client, api):
    await client.list_conditions(patient_id=None, status="active")
    params = api.requests[-1].url.params
    assert params["status"] == "active"
    assert "patient_id" not in params


async def test_access_denied_is_explained(client):
    with pytest.raises(MediKeepError, match="Access denied"):
        await client.condition_context(8, "json")


async def test_must_change_password_is_refused(client, api):
    api.must_change_password = True
    with pytest.raises(MediKeepError, match="must change its password"):
        await client.list_conditions()


def test_settings_report_missing_env(monkeypatch):
    for name in ("MEDIKEEP_URL", "MEDIKEEP_USERNAME", "MEDIKEEP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MediKeepError, match="MEDIKEEP_URL"):
        Settings.from_env()


async def test_tools_return_errors_as_text(client, monkeypatch):
    monkeypatch.setattr(server, "_client", client)
    assert (await server.get_condition_context(8)).startswith("Error: Access denied")


async def test_list_conditions_tool_is_compact(client, monkeypatch):
    monkeypatch.setattr(server, "_client", client)
    text = await server.list_conditions()
    assert '"name": "Asthma"' in text
    assert "long text" not in text
