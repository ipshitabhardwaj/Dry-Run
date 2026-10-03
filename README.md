# Dry Run

Test your process before reality does.

Dry Run takes a written process (a policy, an SOP, a rulebook), converts it to
an explicit state model, injects realistic failures, and reports where people
get stuck. Every finding cites the source sentences its model was built from,
and shows the path that led there.

**Status, stated plainly**

| Part | State |
|---|---|
| Deterministic engine | Works. Covered by the test suite, one small process per finding type. |
| Three demo processes | Work. The models are hand-checked, so they need no language model. |
| Extraction from your own document (Phi-4-mini) | Experimental. See "Measured extraction results" below. |

    document -> numbered sentences -> LLM extraction -> Process model
             -> deterministic engine -> findings with evidence

The language model only parses text and answers narrow questions. It never
decides whether the process is broken. The engine does that, and the engine
makes no model calls.

## Run it

Python 3.10 or newer.

    pip install -r requirements.txt
    python -m dryrun

Open http://127.0.0.1:8000 and pick a demo. **Demo mode needs no model**: the
three demos ship as hand-checked process models in `demos/`, and the same
engine runs on them. The page is one static file with no CDN dependencies, so
it works offline.

Run the tests:

    python -m pytest -q

## Reading your own documents (needs a local model)

