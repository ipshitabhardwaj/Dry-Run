"""First thing to run once Ollama is installed.

    python scripts/check_model.py

1. checks Ollama is up and the model in DRYRUN_MODEL is pulled;
2. asks one schema-constrained question, to prove structured output works;
3. extracts the admission demo text with the real model, runs the engine on the
   result, and prints it next to what the hand-checked model yields, so you can
   see how far the extractor is from the reference.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dryrun.demos import load_demo            # noqa: E402
from dryrun.engine import dry_run             # noqa: E402
from dryrun.extract import extract_process, schema_yes_no  # noqa: E402
from dryrun.llm import LLMError, OllamaClient  # noqa: E402


def main() -> int:
    llm = OllamaClient()
    st = llm.status()
    print(f"Ollama at {st['host']}: {'up' if st['reachable'] else 'NOT reachable'}")
    if not st["reachable"]:
        print("Start it with:  ollama serve")
        return 1
    print(f"Model {st['model']}: {'pulled' if st['model_present'] else 'NOT pulled'}")
    if not st["model_present"]:
        print(f"Pull it with:  ollama pull {st['model']}")
        return 1
    try:
        t = time.time()
        r = llm.ask("Answer with one JSON object.", "[c1] Refunds are paid within 5 days.\n"
                    "Question: does any sentence say when refunds are paid?", schema_yes_no(["c1"]))
        print(f"Structured answer in {time.time() - t:.1f}s: {r}")
        demo = load_demo("1_admission")
        want = sorted(f["type"] for f in dry_run(demo)["findings"] if f["severity"] == "problem")
        t = time.time()
        clock = {"t": time.time()}

        def step(msg):
            now = time.time()
            print(f"  [{now - clock['t']:5.0f}s since last step] {msg}", flush=True)
            clock["t"] = now

        text = demo.text
        if len(sys.argv) > 1:   # python scripts/check_model.py samples/payment_short.txt
            text = Path(sys.argv[1]).read_text(encoding="utf-8")
        process, log = extract_process(text, llm, title=demo.title, progress=step)
        report = dry_run(process)
    except (LLMError, ValueError) as e:
        print("FAILED:", e)
        return 1
    print(f"\nExtraction took {time.time() - t:.0f}s: {', '.join(log)}")
    print(f"states {len(process.states)} (reference {len(demo.states)}), "
          f"transitions {len(process.transitions)} (reference {len(demo.transitions)})")
    got = sorted(f["type"] for f in report["findings"] if f["severity"] == "problem")
    for st in process.states:
        print(f"  state {st.id} | {st.kind} {st.outcome or ''} | {st.evidence}")
    for t in process.transitions:
        print(f"  {t.id:4} {t.source} -> {t.target} | {t.actor} | {t.trigger}/{t.kind} | {t.action} "
              f"| req={t.requires} forb={t.forbids} grants={t.grants} rev={t.revokes} "
              f"| {t.evidence}{' INFERRED' if t.inferred else ''}")
    print("problems from the hand-checked model:", want)
    print("problems from the extracted model:   ", got)
    for f in report["findings"]:
        print(f"  {f['id']} {f['severity']:9} {f['type']:24} {f['confidence']:.2f} "
              f"{[e['sentence'] for e in f['evidence']]} {f['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
