"""Document -> process model, using a language model for interpretation only.

The model is never asked "what is wrong with this process". It is asked small
questions, each with a JSON schema for the answer, and every answer has to
point at numbered sentences. Stages:

  1. actors and states
  2. transitions, a few sentences at a time
  3. facts and what each transition requires / forbids / grants / revokes
  4. narrow follow-ups: what the text says about a failure, a stand-in, a final state

Whatever does not validate against the text is dropped (unknown ids) or kept
and marked `inferred` (no sentence id), and the engine lowers its confidence.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

from .engine import summarise
from .ingest import split_sentences
from .llm import LLMClient, LLMError
from .model import Actor, Fact, Process, State, Transition, validate_process

SYSTEM = ("You convert written procedures into structured data. Answer with one JSON object "
          "and nothing else. Use only what the text says. Refer to sentences by their ids, "
          "such as c3. Never quote the text.")
WINDOW = 10

# ---------------------------------------------------------------- schemas
_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_IDS = {"type": "array", "items": _STR, "maxItems": 6}


def _obj(props: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props)}


HOW = {"a_person_does_it": "action", "a_deadline_passes": "timer", "an_outside_result": "event"}


def _pick(ids=None, nullable=False) -> dict:
    """A string, or when the valid ids are known, exactly one of them. Giving the model
    an enum makes an invented sentence, state or actor id impossible to emit."""
    if not ids:
        return _NSTR if nullable else _STR
    if nullable:
        return {"type": ["string", "null"], "enum": list(ids) + [None]}
    return {"type": "string", "enum": list(ids)}


def _cites(sents=None) -> dict:
    return {"type": "array", "items": _pick(sents), "maxItems": 6}


_OUTCOME = {"type": ["string", "null"], "enum": ["success", "failure", "neutral", None]}


def schema_actors_states(sents=None) -> dict:
    return _obj({
        "subject": _STR,
        "actors": {"type": "array", "maxItems": 12, "items": _obj({"name": _STR, "sentences": _cites(sents)})},
        "states": {"type": "array", "maxItems": 16, "items": _obj({
            "label": _STR,
            "kind": {"type": "string", "enum": ["start", "normal", "setback", "terminal"]},
            "outcome": _OUTCOME, "sentences": _cites(sents)})}})


def schema_transitions(sents=None, states=None, actors=None) -> dict:
    return _obj({"transitions": {"type": "array", "maxItems": 16, "items": _obj({
        # Field names are chosen so that no name is also a legal value of another field:
        # a small model given "action" next to an enum containing "action" mixes them up.
        "sentences": _cites(sents), "step": _STR,
        "who": _pick(actors, True), "from": _pick(states), "to": _pick(states),
        "how": {"type": "string", "enum": list(HOW)},
        "deadline": _NSTR, "can_fail": {"type": "boolean"}, "failure_label": _NSTR,
        "reversal": {"type": "boolean"}})}})


def schema_failures(sents=None, states=None, actors=None, trans=None) -> dict:
    return _obj({"failures": {"type": "array", "maxItems": 12, "items": _obj({
        "transition": _pick(trans), "sentence": _pick(sents, True), "goes_to": _pick(states, True),
        "who": _pick(actors, True),
        "retry": {"type": "string", "enum": ["allowed", "forbidden", "unspecified"]}})}})


def schema_facts(sents=None, trans=None) -> dict:
    names = {"type": "array", "items": _STR, "maxItems": 4}
    return _obj({
        "facts": {"type": "array", "maxItems": 10, "items": _obj({"name": _STR, "sentences": _cites(sents)})},
        "rules": {"type": "array", "maxItems": 20, "items": _obj({
            "transition": _pick(trans), "requires": names, "forbids": names, "grants": names,
            "revokes": names, "sentences": _cites(sents)})}})


def schema_finals(states=None) -> dict:
    return _obj({"states": {"type": "array", "maxItems": 12, "items": _obj({
        "state": _pick(states), "final": {"type": "boolean"}, "outcome": _OUTCOME})}})


def schema_yes_no(sents=None) -> dict:
    return _obj({"answer": {"type": "string", "enum": ["yes", "no"]}, "sentence": _pick(sents, True)})


SCHEMA_YES_NO = schema_yes_no()


# ---------------------------------------------------------------- helpers
def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:40] or "x"


def _numbered(sentences) -> str:
    return "\n".join(f"[{s.id}] {s.text}" for s in sentences)


_ID_RE = re.compile(r"^\[?\s*(?:c|s|sentence|clause)?\s*#?0*(\d+)\s*\]?\.?$")


def _ids(value, known: set) -> List[str]:
    """Keep only ids we know. Tolerates "[c3]", "C3", "3", "c03" and "sentence 3"."""
    if isinstance(value, (str, int)):
        value = [value]
    out = []
    for v in value or []:
        m = _ID_RE.match(str(v).strip().lower()) if isinstance(v, (str, int)) else None
        sid = f"c{m.group(1)}" if m else None
        if sid is None and isinstance(v, str) and isinstance(known, dict) and len(v.strip()) > 15:
            # the model quoted the sentence instead of giving its id: find which one
            quote = v.strip().strip('"“”').lower()
            hits = [k for k, text in known.items() if quote in text.lower() or text.lower() in quote]
            sid = hits[0] if len(hits) == 1 else None
        if sid in known and sid not in out:
            out.append(sid)
    return out


def _resolve(value, table: dict, label=lambda x: x) -> Optional[str]:
    """Match what the model wrote against known ids: exact id, then name, then a
    unique partial match ("the Bank" -> bank). None if nothing fits."""
    if not value:
        return None
    key = slug(value)
    if key in table:
        return key
    bare = re.sub(r"^(the|a|an)_", "", key)
    if bare in table:
        return bare
    by_name = [k for k, v in table.items() if slug(label(v)) in (key, bare)]
    if len(by_name) == 1:
        return by_name[0]
    part = [k for k, v in table.items() if bare and (bare in k or k in bare or bare in slug(label(v)))]
    return part[0] if len(part) == 1 else None


def _list(value) -> list:
    return value if isinstance(value, list) else []


def extract_process(text: str, llm: LLMClient, title: str = "Uploaded process",
                    progress: Optional[Callable[[str], None]] = None) -> tuple[Process, List[str]]:
    say = progress or (lambda _msg: None)
    log: List[str] = []
    sentences = split_sentences(text)
    if len(sentences) < 2:
        raise ValueError("The document has too little text to describe a process.")
    sent_ids = {s.id: s.text for s in sentences}   # id -> text; `in` checks ids
    sid_list = list(sent_ids)
    doc = _numbered(sentences)
    dropped = 0

    # 1. actors and states ----------------------------------------------------
    say("Finding actors and states")
    a = llm.ask(SYSTEM, f"""TEXT:
{doc}

