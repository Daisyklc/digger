import os
import time

os.environ["MOCK"] = "1"
os.environ["API_KEY"] = "test-token"

import pytest
from fastapi.testclient import TestClient

from drama_asr.server.app import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mock"] is True


def test_requires_auth():
    resp = client.post("/api/jobs", files={"file": ("a.wav", b"xxxx")})
    assert resp.status_code == 401


def test_rejects_empty_upload():
    resp = client.post("/api/jobs", headers=AUTH, files={"file": ("a.wav", b"")})
    assert resp.status_code == 400


def _wait_for_done(job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status == "done":
            return
        if status == "error":
            raise AssertionError(resp.json()["error"])
        time.sleep(0.1)
    raise AssertionError("job did not finish in time")


def test_full_flow():
    resp = client.post("/api/jobs", headers=AUTH, files={"file": ("ep1.wav", b"fake audio bytes")})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    _wait_for_done(job_id)

    result = client.get(f"/api/jobs/{job_id}/result", headers=AUTH)
    assert result.status_code == 200
    data = result.json()
    assert data["filename"] == "ep1.wav"
    assert len(data["segments"]) > 0
    assert "srt_bilingual" in data
    assert "-->" in data["srt_bilingual"]


def test_result_404_for_unknown_job():
    resp = client.get("/api/jobs/does-not-exist/result", headers=AUTH)
    assert resp.status_code == 404
