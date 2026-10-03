"""Checks that the engine finds the known problems in the seeded demos."""
from dryrun.demos import list_demos, load_demo
from dryrun.engine import dry_run


def types(demo_id):
    return [f["type"] for f in dry_run(load_demo(demo_id))["findings"]]


def test_all_demos_load_and_run():
    for d in list_demos():
        report = dry_run(load_demo(d["id"]))
        assert report["summary"]["scenarios"] > 0
        for f in report["findings"]:
            assert f["evidence"], f"{d['id']} {f['id']} has no source sentence"


def test_admission_finds_the_five_structural_problems():
    report = dry_run(load_demo("1_admission"))
    problems = sorted(f["type"] for f in report["findings"] if f["severity"] == "problem")
    assert problems == sorted(
        ["TRAPDOOR", "MISSING_RECOVERY", "SILENT_DEFAULT", "OWNERLESS", "UNREACHABLE"])


def test_other_demos_show_dead_end_and_loop():
    assert "DEAD_END" in types("2_expenses")
    assert "LOOP" in types("3_travel")


def test_admission_text_is_the_agreed_eighteen_clauses():
    p = load_demo("1_admission")
    assert len(p.sentences) == 18
    assert p.sentence("c3").text == ("The applicant must pay the seat acceptance fee through "
                                     "the payment page within the lock period.")
    assert p.sentence("c17").text.startswith("An applicant whose application was cancelled for "
                                             "non-payment may appeal within 3 days, provided")