Part 1. List every person, role, team or system that performs an action in this procedure.
Name the one the procedure happens to (the applicant, customer, employee) as "subject".
Part 2. List the distinct states a single case can be in, from the beginning to every ending.
A state is a situation the case rests in, such as "awaiting review" or "refunded", not an action.
kind: "start" (exactly one), "normal", "setback" (something went wrong but the case is not over),
or "terminal" (the case is finished). For terminal states give outcome "success", "failure"
or "neutral"; otherwise null.
For every actor and state give the ids of the sentences that mention it.
Rules for states: name each one as a situation ("Seat locked", "Payment pending", "Refunded").
Give every different ending its own terminal state (completed, cancelled, lapsed, refunded).
A requirement such as "status is Active" or "has a certificate" is NOT a state; leave it out.

EXAMPLE (a different procedure, to show the shape only)
{{"subject": "Member", "actors": [{{"name": "Member", "sentences": ["c1"]}}, {{"name": "Librarian", "sentences": ["c2"]}}],
 "states": [{{"label": "Not yet requested", "kind": "start", "outcome": null, "sentences": ["c1"]}},
  {{"label": "Card requested", "kind": "normal", "outcome": null, "sentences": ["c1"]}},
  {{"label": "Card issued", "kind": "terminal", "outcome": "success", "sentences": ["c2"]}},
  {{"label": "Request expired", "kind": "terminal", "outcome": "failure", "sentences": ["c3"]}}]}}""",
                schema_actors_states(sid_list))
    actors: Dict[str, Actor] = {}
    for item in _list(a.get("actors")):
        if isinstance(item, dict) and item.get("name"):
            actors.setdefault(slug(item["name"]),
                              Actor(id=slug(item["name"]), name=str(item["name"]).strip()))
    subject = slug(a["subject"]) if a.get("subject") else None
    if subject and subject not in actors:
        actors[subject] = Actor(id=subject, name=str(a["subject"]).strip())
    states: Dict[str, State] = {}
    for item in _list(a.get("states")):
        if not isinstance(item, dict) or not item.get("label"):
            continue
        sid = slug(item["label"])
        kind = item.get("kind") if item.get("kind") in ("start", "normal", "setback", "terminal") else "normal"
        outcome = item.get("outcome") if item.get("outcome") in ("success", "failure", "neutral") else None
        if kind == "terminal" and outcome is None:
            outcome = "neutral"
        states.setdefault(sid, State(id=sid, label=str(item["label"]).strip(), kind=kind,
                                     outcome=outcome if kind == "terminal" else None,
                                     evidence=_ids(item.get("sentences"), sent_ids)))
    if len(states) < 2:
        raise ValueError("Could not find at least two states in this text.")
    starts = [x for x in states.values() if x.kind == "start"]
    start = starts[0].id if starts else next(iter(states))
    for extra in starts[1:]:
        extra.kind = "normal"  # the engine needs exactly one entry point
    states[start].kind = "start"
    log.append(f"{len(actors)} actors, {len(states)} states")

    # 2. transitions, a few sentences at a time -------------------------------
    state_list = "\n".join(f"- {x.id}: {x.label}" for x in states.values())
    actor_list = "\n".join(f"- {x.id}: {x.name}" for x in actors.values()) or "- (none found)"
    transitions: Dict[tuple, Transition] = {}
    for i in range(0, len(sentences), WINDOW):
        chunk = sentences[i:i + WINDOW]
        say(f"Reading sentences {chunk[0].id} to {chunk[-1].id} for transitions")
        r = llm.ask(SYSTEM, f"""STATES:
{state_list}

