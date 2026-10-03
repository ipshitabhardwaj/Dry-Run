"""Each test builds a tiny process by hand and checks one behaviour of the engine."""
from dryrun.demos import build_process, load_demo
from dryrun.engine import dry_run


def make(clauses, states, transitions, facts=(), initial=(), subject="user"):
    return build_process({
        "title": "test", "clauses": list(clauses),
        "actors": [{"id": "user", "name": "User"}, {"id": "staff", "name": "Reviewer"},
                   {"id": "backup", "name": "Backup reviewer"}],
        "subject": subject, "start": states[0]["id"], "states": list(states),
        "transitions": list(transitions), "facts": list(facts), "initial_facts": list(initial),
    }, origin="manual")


def types(report, severity=None):
    return [f["type"] for f in report["findings"] if severity is None or f["severity"] == severity]


S = lambda id, **kw: {"id": id, "label": id.replace("_", " ").title(), **kw}
DONE = S("done", kind="terminal", outcome="success")
FAIL = S("failed", kind="terminal", outcome="failure")


def test_valid_process_has_zero_findings():
    p = make(
        ["The user applies.",
         "The reviewer approves within 5 days, otherwise the request escalates.",
         "An escalated request may be approved by the reviewer or by the backup reviewer."],
        [S("new", kind="start"), S("review"), S("escalated"), DONE],
        [{"id": "a", "from": "new", "to": "review", "actor": "user", "action": "apply", "evidence": ["c1"]},
         {"id": "b", "from": "review", "to": "done", "actor": "staff", "action": "approve",
          "deadline": "within 5 days", "evidence": ["c2"]},
         {"id": "c", "from": "review", "to": "escalated", "trigger": "timer",
          "action": "escalate the request", "evidence": ["c2"]},
         {"id": "d", "from": "escalated", "to": "done", "actor": "backup",
          "action": "approve as backup", "evidence": ["c3"]},
         {"id": "e", "from": "escalated", "to": "done", "actor": "staff",
          "action": "approve after escalation", "evidence": ["c3"]}])
    r = dry_run(p)
    assert r["findings"] == []
    assert r["summary"]["structural_findings"] == 0 and r["summary"]["problem"] == 0
    assert r["summary"]["scenarios"] == r["summary"]["completed"] > 3
    assert {s["cls"] for s in r["scenarios"]} >= {"BASELINE", "NO_RESPONSE", "ACTOR_UNAVAILABLE", "DELAY"}


def test_delegate_handles_an_unavailable_actor():
    p = make(["The user applies.", "The reviewer, or the backup reviewer in their absence, approves."],
             [S("new", kind="start"), S("review"), DONE],
             [{"id": "a", "from": "new", "to": "review", "actor": "user", "action": "apply", "evidence": ["c1"]},
              {"id": "b", "from": "review", "to": "done", "actor": "staff", "delegates": ["backup"],
               "action": "approve", "evidence": ["c2"]}])
    r = dry_run(p)
    s = next(s for s in r["scenarios"] if s["cls"] == "ACTOR_UNAVAILABLE")
    assert s["status"] == "completed" and "Backup reviewer" in s["note"]


def test_deadline_with_no_consequence_is_ambiguous_not_a_problem():
    p = make(["The user must pay within 2 days."],
             [S("new", kind="start"), DONE],
             [{"id": "a", "from": "new", "to": "done", "actor": "user", "action": "pay",
               "deadline": "within 2 days", "evidence": ["c1"]}])
    r = dry_run(p)
    assert types(r, "problem") == [] and types(r, "ambiguous") == ["DEADLINE_NO_CONSEQUENCE"]
    assert r["findings"][0]["confidence"] < 0.95 and r["findings"][0]["absence_based"]


