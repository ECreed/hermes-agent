"""Local device-side computer-use executor.

This module is intentionally model-free: a trusted desktop bridge passes an
already-authorized action dict and receives JSON-serializable data.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from tools.computer_use.cua_backend import CuaDriverBackend

_backend = None


def _get_backend():
    global _backend
    if _backend is None:
        _backend = CuaDriverBackend()
        _backend.start()
    return _backend


def _action_dict(result) -> Dict[str, Any]:
    return {
        "ok": bool(result.ok),
        "action": str(result.action),
        "message": str(result.message or ""),
        "meta": dict(result.meta or {}),
    }


def execute(args: Dict[str, Any]) -> Any:
    backend = _get_backend()
    action = str((args or {}).get("action") or "").strip().lower()
    if action == "capture":
        cap = backend.capture(str(args.get("mode") or "som"), args.get("app"))
        return {
            "mode": cap.mode,
            "width": cap.width,
            "height": cap.height,
            "png_b64": cap.png_b64,
            "elements": [
                {
                    "index": item.index,
                    "role": item.role,
                    "label": item.label,
                    "bounds": list(item.bounds),
                    "app": item.app,
                    "element_token": item.element_token,
                }
                for item in cap.elements
            ],
            "app": cap.app,
            "window_title": cap.window_title,
            "png_bytes_len": cap.png_bytes_len,
            "image_mime_type": cap.image_mime_type,
        }
    if action == "list_apps":
        apps = backend.list_apps()
        return {"apps": apps, "count": len(apps)}
    if action == "wait":
        return _action_dict(backend.wait(float(args.get("seconds", 1))))
    if action == "focus_app":
        return _action_dict(backend.focus_app(str(args.get("app") or ""), bool(args.get("raise_window"))))
    if action in {"click", "double_click", "right_click", "middle_click"}:
        coordinate = args.get("coordinate") or [None, None]
        button = args.get("button") or "left"
        if action == "right_click":
            button = "right"
        elif action == "middle_click":
            button = "middle"
        return _action_dict(backend.click(
            element=args.get("element"),
            x=coordinate[0],
            y=coordinate[1],
            button=button,
            click_count=2 if action == "double_click" else 1,
            modifiers=args.get("modifiers"),
        ))
    if action == "drag":
        return _action_dict(backend.drag(
            from_element=args.get("from_element"),
            to_element=args.get("to_element"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            button=args.get("button") or "left",
            modifiers=args.get("modifiers"),
        ))
    if action == "scroll":
        coordinate = args.get("coordinate") or [None, None]
        return _action_dict(backend.scroll(
            direction=args.get("direction") or "down",
            amount=int(args.get("amount", 3)),
            element=args.get("element"),
            x=coordinate[0],
            y=coordinate[1],
            modifiers=args.get("modifiers"),
        ))
    if action == "type":
        return _action_dict(backend.type_text(str(args.get("text") or "")))
    if action == "key":
        return _action_dict(backend.key(str(args.get("keys") or "")))
    if action == "set_value":
        return _action_dict(backend.set_value(str(args.get("value") or ""), args.get("element")))
    raise ValueError(f"unknown computer_use action: {action!r}")
