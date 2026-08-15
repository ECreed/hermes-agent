"""JSON-lines worker used by Hermes Desktop's local device bridge."""

from __future__ import annotations

import json
import sys

from tools.computer_use.device_exec import execute


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            request_id = request.get("id")
            result = execute(request.get("args") or {})
            response = {"id": request_id, "ok": True, "result": result}
        except Exception as exc:
            response = {
                "id": request.get("id") if isinstance(locals().get("request"), dict) else None,
                "ok": False,
                "error": str(exc),
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
