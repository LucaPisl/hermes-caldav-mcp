import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

FIXTURE = Path(__file__).parent / "stdio_fixture_server.py"


async def test_real_sdk_stdio_discovers_only_five_typed_tools():
    params = StdioServerParameters(command=sys.executable, args=[str(FIXTURE)])
    async with Client(params, read_timeout_seconds=5) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "list_calendars",
            "list_events",
            "get_event",
            "create_event",
            "update_event",
        }
        assert client.server_capabilities.resources is None
        assert client.server_capabilities.prompts is None
        create = next(t for t in tools if t.name == "create_event")
        assert create.input_schema["additionalProperties"] is False
        assert create.input_schema["$defs"]["Fields"]["additionalProperties"] is False
        assert create.annotations.read_only_hint is False
        listed = await client.call_tool("list_calendars", {})
        envelope = json.loads(listed.content[0].text)
        assert envelope["data"][0]["calendar_id"] == "calendar-1"
        events = await client.call_tool(
            "list_events",
            {
                "calendar_id": "calendar-1",
                "start": "2025-03-01T00:00:00Z",
                "end": "2025-04-01T00:00:00Z",
            },
        )
        envelope = json.loads(events.content[0].text)
        identifier = envelope["data"][0]["event_id"]
        assert "description" not in envelope["data"][0]
        details = await client.call_tool(
            "get_event", {"calendar_id": "calendar-1", "event_id": identifier}
        )
        envelope = json.loads(details.content[0].text)
        assert envelope["data"]["revision"] == '"old"'
        assert envelope["data"]["events"][0]["description"].startswith("Untrusted text")
        created = await client.call_tool(
            "create_event",
            {
                "calendar_id": "calendar-1",
                "fields": {
                    "title": "Created",
                    "start": "2025-03-29",
                    "end": "2025-03-30",
                    "all_day": True,
                },
            },
        )
        assert json.loads(created.content[0].text)["data"]["created"]
        edited = await client.call_tool(
            "update_event",
            {
                "calendar_id": "calendar-1",
                "event_id": identifier,
                "expected_revision": '"old"',
                "patch": {"title": "Edited"},
            },
        )
        assert json.loads(edited.content[0].text)["data"]["updated"]
        bad = await client.call_tool(
            "create_event",
            {
                "calendar_id": "calendar-1",
                "fields": {
                    "title": "Bad",
                    "start": "x",
                    "end": "x",
                    "password": "synthetic-password",
                },
            },
        )
        assert json.loads(bad.content[0].text)["error"]["code"] == "INVALID_INPUT"
        assert "synthetic-password" not in bad.content[0].text


@pytest.mark.parametrize(
    "timezone,all_day,start,end",
    [
        (
            "Europe/Berlin",
            False,
            "2025-07-01T10:00:00+02:00",
            "2025-07-01T10:15:00+02:00",
        ),
        ("UTC", False, "2025-07-01T08:00:00+00:00", "2025-07-01T08:15:00+00:00"),
        (None, True, "2025-07-01", "2025-07-02"),
    ],
)
async def test_published_examples_create_then_edit_without_changing_times(
    timezone, all_day, start, end
):
    params = StdioServerParameters(command=sys.executable, args=[str(FIXTURE)])
    async with Client(params, read_timeout_seconds=5) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        examples = tools["create_event"].input_schema.get("examples", [])
        example = next(
            (
                e
                for e in examples
                if e["fields"].get("all_day", False) == all_day
                and e["fields"].get("timezone") == timezone
            ),
            None,
        )
        assert example is not None, "Discovery must supply usable creation examples"
        args = copy.deepcopy(example)
        args["calendar_id"] = "calendar-1"
        created = await client.call_tool("create_event", args)
        result = json.loads(created.content[0].text)
        assert result["ok"], result
        identifier = result["data"]["event_id"]
        read_args = {"calendar_id": "calendar-1", "event_id": identifier}
        read = await client.call_tool("get_event", read_args)
        original = json.loads(read.content[0].text)["data"]
        assert original["events"][0]["title"] == "Example event"
        assert original["events"][0]["start"] == start
        assert original["events"][0]["end"] == end
        assert original["events"][0]["all_day"] is all_day

        edits = tools["update_event"].input_schema.get("examples", [])
        assert edits, "Discovery must supply a revision-based title-edit example"
        args = copy.deepcopy(edits[0])
        args.update(read_args, expected_revision=original["revision"])
        updated = await client.call_tool("update_event", args)
        result = json.loads(updated.content[0].text)
        assert result["ok"], result
        read = await client.call_tool("get_event", read_args)
        edited = json.loads(read.content[0].text)["data"]["events"][0]
        assert edited["title"] == "Updated example"
        assert edited["start"] == start
        assert edited["end"] == end
        assert edited["all_day"] is all_day


async def test_legacy_hermes_handshake_and_clean_stdout():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FIXTURE),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-hermes-client", "version": "1"},
            },
        }
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        response = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert response["result"]["protocolVersion"] == "2025-03-26"
        assert response["result"]["capabilities"]["tools"] is not None
        assert "resources" not in response["result"]["capabilities"]
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        )
        await process.stdin.drain()
        listed = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert len(listed["result"]["tools"]) == 5
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)
    assert await process.stderr.read() == b""
