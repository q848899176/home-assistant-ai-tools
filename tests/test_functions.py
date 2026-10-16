"""Functions compatibility, HA execution and trust-boundary regressions."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiohttp import web
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ha_ai_tools import functions, tools


def definition(name="sample", function=None, parameters=None):
    return {
        "spec": {"name": name, "parameters": parameters or {"type": "object", "properties": {}}},
        "function": function or {"type": "template", "value_template": "ok"},
    }


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    await hass.config.async_set_time_zone("Asia/Shanghai")
    user = SimpleNamespace(
        is_admin=True,
        name="测试管理员",
        permissions=SimpleNamespace(check_entity=lambda entity, policy: True),
    )

    async def get_user(user_id):
        return user if user_id == "actor" else None

    hass.auth = SimpleNamespace(async_get_user=get_user)
    hass.states.async_set("light.public", "off")
    hass.states.async_set("light.private", "off")
    monkeypatch.setattr(
        tools, "exposed_entities", lambda hass: {"light.public": hass.states.get("light.public")}
    )
    manager = SimpleNamespace(hass=hass, function_tasks=set())
    actor = SimpleNamespace(context=Context(user_id="actor"), device_id=None)
    yield manager, actor, user
    for task in tuple(manager.function_tasks):
        task.cancel()
    await asyncio.gather(*manager.function_tasks, return_exceptions=True)
    await hass.async_block_till_done()


def test_compile_yaml_and_validate_arguments():
    raw = """
- spec:
    name: multiply
    parameters:
      type: object
      properties:
        count: {type: integer, minimum: 1}
      required: [count]
      additionalProperties: false
  function:
    type: template
    value_template: '{{ count * 2 }}'
