from __future__ import annotations

import json

from scan_release_secrets import scan_paths


def test_exact_runtime_secret_is_detected_without_disclosure(tmp_path, monkeypatch):
    secret = "super-secret-release-value-123456789"
    monkeypatch.setenv("TEST_RELEASE_SECRET", secret)
    artifact = tmp_path / "artifact.log"
    artifact.write_text(f"escaped={secret}\n", encoding="utf-8")
    output = tmp_path / "scan.json"

    report = scan_paths(
        [tmp_path], output=output, secret_env_names=["TEST_RELEASE_SECRET"]
    )

    assert report["status"] == "fail"
    assert report["findings"] == [
        {
            "path": str(artifact),
            "kind": "environment_secret",
            "source": "TEST_RELEASE_SECRET",
        }
    ]
    serialized = output.read_text(encoding="utf-8")
    assert secret not in serialized


def test_high_confidence_private_key_marker_fails(tmp_path):
    (tmp_path / "bad.txt").write_text(
        "-----BEGIN " + "PRIVATE KEY-----\nnot-a-real-key\n",
        encoding="utf-8",
    )
    output = tmp_path / "scan.json"
    report = scan_paths([tmp_path], output=output)
    assert report["status"] == "fail"
    assert report["findings"][0]["kind"] == "private_key"


def test_placeholders_and_loopback_test_credentials_do_not_false_positive(tmp_path):
    (tmp_path / "clean.txt").write_text(
        "DATABASE_URL=postgresql://postgres:postgres@127.0.0.1/test\n"
        "Authorization: Bearer <token>\n"
        "API_KEY=<redacted>\n",
        encoding="utf-8",
    )
    output = tmp_path / "scan.json"
    report = scan_paths([tmp_path], output=output)
    assert report["status"] == "pass"
    assert json.loads(output.read_text(encoding="utf-8"))["findings"] == []
