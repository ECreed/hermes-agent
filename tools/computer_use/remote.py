"""Remote desktop endpoint registry and ComputerUseBackend adapter.

Desktop clients register their authenticated gateway transport and execute
computer-use calls locally. Agent turns bind an origin endpoint through
contextvars so concurrent sessions never race on process-global state.
"""

from __future__ import annotations

import contextvars
import hmac
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.computer_use.backend import ActionResult, CaptureResult, ComputerUseBackend, UIElement


class DeviceUnavailable(RuntimeError):
    pass


@dataclass
class RegisteredDevice:
    endpoint_id: str
    transport: Any
    alias: str = ""
    platform: str = ""
    capabilities: Tuple[str, ...] = ()
    proof: str = ""
    transports: List[Any] = field(default_factory=list)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


_devices: Dict[str, RegisteredDevice] = {}
_devices_lock = threading.RLock()
_pending: Dict[str, Tuple[str, threading.Event, Dict[str, Any]]] = {}
_pending_lock = threading.RLock()

_turn_origin: contextvars.ContextVar[str] = contextvars.ContextVar("computer_use_origin_endpoint", default="")
_turn_yolo: contextvars.ContextVar[bool] = contextvars.ContextVar("computer_use_turn_yolo", default=False)


def register_device(
    endpoint_id: str,
    transport: Any,
    *,
    alias: str = "",
    platform: str = "",
    capabilities: Optional[Iterable[str]] = None,
    proof: str = "",
) -> RegisteredDevice:
    endpoint_id = str(endpoint_id or "").strip()
    proof = str(proof or "__internal__").strip()
    if not endpoint_id:
        raise ValueError("endpoint_id is required")
    with _devices_lock:
        existing = _devices.get(endpoint_id)
        if existing is not None:
            if not hmac.compare_digest(str(getattr(existing, "proof", "")), proof):
                raise ValueError(f"endpoint_id {endpoint_id!r} is already registered")
            if all(item is not transport for item in existing.transports):
                existing.transports.append(transport)
            existing.transport = transport
            existing.last_seen = time.time()
            existing.alias = str(alias or existing.alias).strip()
            existing.platform = str(platform or existing.platform).strip()
            existing.capabilities = tuple(str(item) for item in (capabilities or existing.capabilities))
            return existing
        device = RegisteredDevice(
            endpoint_id=endpoint_id,
            transport=transport,
            alias=str(alias or "").strip(),
            platform=str(platform or "").strip(),
            capabilities=tuple(str(item) for item in (capabilities or ("computer_use",))),
            proof=proof,
            transports=[transport],
        )
        _devices[endpoint_id] = device
    return device


def get_device(endpoint_id: str) -> Optional[RegisteredDevice]:
    with _devices_lock:
        return _devices.get(str(endpoint_id or "").strip())


def unregister_transport(transport: Any) -> None:
    with _devices_lock:
        stale = []
        for key, device in _devices.items():
            device.transports = [item for item in device.transports if item is not transport]
            if not device.transports:
                stale.append(key)
            elif device.transport is transport:
                device.transport = device.transports[-1]
        for key in stale:
            _devices.pop(key, None)


def endpoint_for_transport(transport: Any) -> str:
    if transport is None:
        return ""
    with _devices_lock:
        for device in _devices.values():
            if any(item is transport for item in device.transports):
                return device.endpoint_id
    return ""


def resolve_endpoint(target: str) -> str:
    target = str(target or "").strip()
    if not target:
        raise DeviceUnavailable("no desktop endpoint was selected")
    with _devices_lock:
        exact = _devices.get(target)
        if exact is not None:
            return exact.endpoint_id
        matches = [device.endpoint_id for device in _devices.values() if device.alias and device.alias.casefold() == target.casefold()]
    if not matches:
        raise DeviceUnavailable(f"desktop endpoint {target!r} is offline or unknown")
    if len(matches) > 1:
        raise DeviceUnavailable(f"desktop endpoint alias {target!r} is ambiguous")
    return matches[0]


def list_devices() -> List[Dict[str, Any]]:
    with _devices_lock:
        return [
            {
                "endpoint_id": device.endpoint_id,
                "alias": device.alias,
                "platform": device.platform,
                "capabilities": list(device.capabilities),
                "online": True,
                "last_seen": device.last_seen,
            }
            for device in _devices.values()
        ]


def set_turn_device_context(*, origin_endpoint_id: str = "", yolo: bool = False):
    return (_turn_origin.set(str(origin_endpoint_id or "").strip()), _turn_yolo.set(bool(yolo)))


def reset_turn_device_context(tokens) -> None:
    origin_token, yolo_token = tokens
    _turn_yolo.reset(yolo_token)
    _turn_origin.reset(origin_token)


def current_target_endpoint(explicit_target: Optional[str] = None) -> str:
    return str(explicit_target or "").strip() or _turn_origin.get()


def current_turn_yolo() -> bool:
    return bool(_turn_yolo.get())


def resolve_device_response(
    endpoint_id: str,
    request_id: str,
    payload: Dict[str, Any],
    *,
    transport: Any = None,
) -> bool:
    device = get_device(str(endpoint_id or "").strip())
    if device is None or (
        transport is not None
        and all(item is not transport for item in device.transports)
    ):
        return False
    with _pending_lock:
        pending = _pending.get(str(request_id or ""))
        if pending is None or pending[0] != str(endpoint_id or ""):
            return False
        pending[2].update(payload if isinstance(payload, dict) else {"ok": False, "error": "invalid response"})
        pending[1].set()
        return True