ACTORS:
{actor_list}

SENTENCES:
{_numbered(chunk)}

For each way these sentences move a case from one state to another, give one item.
- "sentences": the ids of the sentences that say so.
- "step": what happens, as a short verb phrase copied from the sentence, such as
  "pay the acceptance fee" or "application is cancelled". Never a single word.
- "who": the actor id of whoever does it, or null if the sentence does not say who.
- "from" and "to": state ids from the list. They must be different states.
- "how": "a_person_does_it", "a_deadline_passes" (it happens automatically when time runs out),
  or "an_outside_result" (for example a bank confirming).
- "deadline": the time limit as written, or null.
- "can_fail": true only if this step can realistically fail (a payment, an upload, a delivery,
  a check by an outside party). "failure_label": how it fails, in a few words, or null.
- "reversal": true if this is the person cancelling, withdrawing or undoing something.

EXAMPLE (a different procedure, to show the shape only)
[c2] The librarian issues the card within 2 days of the request.
[c3] Requests not handled within 7 days expire.
{{"transitions": [
 {{"sentences": ["c2"], "step": "issue the card", "who": "librarian", "from": "card_requested",
   "to": "card_issued", "how": "a_person_does_it", "deadline": "within 2 days of the request",
   "can_fail": false, "failure_label": null, "reversal": false}},
 {{"sentences": ["c3"], "step": "request expires", "who": null, "from": "card_requested",
   "to": "request_expired", "how": "a_deadline_passes", "deadline": "within 7 days",
   "can_fail": false, "failure_label": null, "reversal": false}}]}}""",
                    schema_transitions(sid_list, list(states), list(actors)))
        for item in _list(r.get("transitions")):
            if not isinstance(item, dict):
                continue
            src = _resolve(item.get("from"), states, lambda v: v.label)
            dst = _resolve(item.get("to"), states, lambda v: v.label)
            action = str(item.get("step") or item.get("action") or "").strip()
            if slug(action) in ("action", "timer", "event", "step") or slug(action) in actors:
                dropped += 1  # the model put a keyword where the description belongs
                continue
            if src is None or dst is None or not action or src == dst:
                dropped += 1  # refers to a state that does not exist
                continue
            named = item.get("who") or item.get("actor")
            actor = _resolve(named, actors, lambda v: v.name)
            if named and actor is None and slug(named) not in ("null", "none", "nobody"):
                # the model named someone we had not listed: keep them rather than
                # pretend nobody owns the step
                actor = slug(named)
                actors[actor] = Actor(id=actor, name=str(named).strip())
            trigger = HOW.get(item.get("how")) or (
                item.get("trigger") if item.get("trigger") in ("action", "timer", "event") else "action")
            evidence = _ids(item.get("sentences"), sent_ids)
            key = (src, dst, slug(action))
            if key in transitions:
                continue
            transitions[key] = Transition(
                id=f"t{len(transitions) + 1}", source=src, target=dst, action=action,
                actor=None if trigger == "timer" else actor, trigger=trigger,
                kind="reversal" if item.get("reversal") is True else "normal",
                deadline=(str(item["deadline"]).strip() if item.get("deadline") else None),
                can_fail=item.get("can_fail") is True,
                failure_label=(str(item["failure_label"]).strip() if item.get("failure_label") else None),
                evidence=evidence, inferred=not evidence)
    tlist = list(transitions.values())
    if not tlist:
        raise ValueError("Could not find any step that moves a case from one state to another.")
    by_id = {t.id: t for t in tlist}
    log.append(f"{len(tlist)} transitions")

    # 3. what the text says when a step fails (one call for all of them) -------
    can_fail = [x for x in tlist if x.can_fail]
    if can_fail:
        say("Asking what the text says when a step fails")
        listing = "\n".join(f"- {t.id}: at \"{states[t.source].label}\", "
                            f"{t.failure_label or t.action + ' fails'}" for t in can_fail)
        r = llm.ask(SYSTEM, f"""TEXT:
{doc}

