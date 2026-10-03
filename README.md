# Dry Run

**Test your process before reality does.**

Policies are tested by reading them. Software is tested by running it.
Dry Run lets you run a policy.

It takes a written process (a policy, an SOP, a rulebook), compiles it into an
explicit state machine with an open-weight language model, injects realistic
failures with a deterministic engine, and reports where a person gets stuck.
Each finding shows the path that led there and the source sentences behind it.

> The model interprets. The engine verifies.

Built for **Hacktoberfest Hack Day Chandigarh** (3 October 2026), challenge:
**Best Open-Source AI Project**. MIT licensed.

---

## Status, stated plainly

| Part | State |
|---|---|
| Deterministic engine | Works. Covered by the test suite, one small process per finding type. |
| Three example processes | Work. Their models are hand-checked, so they need no language model. |
| Fix and re-run | Works on any model the engine has run. |
| Extraction from your own document (Phi-4-mini) | Experimental. See [Measured extraction results](#measured-extraction-results). |

---

## The problem

Every policy is written for the happy path: the payment goes through, the
officer responds, the deadline is met. People live on the unhappy path. A
reviewer reading the document catches a bad sentence. A reviewer does not catch
two good sentences that break each other three pages apart.

## The example that shows it

From the admission demo. Each clause is reasonable on its own:

| Clause | Text |
|---|---|
| c4 | When a payment is initiated, the application status changes from 'Active' to 'Payment Pending'. |
| c6 | If a payment attempt fails, the applicant may retry from the payment page. |
| c7 | The payment page is available only to applications with the status 'Active'. |
| c8 | Applications without a confirmed payment at the end of the lock period are cancelled. |

Run it: the applicant accepts, initiates payment, and the bank declines. The
status is now 'Payment Pending'. The promised retry needs 'Active'. Nothing
restores 'Active'. The timer cancels the seat. An applicant loses admission
because of a bank error, and no single sentence is wrong.

Dry Run reports this as `MISSING_RECOVERY`, replays the path on the map, and
highlights c4, c6, c7 and c8 with the reason each one is cited.

---

## Quick start (no model needed)

Python 3.10 or newer.

```
git clone https://github.com/ipshitabhardwaj/Dry-Run
cd Dry-Run
pip install -r requirements.txt
python -m dryrun
```

Open <http://127.0.0.1:8000>.

1. Pick **University admission** under "Start here".
2. Press **Run dry test**. The retry finding opens and replays on the map.
3. Click any other finding to see its path and its sentences.
4. Inside a finding, press **Change the model and re-run** to test a change.
5. The **Model JSON** tab shows the exact model the engine ran.

The page is one static file with no CDN dependencies, so it works offline.

Run the tests:

```
python -m pytest -q
```

## Reading your own documents (local open-weight model)

Uploads (TXT, PDF, DOCX) and pasted text are compiled by a model served locally
by [Ollama](https://ollama.com). Nothing is sent to a remote service.

```
ollama pull phi4-mini
ollama serve
python scripts/check_model.py
python scripts/check_model.py samples/payment_short.txt
python -m dryrun
```

`check_model.py` confirms the model answers, runs an extraction, times each
step and prints the extracted model next to the hand-checked reference.

On Windows, set options in the same terminal before running:

```
set DRYRUN_NUM_CTX=4096
python -m dryrun
```

| Variable | Default | Meaning |
|---|---|---|
| `DRYRUN_MODEL` | `phi4-mini` | Ollama model tag |
| `OLLAMA_HOST` | `http://localhost:11434` | where Ollama listens |
| `DRYRUN_NUM_CTX` | `8192` | context size; 4096 fits a 4 GB GPU better |
| `DRYRUN_MAX_TOKENS` | `1500` | cap on each model answer |
| `DRYRUN_PORT` | `8000` | port for the web page |

**Model:** Microsoft Phi-4-mini, 3.84B parameters, about 2.5 GB as served by
Ollama under the tag `phi4-mini`.
**Model licence:** MIT,
<https://huggingface.co/microsoft/Phi-4-mini-instruct/blob/main/LICENSE>.

Only Phi-4-mini has been tried. The model sits behind one interface
(`dryrun/llm.py`), so another Ollama model with structured outputs can be
selected with `DRYRUN_MODEL`. If you change it, update the name and licence
link above.

---

## How it works

```
document
  -> numbered sentences (c1, c2, ...)          deterministic
  -> extraction by a local open-weight model   the only probabilistic step
  -> validated process model                   deterministic
  -> exploration + failure injection           deterministic
  -> findings with path and evidence           deterministic
```

1. **Split.** The text is cut into sentences with exact character offsets.
2. **Compile.** The model answers four to six small questions, each with a JSON
   schema: actors and states; transitions; what the text says when a step
   fails; conditions; which end states are final.
3. **Validate.** Unknown ids are dropped. Anything without a sentence is marked
   `inferred`.
4. **Explore.** The engine visits every reachable configuration, where a
   configuration is (state, set of facts), by breadth-first search, keeping
   parent pointers so every finding has a witness path.
5. **Inject.** Six disruption classes, generated only where the model grounds
   them: a step fails, someone does not respond, a deadline is missed, an actor
   is unavailable, two things happen at once, someone retries or withdraws.
6. **Report.** Type, explanation, scenario, path, evidence with a reason per
   sentence, what is missing, why it was flagged, and confidence.

### What the engine detects

| Type | Meaning |
|---|---|
| `DEAD_END` | the case reaches a point with no next step, or waits on one party with no deadline and no fallback |
| `TRAPDOOR` | exits exist, but each needs something this person cannot get |
| `MISSING_RECOVERY` | a recovery is promised but cannot be used when it is needed |
| `SILENT_DEFAULT` | someone other than the subject does nothing, and a timer ends the case |
| `OWNERLESS` | a step has to happen and nobody is named to do it |
| `UNREACHABLE` | a provision that is never usable in any reachable configuration |
| `LOOP` | a cycle from which no final state can be reached |

Reported separately as ambiguities, never counted as problems: a deadline with
no stated consequence, a failure the text does not address, and two events
with no stated precedence.

### Fix and re-run

For most structural problems the engine offers a change to test, for example
"what if the retry did not require status 'Active'?". Pressing it applies the
change to a copy of the model, runs the same engine again, and shows whether
the finding is gone and how the problem count moved. The engine only offers a
change it has already verified removes that finding.

This is an experiment on the model, not a recommended policy fix. Some changes
remove one problem and expose another; the interface shows that too.

### Keeping the model honest

- The model is never asked what is wrong with the process.
- It sees numbered sentences and must answer with sentence ids.
- Every id it may return (sentence, state, actor, transition) is a closed list
  in the JSON schema, so it cannot cite something that does not exist.
- A promised retry is modelled as a real action, so the engine can test whether
  it is possible in the state the failure leaves behind.
- On an extracted model, a finding that rests on an `inferred` step is labelled
  **unverified** and is not counted as a structural problem.
- Optional refutation pass: for each finding that rests on the text not saying
  something, the model is asked one yes/no question and must cite a sentence. A
  yes with a real sentence downgrades the finding to ambiguous.
- Confidence is lowered when a finding rests on absence, on an inferred step,
  or on an automatically extracted model, and the finding says which.

---

## How this differs from pasting a policy into a chatbot

| | A general chatbot | Dry Run |
|---|---|---|
| Who decides what is broken | the language model | a deterministic engine |
| Same input twice | answers can differ | identical result |
| What you get | an opinion in prose | a path you can replay, with cited sentences |
| Cross-clause bugs | found if the model happens to notice | found by exhaustive search over states and conditions |
| Checking a claim | re-read and trust | follow the path; change the cause and re-run |
| Where the text goes | usually a remote service | stays on your machine |
| Reading quality | strong on long, messy text | limited by a small local model |

The last row is a real weakness and is listed under limitations.

It is also not a workflow engine (it tests a process, it does not execute one),
not a compliance checker (it knows no regulations), and not a summariser.

## Strengths

- Findings come from search, not from a model's judgement.
- Every finding carries a witness path and sentence-level evidence.
- Findings are falsifiable: change the cause, re-run, and the finding goes.
- Runs fully offline with an MIT-licensed model on a 4 GB laptop GPU.
- Small and readable: about 3,500 lines including tests and the interface.
- No build step, no CDN, no API key.

## Known limitations

- **Extraction with the real model is unreliable.** See the measured results
  below. The automated extraction tests use a scripted fake client, so they
  check the plumbing and say nothing about model accuracy.
- **A finding is correct relative to its model.** If the extracted model is
  wrong, the finding is wrong.
- **The three example documents were written for this project**; their models
  are hand-checked. No real-world policy has been tested yet.
- The engine injects one disruption at a time and does not combine them.
- Time is not modelled numerically: a deadline is a label, and a timer is a
  transition that fires when nothing else does.
- Confidence labels come from fixed deductions, not from calibration.
- Exploration stops at 20,000 configurations.
- Documents are capped at 20,000 characters. Scanned PDFs have no text layer
  and are rejected.
- Delegates ("X acts when Y is away") are not extracted from uploads.
- The model cannot be edited freely in the browser; only the offered changes
  can be tested.

## Measured extraction results

Phi-4-mini on an RTX 3050 (4 GB), 18-clause admission text, compared with the
hand-checked model of the same text (5 known structural problems).

| Run | Time | Model calls | Known problems found |
|---|---|---|---|
| First version | about 19 min | 15 to 20 | 0 of 5, plus false findings |
| After batching calls and capping answers | 264 s | 7 | 0 of 5, plus false findings |
| After constraining ids in the schema | 299 s | 7 | 0 of 5 |

In those runs the model confused field names with their allowed values and
described most steps with a single keyword. The schema has since been changed
to remove that confusion. That change has not yet been measured on the real
model. Treat extraction as a problem this project has instrumented, not solved.

---

## Tests

```
python -m pytest -q
```

46 tests:

- one small fixture process per behaviour: a valid process with zero findings,
  missing transition, ownerless step, silent default, infinite loop,
  unreachable provision, missing recovery, trapdoor, deadline with no
  consequence, simultaneous events;
- the admission demo yields exactly its five structural problems, each citing
  the expected clauses;
- changing the model removes the matching finding, so findings are not
  hardcoded;
- every counterfactual the engine offers removes its finding;
- extraction plumbing with a fake model client, including the retry case end
  to end;
- the HTTP API, and the Ollama request format against a local stub;
- the interface file makes no external requests.

A GitHub Actions workflow runs the suite on every push.

## Project layout

```
dryrun/model.py      the process model (pydantic)
dryrun/engine.py     exploration, static checks, disruptions, findings, counterfactuals
dryrun/ingest.py     TXT / PDF / DOCX to text, text to numbered sentences
dryrun/llm.py        LLMClient interface, OllamaClient, FakeLLMClient
dryrun/extract.py    staged extraction, validation, refutation pass
dryrun/layout.py     layered layout for the map
dryrun/app.py        FastAPI server
static/index.html    the whole interface: vanilla JS and SVG
demos/*.json         three hand-checked models
samples/             a short document for trying extraction
scripts/check_model.py   smoke test and timing against a real Ollama model
tests/               pytest suite
DRY_RUN_AUDIT.md     the author's audit of the extraction layer
```

## Technology and licences

| Component | Used for | Licence |
|---|---|---|
| Phi-4-mini (Microsoft) | natural language to process model | MIT |
| Ollama | serving the model locally | MIT |
| FastAPI | web server | MIT |
| Uvicorn | ASGI server | BSD-3-Clause |
| Pydantic | process model and validation | MIT |
| python-multipart | file uploads | Apache-2.0 |
| pypdf | PDF text | BSD-3-Clause |
| python-docx | DOCX text | MIT |
| pytest, httpx | tests | MIT, BSD-3-Clause |

Licences are as published by each project; check them before redistribution.
No proprietary API is needed for any part of the flow.

## How this was built (disclosure)

- Solo entry, built for Hacktoberfest Hack Day Chandigarh on 3 October 2026.
- An AI coding assistant (Claude) was used throughout, as the event rules
  permit. The engine, extractor, server, interface and tests were produced with
  it and were run, reviewed and audited by the author. `DRY_RUN_AUDIT.md` is
  the author's own audit of the extraction layer.
- The work started from an engine starter (process model, engine, three demo
  models, an early extractor) that was also AI-assisted and was created during
  the event, after hacking began at 10:30 AM. No code from before the event is
  included.

## What comes next

1. Measure the current schema on Phi-4-mini and publish the numbers.
2. A benchmark that scores extracted models against the hand-checked ones.
3. One real public policy, with a finding a human has verified.
4. Free editing of the model in the browser, with before and after comparison.
5. Combined disruptions and numeric time in the engine.

## Licence

MIT. See `LICENSE`.
