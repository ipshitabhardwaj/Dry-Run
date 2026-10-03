# Dry Run

Test your process before reality does.

Dry Run takes a written process (a policy, an SOP, a rulebook), converts it to
an explicit state model, injects realistic failures, and reports where people
get stuck. Every finding cites the exact source sentences it rests on.

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
    python scripts/check_model.py     # proves the model answers, then tries the admission text
    python -m dryrun

**Model:** Microsoft Phi-4-mini (3.8B parameters, about 2.5 GB as served by
Ollama under the tag `phi4-mini`).
**Licence:** MIT, <https://huggingface.co/microsoft/Phi-4-mini-instruct/blob/main/LICENSE>.

| Variable       | Default                  | Meaning                         |
|----------------|--------------------------|---------------------------------|
| `DRYRUN_MODEL` | `phi4-mini`              | Ollama model tag to use         |
| `OLLAMA_HOST`  | `http://localhost:11434` | where Ollama listens            |
| `DRYRUN_PORT`  | `8000`                   | port for the web page           |

Any Ollama model that supports structured outputs will work, for example
`DRYRUN_MODEL=qwen2.5:7b` (Apache 2.0) on a machine with more memory. If you
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
2. The model sees the numbered sentences and must cite ids, never quotes. It
   is called in small JSON-schema-constrained steps: actors and states; then
   transitions, eight sentences at a time; then facts and what each transition
   requires, forbids, grants or revokes; then narrow follow-ups (what happens
   when this step fails, who stands in for this actor, is this state final).
3. The output is validated: references to unknown ids are dropped, and
   anything without a sentence id is marked `inferred`.
4. Refutation pass: for each finding that rests on absence, the model gets one
   yes/no question ("does any sentence say what happens when X fails? cite
   it"). A yes with a real sentence id downgrades the finding to ambiguous and
   shows the sentence. A yes without a valid citation changes nothing.

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

- Extraction quality with a real model has not been measured. The pipeline is
  tested with a scripted fake client only. Expect to tune the prompts in
  `dryrun/extract.py` after running `scripts/check_model.py`.
- Documents are capped at 20,000 characters, because the whole numbered text
  goes into each prompt. Scanned PDFs have no text layer and are rejected.
- The engine injects one disruption at a time. It does not combine them.
- Time is not modelled numerically: a deadline is a label, and a timer is a
  transition that fires when nothing else does.
- The model cannot be edited in the browser, and there is no version comparison.

## Licence

MIT. See `LICENSE`.
