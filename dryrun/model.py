"""The intermediate representation. Everything the engine reasons over lives here.

A Process is a state machine with *facts*. A fact is a token such as
"status_active" or "no_dues_certificate". Transitions can require, forbid,
grant or revoke facts, which is how the engine catches rules that are each
fine on their own and impossible together.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Sentence(BaseModel):
    id: str
    text: str
    start: int
    end: int


class Actor(BaseModel):
    id: str
    name: str


class State(BaseModel):
    id: str
    label: str
    # "setback" = a non-final state reached by something going wrong
    # (documents rejected, payment declined). "terminal" = the process may rest here.
    kind: Literal["start", "normal", "setback", "terminal"] = "normal"
    outcome: Optional[Literal["success", "failure", "neutral"]] = None
    evidence: List[str] = Field(default_factory=list)


class Fact(BaseModel):
    id: str
    label: str


class Transition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    action: str
    actor: Optional[str] = None  # None on an "action" transition means nobody owns it
    delegates: List[str] = Field(default_factory=list)
    # action = someone does it; timer = the clock does it; event = the outside world does it
    trigger: Literal["action", "timer", "event"] = "action"
    kind: Literal["normal", "reversal", "failure_handler"] = "normal"
    on_failure_of: Optional[str] = None  # for failure_handler: which transition it recovers
    can_fail: bool = False
    failure_label: Optional[str] = None
    retry: Literal["allowed", "forbidden", "unspecified"] = "unspecified"
    deadline: Optional[str] = None
    requires: List[str] = Field(default_factory=list)
    forbids: List[str] = Field(default_factory=list)
    grants: List[str] = Field(default_factory=list)
    revokes: List[str] = Field(default_factory=list)
    priority: Optional[int] = None  # stated precedence when two things happen at once
    evidence: List[str] = Field(default_factory=list)
    inferred: bool = False  # True when the extractor could not tie it to a sentence


class Process(BaseModel):
    title: str
    text: str
    sentences: List[Sentence]
    actors: List[Actor]
    subject: Optional[str] = None  # the actor the process happens *to*
    persona: Optional[str] = None  # a name for the simulated person, used in stories
    states: List[State]
    transitions: List[Transition]
    facts: List[Fact] = Field(default_factory=list)
    start: str
    initial_facts: List[str] = Field(default_factory=list)
    origin: Literal["demo", "llm", "manual"] = "manual"

    # -- lookups -----------------------------------------------------------
    def state(self, sid: str) -> State:
        return next(s for s in self.states if s.id == sid)

    def transition(self, tid: str) -> Transition:
        return next(t for t in self.transitions if t.id == tid)

    def actor_name(self, aid: Optional[str]) -> str:
        if aid is None:
            return "nobody"
        return next((a.name for a in self.actors if a.id == aid), aid)

    def fact_label(self, fid: str) -> str:
        return next((f.label for f in self.facts if f.id == fid), fid.replace("_", " "))

    def sentence(self, sid: str) -> Optional[Sentence]:
        return next((s for s in self.sentences if s.id == sid), None)


def validate_process(p: Process) -> List[str]:
    """Structural sanity checks. Returns human-readable problems (empty = fine)."""
    problems: List[str] = []
    state_ids = {s.id for s in p.states}
    actor_ids = {a.id for a in p.actors}
    sent_ids = {s.id for s in p.sentences}
    trans_ids = {t.id for t in p.transitions}
    if p.start not in state_ids:
        problems.append(f"start state '{p.start}' is not a known state")
    for t in p.transitions:
        if t.source not in state_ids:
            problems.append(f"{t.id}: unknown source state '{t.source}'")
        if t.target not in state_ids:
            problems.append(f"{t.id}: unknown target state '{t.target}'")
        if t.actor is not None and t.actor not in actor_ids:
            problems.append(f"{t.id}: unknown actor '{t.actor}'")
        if t.on_failure_of and t.on_failure_of not in trans_ids:
            problems.append(f"{t.id}: recovers unknown transition '{t.on_failure_of}'")
        for e in t.evidence:
            if e not in sent_ids:
                problems.append(f"{t.id}: cites unknown sentence '{e}'")
    if p.subject is not None and p.subject not in actor_ids:
        problems.append(f"subject '{p.subject}' is not a known actor")
    return problems
