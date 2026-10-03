"""OllamaClient against a local stub that speaks Ollama's HTTP shape. This checks the
wire format (endpoint, `format` schema, model name). It is not a real model."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dryrun.extract import SCHEMA_YES_NO
from dryrun.llm import LLMError, OllamaClient

SEEN = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"models": [{"name": "phi4-mini:latest"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, body))
        self._send({"message": {"role": "assistant", "content": '{"answer": "yes", "sentence": "c1"}'}})


@pytest.fixture()
def host():
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_ollama_client_sends_schema_and_parses_reply(host):
    SEEN.clear()
    llm = OllamaClient(model="phi4-mini", host=host)
    assert llm.available()
    assert llm.ask("sys", "user", SCHEMA_YES_NO) == {"answer": "yes", "sentence": "c1"}
    path, body = SEEN[0]
    assert path == "/api/chat" and body["model"] == "phi4-mini" and body["stream"] is False
    assert body["format"] == SCHEMA_YES_NO and body["options"]["temperature"] == 0


def test_model_name_comes_from_the_environment(host, monkeypatch):
    monkeypatch.setenv("DRYRUN_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("OLLAMA_HOST", host)
    llm = OllamaClient()
    assert llm.model == "qwen2.5:7b"
    assert llm.status() == {"reachable": True, "model_present": False, "model": "qwen2.5:7b", "host": host}


def test_unreachable_ollama_raises_a_clear_error():
    llm = OllamaClient(host="http://127.0.0.1:9", timeout=2)
    assert not llm.available()
    with pytest.raises(LLMError, match="could not reach Ollama"):
        llm.ask("s", "u", SCHEMA_YES_NO)
