"""The web server. Thin on purpose: it loads or extracts a Process, hands it to
the engine, and returns the report. No state is kept except extraction jobs
that are still running."""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from .demos import list_demos, load_demo
from .engine import dry_run
from .extract import extract_process, refute
from .ingest import read_upload
from .layout import layout
from .llm import LLMClient, LLMError, OllamaClient
from .model import Process, validate_process

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_CHARS = 20000  # what a small local model can hold in one prompt, with room to answer
ALLOWED = (".txt", ".md", ".pdf", ".docx")


def _payload(process: Process, **extra) -> dict:
    return {"process": process.model_dump(by_alias=True), "layout": layout(process), **extra}


class RunRequest(BaseModel):
    process: dict
    refute: bool = False  # the refutation pass costs one model call per finding: opt in


def create_app(llm_factory: Optional[Callable[[], LLMClient]] = None) -> FastAPI:
    app = FastAPI(title="Dry Run", docs_url=None, redoc_url=None)
    make_llm = llm_factory or OllamaClient
    jobs: Dict[str, dict] = {}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status():
        llm = make_llm()
        info = llm.status() if hasattr(llm, "status") else {
            "reachable": True, "model_present": True, "model": llm.model, "host": ""}
        return {"llm": info, "max_chars": MAX_CHARS}

    @app.get("/api/demos")
    def demos():
        return list_demos()

    @app.get("/api/demos/{demo_id}")
    def demo(demo_id: str):
        if not demo_id.replace("_", "").isalnum():
            raise HTTPException(404, "No such demo.")
        try:
            return _payload(load_demo(demo_id))
        except FileNotFoundError:
            raise HTTPException(404, "No such demo.")

    @app.post("/api/run")
    def run(req: RunRequest):
        try:
            process = Process.model_validate(req.process)
        except ValidationError as e:
            raise HTTPException(422, f"Not a valid process model: {e.errors()[0]['msg']}")
        problems = validate_process(process)
        if problems:
            raise HTTPException(422, "Not a valid process model: " + "; ".join(problems[:3]))
        report = dry_run(process)
        report["refutation"] = "not applicable" if process.origin != "llm" else "not run"
        # The engine has already decided. The model is only asked to look for a
        # sentence that contradicts an absence-based finding.
        if process.origin == "llm" and req.refute:
            llm = make_llm()
            if llm.available():
                try:
                    report = refute(process, report, llm)
                    report["refutation"] = "done"
                except LLMError as e:
                    report["refutation"] = f"failed: {e}"
            else:
                report["refutation"] = "skipped: model not available"
        return report

    @app.post("/api/extract")
    async def extract(text: str = Form(""), title: str = Form(""),
                      file: Optional[UploadFile] = File(None)):
        if file is not None and file.filename:
            name = file.filename
            if not name.lower().endswith(ALLOWED):
                raise HTTPException(400, "Upload a TXT, PDF or DOCX file.")
            data = await file.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(400, "The file is larger than 5 MB.")
            try:
                text = read_upload(name, data)
            except Exception:
                raise HTTPException(400, "Could not read that file. Is it a real "
                                         "TXT, PDF or DOCX document?")
            title = title or Path(name).stem.replace("_", " ").replace("-", " ")
        text = (text or "").replace("\r\n", "\n").strip()
        if len(text) < 40:
            raise HTTPException(400, "No readable text found. A scanned PDF has no text "
                                     "layer; paste the text instead.")
        if len(text) > MAX_CHARS:
            raise HTTPException(400, f"The document has {len(text):,} characters. The limit is "
                                     f"{MAX_CHARS:,}; paste the section you want to test.")
        llm = make_llm()
        if not llm.available():
            info = llm.status() if hasattr(llm, "status") else {}
            if info.get("reachable"):
                msg = (f"Ollama is running but the model “{llm.model}” is not pulled. "
                       f"Run: ollama pull {llm.model}")
            else:
                msg = ("No local model is running, so new documents cannot be read. "
                       "Start Ollama, or pick a demo: demos need no model.")
            raise HTTPException(503, msg)
        job_id = uuid.uuid4().hex[:12]
        job = {"status": "running", "messages": [], "result": None, "error": None}
        jobs[job_id] = job

        def work():
            try:
                process, log = extract_process(text, llm, title=title or "Pasted process",
                                               progress=job["messages"].append)
                job["result"] = _payload(process, log=log, model=llm.model)
                job["status"] = "done"
            except (LLMError, ValueError) as e:
                job["error"], job["status"] = str(e), "failed"
            except Exception as e:  # never leave a job spinning
                job["error"], job["status"] = f"Extraction stopped unexpectedly: {e}", "failed"

        threading.Thread(target=work, daemon=True).start()
        return {"job": job_id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        if job["status"] != "running":
            jobs.pop(job_id, None)
        return job

    return app


app = create_app()
