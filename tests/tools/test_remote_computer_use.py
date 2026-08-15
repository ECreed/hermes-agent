import json
import threading
import time

import pytest

from tools.computer_use import remote


class FakeTransport:
    def __init__(self):
        self.frames = []
        self.on_write = None

    def write(self, frame):
        self.frames.append(frame)
        if self.on_write:
            self.on_write(frame)
        return True


def setup_function():
    remote.reset_device_registry_for_tests()


def test_registered_origin_device_receives_request_and_response():
    transport = FakeTransport()
    remote.register_device(
        "pc-1",
        transport,
        alias="studio",
        platform="linux",
        capabilities=["computer_use"],
    )

    def respond(frame):
        payload = frame["params"]["payload"]
        remote.resolve_device_response(
            "pc-1",
            payload["request_id"],
            {"ok": True, "result": {"action": "click", "ok": True}},
        )

    transport.on_write = respond
    result = remote.call_device("pc-1", "computer_use", {"action": "click", "element": 4}, yolo=True)

    assert result == {"action": "click", "ok": True}
    request = transport.frames[0]["params"]["payload"]
    assert request["endpoint_id"] == "pc-1"
    assert request["args"]["element"] == 4


def test_alias_resolution_is_unique_and_offline_never_falls_back():
    remote.register_device("pc-1", FakeTransport(), alias="studio", platform="linux")
    assert remote.resolve_endpoint("studio") == "pc-1"

    remote.unregister_transport(remote.get_device("pc-1").transport)
    with pytest.raises(remote.DeviceUnavailable, match="offline"):
        remote.resolve_endpoint("studio")


def test_turn_context_defaults_to_origin_and_preserves_explicit_target():
    token = remote.set_turn_device_context(origin_endpoint_id="pc-origin", yolo=True)
    try:
        assert remote.current_target_endpoint() == "pc-origin"
        assert remote.current_turn_yolo() is True
        assert remote.current_target_endpoint("pc-other") == "pc-other"
    finally:
        remote.reset_turn_device_context(token)


def test_tool_backend_uses_turn_origin_and_propagates_yolo(monkeypatch):
    from tools.computer_use import tool

    transport = FakeTransport()
    remote.register_device("pc-1", transport, alias="studio", platform="linux")

    def respond(frame):
        request = frame["params"]["payload"]
        remote.resolve_device_response(
            "pc-1",
            request["request_id"],
            {"ok": True, "result": {"apps": [{"name": "Files", "pid": 7}]}},
        )

    transport.on_write = respond
    token = remote.set_turn_device_context(origin_endpoint_id="pc-1", yolo=True)
    try:
        backend = tool._get_backend()
        assert isinstance(backend, remote.RemoteComputerUseBackend)
        assert backend.yolo is True
        assert backend.list_apps()[0]["name"] == "Files"
    finally:
        remote.reset_turn_device_context(token)
        tool.reset_backend_for_tests()


def test_endpoint_is_derived_from_registered_transport():
    transport = FakeTransport()
    assert remote.endpoint_for_transport(transport) == ""
    remote.register_device("pc-transport", transport, alias="desk", platform="linux")
    assert remote.endpoint_for_transport(transport) == "pc-transport"
    remote.unregister_transport(transport)
    assert remote.endpoint_for_transport(transport) == ""


def test_duplicate_endpoint_and_wrong_transport_response_are_rejected():
    first = FakeTransport()
    second = FakeTransport()
    remote.register_device("pc-owner", first, alias="owner", platform="linux", proof="owner-proof")
    with pytest.raises(ValueError, match="already registered"):
        remote.register_device("pc-owner", second, alias="attacker", platform="linux", proof="attacker-proof")

    pending = threading.Event()

    def invoke():
        try:
            remote.call_device("pc-owner", "computer_use", {"action": "list_apps"}, yolo=True, timeout=1)
        except remote.DeviceUnavailable:
            pass
        finally:
            pending.set()

    worker = threading.Thread(target=invoke)
    worker.start()
    while not first.frames:
        time.sleep(0.01)
    request = first.frames[-1]["params"]["payload"]
    assert remote.resolve_device_response(
        "pc-owner", request["request_id"], {"ok": True, "result": {}}, transport=second
    ) is False
    assert remote.resolve_device_response(
        "pc-owner", request["request_id"], {"ok": True, "result": {}}, transport=first
    ) is True
    worker.join(timeout=2)
    assert pending.is_set()