STATES:
{state_list}

ACTORS:
{actor_list}

FAILURES:
{listing}

For each failure above give one item.
- "transition": its id from the list.
- "sentence": the id of the sentence that says what happens or what may be done when this
  failure occurs, or null if no sentence does.
- "retry": does that sentence allow another attempt? "allowed", "forbidden" or "unspecified".
- "who": the actor id of whoever acts after the failure (who retries, who is informed), or null.
- "goes_to": the state id the case moves to because of the failure, or null if it stays put.""",
                    schema_failures(sid_list, list(states), list(actors), [t.id for t in can_fail]))
        for item in _list(r.get("failures")):
            if not isinstance(item, dict) or item.get("transition") not in by_id:
                dropped += 1
                continue
            t = by_id[item["transition"]]
            if not t.can_fail or any(h.on_failure_of == t.id for h in tlist):
                continue
            label = t.failure_label or f"{t.action} fails"
            cited = _ids(item.get("sentence"), sent_ids)
            retry = item.get("retry") if item.get("retry") in ("allowed", "forbidden", "unspecified") else "unspecified"
            goes = _resolve(item.get("goes_to"), states, lambda v: v.label)
            who = _resolve(item.get("who"), actors, lambda v: v.name) or subject
            if not cited:
                continue  # a recovery nobody can cite is not a recovery
            t.retry = retry
            if goes and goes != t.target and goes != t.source:
                h = Transition(id=f"t{len(tlist) + 1}", source=t.source, target=goes,
                               action=f"after {label}: move to {states[goes].label}",
                               actor=who, kind="failure_handler", on_failure_of=t.id, evidence=cited)
            elif retry == "allowed":
                # A promised retry is a real action. Modelling it as one lets the next
                # step attach its prerequisites, so the engine can test whether the
                # retry is actually possible in the state the failure leaves behind.
                h = Transition(id=f"t{len(tlist) + 1}", source=t.source, target=t.source,
                               action=f"retry after {label}", actor=who, kind="failure_handler",
                               on_failure_of=t.id, evidence=cited)
            else:
                continue
            tlist.append(h)
            by_id[h.id] = h

    # 4. facts and requirements -------------------------------------------------
    say("Finding conditions: what each step requires, forbids, grants and revokes")
    trans_list = "\n".join(
        f"- {t.id}: {states[t.source].label} -> {states[t.target].label}: {t.action} "
        f"[{', '.join(t.evidence) or 'no sentence'}]" for t in tlist)
    r = llm.ask(SYSTEM, f"""TEXT:
{doc}