"""
    compiled = functions.compile_functions(raw)
    assert compiled[0]["function"]["type"] == "template"
    functions.validate_call(compiled[0]["spec"], {"count": 2})
    for arguments in ({}, {"count": 0}, {"count": True}, {"count": 1, "extra": 3}):
        with pytest.raises(ValueError):
            functions.validate_call(compiled[0]["spec"], arguments)


@pytest.mark.parametrize(
    "raw",
    [
        [definition(), definition()],
        [definition("bad name")],
        [definition(function={"type": "native", "name": "unknown"})],
        [definition(function={"type": "rest", "resource_template": "{{ url }}"})],
        [definition(function={"type": "sqlite", "db_url": "{{ file }}"})],
        [
            definition(
                parameters={"type": "object", "properties": {"is_exposed": {"type": "string"}}}
            )
        ],
        [definition(function={"type": "script", "sequence": "bad"})],
    ],
)
def test_compile_rejects_ambiguous_or_unsafe_configuration(raw):
    with pytest.raises(ValueError):
        functions.compile_functions(raw)


def test_compile_detects_reserved_names_and_cycles():
    with pytest.raises(ValueError, match="重名|保留"):
        functions.compile_functions([definition("remember")], reserved=["remember"])
    allowed = definition("execute_services", {"type": "native", "name": "execute_service"})
    assert functions.compile_functions([allowed], reserved=["execute_services"])
    cycle = {"type": "composite", "sequence": []}
    cycle["sequence"].append(cycle)
    with pytest.raises(ValueError):
        functions.compile_functions([definition(function=cycle)])


async def test_template_and_composite_preserve_response_variables(runtime):
    manager, actor, _ = runtime
    executor = functions.FunctionExecutor(manager)
    config = {
        "type": "composite",
        "sequence": [
            {
                "type": "template",
                "value_template": "{{ count * 2 }}",
                "parse_result": True,
                "response_variable": "first",
            },
            {"type": "template", "value_template": "{{ first + 1 }}", "parse_result": True},
        ],
    }
    assert await executor.async_execute(config, {"count": 3}, actor) == 7
    with pytest.raises(ValueError, match="保留|参数"):
        await executor.async_execute(
            {"type": "template", "value_template": "{{ is_exposed('light.private') }}"},
            {"is_exposed": "override"},
            actor,
        )


async def test_native_service_checks_targets_permissions_and_returns_response(runtime):
    manager, actor, user = runtime
    calls = []

    async def turn_on(call):
        calls.append(call)
        return {"actual": call.data["entity_id"]}

    manager.hass.services.async_register(
        "light", "turn_on", turn_on, supports_response=SupportsResponse.OPTIONAL
    )
    execute = functions.FunctionExecutor(manager).async_execute
    config = {"type": "native", "name": "execute_service_single"}
    result = await execute(
        config,
        {"domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.public"}},
        actor,
    )
    assert result["success"] is True
    assert result["response"] == {"actual": ["light.public"]}
    assert calls[0].context.user_id == "actor"
    with pytest.raises(ValueError, match="公开"):
        await execute(
            config,
            {
                "domain": "light",
                "service": "turn_on",
                "service_data": {"entity_id": "light.private"},
            },
            actor,
        )
    user.permissions.check_entity = lambda entity, policy: False
    with pytest.raises(ValueError, match="权限"):
        await execute(
            config, {"domain": "light", "service": "turn_on", "entity_id": "light.public"}, actor
        )
    assert len(calls) == 1


@pytest.mark.parametrize("target_key", ["area_id", "device_id"])
async def test_native_indirect_targets_cannot_include_private_entities(runtime, target_key):
    manager, actor, _ = runtime
    hass = manager.hass
    await ar.async_load(hass)
    dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    areas, devices, entities = ar.async_get(hass), dr.async_get(hass), er.async_get(hass)
    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda entry_id: SimpleNamespace(
            entry_id="test", domain="test", title="Test device", disabled_by=None, subentries={}
        )
    )
    area = areas.async_create("测试房间")
    device = devices.async_get_or_create(config_entry_id="test", identifiers={("test", "device")})
    devices.async_update_device(device.id, area_id=area.id)
    for name in ("public", "private"):
        entities.async_get_or_create(
            "light", "test", name, suggested_object_id=name, device_id=device.id
        )
    hass.services.async_register("light", "turn_on", lambda call: None)
    target = area.id if target_key == "area_id" else device.id
    with pytest.raises(ValueError, match="公开"):
        await functions.FunctionExecutor(manager).async_execute(
            {"type": "native", "name": "execute_service_single"},
            {"domain": "light", "service": "turn_on", "service_data": {target_key: target}},
            actor,
        )


async def test_native_batch_preflight_prevents_partial_execution(runtime):
    manager, actor, _ = runtime
    calls = []
    manager.hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    with pytest.raises(ValueError):
        await functions.FunctionExecutor(manager).async_execute(
            {"type": "native", "name": "execute_service"},
            {
                "list": [
                    {"domain": "light", "service": "turn_on", "entity_id": "light.public"},
                    {"domain": "light", "service": "turn_on", "entity_id": "light.private"},
                ]
            },
            actor,
        )
    assert calls == []


async def test_real_homeassistant_script_returns_function_result(runtime):
    manager, actor, _ = runtime
    from homeassistant.helpers.script import async_validate_actions_config

    sequence = await async_validate_actions_config(
        manager.hass,
        [
            {"variables": {"_function_result": "{{ amount * 4 }}"}},
        ],
    )
    result = await functions.FunctionExecutor(manager).async_execute(
        {"type": "script", "sequence": sequence},
        {"amount": 3},
        actor,
    )
    assert result == 12


async def test_script_service_preflight_blocks_hidden_target(runtime):
    manager, actor, _ = runtime
    manager.hass.services.async_register("light", "turn_on", lambda call: None)
    with pytest.raises(ValueError, match="公开"):
        await functions.FunctionExecutor(manager).async_execute(
            {
                "type": "script",
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "{{ target }}"}}],
            },
            {"target": "light.private"},
            actor,
        )


async def test_native_current_user_and_automation_delegate_real_status(runtime, monkeypatch):
    manager, actor, _ = runtime
    executor = functions.FunctionExecutor(manager)
    assert await executor.async_execute(
        {"type": "native", "name": "get_user_from_user_id"}, {"user_id": "someone_else"}, actor
    ) == {"name": "测试管理员"}

    async def create(manager_arg, text, user_arg, request_id):
        assert manager_arg is manager and user_arg is actor and request_id == "turn-1"
        assert text == "alias: test"
        return {"id": "one", "saved": True, "loaded": False}

    monkeypatch.setattr(tools, "create_automation", create)
    result = await executor.async_execute(
        {"type": "native", "name": "add_automation"},
        {"automation_config": "alias: test"},
        actor,
        "turn-1",
    )
    assert result == {"id": "one", "saved": True, "loaded": False}


async def test_rest_scrape_with_real_http_server(runtime):
    manager, actor, _ = runtime
    seen = []

    async def handle(request):
        seen.append((request.method, request.headers.get("X-Test"), await request.text()))
        if request.path == "/html":
            return web.Response(
                text='<b data-num="2">first</b><b data-num="7">second</b>', content_type="text/html"
            )
        return web.json_response({"result": request.query.get("name")})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    execute = functions.FunctionExecutor(manager).async_execute
    try:
        result = await execute(
            {
                "type": "rest",
                "resource_template": base + "/api?name={{ name | urlencode }}",
                "method": "POST",
                "headers": {"X-Test": "configured"},
                "payload_template": "value={{ name }}",
                "value_template": "{{ value_json.result }}",
            },
            {"name": "hello"},
            actor,
        )
        assert result == "hello"
        assert seen == [("POST", "configured", "value=hello")]
        result = await execute(
            {
                "type": "scrape",
                "resource": base + "/html",
                "sensor": [
                    {
                        "name": "number",
                        "select": "b",
                        "index": 1,
                        "attribute": "data-num",
                        "value_template": "{{ value | int + 1 }}",
                    }
                ],
                "value_template": "{{ number | int * 2 }}",
            },
            {},
            actor,
        )
        assert result == "16"
        with pytest.raises(ValueError, match="范围|地址|主机"):
            await execute(
                {"type": "rest", "resource": base + "/api", "resource_template": "{{ url }}"},
                {"url": "http://unconfigured.invalid"},
                actor,
            )
    finally:
        await runner.cleanup()


async def test_sqlite_read_only_results_helpers_and_fixed_path(runtime, tmp_path):
    manager, actor, _ = runtime
    path = tmp_path / "test.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings(value INTEGER)")
        db.executemany("INSERT INTO readings VALUES (?)", [(2,), (3,)])
    execute = functions.FunctionExecutor(manager).async_execute
    config = {
        "type": "sqlite",
        "db_url": str(path),
        "query": "SELECT value FROM readings ORDER BY value",
    }
    assert await execute(config, {}, actor) == [{"value": 2}, {"value": 3}]
    assert await execute(dict(config, single=True), {}, actor) == {"value": 2}
    assert (
        await execute(
            dict(config, query="SELECT value FROM readings WHERE value=9", single=True), {}, actor
        )
        is None
    )
    assert await execute(
        dict(config, query="SELECT {{ 1 if is_exposed('light.public') else 0 }} AS visible"),
        {},
        actor,
    ) == [{"visible": 1}]
    for query in (
        "DELETE FROM readings",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA writable_schema=ON",
        "SELECT load_extension('x')",
        "SELECT * FROM sqlite_master",
    ):
        with pytest.raises(ValueError):
            await execute(dict(config, query=query), {}, actor)
    with pytest.raises(ValueError, match="路径|范围"):
        await execute(dict(config, db_url="/etc/hosts"), {}, actor)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM readings").fetchone()[0] == 2


async def test_recorder_sql_filters_hidden_entities_even_if_query_omits_where(runtime, tmp_path):
    manager, actor, _ = runtime
    path = tmp_path / "home-assistant_v2.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE states_meta(metadata_id INTEGER, entity_id TEXT)")
        db.execute(
            "CREATE TABLE states(state_id INTEGER, metadata_id INTEGER, state TEXT, attributes_id INTEGER)"
        )
        db.executemany(
            "INSERT INTO states_meta VALUES (?, ?)", [(1, "light.public"), (2, "light.private")]
        )
        db.executemany(
            "INSERT INTO states VALUES (?, ?, ?, ?)", [(1, 1, "on", 1), (2, 2, "SECRET", 2)]
        )
    execute = functions.FunctionExecutor(manager).async_execute
    assert await execute({"type": "sqlite", "query": "SELECT state FROM states"}, {}, actor) == [
        {"state": "on"}
    ]
    with pytest.raises(ValueError):
        await execute({"type": "sqlite", "query": "SELECT state FROM main.states"}, {}, actor)


async def test_delay_schedules_and_cancellation_propagates(runtime):
    manager, actor, _ = runtime
    executor = functions.FunctionExecutor(manager)
    result = await executor.async_execute(
        {"type": "template", "value_template": "done"}, {"delay": 60}, actor
    )
    assert result["scheduled"] is True and len(manager.function_tasks) == 1
    task = next(iter(manager.function_tasks))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert not manager.function_tasks


def test_compile_script_can_validate_without_a_live_hass():
    config = definition(
        function={
            "type": "script",
            "sequence": [{"variables": {"_function_result": "{{ amount * 4 }}"}}],
        }
    )
    assert functions.compile_functions([config])[0]["function"]["type"] == "script"


async def test_script_response_can_feed_later_variables(runtime):
    manager, actor, _ = runtime

    async def response(call):
        return {"brightness": 42}

    manager.hass.services.async_register(
        "light", "turn_on", response, supports_response=SupportsResponse.ONLY
    )
    result = await functions.FunctionExecutor(manager).async_execute(
        {
            "type": "script",
            "sequence": [
                {
                    "action": "light.turn_on",
                    "target": {"entity_id": "light.public"},
                    "response_variable": "reply",
                },
                {"variables": {"_function_result": "{{ reply.brightness }}"}},
            ],
        },
        {},
        actor,
    )
    assert result == 42


async def test_template_cannot_read_unexposed_state_via_model_entity(runtime):
    manager, actor, _ = runtime
    with pytest.raises(ValueError, match="公开|权限"):
        await functions.FunctionExecutor(manager).async_execute(
            {"type": "template", "value_template": "{{ states(entity) }}"},
            {"entity": "light.private"},
            actor,
        )


async def test_recorder_native_operations_check_entities_and_use_beijing(runtime, monkeypatch):
    from homeassistant.components import energy, recorder
    from homeassistant.components.recorder import history, statistics
    from homeassistant.core import State

    manager, actor, user = runtime
    seen = []

    class Recorder:
        async def async_add_executor_job(self, operation, *args):
            return await asyncio.to_thread(operation, *args)

    def history_result(hass, start, end, ids, *flags):
        seen.append(("history", start, end, ids, flags))
        return {"light.public": [State("light.public", "on")]}

    def statistics_result(hass, start, end, ids, period, units, types):
        seen.append(("statistics", start, end, ids, period, units, types))
        return {"light.public": [{"change": 3, "start": datetime(2026, 10, 16, 4, tzinfo=UTC)}]}

    async def energy_manager(hass):
        return SimpleNamespace(data={"device_consumption": [{"stat_consumption": "light.public"}]})

    monkeypatch.setattr(recorder, "get_instance", lambda hass: Recorder())
    monkeypatch.setattr(history, "get_significant_states", history_result)
    monkeypatch.setattr(statistics, "statistics_during_period", statistics_result)
    monkeypatch.setattr(energy, "async_get_manager", energy_manager)
    execute = functions.FunctionExecutor(manager).async_execute
    config = {"type": "native", "name": "get_history"}
    args = {
        "entity_ids": ["light.public"],
        "start_time": "2026-10-16T12:00:00",
        "end_time": "2026-10-16T13:00:00",
    }
    result = await execute(config, args, actor)
    assert result[0][0]["state"] == "on"
    assert result[0][0]["last_changed"].endswith("+08:00")
    assert seen[0][1].hour == 12 and str(seen[0][1].tzinfo) == "Asia/Shanghai"
    with pytest.raises(ValueError, match="公开"):
        await execute(config, dict(args, entity_ids=["light.private"]), actor)
    stats = await execute(
        {"type": "native", "name": "get_statistics"},
        {
            "statistic_ids": ["light.public"],
            "start_time": "2026-10-16T12:00:00",
            "end_time": "2026-10-16T13:00:00",
            "types": ["change"],
        },
        actor,
    )
    assert stats == {"light.public": [{"change": 3, "start": "2026-10-16T12:00:00+08:00"}]}
    with pytest.raises(ValueError, match="公开"):
        await execute(
            {"type": "native", "name": "get_statistics"},
            {"statistic_ids": ["light.private"]},
            actor,
        )
    assert await execute({"type": "native", "name": "get_energy"}, {}, actor) == {
        "device_consumption": [{"stat_consumption": "light.public"}]
    }
    user.permissions.check_entity = lambda entity, policy: False
    with pytest.raises(ValueError, match="公开|权限"):
        await execute({"type": "native", "name": "get_energy"}, {}, actor)


async def test_sqlite_bounds_and_non_admin_custom_database(runtime, tmp_path):
    manager, actor, user = runtime
    path = tmp_path / "bounded.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE readings(value INTEGER)")
        db.executemany("INSERT INTO readings VALUES (?)", [(1,), (2,), (3,)])
    execute = functions.FunctionExecutor(manager).async_execute
    config = {
        "type": "sqlite",
        "db_url": str(path),
        "query": "SELECT * FROM readings",
        "max_rows": 2,
    }
    with pytest.raises(ValueError, match="行数"):
        await execute(config, {}, actor)
    user.is_admin = False
    with pytest.raises(ValueError, match="管理员"):
        await execute(dict(config, max_rows=5), {}, actor)
    user.is_admin = True
    with pytest.raises(ValueError, match="超时"):
        await execute(
            dict(
                config,
                query="WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n",
                timeout=0.01,
            ),
            {},
            actor,
        )


async def test_add_automation_emits_compatible_event_with_real_saved_loaded(runtime, monkeypatch):
    manager, actor, _ = runtime
    events = []
    manager.hass.bus.async_listen(
        "automation_registered_via_ha_ai_tools", lambda event: events.append(event.data)
    )

    async def create(*args):
        return {"id": "created", "saved": True, "loaded": False}

    monkeypatch.setattr(tools, "create_automation", create)
    await functions.FunctionExecutor(manager).async_execute(
        {"type": "native", "name": "add_automation"},
        {"automation_config": "alias: 示例\nactions: []"},
        actor,
    )
    await manager.hass.async_block_till_done()
    assert events[0]["automation_config"]["id"] == "created"
    assert events[0]["saved"] is True and events[0]["loaded"] is False
    assert "示例" in events[0]["raw_config"]


async def test_script_read_service_response_and_hidden_condition(runtime):
    manager, actor, _ = runtime
    manager.hass.states.async_set("weather.public", "sunny")
    original = tools.exposed_entities
    # Keep actual HA State objects; only the exposure registry boundary is controlled.
    manager.hass.services.async_register(
        "weather",
        "get_forecasts",
        lambda call: {"forecast": [23]},
        supports_response=SupportsResponse.ONLY,
    )
    from unittest.mock import patch

    with patch.object(
        tools,
        "exposed_entities",
        lambda hass: dict(original(hass), **{"weather.public": hass.states.get("weather.public")}),
    ):
        result = await functions.FunctionExecutor(manager).async_execute(
            {
                "type": "script",
                "sequence": [
                    {
                        "action": "weather.get_forecasts",
                        "target": {"entity_id": "weather.public"},
                        "data": {"type": "daily"},
                        "response_variable": "forecast",
                    },
                    {"variables": {"_function_result": "{{ forecast.forecast }}"}},
                ],
            },
            {},
            actor,
        )
        assert result == [23]
    with pytest.raises(ValueError, match="公开"):
        await functions.FunctionExecutor(manager).async_execute(
            {
                "type": "script",
                "sequence": [
                    {"condition": "state", "entity_id": "light.private", "state": "off"},
                    {"variables": {"_function_result": True}},
                ],
            },
            {},
            actor,
        )


async def test_http_timeout_auth_params_xml_and_redirect_scope(runtime):
    manager, actor, _ = runtime
    seen = []

    async def handler(request):
        seen.append((request.query.get("count"), request.headers.get("Authorization")))
        if request.path == "/slow":
            await asyncio.sleep(0.1)
        if request.path == "/redirect":
            raise web.HTTPFound("http://unconfigured.invalid/")
        return web.Response(
            text='<root><item id="one">42</item></root>', content_type="application/xml"
        )

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    execute = functions.FunctionExecutor(manager).async_execute
    try:
        result = await execute(
            {
                "type": "rest",
                "resource": base + "/xml",
                "params": {"count": "{{ count }}"},
                "username": "test",
                "password": "example",
                "value_template": "{{ value_json.root.item['#text'] }}",
            },
            {"count": 3},
            actor,
        )
        assert result == "42"
        assert seen[0] == ("3", "Basic dGVzdDpleGFtcGxl")
        with pytest.raises(ValueError, match="超时"):
            await execute({"type": "rest", "resource": base + "/slow", "timeout": 0.01}, {}, actor)
        with pytest.raises(ValueError, match="范围"):
            await execute({"type": "rest", "resource": base + "/redirect"}, {}, actor)
    finally:
        await runner.cleanup()


async def test_sqlite_cannot_spoof_filter_view_with_quoted_cte_or_schema(runtime, tmp_path):
    manager, actor, _ = runtime
    path = tmp_path / "home-assistant_v2.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE states_meta(metadata_id INTEGER, entity_id TEXT)")
        db.execute("CREATE TABLE states(state TEXT, metadata_id INTEGER)")
        db.executemany(
            "INSERT INTO states_meta VALUES (?,?)", [(1, "light.public"), (2, "light.private")]
        )
        db.executemany("INSERT INTO states VALUES (?,?)", [("safe", 1), ("SECRET", 2)])
    execute = functions.FunctionExecutor(manager).async_execute
    for query in (
        "WITH 'states' AS (SELECT * FROM 'main'.'states') SELECT state FROM 'states'",
        'WITH "states"(state,metadata_id) AS (SELECT * FROM "main"."states") SELECT state FROM "states"',
        "SELECT state FROM 'main'.'states'",
    ):
        with pytest.raises(ValueError):
            await execute({"type": "sqlite", "query": query}, {}, actor)
    assert await execute(
        {
            "type": "sqlite",
            "query": "WITH allowed AS (SELECT state FROM states) SELECT * FROM allowed",
        },
        {},
        actor,
    ) == [{"state": "safe"}]


@pytest.mark.parametrize("directive", ["break", "continue", "do items.append(x)"])
def test_compile_accepts_homeassistant_template_extensions(directive):
    template = "{% set items = [] %}{% for x in [1,2] %}{% " + directive + " %}{% endfor %}ok"
    assert functions.compile_functions(
        [definition(function={"type": "template", "value_template": template})]
    )


@pytest.mark.parametrize(
    "branch",
    [
        {
            "if": "{{ is_state('light.private', 'off') }}",
            "then": [{"variables": {"_function_result": "leaked"}}],
        },
        {
            "choose": [
                {
                    "conditions": ["{{ is_state('light.private', 'off') }}"],
                    "sequence": [{"variables": {"_function_result": "leaked"}}],
                }
            ]
        },
    ],
)
async def test_script_shorthand_conditions_cannot_read_hidden_state(runtime, branch):
    manager, actor, user = runtime
    user.is_admin = False
    with pytest.raises(ValueError, match="公开|权限"):
        await functions.FunctionExecutor(manager).async_execute(
            {"type": "script", "sequence": [branch]}, {}, actor
        )


@pytest.mark.parametrize(
    "domain,service", [("input_datetime", "set_datetime"), ("automation", "trigger")]
)
async def test_native_ordinary_exposed_entity_service_outside_old_list(
    runtime, monkeypatch, domain, service
):
    manager, actor, _ = runtime
    entity = domain + ".public"
    manager.hass.states.async_set(entity, "off")
    monkeypatch.setattr(tools, "exposed_entities", lambda hass: {entity: hass.states.get(entity)})
    called = []
    manager.hass.services.async_register(domain, service, lambda call: called.append(call.data))
    result = await functions.FunctionExecutor(manager).async_execute(
        {"type": "native", "name": "execute_service_single"},
        {"domain": domain, "service": service, "data": {"entity_id": entity}},
        actor,
    )
    assert result["success"] is True and called == [{"entity_id": [entity]}]


@pytest.mark.parametrize(
    "domain,service,entity",
    [
        ("shell_command", "execute", "light.public"),
        ("hassio", "host_reboot", "light.public"),
        ("automation", "reload", "light.public"),
        ("camera", "record", "light.public"),
    ],
)
async def test_native_admin_or_file_services_rejected(runtime, domain, service, entity):
    manager, actor, _ = runtime
    called = []
    manager.hass.services.async_register(domain, service, lambda call: called.append(call))
    with pytest.raises(ValueError):
        await functions.FunctionExecutor(manager).async_execute(
            {"type": "native", "name": "execute_service_single"},
            {
                "domain": domain,
                "service": service,
                "data": {"entity_id": entity, "filename": "/tmp/unbounded"},
            },
            actor,
        )
    assert not called