def test_timer_racing_an_event_without_priority_is_ambiguous():
    base = [{"id": "a", "from": "new", "to": "done", "actor": "staff", "trigger": "event",
             "action": "payment clears", "evidence": ["c1"]},
            {"id": "b", "from": "new", "to": "failed", "trigger": "timer",
             "action": "expire", "evidence": ["c2"]}]
    states = [S("new", kind="start"), DONE, FAIL]
    clauses = ["Payment clears the order.", "Unpaid orders expire."]
    assert "RACE" in types(dry_run(make(clauses, states, base)), "ambiguous")
    ranked = [{**base[0], "priority": 1}, {**base[1], "priority": 2}]
    assert "RACE" not in types(dry_run(make(clauses, states, ranked)))


def test_findings_are_deduplicated_across_scenarios_and_carry_a_witness_path():
    r = dry_run(load_demo("1_admission"))
    keys = [(f["type"], tuple(f["states"]), tuple(f["transitions"])) for f in r["findings"]]
    assert len(keys) == len(set(keys))
    trap = next(f for f in r["findings"] if f["type"] == "TRAPDOOR")
    assert len(trap["scenarios"]) == 3          # withdrawing from three different states
    p = load_demo("1_admission")
    state = p.start                              # the witness path is a real walk from the start
    for tid in trap["path"]:
        t = p.transition(tid)
        assert t.source == state
        state = t.target
    assert state == "withdrawn"
    for f in r["findings"]:
        assert f["why"] and f["missing"] and f["explanation"] and 0 < f["confidence"] <= 1


def test_missing_transition_is_a_dead_end():
    p = make(["The user applies.", "Applications are checked."],
             [S("new", kind="start"), S("checking"), DONE],
             [{"id": "a", "from": "new", "to": "checking", "actor": "user", "action": "apply",
               "evidence": ["c1"]}])
    r = dry_run(p)
    f = next(f for f in r["findings"] if f["type"] == "DEAD_END")
    assert f["states"] == ["checking"]
    assert f["evidence"] and f["evidence"][0]["text"] == "The user applies."


def test_ownerless_step():
    p = make(["The user applies.", "Applications will be reviewed."],
             [S("new", kind="start"), S("review"), DONE],
             [{"id": "a", "from": "new", "to": "review", "actor": "user", "action": "apply", "evidence": ["c1"]},
              {"id": "b", "from": "review", "to": "done", "actor": None, "action": "review the application",
               "evidence": ["c2"]}])
    f = next(f for f in dry_run(p)["findings"] if f["type"] == "OWNERLESS")
    assert f["transitions"] == ["b"]
    assert f["evidence"][0]["sentence"] == "c2"


def test_silent_default_when_someone_else_does_nothing():
    p = make(["The user applies.", "The reviewer decides.", "Undecided requests expire after 10 days."],
             [S("new", kind="start"), S("review"), DONE, FAIL],
             [{"id": "a", "from": "new", "to": "review", "actor": "user", "action": "apply", "evidence": ["c1"]},
              {"id": "b", "from": "review", "to": "done", "actor": "staff", "action": "approve", "evidence": ["c2"]},
              {"id": "c", "from": "review", "to": "failed", "trigger": "timer",
               "action": "expire the request", "evidence": ["c3"]}])
    r = dry_run(p)
    f = next(f for f in r["findings"] if f["type"] == "SILENT_DEFAULT")
    assert {e["sentence"] for e in f["evidence"]} == {"c2", "c3"}
    assert len(f["scenarios"]) >= 2  # no response + unavailable


def test_subject_missing_their_own_deadline_is_not_a_silent_default():
    p = make(["The user must pay within 2 days.", "Unpaid orders are cancelled."],
             [S("new", kind="start"), DONE, FAIL],
             [{"id": "a", "from": "new", "to": "done", "actor": "user", "action": "pay",
               "deadline": "within 2 days", "evidence": ["c1"]},
              {"id": "b", "from": "new", "to": "failed", "trigger": "timer", "action": "cancel the order",
               "evidence": ["c2"]}])
    assert types(dry_run(p), "problem") == []