def call_device(
    endpoint_id: str,
    method: str,
    args: Dict[str, Any],
    *,
    yolo: bool,
    timeout: float = 45.0,
) -> Any:
    resolved = resolve_endpoint(endpoint_id)
    device = get_device(resolved)
    if device is None:
        raise DeviceUnavailable(f"desktop endpoint {resolved!r} is offline")
    if method not in device.capabilities:
        raise DeviceUnavailable(f"desktop endpoint {resolved!r} does not support {method}")
    request_id = uuid.uuid4().hex
    done = threading.Event()
    response: Dict[str, Any] = {}
    with _pending_lock:
        _pending[request_id] = (resolved, done, response)
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "device.request",
            "payload": {
                "request_id": request_id,
                "endpoint_id": resolved,
                "method": method,
                "args": dict(args or {}),
            },
        },
    }
    try:
        if not device.transport.write(frame):
            raise DeviceUnavailable(f"desktop endpoint {resolved!r} disconnected")
        if not done.wait(timeout=max(0.1, float(timeout))):
            raise DeviceUnavailable(f"desktop endpoint {resolved!r} timed out")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "desktop execution failed"))
        return response.get("result")
    finally:
        with _pending_lock:
            _pending.pop(request_id, None)


def _action(payload: Any, fallback: str) -> ActionResult:
    data = payload if isinstance(payload, dict) else {}
    return ActionResult(
        ok=bool(data.get("ok", False)),
        action=str(data.get("action") or fallback),
        message=str(data.get("message") or ""),
        meta=data.get("meta") if isinstance(data.get("meta"), dict) else {},
    )


class RemoteComputerUseBackend(ComputerUseBackend):
    def __init__(self, endpoint_id: str, *, yolo: bool = False) -> None:
        self.endpoint_id = endpoint_id
        self.yolo = bool(yolo)
        self._last_app: Optional[str] = None

    def start(self) -> None:
        resolve_endpoint(self.endpoint_id)

    def stop(self) -> None:
        return None

    def is_available(self) -> bool:
        try:
            resolve_endpoint(self.endpoint_id)
            return True
        except DeviceUnavailable:
            return False

    def _call(self, action: str, **args: Any) -> Any:
        clean = {key: value for key, value in args.items() if value is not None}
        return call_device(self.endpoint_id, "computer_use", {"action": action, **clean}, yolo=self.yolo)

    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        data = self._call("capture", mode=mode, app=app)
        if not isinstance(data, dict):
            raise RuntimeError("invalid remote capture response")
        self._last_app = str(data.get("app") or app or "") or None
        elements = []
        for raw in data.get("elements") or []:
            if not isinstance(raw, dict):
                continue
            bounds = raw.get("bounds") or (0, 0, 0, 0)
            elements.append(UIElement(
                index=int(raw.get("index", 0)),
                role=str(raw.get("role") or ""),
                label=str(raw.get("label") or ""),
                bounds=tuple(int(v) for v in bounds[:4]),
                app=str(raw.get("app") or ""),
                element_token=raw.get("element_token"),
            ))
        return CaptureResult(
            mode=str(data.get("mode") or mode),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            png_b64=data.get("png_b64"),
            elements=elements,
            app=str(data.get("app") or ""),
            window_title=str(data.get("window_title") or ""),
            png_bytes_len=int(data.get("png_bytes_len") or 0),
            image_mime_type=data.get("image_mime_type"),
        )

    def click(self, *, element=None, x=None, y=None, button="left", click_count=1, modifiers=None):
        action = "double_click" if click_count == 2 else "click"
        coordinate = [x, y] if x is not None and y is not None else None
        return _action(self._call(action, element=element, coordinate=coordinate, button=button, modifiers=modifiers), action)

    def drag(self, *, from_element=None, to_element=None, from_xy=None, to_xy=None, button="left", modifiers=None):
        return _action(self._call("drag", from_element=from_element, to_element=to_element, from_coordinate=from_xy, to_coordinate=to_xy, button=button, modifiers=modifiers), "drag")

    def scroll(self, *, direction, amount=3, element=None, x=None, y=None, modifiers=None):
        coordinate = [x, y] if x is not None and y is not None else None
        return _action(self._call("scroll", direction=direction, amount=amount, element=element, coordinate=coordinate, modifiers=modifiers), "scroll")

    def type_text(self, text: str) -> ActionResult:
        return _action(self._call("type", text=text), "type")

    def key(self, keys: str) -> ActionResult:
        return _action(self._call("key", keys=keys), "key")

    def list_apps(self) -> List[Dict[str, Any]]:
        data = self._call("list_apps")
        return list((data or {}).get("apps") or []) if isinstance(data, dict) else []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        self._last_app = app
        return _action(self._call("focus_app", app=app, raise_window=raise_window), "focus_app")

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        return _action(self._call("set_value", value=value, element=element), "set_value")


def reset_device_registry_for_tests() -> None:
    with _devices_lock:
        _devices.clear()
    with _pending_lock:
        for _endpoint, event, response in _pending.values():
            response.update({"ok": False, "error": "registry reset"})
            event.set()
        _pending.clear()