Uploads and pasted text are read by an open-weight model served locally by
[Ollama](https://ollama.com). Nothing leaves your machine.

    ollama pull phi4-mini
    ollama serve                      # if it is not already running
    python scripts/check_model.py     # checks the model answers, then tries the admission text
    python scripts/check_model.py samples/payment_short.txt   # a seven-sentence example
    python -m dryrun

**Model:** Microsoft Phi-4-mini (3.84B parameters, about 2.5 GB as served by
Ollama under the tag `phi4-mini`).
**Licence:** MIT, <https://huggingface.co/microsoft/Phi-4-mini-instruct/blob/main/LICENSE>.

| Variable       | Default                  | Meaning                         |
|----------------|--------------------------|---------------------------------|
| `DRYRUN_MODEL` | `phi4-mini`              | Ollama model tag to use         |
| `OLLAMA_HOST`  | `http://localhost:11434` | where Ollama listens            |
| `DRYRUN_PORT`  | `8000`                   | port for the web page           |

Only Phi-4-mini has been tried. The model sits behind one interface
(`dryrun/llm.py`), so another Ollama model that supports structured outputs can
be selected with `DRYRUN_MODEL`, for example `qwen2.5:7b` (Apache 2.0) on a
machine with more memory. `DRYRUN_NUM_CTX` (default 8192) sets the context
size; 4096 fits a 4 GB GPU better. If you
change the model, change the name and licence link above to match.

## What the engine checks

It explores every reachable configuration, where a configuration is
(state, set of facts), by breadth-first search with parent pointers, so each
finding has a witness path.

Structural problems:

| Type | Meaning |
|---|---|
| `DEAD_END` | a reachable non-final state with nothing leading out, or a step that waits on one party with no deadline and no fallback |
| `TRAPDOOR` | exits exist, but none is enabled for this person; the finding says which fact is missing and why it cannot be obtained |
| `OWNERLESS` | a step somebody must perform, with nobody named |
| `LOOP` | a reachable cycle from which no final state is reachable |
| `UNREACHABLE` | a transition that is never enabled in any configuration |
| `MISSING_RECOVERY` | a step fails, the recovery is unavailable, and only a timer to a failure remains |
| `SILENT_DEFAULT` | someone other than the subject does nothing, and a timer ends the case |

Ambiguities (reported separately, never counted as problems):
`DEADLINE_NO_CONSEQUENCE`, `FAILURE_UNADDRESSED`, and `RACE` (a timer and an
event or reversal enabled together, different targets, no stated priority).

Disruptions are generated only where the model grounds them: action failure
(transitions with `can_fail`), no response (per state and actor), delay
(transitions with a deadline), actor unavailable (non-subject actors; handled
if delegates exist), simultaneous conditions, and retry / reversal.

Confidence starts at 0.95 and is lowered when a finding rests on the text not
saying something (-0.12), on a transition no sentence was cited for (-0.15),
or on a model that was extracted automatically (-0.12). Each finding lists
which of these applied.

## Extraction

1. The text is split deterministically into sentences `c1, c2, ...` with exact
   character offsets.
2. The model sees the numbered sentences and answers four to six small
   JSON-schema-constrained questions: actors and states; transitions, ten
   sentences at a time; what the text says when a step fails; conditions (what
   each step requires, forbids, grants, revokes); and which end states are final.
3. Every id the model may return (sentence, state, actor, transition) is a
   closed list in the schema, so it cannot cite a sentence that does not exist.
4. A promised retry is modelled as a real action, so the engine can test
   whether it is actually possible in the state the failure leaves behind.
5. The output is validated: references to unknown ids are dropped, and anything
   without a sentence id is marked `inferred`.
6. On an extracted model, a finding that rests on an inferred step is labelled
   **unverified** and is not counted as a structural problem. It may be a gap
   in the model and not in the document.
7. Optional refutation pass (a button in the findings panel): for each finding
   that rests on the text not saying something, the model gets one yes/no
   question and must cite a sentence. A yes with a real sentence downgrades the
   finding to ambiguous and shows the sentence.

## Measured extraction results (Phi-4-mini, RTX 3050 4 GB, 18-clause admission text)

| Run | Time | Model calls | Known problems found (of 5) |
|---|---|---|---|
| First version | about 19 min | 15 to 20 | 0, plus false findings |
| After batching calls and capping answers | 264 s | 7 | 0, plus false findings |
| After constraining ids in the schema | 299 s | 7 | 0 |

The model confused field names with their allowed values and described most
steps with a single keyword. The schema has since been changed to remove that
confusion; that change has not yet been measured on the real model. Treat
extraction as a research problem this project has instrumented, not solved.

## Layout

    dryrun/model.py    the process model (pydantic)
    dryrun/engine.py   exploration, static checks, disruptions, findings
    dryrun/ingest.py   TXT / PDF / DOCX to text, text to numbered sentences
    dryrun/llm.py      LLMClient interface, OllamaClient, FakeLLMClient
    dryrun/extract.py  staged extraction, validation, refutation pass
    dryrun/layout.py   layered layout for the map
    dryrun/app.py      FastAPI server
    static/index.html  the whole interface: vanilla JS and SVG
    demos/*.json       three hand-checked models
    tests/             pytest suite
    scripts/check_model.py   smoke test against a real Ollama model

## Known limitations

- Extraction with the real model is unreliable (see the measured results
  above). The automated tests for extraction use a scripted fake client, so
  they check the plumbing and say nothing about model accuracy.
- A finding is correct relative to the model it was computed from. If the
  extracted model is wrong, the finding is wrong.
- The three demo documents were written for this project; their models are
  hand-checked. No real-world policy has been tested yet.
- Confidence labels come from fixed deductions, not from calibration.
- Exploration stops at 20,000 configurations.
- Delegates ("X acts when Y is away") are not extracted from uploads.
- Documents are capped at 20,000 characters, because the whole numbered text
  goes into each prompt. Scanned PDFs have no text layer and are rejected.
- The engine injects one disruption at a time. It does not combine them.
- Time is not modelled numerically: a deadline is a label, and a timer is a
  transition that fires when nothing else does.
- The model cannot be edited in the browser, and there is no version comparison.

## How this was built (disclosure)

- Built for Hacktoberfest Hack Day Chandigarh, 3 October 2026, as a solo entry.
- An AI coding assistant (Claude) was used throughout, as the event rules
  permit. The engine, extractor, server, interface and tests were produced with
  it and reviewed, run and audited by the author; `DRY_RUN_AUDIT.md` is the
  author's own audit of the extraction layer.
- The work started from an engine starter (process model, engine, three demo
  models, an early extractor) that was also AI-assisted.
- Dependencies: FastAPI, Uvicorn, Pydantic, python-multipart, pypdf,
  python-docx, pytest, httpx. See each project for its licence.
- Model: Microsoft Phi-4-mini, MIT licence, run locally through Ollama.

## Licence

MIT. See `LICENSE`.