def test_infinite_loop():
    p = make(["The user applies.", "A sends it to B.", "B sends it back to A."],
             [S("new", kind="start"), S("desk_a"), S("desk_b"), DONE],
             [{"id": "a", "from": "new", "to": "desk_a", "actor": "user", "action": "apply", "evidence": ["c1"]},
              {"id": "b", "from": "desk_a", "to": "desk_b", "actor": "staff", "action": "forward", "evidence": ["c2"]},
              {"id": "c", "from": "desk_b", "to": "desk_a", "actor": "backup", "action": "return", "evidence": ["c3"]},
              {"id": "d", "from": "desk_b", "to": "done", "actor": "backup", "action": "approve",
               "requires": ["stamp"], "evidence": ["c3"]}],
             facts=[{"id": "stamp", "label": "a stamp"}])
    r = dry_run(p)
    f = next(f for f in r["findings"] if f["type"] == "LOOP")
    assert set(f["states"]) == {"desk_a", "desk_b"}
    assert "UNREACHABLE" not in types(r)  # the blocked exit is explained by the loop, not double-counted


def test_unreachable_provision():
    p = make(["The user applies.", "Late requests are cancelled and archived.",
              "Cancelled requests may be reopened if they have not been archived."],
             [S("new", kind="start"), FAIL, DONE, S("reopened")],
             [{"id": "a", "from": "new", "to": "done", "actor": "user", "action": "apply", "evidence": ["c1"]},
              {"id": "b", "from": "new", "to": "failed", "trigger": "timer", "action": "cancel and archive",
               "grants": ["archived"], "evidence": ["c2"]},
              {"id": "c", "from": "failed", "to": "reopened", "actor": "user", "action": "reopen",
               "forbids": ["archived"], "evidence": ["c3"]},
              {"id": "d", "from": "reopened", "to": "done", "actor": "staff", "action": "approve",
               "evidence": ["c3"]}],
             facts=[{"id": "archived", "label": "archived"}])
    f = next(f for f in dry_run(p)["findings"] if f["type"] == "UNREACHABLE")
    assert f["transitions"] == ["c"]
    assert {e["sentence"] for e in f["evidence"]} == {"c2", "c3"}


def test_recovery_missing():
    p = make(["The user uploads a file.", "Uploads are processed.", "Unprocessed uploads are deleted after a day."],
             [S("new", kind="start"), S("uploading"), DONE, FAIL],
             [{"id": "a", "from": "new", "to": "uploading", "actor": "user", "action": "upload", "evidence": ["c1"]},
              {"id": "b", "from": "uploading", "to": "done", "trigger": "event", "action": "processing succeeds",
               "can_fail": True, "failure_label": "processing fails", "evidence": ["c2"]},
              {"id": "c", "from": "uploading", "to": "failed", "trigger": "timer", "action": "delete the upload",
               "evidence": ["c3"]}])
    f = next(f for f in dry_run(p)["findings"] if f["type"] == "MISSING_RECOVERY")
    assert f["states"][0] == "uploading"


def test_recovery_present_is_not_flagged():
    p = make(["The user uploads a file.", "Uploads are processed.", "If processing fails the user may upload again."],
             [S("new", kind="start"), S("uploading"), DONE],
             [{"id": "a", "from": "new", "to": "uploading", "actor": "user", "action": "upload", "evidence": ["c1"]},
              {"id": "b", "from": "uploading", "to": "done", "trigger": "event", "action": "processing succeeds",
               "can_fail": True, "failure_label": "processing fails", "evidence": ["c2"]},
              {"id": "c", "from": "uploading", "to": "new", "actor": "user", "kind": "failure_handler",
               "on_failure_of": "b", "action": "upload again", "evidence": ["c3"]}])
    assert types(dry_run(p), "problem") == []


