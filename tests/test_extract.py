"""The extractor is tested with FakeLLMClient, so these tests check the plumbing:
schema-constrained calls, validation, sentence offsets, follow-up questions and
the refutation pass. They say nothing about how well a real model extracts."""
from dryrun.engine import dry_run
from dryrun.extract import extract_process, refute
from dryrun.ingest import read_upload, split_sentences, unwrap
from dryrun.llm import FakeLLMClient, parse_json

TEXT = """Refund procedure
1. A customer requests a refund by email.
2. Support reviews the request.
3. Approved refunds are paid by the bank transfer team once a signed refund form is on file.
4. Rejected requests are closed."""


def fake(refute_answer=None):
    return FakeLLMClient([
        ("Part 1. List every person", {
            "subject": "Customer",
            "actors": [{"name": "Customer", "sentences": ["c2"]},
                       {"name": "Support", "sentences": ["c3"]},
                       {"name": "Bank transfer team", "sentences": ["c4"]}],
            "states": [
                {"label": "New", "kind": "start", "outcome": None, "sentences": ["c2"]},
                {"label": "Under review", "kind": "normal", "outcome": None, "sentences": ["c3"]},
                {"label": "Approved", "kind": "normal", "outcome": None, "sentences": ["c4"]},
                {"label": "Paid", "kind": "terminal", "outcome": "success", "sentences": ["c4"]},
                {"label": "Closed", "kind": "normal", "outcome": None, "sentences": ["c5", "c77"]}]}),
        ("For each way these sentences", {"transitions": [
            {"from": "new", "to": "under_review", "action": "request a refund", "actor": "customer",
             "trigger": "action", "sentences": ["c2"]},
            {"from": "under_review", "to": "approved", "action": "approve the request",
             "actor": "support", "sentences": ["[C3]"]},
            {"from": "under_review", "to": "closed", "action": "reject the request",
             "actor": "support", "sentences": []},                       # no sentence -> inferred
            {"from": "approved", "to": "paid", "action": "pay the refund", "actor": "bank_transfer_team",
             "can_fail": True, "failure_label": "the transfer fails", "sentences": ["c4"]},
            {"from": "approved", "to": "nowhere", "action": "bogus", "sentences": ["c99"]}]}),
        ("Part 1. List the named conditions", {
            "facts": [{"name": "Signed refund form", "sentences": ["c4"]}],
            "rules": [
                {"transition": "t4", "requires": ["Signed refund form", "made up fact"], "forbids": [],
                 "grants": [], "revokes": [], "sentences": ["c4"]},
                {"transition": "t404", "requires": ["Signed refund form"], "sentences": ["c4"]}]}),
        ("For each failure above", {"failures": [
            {"transition": "t4", "sentence": None, "goes_to": None, "who": None, "retry": "unspecified"}]}),
        ("is the case finished", {"states": [{"state": "Closed", "final": True, "outcome": "failure"}]}),
        ("Question: does any sentence say",
         refute_answer or (lambda user: {"answer": "yes", "sentence": "c5"}
                           if "transfer fails" in user else {"answer": "no", "sentence": None})),
    ])


def test_extraction_builds_a_valid_model_with_exact_spans():
    process, log = extract_process(TEXT, fake())
    assert process.origin == "llm" and process.subject == "customer"
    assert [t.id for t in process.transitions] == ["t1", "t2", "t3", "t4"]  # bogus one dropped
    assert process.state("closed").kind == "terminal"       # settled by a narrow question
    assert process.state("closed").evidence == ["c5"]       # unknown sentence id dropped
    assert process.transition("t2").evidence == ["c3"]      # "[C3]" normalised
    for s in process.sentences:
        assert TEXT[s.start:s.end] == s.text
    assert any("narrow questions" in line for line in log)


def test_validation_drops_unknown_ids_and_marks_uncited_as_inferred():
    process, log = extract_process(TEXT, fake())
    assert process.transition("t3").inferred and not process.transition("t1").inferred
    assert process.transition("t4").requires == ["signed_refund_form"]   # made-up fact dropped
    assert [f.id for f in process.facts] == ["signed_refund_form"]
    assert process.initial_facts == ["signed_refund_form"]
    assert any("unknown ids dropped" in line for line in log)
    # an inferred transition lowers the confidence of findings that rest on it
    report = dry_run(process)
    low = [f for f in report["findings"] if "t3" in f["transitions"]]
    assert low and all("a transition no sentence was cited for" in f["rests_on"] for f in low)


