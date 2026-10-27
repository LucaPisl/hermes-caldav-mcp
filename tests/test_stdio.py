import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import ElicitResult
from test_server import LITE

FIXTURE = Path(__file__).parent / "stdio_fixture_server.py"
MARCH = {"from": "2025-03-01", "to": "2025-03-31"}


def server(*args):
    return StdioServerParameters(command=sys.executable, args=[str(FIXTURE), *args])


def data(result):
    envelope = json.loads(result.content[0].text)
    assert envelope["ok"], envelope
    return envelope["data"]


async def test_real_sdk_stdio_flat_lite_tools_round_trip():
    async with Client(server(), read_timeout_seconds=5) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert list(tools) == LITE
        assert client.server_capabilities.resources is None
        assert client.server_capabilities.prompts is None
        create = tools["create_event"]
        assert create.input_schema["additionalProperties"] is False
        assert "$defs" not in create.input_schema
        assert create.annotations.read_only_hint is False
        assert create.annotations.destructive_hint is False
        assert tools["list_events"].annotations.read_only_hint is True
        listed = data(await client.call_tool("list_calendars", {}))
        assert listed["calendars"][0]["name"] == "Synthetic calendar"
        assert listed["calendars"][0]["holds"] == ["events", "tasks"]
        events = data(await client.call_tool("list_events", MARCH))["events"]
        identifier = events[0]["event_id"]
        assert "description" not in events[0]
        details = data(await client.call_tool("get_event", {"event_id": identifier}))
        assert details["revision"] == '"e1"'
        assert details["events"][0]["description"].startswith("Untrusted text")
        edited = data(
            await client.call_tool(
                "update_event",
                {"event_id": identifier, "revision": "e1", "title": "Edited"},
            )
        )
        assert edited["updated"] is True
        task = data(await client.call_tool("create_task", {"title": "Synthetic task"}))
        done = data(
            await client.call_tool("complete_task", {"task_id": task["task_id"]})
        )
        assert done["completed"] is True
        bad = await client.call_tool(
            "create_event",
            {"title": "Bad", "start": "x", "password": "synthetic-password"},
        )
        envelope = json.loads(bad.content[0].text)
        assert bad.is_error and envelope["error"]["code"] == "INVALID_INPUT"
        assert "password: unknown argument" in envelope["error"]["message"]
        assert "synthetic-password" not in bad.content[0].text


async def test_published_examples_work_as_written():
    async with Client(server("full"), read_timeout_seconds=5) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {"find_free_time", "update_task"} <= set(tools)
        assert "repeat_days" in tools["create_event"].input_schema["properties"]
        examples = tools["create_event"].input_schema["examples"]
        assert len(examples) == 3
        for example in examples:
            created = data(await client.call_tool("create_event", example))
            assert created["title"] == example["title"]
            update = dict(tools["update_event"].input_schema["examples"][0])
            update["event_id"] = created["event_id"]
            if "repeat" in example:
                update["scope"] = "series"
            data(await client.call_tool("update_event", update))
            read = data(
                await client.call_tool("get_event", {"event_id": created["event_id"]})
            )["events"][0]
            assert read["title"] == "New title"
            assert (read["start"], read["end"]) == (created["start"], created["end"])


async def test_delete_is_hidden_without_the_flag(tmp_path):
    async with Client(server(), read_timeout_seconds=5) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "delete_event" not in names and "delete_task" not in names


async def test_delete_asks_the_user_through_elicitation(tmp_path):
    asked, answers = [], ["decline", "accept"]

    async def elicit(context, params):
        asked.append(params)
        return ElicitResult(action=answers.pop(0), content={"confirm": True})

    # Hermes uses the initialize handshake, which keeps a back-channel.
    params = server("delete", str(tmp_path))
    async with Client(
        params, read_timeout_seconds=5, elicitation_callback=elicit, mode="legacy"
    ) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["delete_event"].annotations.destructive_hint is True
        assert tools["delete_event"].annotations.read_only_hint is False
        events = data(await client.call_tool("list_events", MARCH))["events"]
        args = {"event_id": events[0]["event_id"]}
        refused = await client.call_tool("delete_event", args)
        assert refused.is_error
        assert "Not deleted: the user declined" in refused.content[0].text
        deleted = data(await client.call_tool("delete_event", args))
        assert deleted["deleted"] is True
        assert "Synthetic meeting" in asked[0].message
        assert (tmp_path / "deleted" / deleted["backup"]).exists()
        assert data(await client.call_tool("list_events", MARCH))["events"] == []


async def accept(context, params):
    return ElicitResult(action="accept", content={"confirm": True})


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "legacy"},  # no elicitation capability advertised
        {"elicitation_callback": accept},  # stateless protocol: no back-channel
    ],
)
async def test_delete_without_a_way_to_ask_fails_closed(tmp_path, options):
    async with Client(
        server("delete", str(tmp_path)), read_timeout_seconds=5, **options
    ) as client:
        events = data(await client.call_tool("list_events", MARCH))["events"]
        refused = await client.call_tool(
            "delete_event", {"event_id": events[0]["event_id"]}
        )
        assert refused.is_error
        assert "Not deleted: the client cannot ask" in refused.content[0].text
    assert not (tmp_path / "deleted").exists()


@pytest.mark.parametrize("protocol", ["2025-03-26", "2025-06-18"])
async def test_legacy_hermes_handshake_and_clean_stdout(protocol):
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
                "protocolVersion": protocol,
                "capabilities": {},
                "clientInfo": {"name": "synthetic-hermes-client", "version": "1"},
            },
        }
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        response = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert response["result"]["protocolVersion"] == protocol
        assert response["result"]["capabilities"]["tools"] is not None
        assert "resources" not in response["result"]["capabilities"]
        assert "untrusted" in response["result"]["instructions"]
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        )
        await process.stdin.drain()
        listed = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert [t["name"] for t in listed["result"]["tools"]] == LITE
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)
    assert await process.stderr.read() == b""