def test_same_endpoint_proof_allows_multiple_profile_transports():
    first = FakeTransport()
    second = FakeTransport()
    remote.register_device("pc-multi", first, alias="desk", platform="linux", proof="same-proof")
    remote.register_device("pc-multi", second, alias="desk", platform="linux", proof="same-proof")

    assert remote.endpoint_for_transport(first) == "pc-multi"
    assert remote.endpoint_for_transport(second) == "pc-multi"
    remote.unregister_transport(second)
    assert remote.endpoint_for_transport(first) == "pc-multi"
    assert remote.get_device("pc-multi") is not None


def test_explicit_target_overrides_turn_origin_for_tool_call():
    from tools.computer_use import tool

    origin = FakeTransport()
    other = FakeTransport()
    remote.register_device("pc-origin", origin, alias="origin", platform="linux")
    remote.register_device("pc-other", other, alias="studio", platform="linux")

    def respond(frame):
        request = frame["params"]["payload"]
        remote.resolve_device_response(
            "pc-other",
            request["request_id"],
            {"ok": True, "result": {"apps": [{"name": "Other", "pid": 9}]}},
        )

    other.on_write = respond
    token = remote.set_turn_device_context(origin_endpoint_id="pc-origin", yolo=True)
    try:
        result = json.loads(tool.handle_computer_use({"action": "list_apps", "target": "studio"}))
    finally:
        remote.reset_turn_device_context(token)
        tool.reset_backend_for_tests()

    assert result["apps"][0]["name"] == "Other"
    assert origin.frames == []


def test_gateway_device_rpc_registers_transport_and_accepts_response():
    from tui_gateway import server

    transport = FakeTransport()
    register = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "device.register",
            "params": {"endpoint_id": "pc-gw", "proof": "proof", "alias": "desk", "platform": "linux"},
        },
        transport,
    )
    assert register["result"]["endpoint_id"] == "pc-gw"
    assert remote.resolve_endpoint("desk") == "pc-gw"

    pending_done = threading.Event()
    captured = {}

    def invoke():
        try:
            captured["result"] = remote.call_device("pc-gw", "computer_use", {"action": "list_apps"}, yolo=True)
        finally:
            pending_done.set()

    worker = threading.Thread(target=invoke)
    worker.start()
    while not transport.frames:
        time.sleep(0.01)
    request = transport.frames[-1]["params"]["payload"]
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "device.respond",
            "params": {
                "endpoint_id": "pc-gw",
                "request_id": request["request_id"],
                "ok": True,
                "result": {"apps": []},
            },
        },
        transport,
    )
    worker.join(timeout=2)

    assert response["result"]["accepted"] is True
    assert pending_done.is_set()
    assert captured["result"] == {"apps": []}


def test_busy_submit_does_not_rebind_running_turn_and_rejects_cross_endpoint_merge(monkeypatch):
    from tui_gateway import server

    running_transport = FakeTransport()
    queued_a = FakeTransport()
    queued_b = FakeTransport()
    session = {
        "agent": None,
        "history": [],
        "history_lock": threading.Lock(),
        "running": True,
        "session_key": "stored-busy",
        "transport": running_transport,
    }
    monkeypatch.setitem(server._sessions, "live-busy", session)
    remote.register_device("pc-a", queued_a, alias="a", platform="linux", proof="a-proof")
    remote.register_device("pc-b", queued_b, alias="b", platform="linux", proof="b-proof")

    first = server.dispatch(
        {"id": 1, "method": "prompt.submit", "params": {"session_id": "live-busy", "text": "a"}},
        queued_a,
    )
    second = server.dispatch(
        {"id": 2, "method": "prompt.submit", "params": {"session_id": "live-busy", "text": "b"}},
        queued_b,
    )

    assert first["result"]["status"] == "queued"
    assert second["error"]["code"] == 4009
    assert session["transport"] is running_transport
    assert session["queued_prompt"]["origin_endpoint_id"] == "pc-a"
    assert session["queued_prompt"]["text"] == "a"