TRANSITIONS:
{trans_list}

Part 1. List the named conditions this procedure depends on: a status, a document, a
certificate, an approval, a flag. Give each a short name and the sentences that mention it.
Part 2. For each transition that depends on or changes one of those conditions, give one rule.
- "transition": a transition id from the list.
- "requires": conditions that must be true for the step to be possible.
- "forbids": conditions that must NOT be true.
- "grants": conditions the step makes true. "revokes": conditions the step makes false.
- "sentences": the ids of the sentences that say so.
Use only the condition names from Part 1. Leave out transitions with no conditions.
A condition is never a state: do not use state names as conditions.

EXAMPLE (a different procedure, to show the shape only)
"[c4] Borrowing is open only to members whose card is valid." and "[c5] Reporting a card lost makes it invalid."
{{"facts": [{{"name": "card valid", "sentences": ["c4", "c5"]}}],
 "rules": [{{"transition": "t3", "requires": ["card valid"], "forbids": [], "grants": [], "revokes": [], "sentences": ["c4"]}},
  {{"transition": "t5", "requires": [], "forbids": [], "grants": [], "revokes": ["card valid"], "sentences": ["c5"]}}]}}""",
                schema_facts(sid_list, [t.id for t in tlist]))
    facts: Dict[str, Fact] = {}
    for item in _list(r.get("facts")):
        if isinstance(item, dict) and item.get("name"):
            facts.setdefault(slug(item["name"]), Fact(id=slug(item["name"]),
                                                      label=str(item["name"]).strip()))
    for rule in _list(r.get("rules")):
        if not isinstance(rule, dict) or rule.get("transition") not in by_id:
            dropped += 1
            continue
        t = by_id[rule["transition"]]
        cited = _ids(rule.get("sentences"), sent_ids)
        changed = False
        for field in ("requires", "forbids", "grants", "revokes"):
            names = [slug(x) for x in _list(rule.get(field)) if isinstance(x, str)]
            known = [n for n in names if n in facts]
            dropped += len(names) - len(known)
            if known:
                setattr(t, field, sorted(set(getattr(t, field)) | set(known)))
                changed = True
        if changed:
            if cited:
                t.evidence += [c for c in cited if c not in t.evidence]
            else:
                t.inferred = True  # a condition nobody could point to in the text
    for t in tlist:  # a "condition" that is just a state name carries no information
        for field in ("requires", "forbids", "grants", "revokes"):
            setattr(t, field, [f for f in getattr(t, field) if f not in states])
    used = {f for t in tlist for f in t.requires + t.forbids + t.grants + t.revokes}
    facts = {k: v for k, v in facts.items() if k in used}
    # a requirement that no step produces is assumed to be something the person starts with
    produced = {f for t in tlist for f in t.grants}
    initial = sorted({f for t in tlist for f in t.requires} - produced)
    log.append(f"{len(facts)} facts")

    # 5. are the states nothing leaves meant to be final? (one call) -----------
    leaving = {t.source for t in tlist if t.source != t.target}
    open_ends = [st for st in states.values() if st.kind != "terminal" and st.id not in leaving]
    if open_ends:
        say("Asking which end states are meant to be final")
        listing = "\n".join(f"- {st.id}: {st.label}" for st in open_ends)
        r = llm.ask(SYSTEM, f"""TEXT:
{doc}

