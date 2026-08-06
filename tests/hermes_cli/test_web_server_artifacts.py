"""Security-focused tests for the Z13 artifact API and project bundles."""

import json
import zipfile

from starlette.testclient import TestClient

from hermes_cli import web_server


def _restore_app_state(auth_required, bound_host) -> None:
    if auth_required is None:
        if hasattr(web_server.app.state, "auth_required"):
            delattr(web_server.app.state, "auth_required")
    else:
        web_server.app.state.auth_required = auth_required
    if bound_host is None:
        if hasattr(web_server.app.state, "bound_host"):
            delattr(web_server.app.state, "bound_host")
    else:
        web_server.app.state.bound_host = bound_host


def test_artifact_routes_require_header_and_reject_query_token(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))
    previous_auth = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = None
    client = TestClient(web_server.app)
    try:
        rejected = client.get(
            "/api/artifacts",
            params={"token": web_server._SESSION_TOKEN},
        )
        assert rejected.status_code == 401

        rejected_download = client.get(
            "/api/artifacts/download",
            params={"id": "missing", "token": web_server._SESSION_TOKEN},
        )
        assert rejected_download.status_code == 401

        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        accepted = client.get("/api/artifacts")
        assert accepted.status_code == 200
        assert accepted.json() == {"artifacts": []}
        assert client.get("/api/artifacts/download", params={"id": "missing"}).status_code == 404
    finally:
        client.close()
        _restore_app_state(previous_auth, previous_host)


def test_project_bundle_redacts_content_and_audits_skips(monkeypatch, tmp_path):
    artifacts_root = tmp_path / "artifacts"
    projects_root = tmp_path / "projects"
    project_root = projects_root / "demo"
    capsule_root = tmp_path / "capsules"
    audit_events = tmp_path / "events.jsonl"
    project_root.mkdir(parents=True)
    capsule_root.mkdir()

    file_secret = "sk-test-only-1234567890"
    memory_secret = "unit-test-memory-secret"
    capsule_secret = "unit-test-capsule-secret"
    audit_secret = "unit-test-audit-secret"
    env_secret = "unit-test-env-secret"

    (project_root / "notes.txt").write_text(
        f"release notes\nOPENAI_API_KEY={file_secret}\n",
        encoding="utf-8",
    )
    (project_root / ".env").write_text(
        f"PASSWORD={env_secret}\n",
        encoding="utf-8",
    )
    (project_root / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nunit-test-image")
    (project_root / "opaque.bin").write_bytes(b"opaque\x00payload")
    (capsule_root / "demo.json").write_text(
        json.dumps({"project": "demo", "api_key": capsule_secret}),
        encoding="utf-8",
    )
    audit_events.write_text(
        json.dumps({"project": "demo", "access_token": audit_secret}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_ARTIFACTS_ROOT", str(artifacts_root))
    monkeypatch.setattr(web_server, "_ARTIFACT_PROJECTS_DIR", projects_root)
    monkeypatch.setattr(web_server, "_ARTIFACT_CAPSULE_DIR", capsule_root)
    monkeypatch.setattr(web_server, "_ARTIFACT_AUDIT_EVENTS", audit_events)
    monkeypatch.setattr(
        web_server,
        "_artifact_project_memory",
        lambda _project: [
            {
                "id": "memory-1",
                "project_id": "demo",
                "summary": "safe summary",
                "client_secret": memory_secret,
            }
        ],
    )

    result = web_server._create_artifact_project_bundle("demo", False)
    bundle = result["artifact"]["path"]

    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        payload = b"\n".join(archive.read(name) for name in names)
        files_manifest = json.loads(archive.read("manifest.files.json"))
        security_report = json.loads(archive.read("security-report.json"))

    for secret in (file_secret, memory_secret, capsule_secret, audit_secret, env_secret):
        assert secret.encode() not in payload

    assert "projects/demo/notes.txt" in names
    assert "projects/demo/diagram.png" in names
    assert "projects/demo/.env" not in names
    assert "projects/demo/opaque.bin" not in names

    entries = {entry["path"]: entry for entry in files_manifest}
    assert entries["projects/demo/notes.txt"]["redacted"] is True
    assert entries["capsules/demo.json"]["redacted"] is True
    assert entries["memory/entries.json"]["redacted"] is True
    assert entries["audit/events.jsonl"]["redacted"] is True
    assert entries["projects/demo/.env"]["skipped"] == "sensitive_path"
    assert entries["projects/demo/opaque.bin"]["skipped"] == "unscannable_binary"
    assert security_report["redacted_entries"] >= 4
    assert security_report["redactions"] >= 4
    assert security_report["skipped_entries"] == {
        "sensitive_path": 1,
        "unscannable_binary": 1,
    }