def test_prompt_submit_snapshots_origin_from_current_transport(monkeypatch):
    from tui_gateway import server

    transport = FakeTransport()
    remote.register_device("pc-submit", transport, alias="desk", platform="linux")
    session = {
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "stored-submit",
        "transport": None,
    }
    monkeypatch.setitem(server._sessions, "live-submit", session)
    observed_origin = {}
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda value: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda value: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, value: None)

    def capture_submit(rid, sid, value, text, *, origin_endpoint_id=""):
        observed_origin["value"] = origin_endpoint_id

    monkeypatch.setattr(server, "_run_prompt_submit", capture_submit)

    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "prompt.submit",
            "params": {"session_id": "live-submit", "text": "hello"},
        },
        transport,
    )

    assert response["result"]["status"] == "streaming"
    thread = session["_run_thread"]
    thread.join(timeout=2)
    assert observed_origin["value"] == "pc-submit"


def test_session_origin_endpoint_is_bound_during_agent_turn(monkeypatch):
    from tui_gateway import server

    observed = {}

    class Agent:
        session_id = "stored"

        def clear_interrupt(self):
            pass

        def run_conversation(self, message, **kwargs):
            observed["origin"] = remote.current_target_endpoint()
            observed["yolo"] = remote.current_turn_yolo()
            return {"final_response": "ok", "messages": []}

    session = {
        "agent": Agent(),
        "attached_images": [],
        "cols": 80,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "inflight_turn": None,
        "session_key": "stored",
        "transport": FakeTransport(),
        "running": True,
    }
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, value: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda value: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *args, **kwargs: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda tokens: None)
    monkeypatch.setattr(server, "_session_cwd", lambda value: ".")
    monkeypatch.setattr(server, "_get_db", lambda: None)

    from tools.approval import enable_session_yolo, disable_session_yolo
    enable_session_yolo("stored")
    try:
        server._run_prompt_submit(
            1,
            "live",
            session,
            "hello",
            origin_endpoint_id="pc-turn",
        )
        thread = session["_run_thread"]
        thread.join(timeout=3)
    finally:
        disable_session_yolo("stored")

    assert observed == {"origin": "pc-turn", "yolo": True}


def test_computer_use_requirements_accepts_registered_remote_device(monkeypatch):
    from tools.computer_use import tool

    monkeypatch.setattr(tool.sys, "platform", "linux")
    monkeypatch.setattr("tools.computer_use.cua_backend.cua_driver_binary_available", lambda: False)
    assert tool.check_computer_use_requirements() is False

    remote.register_device("pc-ready", FakeTransport(), alias="desk", platform="linux")
    assert tool.check_computer_use_requirements() is True


def test_device_executor_serializes_capture_and_actions(monkeypatch):
    from tools.computer_use import device_exec
    from tools.computer_use.backend import ActionResult, CaptureResult, UIElement

    class Backend:
        def capture(self, mode="som", app=None):
            return CaptureResult(
                mode=mode,
                width=10,
                height=20,
                elements=[UIElement(index=1, role="Button", label="OK", bounds=(1, 2, 3, 4))],
                app=app or "",
            )

    monkeypatch.setattr(device_exec, "_backend", Backend())
    result = device_exec.execute({"action": "capture", "mode": "ax", "app": "Demo"})
    assert result["width"] == 10
    assert result["elements"][0]["label"] == "OK"


def test_remote_backend_converts_capture_payload():
    transport = FakeTransport()
    remote.register_device("pc-1", transport, alias="studio", platform="linux")

    def respond(frame):
        request = frame["params"]["payload"]
        remote.resolve_device_response(
            "pc-1",
            request["request_id"],
            {
                "ok": True,
                "result": {
                    "mode": "som",
                    "width": 800,
                    "height": 600,
                    "png_b64": None,
                    "elements": [
                        {"index": 1, "role": "Button", "label": "Open", "bounds": [1, 2, 3, 4]}
                    ],
                    "app": "Files",
                    "window_title": "Home",
                },
            },
        )

    transport.on_write = respond
    backend = remote.RemoteComputerUseBackend("pc-1", yolo=True)
    capture = backend.capture("som", "Files")

    assert capture.width == 800
    assert capture.elements[0].label == "Open"
    assert capture.app == "Files"