STATES:
{listing}

For each state above: once a case is in it, is the case finished, with nothing more expected
to happen? Give "final" true or false, and if true the outcome: "success", "failure" or "neutral".""",
                    schema_finals([st.id for st in open_ends]))
        for item in _list(r.get("states")):
            sid = _resolve(item.get("state"), states, lambda v: v.label) if isinstance(item, dict) else None
            if sid and item.get("final") is True and states[sid] in open_ends:
                states[sid].kind = "terminal"
                states[sid].outcome = item.get("outcome") if item.get("outcome") in ("success", "failure", "neutral") else "neutral"
    if dropped:
        log.append(f"{dropped} references to unknown ids dropped")
    inferred = sum(1 for t in tlist if t.inferred)
    if inferred:
        log.append(f"{inferred} transitions marked inferred (no sentence cited)")
    log.append(f"{getattr(llm, 'calls', 0)} narrow questions asked")

    process = Process(
        title=title, text=text, sentences=sentences, actors=list(actors.values()),
        subject=subject, states=list(states.values()), transitions=tlist,
        facts=list(facts.values()), start=start, initial_facts=initial, origin="llm")
    problems = validate_process(process)
    if problems:
        raise ValueError("The extracted model is inconsistent: " + "; ".join(problems[:3]))
    return process, log


def refute(process: Process, report: dict, llm: LLMClient,
           progress: Optional[Callable[[str], None]] = None) -> dict:
    """Second pass. For each finding that rests on the text *not* saying something,
    ask one yes/no question and demand a sentence id. A yes with a real, new
    sentence downgrades the finding to ambiguous and shows that sentence.
    A yes with no valid citation changes nothing."""
    say = progress or (lambda _msg: None)
    doc = _numbered(process.sentences)
    known = {s.id: s.text for s in process.sentences}
    for f in report["findings"]:
        if not f.get("absence_based"):
            continue
        question = f.get("question") or f["missing"]
        say(f"Checking {f['id']} against the text")
        try:
            r = llm.ask(SYSTEM, f"""TEXT:
{doc}

Question: does any sentence say {question}?
Answer "yes" or "no". If yes, give the id of that one sentence as "sentence"; otherwise null.""",
                        schema_yes_no(list(known)))
        except LLMError:
            f["status"] = "unchecked"
            continue
        cited = _ids(r.get("sentence"), known)
        already = {e["sentence"] for e in f["evidence"]}
        if r.get("answer") == "yes" and cited and cited[0] not in already:
            sid = cited[0]
            f["status"] = "downgraded"
            f["severity"] = "ambiguous"
            f["confidence"] = round(max(f["confidence"] - 0.3, 0.2), 2)
            f["confidence_label"] = "low"
            f["refuted_by"] = sid
            f["evidence"].append({
                "sentence": sid, "text": process.sentence(sid).text, "refutation": True,
                "because": "A second pass says this sentence may already cover the gap, so the "
                           "finding was downgraded to ambiguous. Read it and decide."})
            f["why"] = f["why"] + [f"Refutation pass: asked whether any sentence says {question}. "
                                   f"The model answered yes and cited {sid}."]
        else:
            f["status"] = "checked"
    return summarise(report)