def test_every_call_carries_a_json_schema():
    llm = fake()
    process, _ = extract_process(TEXT, llm)
    refute(process, dry_run(process), llm)
    schemas = [c["schema"] for c in llm.log]
    assert all(s.get("type") == "object" and s.get("properties") for s in schemas)
    build = [s for s in schemas if "answer" not in s["properties"]]
    assert len(build) == 5                                  # five calls to build a model
    # ids are closed sets: the model cannot emit a sentence, state or actor that does not exist
    ids = ["c1", "c2", "c3", "c4", "c5"]
    assert build[0]["properties"]["states"]["items"]["properties"]["sentences"]["items"]["enum"] == ids
    tr = build[1]["properties"]["transitions"]["items"]["properties"]
    assert tr["from"]["enum"] == ["new", "under_review", "approved", "paid", "closed"]
    assert tr["actor"]["enum"] == ["customer", "support", "bank_transfer_team", None]
    assert schemas[-1]["properties"]["sentence"]["enum"] == ids + [None]
    # the model only ever sees numbered sentences
    assert "[c2] A customer requests a refund by email." in llm.log[0]["user"]


def test_refutation_downgrades_an_absence_finding_and_shows_the_sentence():
    llm = fake()
    process, _ = extract_process(TEXT, llm)
    report = dry_run(process)
    gap = next(f for f in report["findings"] if "transfer fails" in f["title"])
    assert gap["absence_based"] and gap["status"] == "confirmed"
    before = report["summary"]["ambiguous_findings"]
    was_problem = gap["severity"] == "problem"
    report = refute(process, report, llm)
    gap = next(f for f in report["findings"] if "transfer fails" in f["title"])
    assert gap["severity"] == "ambiguous" and gap["status"] == "downgraded"
    assert gap["confidence_label"] == "low" and gap["refuted_by"] == "c5"
    assert gap["evidence"][-1]["text"] == "Rejected requests are closed."
    assert report["summary"]["ambiguous_findings"] == before + (1 if was_problem else 0)
    others = [f for f in report["findings"] if f["absence_based"] and f is not gap]
    assert others and all(f["status"] == "checked" for f in others)


def test_refutation_ignores_a_yes_without_a_real_sentence():
    llm = fake(refute_answer={"answer": "yes", "sentence": "c999"})
    process, _ = extract_process(TEXT, llm)
    report = refute(process, dry_run(process), llm)
    assert all(f["status"] in ("checked", "confirmed") for f in report["findings"])


def test_json_parsing_is_tolerant():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}


def test_sentence_ids_are_stable():
    assert [s.id for s in split_sentences(TEXT)] == ["c1", "c2", "c3", "c4", "c5"]


def test_pdf_line_wraps_are_rejoined():
    wrapped = "1. The applicant must pay the\nfee within two days.\n2. Late payments are\nrejected."
    assert unwrap(wrapped) == ("1. The applicant must pay the fee within two days.\n"
                               "2. Late payments are rejected.")


def test_reads_txt_pdf_and_docx(tmp_path):
    import io
    import docx
    assert read_upload("a.txt", TEXT.encode()) == TEXT
    d = docx.Document()
    for line in TEXT.split("\n"):
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    assert read_upload("a.docx", buf.getvalue()) == TEXT
    from tests.pdfutil import make_pdf
    text = read_upload("a.pdf", make_pdf(TEXT.split("\n")))
    assert "A customer requests a refund by email." in text
    assert len(split_sentences(text)) == 5


RETRY_TEXT = """1. The applicant pays the fee through the payment page.
2. When a payment is initiated, the status changes from Active to Pending.
3. Once the bank confirms the payment, the applicant is admitted.
4. If a payment fails, the applicant may retry from the payment page.
5. The payment page is available only to applications with the status Active.
6. Applications without a confirmed payment after 48 hours are cancelled."""