def test_trapdoor_from_a_fact_nobody_can_obtain():
    p = make(["The user may withdraw.", "Refunds need a clearance slip.", "Slips are given to members."],
             [S("new", kind="start"), S("withdrawn"), S("refunded", kind="terminal", outcome="neutral"), DONE],
             [{"id": "j", "from": "new", "to": "done", "actor": "user", "action": "join", "evidence": ["c3"]},
              {"id": "w", "from": "new", "to": "withdrawn", "actor": "user", "kind": "reversal",
               "action": "withdraw", "evidence": ["c1"]},
              {"id": "r", "from": "withdrawn", "to": "refunded", "actor": "staff", "action": "refund",
               "requires": ["slip"], "evidence": ["c2"]},
              {"id": "s", "from": "done", "to": "done", "actor": "staff", "action": "issue a slip",
               "grants": ["slip"], "evidence": ["c3"]}],
             facts=[{"id": "slip", "label": "a clearance slip"}])
    r = dry_run(p)
    f = next(f for f in r["findings"] if f["type"] == "TRAPDOOR")
    assert f["states"] == ["withdrawn"]
    assert {"c1", "c2", "c3"} <= {e["sentence"] for e in f["evidence"]}


def test_every_finding_cites_real_source_text():
    for demo in ("1_admission", "2_expenses", "3_travel"):
        p = load_demo(demo)
        for f in dry_run(p)["findings"]:
            assert f["evidence"], (demo, f["title"])
            for e in f["evidence"]:
                s = p.sentence(e["sentence"])
                assert p.text[s.start:s.end] == e["text"]


def test_admission_demo_yields_exactly_the_five_problems_with_their_clauses():
    r = dry_run(load_demo("1_admission"))
    problems = {f["type"]: f for f in r["findings"] if f["severity"] == "problem"}
    assert len([f for f in r["findings"] if f["severity"] == "problem"]) == 5
    cites = lambda t: {int(e["sentence"][1:]) for e in problems[t]["evidence"]}
    assert cites("MISSING_RECOVERY") >= {4, 6, 7, 8}
    assert cites("SILENT_DEFAULT") >= {9, 12}
    assert cites("OWNERLESS") == {11}
    assert cites("TRAPDOOR") == {14, 15, 16}
    assert cites("UNREACHABLE") == {8, 17}
    assert set(problems) == {"MISSING_RECOVERY", "SILENT_DEFAULT", "OWNERLESS", "TRAPDOOR", "UNREACHABLE"}
    assert problems["MISSING_RECOVERY"]["states"] == ["pay_pending", "cancelled"]
    assert "status 'Active'" in " ".join(problems["MISSING_RECOVERY"]["why"])
    assert problems["OWNERLESS"]["transitions"] == ["t_resolve"]
    assert problems["TRAPDOOR"]["states"] == ["withdrawn"]
    assert problems["UNREACHABLE"]["transitions"] == ["t_appeal"]
    assert r["summary"]["structural_findings"] == 5
    # everything else the engine reports here is an ambiguity, never a problem
    assert {f["type"] for f in r["findings"] if f["severity"] == "ambiguous"} <= {
        "DEADLINE_NO_CONSEQUENCE", "FAILURE_UNADDRESSED", "RACE"}


def test_findings_are_not_hardcoded_fixing_the_text_removes_them():
    """Give the retry no status requirement and name an owner: two findings disappear."""
    import json
    from dryrun.demos import DEMO_DIR
    raw = json.loads((DEMO_DIR / "1_admission.json").read_text(encoding="utf-8"))
    for t in raw["transitions"]:
        if t["id"] == "t_retry":
            t["requires"] = []
        if t["id"] == "t_resolve":
            t["actor"] = "applicant"
    got = types(dry_run(build_process(raw)), "problem")
    assert sorted(got) == ["SILENT_DEFAULT", "TRAPDOOR", "UNREACHABLE"]


def test_engine_is_deterministic():
    a, b = dry_run(load_demo("3_travel")), dry_run(load_demo("3_travel"))
    assert a == b
