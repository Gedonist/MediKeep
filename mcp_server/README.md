# MediKeep MCP server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that
lets an AI agent (Claude Code, Cursor, Gemini CLI, a local model…) read MediKeep
records through the REST API. The agent gets a full summary of a condition in
one call, without exploring the code or holding server secrets.

## Tools

| Tool | What it returns |
|---|---|
| `list_patients` | Patients the account can read, and the active one |
| `select_patient(patient_id)` | Makes a patient active (only changes the agent account) |
| `list_conditions(patient_id?, status?)` | Conditions with ids |
| `get_condition_context(condition_id, format)` | The condition with every linked record, plus allergies, active medications and latest vitals |
| `get_patient_overview` | All records of the active patient by category |
| `search_records(query, patient_id?)` | Full-text search |

Prompt: `consult_on_condition(condition_id)` asks for a structured second-opinion
review that sticks to the recorded data.

## Recommended account setup

Do not give the agent your own login. Instead:

1. Create a separate MediKeep user, for example `ai-agent`.
2. From the patient's page, share the patient with `ai-agent` with **view**
   permission and an expiry date.
3. To cut access, revoke the share or deactivate the user.

A view share cannot change records, so the agent stays read-only even if a
tool were misused.

## Install and connect

Requires Python 3.10+.

```bash
pip install ./mcp_server            # or: uvx --from ./mcp_server medikeep-mcp
```

Claude Code:

```bash
claude mcp add medikeep \
  -e MEDIKEEP_URL=https://medikeep.example.com \
  -e MEDIKEEP_USERNAME=ai-agent \
  -e MEDIKEEP_PASSWORD=... \
  -- medikeep-mcp
```

Other clients take the same command and environment in their MCP config:

```json
{
  "mcpServers": {
    "medikeep": {
      "command": "medikeep-mcp",
      "env": {
        "MEDIKEEP_URL": "https://medikeep.example.com",
        "MEDIKEEP_USERNAME": "ai-agent",
        "MEDIKEEP_PASSWORD": "..."
      }
    }
  }
}
```

Set `MEDIKEEP_VERIFY_SSL=false` only for a local instance with a self-signed
certificate.

The server honours `HTTP_PROXY`/`HTTPS_PROXY`. If those are set and MediKeep
runs locally, add its host to `NO_PROXY` (for example
`"NO_PROXY": "127.0.0.1,localhost"` in `env`), or requests go through the proxy
and fail with 502.

## Privacy

Whatever the tools return is sent to the model provider your agent uses. For
records that must not leave your network, use a locally hosted model. Patient
names are returned only by `list_patients`; condition contexts carry age,
gender, blood type, height and weight but no name, address or contacts.

## Limitations

- Password login is refused when the instance runs with `SSO_ONLY_MODE`.
- The account must not be waiting for a forced password change.

## Development

```bash
pip install -e "./mcp_server[test]"
cd mcp_server && pytest
npx @modelcontextprotocol/inspector medikeep-mcp   # try the tools by hand
```
