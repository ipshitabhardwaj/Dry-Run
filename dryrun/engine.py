"""The deterministic part of Dry Run.

Nothing in this file calls a language model. It takes a Process (states,
transitions, facts) and does three things:

1. explores every configuration the process can reach, where a configuration
   is (state, set of facts);
2. checks the structure for states with no way out, steps nobody owns,
   provisions that can never be used and cycles with no exit;
3. injects six classes of disruption at every point where they can realistically
   occur and follows each one to see where the person ends up.

Every finding is assembled from graph facts plus the source sentences attached
to the transitions involved, so each one can be checked by reading the text.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, FrozenSet, List, Optional, Tuple

from .model import Process, Transition

Config = Tuple[str, FrozenSet[str]]

PROBLEM_TYPES = [
    "DEAD_END", "TRAPDOOR", "MISSING_RECOVERY", "SILENT_DEFAULT",
    "OWNERLESS", "UNREACHABLE", "LOOP",
]
AMBIGUOUS_TYPES = ["DEADLINE_NO_CONSEQUENCE", "FAILURE_UNADDRESSED", "RACE"]

CLASS_LABELS = {
    "BASELINE": "Happy path",
    "ACTION_FAILURE": "Action failure",
    "NO_RESPONSE": "No response",
    "DELAY": "Delay",
    "ACTOR_UNAVAILABLE": "Actor unavailable",
    "SIMULTANEOUS": "Simultaneous conditions",
    "RETRY_REVERSAL": "Retry / reversal",
}

MAX_CONFIGS = 20000


class Engine:
    def __init__(self, process: Process):
        self.p = process
        self.out: Dict[str, List[Transition]] = defaultdict(list)
        for t in process.transitions:
            self.out[t.source].append(t)
        self.terminal = {s.id for s in process.states if s.kind == "terminal"}
        self.findings: Dict[tuple, dict] = {}
        self.scenarios: List[dict] = []
        self.explained: set = set()  # transitions already accounted for by a finding
        self._fate_cache: Dict[Config, dict] = {}
        self._explore()

    # ------------------------------------------------------------------ core
    def enabled(self, t: Transition, cfg: Config) -> bool:
        state, facts = cfg
        return (t.source == state and set(t.requires) <= facts
                and not (set(t.forbids) & facts))

    def apply(self, t: Transition, cfg: Config) -> Config:
        return (t.target, frozenset((cfg[1] - set(t.revokes)) | set(t.grants)))

    def normal_moves(self, cfg: Config) -> List[Transition]:
        return [t for t in self.out[cfg[0]]
                if t.kind != "failure_handler" and self.enabled(t, cfg)]

    def handler_moves(self, cfg: Config) -> List[Transition]:
        """Failure handlers only fire when the thing they recover could have been attempted."""
        res = []
        for h in self.out[cfg[0]]:
            if h.kind != "failure_handler" or not self.enabled(h, cfg):
                continue
            failed = next((t for t in self.out[cfg[0]] if t.id == h.on_failure_of), None)
            if failed is not None and self.enabled(failed, cfg):
                res.append(h)
        return res

    def _explore(self) -> None:
        start: Config = (self.p.start, frozenset(self.p.initial_facts))
        self.start_cfg = start
        self.parent: Dict[Config, Optional[Tuple[Config, str]]] = {start: None}
        self.order: List[Config] = []
        self.used: set = set()
        q = deque([start])
        while q and len(self.parent) < MAX_CONFIGS:
            cfg = q.popleft()
            self.order.append(cfg)
            for t in self.normal_moves(cfg) + self.handler_moves(cfg):
                self.used.add(t.id)
                nxt = self.apply(t, cfg)
                if nxt not in self.parent:
                    self.parent[nxt] = (cfg, t.id)
                    q.append(nxt)
        self.reachable_states = {c[0] for c in self.parent}

    def path_to(self, cfg: Config) -> List[str]:
        path: List[str] = []
        cur = cfg
        while self.parent.get(cur) is not None:
            prev, tid = self.parent[cur]
            path.append(tid)
            cur = prev
        return list(reversed(path))

    def fate(self, cfg: Config) -> dict:
        """What can happen from here under normal rules."""
        if cfg in self._fate_cache:
            return self._fate_cache[cfg]
        seen = {cfg}
        q = deque([cfg])
        terminal_outcomes = set()
        stuck: List[Config] = []
        while q:
            c = q.popleft()
            if c[0] in self.terminal:
                terminal_outcomes.add(self.p.state(c[0]).outcome or "neutral")
            moves = self.normal_moves(c)
            if not moves and c[0] not in self.terminal:
                stuck.append(c)
            for t in moves:
                n = self.apply(t, c)
                if n not in seen:
                    seen.add(n)
                    q.append(n)
        res = {
            "terminal": bool(terminal_outcomes),
            "success": "success" in terminal_outcomes,
            "stuck": stuck,
            "loop": not terminal_outcomes and not stuck,
            "states": {c[0] for c in seen},
        }
        self._fate_cache[cfg] = res
        return res

    # --------------------------------------------------------------- wording
    def _label(self, sid: str) -> str:
        return self.p.state(sid).label

    def _who(self, t: Transition) -> str:
        if t.trigger == "timer":
            return "The clock"
        if t.trigger == "event":
            return self.p.actor_name(t.actor) if t.actor else "Outside event"
        return self.p.actor_name(t.actor) if t.actor else "Nobody assigned"

    def _the(self, actor_id: Optional[str]) -> str:
        name = self.p.actor_name(actor_id)
        bare = {"payroll", "finance", "accounts", "hr", "legal", "it", "admissions", "nobody"}
        if name.lower().startswith("the ") or name.lower() in bare:
            return name
        return f"the {name}"

    def _persona(self) -> str:
        subject = self.p.actor_name(self.p.subject).lower() if self.p.subject else "person"
        return f"{self.p.persona or 'Meera'}, the {subject}"

    def _ev(self, t: Transition, because: Optional[str] = None) -> List[dict]:
        return [{"sentence": sid, "because": because or f"Defines “{t.action}”."}
                for sid in t.evidence]

    def _story(self, path: List[str], extra: List[dict]) -> List[dict]:
        steps = [{"kind": "start", "who": self._persona(),
                  "text": f"starts at “{self._label(self.p.start)}”.",
                  "state": self.p.start}]
        for tid in path:
            t = self.p.transition(tid)
            steps.append({"kind": "step", "who": self._who(t), "text": t.action,
                          "state": t.target, "transition": t.id,
                          "to": self._label(t.target)})
        return steps + extra

    def _why_missing(self, fact: str, cfg: Config) -> str:
        """Explain, from the model alone, why a required fact is absent at cfg."""
        label = self.p.fact_label(fact)
        for tid in reversed(self.path_to(cfg)):
            t = self.p.transition(tid)
            if fact in t.revokes:
                return (f"“{label}” was removed earlier by “{t.action}”, "
                        f"and nothing restores it")
        grantors = [g for g in self.p.transitions if fact in g.grants]
        if not grantors:
            return f"nothing in the process ever grants “{label}”"
        bits = []
        for g in grantors:
            reach = g.source in self.reachable_states and g.id in self.used
            via = self._graph_reach(g.target) if reach else set()
            if not reach:
                bits.append(f"“{g.action}”, which itself can never happen")
            elif cfg[0] not in via:
                bits.append(f"“{g.action}” at “{self._label(g.source)}”, "
                            f"and there is no route from there to “{self._label(cfg[0])}”")
            else:
                bits.append(f"“{g.action}” at “{self._label(g.source)}”, "
                            f"which this path did not pass through")
        return f"“{label}” is only granted by " + "; or by ".join(bits)

    def _graph_reach(self, sid: str) -> set:
        """States reachable from sid in the bare graph, ignoring facts."""
        seen, stack = {sid}, [sid]
        while stack:
            for t in self.out[stack.pop()]:
                if t.target not in seen:
                    seen.add(t.target)
                    stack.append(t.target)
        return seen

    def _why_forbidden(self, fact: str, cfg: Config) -> str:
        label = self.p.fact_label(fact)
        for tid in reversed(self.path_to(cfg)):
            t = self.p.transition(tid)
            if fact in t.grants:
                return f"“{label}” was already made true by “{t.action}”"
        return f"“{label}” is already true here"

    def _blocked_reason(self, t: Transition, cfg: Config) -> str:
        parts = [self._why_missing(f, cfg) for f in t.requires if f not in cfg[1]]
        parts += [self._why_forbidden(f, cfg) for f in t.forbids if f in cfg[1]]
        return "; ".join(parts)

    # -------------------------------------------------------------- findings
    def _finding(self, key: tuple, **fields) -> dict:
        if key not in self.findings:
            f = {"key": key, "scenarios": [], "status": "confirmed",
                 "absence_based": False, **fields}
            f.setdefault("states", [])
            f.setdefault("transitions", [])
            f.setdefault("evidence", [])
            f.setdefault("why", [])
            f.setdefault("missing", "")
            self.findings[key] = f
        return self.findings[key]

    def _scenario(self, cls: str, title: str, cfg: Config, status: str,
                  note: str, extra_steps: List[dict], finding: Optional[dict] = None) -> dict:
        s = {"id": f"S{len(self.scenarios) + 1:03d}", "cls": cls,
             "cls_label": CLASS_LABELS[cls], "title": title, "state": cfg[0],
             "status": status, "note": note,
             "path": self.path_to(cfg),
             "story": self._story(self.path_to(cfg), extra_steps),
             "finding": None}
        self.scenarios.append(s)
        if finding is not None:
            finding["scenarios"].append(s["id"])
            s["finding"] = finding["key"]
            if "story" not in finding:
                finding["story"] = s["story"]
                finding["path"] = s["path"]
                finding["scenario_title"] = title
        return s

    # ------------------------------------------------------- static analysis
    def _static(self) -> None:
        p = self.p
        # states a person can be in with no usable way out
        by_state: Dict[str, List[Config]] = defaultdict(list)
        for cfg in self.order:
            if cfg[0] not in self.terminal and not self.normal_moves(cfg):
                by_state[cfg[0]].append(cfg)
        for sid, cfgs in by_state.items():
            cfg = cfgs[0]
            defined = [t for t in self.out[sid] if t.kind != "failure_handler"]
            st = p.state(sid)
            evidence = [{"sentence": e, "because": f"Puts someone in “{st.label}”."}
                        for e in st.evidence]
            entry = self.path_to(cfg)
            if entry:
                evidence += self._ev(p.transition(entry[-1]),
                                     f"This is how someone arrives in “{st.label}”.")
            if defined:
                why = [f"“{st.label}” is reachable and is not a final state."]
                for t in defined:
                    self.explained.add(t.id)
                    why.append(f"Its exit “{t.action}” exists on paper but cannot be used: "
                               f"{self._blocked_reason(t, cfg)}.")
                    evidence += self._ev(t, f"Defines the exit “{t.action}” and its condition.")
                    for f in t.requires:
                        for g in p.transitions:
                            if f in g.grants:
                                evidence += self._ev(g, f"The only source of “{p.fact_label(f)}”.")
                self._finding(
                    ("TRAPDOOR", sid), type="TRAPDOOR",
                    title=f"There is a way into “{st.label}” and no usable way out",
                    explanation=(f"Someone who reaches “{st.label}” by this route can never leave it. "
                                 f"The process defines {len(defined)} exit(s), but each depends on "
                                 f"something this person cannot have."),
                    missing=f"No exit from “{st.label}” that this person can actually take.",
                    states=[sid], transitions=[t.id for t in defined],
                    evidence=evidence, why=why, path=entry,
                    story=self._story(entry, [{"kind": "stuck", "who": self._persona(),
                                               "text": f"is in “{st.label}” and every exit is closed.",
                                               "state": sid}]),
                    scenario_title=f"Arrive at “{st.label}” by the normal route")
            else:
                self._finding(
                    ("DEAD_END", sid), type="DEAD_END",
                    title=f"“{st.label}” has no next step",
                    explanation=(f"The process can put someone in “{st.label}”, it is not described "
                                 f"as a final outcome, and no rule says what happens next."),
                    missing=f"Any transition out of “{st.label}”, or a statement that it is final.",
                    states=[sid], evidence=evidence, absence_based=True,
                    question=f"what happens after a case reaches “{st.label}”",
                    why=[f"“{st.label}” is reachable.",
                         "It is not marked as a final state.",
                         "The model contains zero transitions leaving it."],
                    path=entry,
                    story=self._story(entry, [{"kind": "stuck", "who": self._persona(),
                                               "text": f"is in “{st.label}” with no defined next step.",
                                               "state": sid}]),
                    scenario_title=f"Arrive at “{st.label}” by the normal route")

        # steps that must be done by someone, where the text names no one
        for t in p.transitions:
            if t.trigger == "action" and t.actor is None and t.source in self.reachable_states:
                self._finding(
                    ("OWNERLESS", t.id), type="OWNERLESS",
                    title=f"Nobody is responsible for “{t.action}”",
                    explanation=(f"Leaving “{self._label(t.source)}” depends on “{t.action}”, "
                                 f"but the text does not say who does it. If each party assumes "
                                 f"it is someone else's job, the person waits indefinitely."),
                    missing=f"A named owner for “{t.action}”.",
                    states=[t.source], transitions=[t.id],
                    evidence=self._ev(t, "Describes the step without naming who performs it."),
                    why=[f"“{t.action}” needs a person to perform it.",
                         "No actor is attached to it in the model.",
                         "The source sentence is written in the passive, with no subject."],
                    path=self._first_path(t.source),
                    story=self._story(self._first_path(t.source),
                                      [{"kind": "stuck", "who": "Nobody assigned",
                                        "text": f"“{t.action}” is waiting for an owner.",
                                        "state": t.source, "transition": t.id}]),
                    scenario_title=f"Reach “{self._label(t.source)}”")

        # cycles that can never reach a final state
        loop_groups: Dict[frozenset, Config] = {}
        for cfg in self.order:
            f = self.fate(cfg)
            if f["loop"] and self.normal_moves(cfg):
                loop_groups.setdefault(frozenset(f["states"]), cfg)
        # keep only the cycle itself, not the states that merely lead into it
        loop_groups = {k: v for k, v in loop_groups.items()
                       if not any(o < k for o in loop_groups)}
        for states, cfg in loop_groups.items():
            trans = [t for t in p.transitions if t.source in states and t.target in states]
            evidence = []
            for t in trans:
                evidence += self._ev(t)
            exits = [t for t in p.transitions if t.source in states and t.target not in states
                     and t.kind != "failure_handler"]
            exit_why = []
            for t in exits:
                at = next(c for c in self.order if c[0] == t.source and self.fate(c)["loop"])
                self.explained.add(t.id)
                evidence += self._ev(t, f"The exit “{t.action}”, which is never available here.")
                exit_why.append(f"The exit “{t.action}” cannot be taken: "
                                f"{self._blocked_reason(t, at)}.")
            for t in p.transitions:
                if t.source in states and t.id not in self.used and t not in exits:
                    at = next(c for c in self.order if c[0] == t.source and self.fate(c)["loop"])
                    self.explained.add(t.id)
                    evidence += self._ev(t, f"Defines “{t.action}”, which can never happen.")
                    exit_why.append(f"“{t.action}” can never happen: {self._blocked_reason(t, at)}.")
            names = " and ".join(f"“{self._label(s)}”" for s in sorted(states))
            self._finding(
                ("LOOP", tuple(sorted(states))), type="LOOP",
                title=f"A cycle with no exit between {names}",
                explanation=(f"Once a case of this kind enters {names}, every available step leads "
                             f"back into the same states. Each exit depends on something that can "
                             f"only be obtained after leaving. No final outcome is reachable."),
                missing="A transition from this cycle to a final state.",
                states=sorted(states), transitions=[t.id for t in trans + exits], evidence=evidence,
                why=["Searched every path forward from this point.",
                     "None reaches a final state, and none gets stuck: it repeats forever."]
                    + exit_why,
                path=self.path_to(cfg),
                story=self._story(self.path_to(cfg),
                                  [{"kind": "stuck", "who": self._persona(),
                                    "text": "goes round this cycle with no way to finish.",
                                    "state": cfg[0]}]),
                scenario_title="Enter the cycle")

    def _first_path(self, sid: str) -> List[str]:
        cfg = next((c for c in self.order if c[0] == sid), None)
        return self.path_to(cfg) if cfg else []

    def _unreachable(self) -> None:
        p = self.p
        for t in p.transitions:
            if t.id in self.used or t.id in self.explained:
                continue
            if t.source not in self.reachable_states:
                continue  # consequence of something upstream, not a root cause
            cfgs = [c for c in self.order if c[0] == t.source]
            cfg = cfgs[0]
            downstream = [s.label for s in p.states
                          if s.id not in self.reachable_states and s.id == t.target]
            evidence = self._ev(t, f"Defines “{t.action}” and its condition.")
            for f in t.forbids:
                for g in p.transitions:
                    if f in g.grants and g.target == t.source:
                        evidence += self._ev(g, f"Makes “{p.fact_label(f)}” true on the way in.")
            for f in t.requires:
                for g in p.transitions:
                    if f in g.grants or f in g.revokes:
                        evidence += self._ev(g, f"Controls “{p.fact_label(f)}”.")
            more = (f" As a result “{downstream[0]}” can never be reached either."
                    if downstream else "")
            self._finding(
                ("UNREACHABLE", t.id), type="UNREACHABLE",
                title=f"“{t.action}” can never actually be used",
                explanation=(f"The text offers “{t.action}” from “{self._label(t.source)}”, "
                             f"but in every situation where someone is in that state its "
                             f"conditions are already impossible: {self._blocked_reason(t, cfg)}."
                             + more),
                missing="A route on which this provision's conditions can hold.",
                states=[t.source], transitions=[t.id], evidence=evidence,
                why=[f"Enumerated all {len(cfgs)} situation(s) in which someone is in "
                     f"“{self._label(t.source)}”.",
                     f"“{t.action}” is not enabled in any of them.",
                     self._blocked_reason(t, cfg).capitalize() + "."],
                path=self.path_to(cfg),
                story=self._story(self.path_to(cfg),
                                  [{"kind": "stuck", "who": self._persona(),
                                    "text": f"tries “{t.action}” and is not eligible.",
                                    "state": t.source, "transition": t.id}]),
                scenario_title=f"Try “{t.action}”")
        incoming = defaultdict(int)
        for t in p.transitions:
            if t.source != t.target:
                incoming[t.target] += 1
        for s in p.states:
            if s.id != p.start and incoming[s.id] == 0:
                self._finding(
                    ("UNREACHABLE", "state", s.id), type="UNREACHABLE",
                    title=f"Nothing leads to “{s.label}”",
                    explanation=(f"“{s.label}” is described, but no step in the process "
                                 f"moves anyone into it."),
                    missing=f"A transition into “{s.label}”.",
                    states=[s.id],
                    evidence=[{"sentence": e, "because": f"Mentions “{s.label}”."} for e in s.evidence],
                    absence_based=True,
                    question=f"how a case gets into “{s.label}”",
                    why=["Counted the transitions that arrive at this state: zero."],
                    path=[], story=[], scenario_title="")

    # ---------------------------------------------------------- disruptions
    def _downstream(self, nexts: List[Config]) -> Optional[dict]:
        """If the continuation cannot finish, return the finding that explains why."""
        for n in nexts:
            f = self.fate(n)
            if f["terminal"]:
                continue
            if f["stuck"]:
                sid = f["stuck"][0][0]
                return self.findings.get(("TRAPDOOR", sid)) or self.findings.get(("DEAD_END", sid))
            for key, finding in self.findings.items():
                if key[0] == "LOOP" and n[0] in finding["states"]:
                    return finding
        return None

    def _inaction(self, cls: str, cfg: Config, actor: str, removed: List[Transition],
                  title: str, lead: str) -> None:
        """Shared logic for 'the responsible person does not act (in time)'."""
        p = self.p
        sid = cfg[0]
        is_subject = actor == p.subject
        who = self._the(actor)
        others = [o for o in self.normal_moves(cfg) if o not in removed]
        timers = [o for o in others if o.trigger == "timer"]
        progress = [o for o in others if o.trigger != "timer" and o.kind != "reversal"]
        escape = [o for o in others if o.kind == "reversal"]
        base_ev = []
        for r in removed:
            base_ev += self._ev(r, f"Makes {who} responsible for “{r.action}”.")
        disrupt = {"kind": "disruption", "who": p.actor_name(actor), "text": lead, "state": sid}

        if timers:
            tm = timers[0]
            final = tm.target in self.terminal
            step = {"kind": "step", "who": "The clock", "text": tm.action,
                    "state": tm.target, "transition": tm.id, "to": self._label(tm.target)}
            if is_subject or not final:
                note = (f"Defined consequence: “{tm.action}”." if is_subject
                        else f"Escalates to “{self._label(tm.target)}”.")
                self._scenario(cls, title, cfg, "completed", note, [disrupt, step])
                return
            f = self._finding(
                ("SILENT_DEFAULT", sid), type="SILENT_DEFAULT",
                title=f"If {who} does nothing, the case ends as “{self._label(tm.target)}”",
                explanation=(f"At “{self._label(sid)}” the next move belongs to {who}. If they do "
                             f"not act, a timer moves the case to “{self._label(tm.target)}”. "
                             f"Nobody decides that outcome, and the person it happens to did "
                             f"nothing wrong."),
                missing=(f"An escalation, a reminder or a named decision-maker before "
                         f"“{self._label(tm.target)}” takes effect."),
                states=[sid, tm.target], transitions=[tm.id] + [r.id for r in removed],
                evidence=base_ev + self._ev(tm, "Fires automatically when the time runs out."),
                why=[f"The only actions that advance “{self._label(sid)}” belong to {who}.",
                     f"Removed them, to simulate {who} not acting.",
                     f"The one remaining move is a timer, “{tm.action}”, and it ends in a "
                     f"final state.",
                     f"{who[0].upper()}{who[1:]} is not {self._the(p.subject).lower() if p.subject else 'the person affected'}, "
                     f"so the cost lands on someone who had no say."])
            self._scenario(cls, title, cfg, "problem",
                           f"Timer decides: “{self._label(tm.target)}”.",
                           [disrupt, {**step, "kind": "stuck"}], f)
            return
        if progress:
            self._scenario(cls, title, cfg, "completed",
                           f"Another route remains: “{progress[0].action}”.", [disrupt])
            return
        if is_subject:
            dl = [r for r in removed if r.deadline]
            if dl:
                self._deadline_gap(cls, title, cfg, dl[0], disrupt)
            else:
                self._scenario(cls, title, cfg, "completed",
                               "The process simply waits; no one else is affected.", [disrupt])
            return
        esc = (f" The only thing {self._persona().split(',')[0]} can do is “{escape[0].action}”."
               if escape else "")
        key = ("DEAD_END", "wait")
        f = self._finding(
            key, type="DEAD_END", title="", explanation="", missing="",
            states=[], transitions=[], evidence=[], absence_based=True, why=[])
        f.setdefault("waits", [])
        if (sid, actor) not in f["waits"]:
            f["waits"].append((sid, actor))
            if sid not in f["states"]:
                f["states"].append(sid)
            f["transitions"] += [r.id for r in removed if r.id not in f["transitions"]]
            f["evidence"] += base_ev
        pairs = [f"{self._the(a)} at “{self._label(s_)}”" for s_, a in f["waits"]]
        listing = "; ".join(pairs)
        n = len(f["waits"])
        f["title"] = (f"The case waits on {who} with no deadline and no fallback" if n == 1
                      else f"{n} steps wait on one party with no deadline and no fallback")
        f["explanation"] = (f"Only one party can move the case forward at each of these points: "
                            f"{listing}. The text sets no time limit that triggers anything, "
                            f"names no substitute and defines no escalation, so if that party "
                            f"does not act the case stays there permanently." + esc)
        f["missing"] = "A time limit with a consequence, a delegate, or an escalation path."
        f.setdefault("question", f"what happens if {who} does not act at "
                                 f"“{self._label(sid)}”, or who acts in their place")
        f["why"] = ["Found each state where a single party owns every action that advances the case.",
                    "Removed those actions, to simulate that party not acting.",
                    "No timer, delegate or other party's step remains: " + listing + "."]
        self._scenario(cls, title, cfg, "problem", "No one else can move the case.",
                       [disrupt, {"kind": "stuck", "who": self._persona(),
                                  "text": "waits with no defined next step.", "state": sid}], f)

    def _deadline_gap(self, cls: str, title: str, cfg: Config, t: Transition, disrupt: dict) -> None:
        f = self._finding(
            ("DEADLINE_NO_CONSEQUENCE", t.id), type="DEADLINE_NO_CONSEQUENCE",
            title=f"A deadline with no stated consequence: “{t.deadline}”",
            explanation=(f"“{t.action}” must happen {t.deadline}, but the text never says what "
                         f"happens when it does not. The case stays in “{self._label(t.source)}” "
                         f"and readers will disagree about whether it is still open."),
            missing="What happens when this deadline passes.",
            states=[t.source], transitions=[t.id],
            evidence=self._ev(t, "States the deadline."), absence_based=True,
            question=f"what happens if “{t.action}” is not done {t.deadline}",
            why=[f"“{t.action}” carries a deadline.",
                 f"No timer leaves “{self._label(t.source)}”.",
                 "So the model has no outcome for a missed deadline."])
        self._scenario(cls, title, cfg, "ambiguous", "Deadline passes; outcome undefined.",
                       [disrupt, {"kind": "stuck", "who": "The process",
                                  "text": "says nothing about what happens now.",
                                  "state": t.source}], f)

    def _disrupt(self) -> None:
        p = self.p
        seen: set = set()

        def once(*key) -> bool:
            if key in seen:
                return False
            seen.add(key)
            return True

        for cfg in self.order:
            sid = cfg[0]
            moves = self.normal_moves(cfg)
            sig = frozenset(t.id for t in moves)

            # 1. ACTION FAILURE -------------------------------------------------
            for t in moves:
                if not t.can_fail or not once("AF", t.id, sig):
                    continue
                label = t.failure_label or f"“{t.action}” fails"
                title = f"{label[0].upper()}{label[1:]}"
                disrupt = {"kind": "disruption", "who": self._who(t), "text": label,
                           "state": sid, "transition": t.id}
                handlers = [h for h in self.out[sid]
                            if h.kind == "failure_handler" and h.on_failure_of == t.id]
                live = [h for h in handlers if self.enabled(h, cfg)]
                blocked = [h for h in handlers if not self.enabled(h, cfg)]
                if live:
                    nexts = [self.apply(h, cfg) for h in live]
                    down = self._downstream(nexts)
                    step = {"kind": "step", "who": self._who(live[0]), "text": live[0].action,
                            "state": live[0].target, "transition": live[0].id,
                            "to": self._label(live[0].target)}
                    if down:
                        self._scenario("ACTION_FAILURE", title, cfg, "problem",
                                       "Recovery leads somewhere with no way out.",
                                       [disrupt, step], down)
                    else:
                        self._scenario("ACTION_FAILURE", title, cfg, "completed",
                                       f"Handled by “{live[0].action}”.", [disrupt, step])
                    continue
                if t.retry == "allowed" and not blocked:
                    self._scenario("ACTION_FAILURE", title, cfg, "completed",
                                   "The text allows another attempt.", [disrupt])
                    continue
                others = [o for o in moves if o.id != t.id]
                timers = [o for o in others if o.trigger == "timer"]
                acts = [o for o in others if o.trigger != "timer"]
                evidence = self._ev(t, f"Defines the step that can fail: “{t.action}”.")
                why = [f"“{t.action}” is a step that can realistically fail.",
                       f"Looked for a rule covering the failure at “{self._label(sid)}”."]
                for b in blocked:
                    self.explained.add(b.id)
                    evidence += self._ev(b, f"Offers the recovery “{b.action}”.")
                    for f_ in b.requires:
                        for g in p.transitions:
                            if f_ in g.revokes:
                                evidence += self._ev(g, f"Removes “{p.fact_label(f_)}”.")
                            elif f_ in g.grants and g.id in self.used:
                                evidence += self._ev(g, f"Can restore “{p.fact_label(f_)}”.")
                    why.append(f"Found the recovery “{b.action}”, but it cannot be used here: "
                               f"{self._blocked_reason(b, cfg)}.")
                if not handlers:
                    why.append("Found no rule for it."
                               + ("" if t.retry == "unspecified" else " A retry is explicitly ruled out."))
                blocked_txt = (f" The text does offer “{blocked[0].action}”, but "
                               f"{self._blocked_reason(blocked[0], cfg)}." if blocked else "")
                if not others:
                    f = self._finding(
                        ("DEAD_END", sid, "fail", t.id), type="DEAD_END",
                        title=f"No next step when {label}",
                        explanation=(f"When {label}, the case stays in “{self._label(sid)}” and no "
                                     f"rule applies to it." + blocked_txt),
                        missing=f"A rule for what happens when {label}.",
                        states=[sid], transitions=[t.id] + [b.id for b in blocked],
                        evidence=evidence, absence_based=not blocked,
                        question=f"what happens when {label}",
                        why=why + ["No other transition leaves this state."])
                    self._scenario("ACTION_FAILURE", title, cfg, "problem", "Nothing applies.",
                                   [disrupt, {"kind": "stuck", "who": self._persona(),
                                              "text": "has no defined next step.", "state": sid}], f)
                elif not acts and all(o.target in self.terminal
                                      and p.state(o.target).outcome != "success" for o in timers):
                    tm = timers[0]
                    f = self._finding(
                        ("MISSING_RECOVERY", sid, t.id), type="MISSING_RECOVERY",
                        title=f"No way to recover when {label}",
                        explanation=(f"When {label}, there is no usable way to try again."
                                     + blocked_txt + f" The only thing left is the timer: "
                                     f"“{tm.action}”. A fault the person did not cause ends in "
                                     f"“{self._label(tm.target)}”."),
                        missing=f"A recovery path that is actually available after {label}.",
                        states=[sid, tm.target],
                        transitions=[t.id, tm.id] + [b.id for b in blocked],
                        evidence=evidence + self._ev(tm, "The only move left, and it is automatic."),
                        absence_based=not blocked,
                        question=f"what happens when {label}",
                        why=why + [f"The only remaining move is the timer “{tm.action}”, "
                                   f"which ends in “{self._label(tm.target)}”."])
                    self._scenario("ACTION_FAILURE", title, cfg, "problem",
                                   f"Ends in “{self._label(tm.target)}” by timer.",
                                   [disrupt,
                                    {"kind": "stuck", "who": "The clock", "text": tm.action,
                                     "state": tm.target, "transition": tm.id,
                                     "to": self._label(tm.target)}], f)
                else:
                    opts = ", ".join(f"“{o.action}”" for o in others)
                    f = self._finding(
                        ("FAILURE_UNADDRESSED", sid, t.id), type="FAILURE_UNADDRESSED",
                        title=f"The text does not say what happens when {label}",
                        explanation=(f"When {label}, no rule covers it and the text does not say "
                                     f"whether another attempt is allowed. The only defined "
                                     f"options from “{self._label(sid)}” are {opts}."),
                        missing=f"Whether a retry is allowed, and what happens if it also fails.",
                        states=[sid], transitions=[t.id], evidence=evidence,
                        absence_based=True, why=why,
                        question=f"what happens when {label}")
                    self._scenario("ACTION_FAILURE", title, cfg, "ambiguous",
                                   "Outcome depends on something the text leaves open.",
                                   [disrupt], f)

            # 2. NO RESPONSE and 4. ACTOR UNAVAILABLE ----------------------------
            actors = []
            for t in ([] if sid in self.terminal else moves):
                if t.trigger == "action" and t.kind == "normal" and t.actor and t.actor not in actors:
                    actors.append(t.actor)
            for a in actors:
                removed = [t for t in moves if t.actor == a and t.trigger == "action"
                           and t.kind == "normal"]
                who = p.actor_name(a)
                if once("NR", sid, a, sig):
                    self._inaction("NO_RESPONSE", cfg, a, removed,
                                   f"{who} never acts at “{self._label(sid)}”",
                                   "does not respond.")
                if a != p.subject and once("AU", sid, a, sig):
                    title = f"{who} is unavailable at “{self._label(sid)}”"
                    if all(r.delegates for r in removed):
                        d = p.actor_name(removed[0].delegates[0])
                        self._scenario("ACTOR_UNAVAILABLE", title, cfg, "completed",
                                       f"{d} can act instead.",
                                       [{"kind": "disruption", "who": who,
                                         "text": "is away and cannot act.", "state": sid}])
                    else:
                        self._inaction("ACTOR_UNAVAILABLE", cfg, a, removed, title,
                                       "is away and cannot act.")

            # 3. DELAY -----------------------------------------------------------
            for t in moves:
                if t.deadline and t.trigger == "action" and once("DL", t.id, sig):
                    who = p.actor_name(t.actor)
                    title = f"“{t.action}” happens after the deadline"
                    disrupt = {"kind": "disruption", "who": who,
                               "text": f"acts late, after “{t.deadline}”.", "state": sid,
                               "transition": t.id}
                    timers = [o for o in moves if o.trigger == "timer"]
                    if not timers:
                        self._deadline_gap("DELAY", title, cfg, t, disrupt)
                    elif t.actor == p.subject or timers[0].target not in self.terminal:
                        self._scenario("DELAY", title, cfg, "completed",
                                       f"Defined consequence: “{timers[0].action}”.",
                                       [disrupt, {"kind": "step", "who": "The clock",
                                                  "text": timers[0].action,
                                                  "state": timers[0].target,
                                                  "transition": timers[0].id,
                                                  "to": self._label(timers[0].target)}])
                    else:
                        self._inaction("DELAY", cfg, t.actor, [t], title,
                                       f"acts late, after “{t.deadline}”.")

            # 5. SIMULTANEOUS CONDITIONS -----------------------------------------
            for i, x in enumerate(moves):
                for y in moves[i + 1:]:
                    if x.target == y.target:
                        continue
                    if (x.trigger == "timer") == (y.trigger == "timer"):
                        continue  # exactly one of the two must be the clock
                    other = y if x.trigger == "timer" else x
                    if not (other.kind == "reversal" or other.trigger == "event"):
                        continue  # a plain action racing its own deadline is covered by DELAY
                    if not once("SIM", *sorted([x.id, y.id])):
                        continue
                    title = f"“{x.action}” and “{y.action}” happen at the same moment"
                    disrupt = {"kind": "disruption", "who": f"{self._who(x)} and {self._who(y)}",
                               "text": "act at the same moment.", "state": sid}
                    if x.priority is not None and y.priority is not None and x.priority != y.priority:
                        win = x if x.priority < y.priority else y
                        self._scenario("SIMULTANEOUS", title, cfg, "completed",
                                       f"The text gives precedence to “{win.action}”.", [disrupt])
                        continue
                    f = self._finding(
                        ("RACE", sid, *sorted([x.id, y.id])), type="RACE",
                        title=f"No precedence between “{x.action}” and “{y.action}”",
                        explanation=(f"From “{self._label(sid)}” both can apply at once, and they "
                                     f"lead to different outcomes: “{self._label(x.target)}” and "
                                     f"“{self._label(y.target)}”. The text does not say which wins."),
                        missing="A precedence rule for when both apply.",
                        states=[sid, x.target, y.target], transitions=[x.id, y.id],
                        evidence=self._ev(x) + self._ev(y), absence_based=True,
                        question=f"which takes precedence when “{x.action}” and "
                                 f"“{y.action}” both apply",
                        why=["Both transitions are enabled in the same situation.",
                             "They end in different states.",
                             "Neither carries a stated priority."])
                    self._scenario("SIMULTANEOUS", title, cfg, "ambiguous",
                                   "Two outcomes are both valid.", [disrupt], f)

            # 6. RETRY / REVERSAL -------------------------------------------------
            for t in moves:
                if t.kind == "reversal" and once("REV", t.id, sig):
                    nxt = self.apply(t, cfg)
                    title = f"{p.actor_name(t.actor)} chooses to “{t.action}” at “{self._label(sid)}”"
                    step = {"kind": "disruption", "who": self._who(t), "text": t.action,
                            "state": t.target, "transition": t.id, "to": self._label(t.target)}
                    down = self._downstream([nxt])
                    if down:
                        self._scenario("RETRY_REVERSAL", title, cfg, "problem",
                                       f"Stuck in “{self._label(down['states'][0])}”.",
                                       [step, {"kind": "stuck", "who": self._persona(),
                                               "text": "cannot complete it.",
                                               "state": down["states"][0]}], down)
                    else:
                        self._scenario("RETRY_REVERSAL", title, cfg, "completed",
                                       "The reversal completes.", [step])
            if p.state(sid).kind == "setback" and once("SB", sid, cfg[1]):
                title = f"{self._persona().split(',')[0]} needs to fix and resubmit from “{self._label(sid)}”"
                disrupt = {"kind": "disruption", "who": self._persona(),
                           "text": "needs to correct the problem and continue.", "state": sid}
                fate = self.fate(cfg)
                owned = [o for o in moves if not (o.trigger == "action" and o.actor is None)]
                ownerless = [o for o in moves if o.trigger == "action" and o.actor is None]
                if fate["success"] and (owned and any(self.fate(self.apply(o, cfg))["success"]
                                                      for o in owned)):
                    self._scenario("RETRY_REVERSAL", title, cfg, "completed",
                                   "A route back to the main flow exists.", [disrupt])
                elif ownerless and ("OWNERLESS", ownerless[0].id) in self.findings:
                    self._scenario("RETRY_REVERSAL", title, cfg, "problem",
                                   "The way back depends on a step nobody owns.", [disrupt],
                                   self.findings[("OWNERLESS", ownerless[0].id)])
                elif fate["stuck"] or fate["loop"]:
                    down = self._downstream([cfg])
                    self._scenario("RETRY_REVERSAL", title, cfg, "problem",
                                   "No way back.", [disrupt], down)
                else:
                    f = self._finding(
                        ("MISSING_RECOVERY", sid, "setback"), type="MISSING_RECOVERY",
                        title=f"No way back from “{self._label(sid)}”",
                        explanation=(f"“{self._label(sid)}” is a setback, not a decision, yet every "
                                     f"path from it ends in failure. The text gives no way to "
                                     f"correct the problem and continue."),
                        missing="A resubmission or correction path.",
                        states=[sid],
                        evidence=[{"sentence": e, "because": f"Describes “{self._label(sid)}”."}
                                  for e in p.state(sid).evidence],
                        absence_based=True,
                        question=f"how a case in “{self._label(sid)}” can be corrected "
                                 f"and continue",
                        why=["Searched every path forward from this setback.",
                             "None reaches a successful outcome."])
                    self._scenario("RETRY_REVERSAL", title, cfg, "problem", "No way back.",
                                   [disrupt], f)

    # ------------------------------------------------------------------- run
    def run(self) -> dict:
        p = self.p
        self._static()
        base = self.fate(self.start_cfg)
        success_cfg = next((c for c in self.order if c[0] in self.terminal
                            and p.state(c[0]).outcome == "success"), None)
        if success_cfg is not None:
            s = {"id": "S001", "cls": "BASELINE", "cls_label": CLASS_LABELS["BASELINE"],
                 "title": "Everything goes right", "state": success_cfg[0],
                 "status": "completed", "note": f"Ends in “{self._label(success_cfg[0])}”.",
                 "path": self.path_to(success_cfg),
                 "story": self._story(self.path_to(success_cfg), []), "finding": None}
            self.scenarios.append(s)
        else:
            self.scenarios.append({
                "id": "S001", "cls": "BASELINE", "cls_label": CLASS_LABELS["BASELINE"],
                "title": "Everything goes right", "state": p.start,
                "status": "ambiguous" if base["terminal"] else "problem",
                "note": "No successful outcome is reachable even when nothing goes wrong.",
                "path": [], "story": [], "finding": None})
        self._disrupt()
        self._unreachable()
        return self._report()

    def _report(self) -> dict:
        p = self.p
        findings = []
        order = {t: i for i, t in enumerate(PROBLEM_TYPES + AMBIGUOUS_TYPES)}
        for i, f in enumerate(sorted(self.findings.values(), key=lambda f: order[f["type"]])):
            conf, rests_on = 0.95, []
            if f.get("absence_based"):
                conf -= 0.12
                rests_on.append("the text not saying something")
            if any(p.transition(t).inferred for t in f["transitions"]):
                conf -= 0.15
                rests_on.append("a transition no sentence was cited for")
            if p.origin == "llm":
                conf -= 0.12
                rests_on.append("a model extracted automatically, not hand-checked")
            if not f["evidence"]:
                conf -= 0.2
                rests_on.append("no source sentence at all")
            seen, evidence = set(), []
            for e in f["evidence"]:
                if e["sentence"] in seen:
                    continue
                seen.add(e["sentence"])
                s = p.sentence(e["sentence"])
                if s:
                    evidence.append({**e, "text": s.text})
            # Compilation uncertainty: on an automatically extracted model, a finding that
            # leans on a step no sentence backs may be a flaw in the model, not in the
            # document. Report it, but never as a structural problem.
            severity = "problem" if f["type"] in PROBLEM_TYPES else "ambiguous"
            shaky = [t for t in f["transitions"] if p.transition(t).inferred]
            if p.origin == "llm" and severity == "problem" and (shaky or not evidence):
                severity = "unverified"
            key = f["key"]
            for s in self.scenarios:
                if s["finding"] == key:
                    s["finding"] = f"F{i + 1:02d}"
            findings.append({
                "id": f"F{i + 1:02d}", "type": f["type"],
                "severity": severity,
                "title": f["title"], "explanation": f["explanation"],
                "missing": f["missing"], "why": f["why"],
                "states": f["states"], "transitions": f["transitions"],
                "evidence": evidence, "scenarios": f["scenarios"],
                "scenario": f.get("scenario_title", ""),
                "story": f.get("story", []), "path": f.get("path", []),
                "absence_based": bool(f.get("absence_based")),
                "question": f.get("question") if f.get("absence_based") else None,
                "rests_on": rests_on,
                "confidence": round(max(conf, 0.3), 2),
                "confidence_label": "high" if conf >= 0.8 else "medium" if conf >= 0.6 else "low",
                "status": f["status"],
            })
        for s in self.scenarios:
            if isinstance(s["finding"], tuple):
                s["finding"] = None
        report = {"summary": {"configurations_explored": len(self.parent)},
                  "scenarios": self.scenarios, "findings": findings}
        return summarise(report)


def summarise(report: dict) -> dict:
    """(Re)compute the counts. Called again after the refutation pass changes a finding."""
    counts = defaultdict(int)
    for s in report["scenarios"]:
        counts[s["status"]] += 1
    by_type = defaultdict(int)
    for f in report["findings"]:
        by_type[f["type"]] += 1
    report["summary"] = {
        "scenarios": len(report["scenarios"]),
        "completed": counts["completed"],
        "ambiguous": counts["ambiguous"],
        "problem": counts["problem"],
        "structural_findings": sum(1 for f in report["findings"] if f["severity"] == "problem"),
        "ambiguous_findings": sum(1 for f in report["findings"] if f["severity"] == "ambiguous"),
        "unverified_findings": sum(1 for f in report["findings"] if f["severity"] == "unverified"),
        "by_type": dict(by_type),
        "configurations_explored": report["summary"].get("configurations_explored", 0),
    }
    return report


def _same_finding(f: dict, g: dict) -> bool:
    return (g["type"] == f["type"] and g["severity"] == f["severity"]
            and (set(g["transitions"]) & set(f["transitions"]) or g["states"][:1] == f["states"][:1]))


def apply_patch(process: Process, patch: List[dict]) -> Process:
    """Return a copy of the process with a few transition fields replaced."""
    changed = process.model_copy(deep=True)
    for item in patch:
        t = changed.transition(item["transition"])
        for field, value in item["set"].items():
            setattr(t, field, value)
    return changed


def _counterfactuals(engine: Engine, f: dict) -> List[dict]:
    """Changes to the *model* that would be worth testing for this finding. These are
    experiments, not recommendations: each one removes a cause the finding names."""
    p, out = engine.p, []
    if f["type"] in ("MISSING_RECOVERY", "TRAPDOOR", "UNREACHABLE", "LOOP"):
        for tid in f["transitions"]:
            t = p.transition(tid)
            if tid in engine.used or not (t.requires or t.forbids):
                continue
            needs = [f"it did not require {p.fact_label(x)}" for x in t.requires]
            needs += [f"it were still allowed when {p.fact_label(x)}" for x in t.forbids]
            out.append({"label": f"“{t.action}”: what if " + " and ".join(needs) + "?",
                        "patch": [{"transition": tid, "set": {"requires": [], "forbids": []}}]})
    if f["type"] == "OWNERLESS" and p.subject:
        for tid in f["transitions"]:
            t = p.transition(tid)
            out.append({"label": f"“{t.action}”: what if the text made {engine._the(p.subject)} "
                                 f"responsible for it?",
                        "patch": [{"transition": tid, "set": {"actor": p.subject}}]})
    return out


def dry_run(process: Process, counterfactuals: bool = True) -> dict:
    engine = Engine(process)
    report = engine.run()
    for f in report["findings"]:
        f["what_if"] = []
        if not counterfactuals or f["severity"] != "problem":
            continue
        for cf in _counterfactuals(engine, f):
            # Only offer an experiment the engine has already run and seen work.
            after = dry_run(apply_patch(process, cf["patch"]), counterfactuals=False)
            if not any(_same_finding(f, g) for g in after["findings"]):
                f["what_if"].append({**cf, "problems_after": after["summary"]["structural_findings"]})
    return report
