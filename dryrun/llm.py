"""The only door to a language model.

Everything that talks to a model goes through `LLMClient.ask`: one narrow
question, one JSON schema, one JSON object back. `OllamaClient` sends that to
an open-weight model served locally by Ollama, using Ollama's structured
outputs (`format` = a JSON schema). `FakeLLMClient` is the stand-in used by
the tests, so the test suite never needs a model.

Environment:
    DRYRUN_MODEL   model tag to ask Ollama for   (default: phi4-mini)
    OLLAMA_HOST    where Ollama listens          (default: http://localhost:11434)
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, List, Optional, Protocol, Tuple, Union

DEFAULT_MODEL = "phi4-mini"
DEFAULT_HOST = "http://localhost:11434"


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    model: str

    def available(self) -> bool: ...

    def ask(self, system: str, user: str, schema: dict) -> dict:
        """Return one JSON object that follows `schema`."""
        ...


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a == -1 or b <= a:
            raise LLMError("the model did not return JSON")
        try:
            value = json.loads(text[a:b + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"the model returned malformed JSON: {e}") from e
    if not isinstance(value, dict):
        raise LLMError("the model returned JSON that is not an object")
    return value


def configured_model() -> str:
    return os.environ.get("DRYRUN_MODEL", DEFAULT_MODEL)


class OllamaClient:
    def __init__(self, model: Optional[str] = None, host: Optional[str] = None,
                 timeout: float = 240):
        self.model = model or configured_model()
        host = host or os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
        if not host.startswith("http"):
            host = "http://" + host
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.calls = 0

    def status(self) -> dict:
        """Is Ollama running, and has the configured model been pulled?"""
        try:
            with urllib.request.urlopen(self.host + "/api/tags", timeout=2) as resp:
                names = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        except Exception:
            return {"reachable": False, "model_present": False, "model": self.model,
                    "host": self.host}
        want = self.model if ":" in self.model else self.model + ":latest"
        return {"reachable": True, "model_present": want in names or self.model in names,
                "model": self.model, "host": self.host}

    def available(self) -> bool:
        s = self.status()
        return s["reachable"] and s["model_present"]

    def ask(self, system: str, user: str, schema: dict) -> dict:
        body = json.dumps({
            "model": self.model, "stream": False, "format": schema,
            "options": {"temperature": 0, "repeat_penalty": 1.1,
                        "num_ctx": int(os.environ.get("DRYRUN_NUM_CTX", "8192")),
                        # a small model can repeat list items forever under a schema;
                        # cap the answer so one call can never hang the run
                        "num_predict": int(os.environ.get("DRYRUN_MAX_TOKENS", "1500"))},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(self.host + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama refused the request ({e.code}): "
                           f"{e.read().decode(errors='replace')[:200]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"could not reach Ollama at {self.host}: {e}") from e
        self.calls += 1
        if data.get("done_reason") == "length":
            raise LLMError("the model's answer ran past the length limit (it was probably "
                           "repeating itself). Try a shorter document or a larger model.")
        return parse_json(data.get("message", {}).get("content", ""))


Rule = Tuple[str, Union[dict, Callable[[str], dict]]]


class FakeLLMClient:
    """Scripted answers for tests: the first rule whose keyword appears in the
    prompt wins. Records every call, including the schema it was given."""

    model = "fake"

    def __init__(self, rules: List[Rule]):
        self.rules = rules
        self.calls = 0
        self.log: List[dict] = []

    def available(self) -> bool:
        return True

    def ask(self, system: str, user: str, schema: dict) -> dict:
        self.calls += 1
        self.log.append({"user": user, "schema": schema})
        for keyword, answer in self.rules:
            if keyword in user:
                return answer(user) if callable(answer) else json.loads(json.dumps(answer))
        raise AssertionError("FakeLLMClient has no rule for: " + user[-160:])
