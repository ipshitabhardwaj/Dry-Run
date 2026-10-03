"""The HTTP layer, end to end, with the fake model standing in for Ollama."""
import time

from fastapi.testclient import TestClient

from dryrun.app import create_app
from dryrun.llm import OllamaClient
from tests.pdfutil import make_pdf
from tests.test_extract import TEXT, fake


def client(factory=None):
    return TestClient(create_app(factory))


def wait(c, job):
    for _ in range(200):
        j = c.get(f"/api/jobs/{job}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_demo_mode_needs_no_model():
    c = client(lambda: OllamaClient(host="http://127.0.0.1:9"))  # nothing listens there
    assert c.get("/api/status").json()["llm"]["reachable"] is False
    demos = c.get("/api/demos").json()
    assert [d["id"] for d in demos] == ["1_admission", "2_expenses", "3_travel"]
    d = c.get("/api/demos/1_admission").json()
    assert set(d["layout"]["nodes"]) == {s["id"] for s in d["process"]["states"]}
    assert d["process"]["transitions"][0]["from"] == "offer"
    r = c.post("/api/run", json={"process": d["process"]}).json()
    assert r["summary"]["structural_findings"] == 5 and r["refutation"] == "not applicable"


def test_index_is_served_and_has_no_external_references():
    html = client().get("/").text
    assert "Test your process before reality does" in html
    for needle in ("http://", "https://", "//cdn", "@import"):
        assert needle not in html.replace("http://www.w3.org/2000/svg", ""), needle


def test_extract_without_a_model_explains_itself():
    c = client(lambda: OllamaClient(host="http://127.0.0.1:9"))
    r = c.post("/api/extract", data={"text": TEXT})
    assert r.status_code == 503 and "pick a demo" in r.json()["detail"]


def test_paste_extract_then_run_with_refutation():
    c = client(fake)
    job = c.post("/api/extract", data={"text": TEXT, "title": "Refunds"}).json()["job"]
    j = wait(c, job)
    assert j["status"] == "done", j
    assert j["messages"][0] == "Finding actors and states"
    process = j["result"]["process"]
    assert process["origin"] == "llm" and process["title"] == "Refunds"
    assert c.post("/api/run", json={"process": process}).json()["refutation"] == "not run"
    r = c.post("/api/run", json={"process": process, "refute": True}).json()
    assert r["refutation"] == "done"
    assert any(f["status"] == "downgraded" for f in r["findings"])
    assert all(f["confidence"] < 0.95 for f in r["findings"])  # extracted models are trusted less


def test_upload_pdf_and_reject_other_types():
    c = client(fake)
    r = c.post("/api/extract", files={"file": ("refunds.pdf", make_pdf(TEXT.split("\n")), "application/pdf")})
    assert wait(c, r.json()["job"])["status"] == "done"
    assert c.post("/api/extract", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
    assert c.post("/api/extract", files={"file": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 400
    assert c.post("/api/extract", data={"text": "too short"}).status_code == 400


def test_run_rejects_a_broken_model():
    c = client()
    assert c.post("/api/run", json={"process": {"title": "x"}}).status_code == 422
    d = c.get("/api/demos/2_expenses").json()["process"]
    d["transitions"][0]["to"] = "nowhere"
    assert c.post("/api/run", json={"process": d}).status_code == 422
    assert c.get("/api/demos/..%2Fsecret").status_code == 404