def retry_fake(rules):
    return FakeLLMClient([
        ("Part 1. List every person", {
            "subject": "Applicant",
            "actors": [{"name": "Applicant", "sentences": ["c1"]}, {"name": "Bank", "sentences": ["c3"]}],
            "states": [
                {"label": "Offer", "kind": "start", "outcome": None, "sentences": ["c1"]},
                {"label": "Payment pending", "kind": "normal", "outcome": None, "sentences": ["c2"]},
                {"label": "Admitted", "kind": "terminal", "outcome": "success", "sentences": ["c3"]},
                {"label": "Cancelled", "kind": "terminal", "outcome": "failure", "sentences": ["c6"]}]}),
        ("For each way these sentences", {"transitions": [
            {"from": "Offer", "to": "payment_pending", "action": "initiate payment", "actor": "the Applicant",
             "trigger": "action", "sentences": ["1", "sentence 2"]},
            {"from": "payment_pending", "to": "admitted", "action": "bank confirms payment", "actor": "The Bank",
             "trigger": "event", "can_fail": True, "failure_label": "the payment fails", "sentences": ["c03"]},
            {"from": "payment_pending", "to": "cancelled", "action": "cancel after 48 hours", "actor": None,
             "trigger": "timer", "sentences": [6]}]}),
        ("For each failure above", {"failures": [
            {"transition": "t2", "sentence": "c4", "goes_to": None, "who": "applicant", "retry": "allowed"}]}),
        ("Part 1. List the named conditions", rules),
    ])


def test_lenient_matching_of_sentence_ids_actors_and_states():
    process, log = extract_process(RETRY_TEXT, retry_fake({"facts": [], "rules": []}))
    t1, t2, t3 = process.transitions[:3]
    assert (t1.source, t1.actor, t1.evidence) == ("offer", "applicant", ["c1", "c2"])
    assert (t2.actor, t2.evidence) == ("bank", ["c3"]) and t3.evidence == ["c6"]
    assert not any(t.inferred for t in process.transitions)
    assert not any("unknown ids" in line for line in log)


def test_a_promised_retry_becomes_an_action_the_engine_can_test():
    """The admission bug, end to end through extraction: retry is promised (c4) but needs
    a status (c5) that initiating payment removed (c2)."""
    rules = {"facts": [{"name": "status Active", "sentences": ["c2", "c5"]}],
             "rules": [{"transition": "t1", "requires": ["status Active"], "forbids": [], "grants": [],
                        "revokes": ["status Active"], "sentences": ["c2", "c5"]},
                       {"transition": "t4", "requires": ["status Active"], "forbids": [], "grants": [],
                        "revokes": [], "sentences": ["c5"]}]}
    process, _ = extract_process(RETRY_TEXT, retry_fake(rules))
    retry = process.transition("t4")
    assert (retry.kind, retry.on_failure_of, retry.actor, retry.source, retry.target) == (
        "failure_handler", "t2", "applicant", "payment_pending", "payment_pending")
    assert retry.requires == ["status_active"] and retry.evidence == ["c4", "c5"]
    report = dry_run(process)
    f = next(f for f in report["findings"] if f["type"] == "MISSING_RECOVERY")
    assert f["severity"] == "problem"
    assert {e["sentence"] for e in f["evidence"]} >= {"c2", "c4", "c5", "c6"}
    assert "OWNERLESS" not in [x["type"] for x in report["findings"]]
    # without the prerequisite the retry works and nothing is flagged
    ok, _ = extract_process(RETRY_TEXT, retry_fake({"facts": [], "rules": []}))
    assert "MISSING_RECOVERY" not in [x["type"] for x in dry_run(ok)["findings"]]


def test_findings_resting_on_uncited_steps_are_unverified_not_problems():
    process, _ = extract_process(TEXT, fake())
    report = dry_run(process)
    shaky = [f for f in report["findings"] if "t3" in f["transitions"] and f["type"] in
             ("DEAD_END", "TRAPDOOR", "OWNERLESS", "UNREACHABLE", "LOOP", "MISSING_RECOVERY", "SILENT_DEFAULT")]
    assert shaky and all(f["severity"] == "unverified" for f in shaky)
    assert report["summary"]["unverified_findings"] == len(shaky)
    assert report["summary"]["structural_findings"] == sum(f["severity"] == "problem" for f in report["findings"])


def test_a_quoted_sentence_is_mapped_back_to_its_id():
    from dryrun.extract import _ids
    known = {"c1": "Refunds are paid within 5 days.", "c2": "Rejected requests are closed."}
    assert _ids("Refunds are paid within 5 days.", known) == ["c1"]
    assert _ids(["rejected requests are closed", "c1", "nonsense that is long enough"], known) == ["c2", "c1"]
