# Dry Run — Audit & Improvement Checklist

> Working audit document for the current hackathon build.
>
> Goal: make the existing core trustworthy before adding features.
>
> **Core principle:**
> **The model interprets. The engine verifies.**

---

# 1. Core Extraction Problems

## 1.1 LLM → Process Model is currently unreliable

The hand-checked admission model and the LLM-extracted model disagree significantly.

Hand-checked model:

* 12 reachable configurations
* 5 structural problems
* correctly detects `MISSING_RECOVERY`

LLM-extracted model:

* only 9 states
* misses `MISSING_RECOVERY`
* produces several `OWNERLESS` / `UNREACHABLE` findings that appear to be extraction artifacts

### Question to solve

Is the deterministic engine actually wrong, or is it being given an incorrect process model?

Current evidence strongly suggests the **compiler/extraction layer is the weak link**.

---

# 2. Too Many Inferred Transitions

Current extraction produced approximately:

> 12 inferred transitions out of 19

This is problematic for an evidence-backed debugging system.

A transition should ideally be:

* explicitly grounded in source text, or
* clearly marked as inferred,
* and inferred relationships should NOT automatically produce high-confidence structural findings.

Need to distinguish:

* `explicit`
* `strongly_implied`
* `inferred`
* `unknown`

The system should never silently turn a model guess into process truth.

---

# 3. Unknown Sentence IDs

Current extraction reported:

> 8 references to unknown IDs dropped

The model sometimes generates sentence IDs that do not exist in the document.

Need stronger evidence validation.

Possible rule:

> Every piece of evidence must reference an actual source sentence.

If it cannot, discard or downgrade it rather than silently using it.

---

# 4. Recovery / Retry Representation is Too Weak

This is the clearest concrete extraction bug.

Admission example:

* c6: failed payment may be retried from payment page
* c7: payment page is available only to `Active`
* c4: initiating payment changes `Active` → `Payment Pending`

The actual structural bug is:

> Retry is promised by the policy, but after payment initiation/failure the retry action may no longer be executable because its prerequisite (`Active`) has been revoked.

Current failure schema only has:

```text
sentence
goes_to
retry
```

Because c6 does not explicitly name a destination state, `goes_to = null`.

Therefore:

> `retry = allowed`

does not become an executable recovery path.

### Required improvement

Represent recovery as an actual action with:

* action
* actor
* source state / context
* target state if applicable
* prerequisites
* effects
* evidence
* failure condition
* recovery condition

---

# 5. Important Relationships are Distributed Across Sentences

The payment example requires combining:

* c4 — state changes after payment initiation
* c6 — retry is allowed
* c7 — payment page requires Active
* c8 — timer cancels the application

The bug only becomes visible when these clauses are combined.

Current extraction uses:

```text
WINDOW = 8
```

This risks breaking relationships across distant sentences.

### Required improvement

Need a way to maintain:

> global document understanding + local evidence grounding

without exploding LLM latency.

---

# 6. Too Many LLM Calls / Unacceptable Latency

Real extraction currently took approximately:

> **19 minutes**

Observed problems:

* some calls took 1–2.5 minutes
* one call took approximately 10 minutes and returned HTTP 500
* many narrow follow-up questions were asked
* extraction eventually completed but is not demo-acceptable

Current architecture contains:

* actor/state extraction
* multiple transition extraction calls
* facts/rules extraction
* failure follow-ups
* actor/delegate follow-ups
* final-state follow-ups
* refutation calls

### Required improvement

Reduce LLM calls drastically.

Target architecture should be closer to:

```text
Document
    ↓
1–2 strong structured extraction calls
    ↓
Validation / normalization
    ↓
Deterministic engine
    ↓
Failure simulation
    ↓
Finding
```

Do not replace the LLM with a pile of new heuristic calls.

---

# 7. Too Much Reasoning is Being Delegated Back to the LLM

Some questions should become deterministic once the process model exists.

Examples:

* Is a state reachable?
* Does a state have an outgoing transition?
* Can an action execute under current prerequisites?
* Is a transition blocked?
* Is there a valid recovery path?
* Can two transitions happen simultaneously?
* Does a failure lead to a dead end?

The LLM should interpret the document.

The engine should reason over the structured representation.

### Principle

> **LLM interprets. Code reasons.**

---

# 8. Facts / Rules are Not Connected Strongly Enough to the Process Model

Example:

> “The payment page is available only to applications with status Active.”

This is not just a generic fact.

It is an **executable prerequisite**.

Likewise:

> “When payment is initiated, status changes from Active to Payment Pending.”

This is an **effect** that changes which actions are subsequently executable.

The structured model needs first-class support for:

* prerequisites
* prohibitions
* effects
* grants
* revocations
* recovery conditions

---

# 9. State Extraction is Incomplete

Reference model:

> 12 reachable configurations

Extracted model:

> 9 states

Need to investigate whether the extractor is incorrectly collapsing:

* lifecycle states
* statuses
* temporary states
* conditions
* terminal outcomes
* combinations of status + facts

A policy does not always express every meaningful executable configuration as an explicit named state.

---

# 10. False OWNERLESS Findings

Example extracted finding:

> Nobody is responsible for “handle: Payment failed”

This is misleading.

The document explicitly says the applicant may retry.

The real problem is closer to:

> The retry action exists, but its prerequisites may be unreachable.

Need to prevent extraction errors from turning nuanced structural defects into generic `OWNERLESS` findings.

---

# 11. False UNREACHABLE Findings

Example:

> “admissions portal initiates document verification process” can never actually be used

But c5 explicitly says:

> Once the bank confirms payment, the application moves to document verification.

Therefore a finding like this may indicate an **incorrect compiled model**, not an actual document defect.

Important distinction:

```text
REAL DOCUMENT BUG
vs.
MODEL COMPILATION BUG
```

The system should not confidently present the latter as the former.

---

# 12. Extraction Uncertainty Needs to be a First-Class Concept

Current finding types include things like:

* `UNREACHABLE`
* `OWNERLESS`
* `TRAPDOOR`
* `FAILURE_UNADDRESSED`
* `RACE`

But there is no clear category for:

> “We could not compile this part of the document confidently enough to reason about it.”

Need something like:

```text
COMPILATION_UNCERTAINTY
```

or equivalent internal handling.

If the compiler does not have enough grounded information:

> do not invent a structural conclusion.

Fail safely.

---

# 13. Confidence Numbers Need Reconsideration

Current findings can show values like:

* 95%
* 83%
* 71%
* 68%

Need to determine exactly what these percentages mean.

Potentially distinguish:

* finding confidence
* extraction confidence
* evidence coverage
* simulation strength

Avoid presenting arbitrary percentages as if they were statistically calibrated probabilities.

A finding supported by:

* 3 source clauses
* a reproducible simulation
* no contradictory evidence

is fundamentally different from a finding based on:

* an inferred transition
* one ambiguous clause.

---

# 14. Distinguish Types of Problems

Not everything should be treated as the same type of defect.

Potential categories:

### Structural defect

The process cannot perform something it claims to support.

Example:

> Retry exists but is unreachable.

### Missing rule

The document does not define what happens.

Example:

> Deadline exists but no consequence is specified.

### Ambiguity

Multiple interpretations are possible.

### Compilation uncertainty

The document may be fine, but the system cannot safely map it into an executable model.

These should not be conflated.

---

# 15. Current Demos are Hand-Checked / Constructed

Current demos are useful for validating the deterministic engine.

However, they do not prove that Dry Run can discover bugs in documents it has never seen.

Need at least one **real public document**.

Ideal test:

```text
Real document
    ↓
Dry Run
    ↓
Finding
    ↓
Human verifies source
    ↓
Finding is genuinely supported
```

Even better:

```text
Real document
    ↓
Finding
    ↓
Fix document
    ↓
Run again
    ↓
Finding disappears
```

This would demonstrate the actual debugger loop.

---

# 16. Need a Fix → Rerun Loop

Dry Run should feel like:

```text
Find bug
    ↓
Fix process
    ↓
Run again
    ↓
Verify bug is gone
```

Not simply:

```text
Upload document
    ↓
Receive list of risks
```

This distinction is important for positioning.

---

# 17. Risk of Looking Like a Generic AI Policy Analyzer

Current product could be misunderstood as:

```text
Upload policy
    ↓
LLM reads policy
    ↓
LLM lists risks / ambiguities
```

That is not the intended identity.

Dry Run should demonstrate:

```text
Policy
    ↓
Natural-language compilation
    ↓
Executable process model
    ↓
Failure injection
    ↓
Deterministic execution
    ↓
Structural finding
    ↓
Evidence + trace
```

The simulation must be central.

---

# 18. AI Must Have a Meaningful Role

Need to clearly answer:

> Why use an open-weight LLM at all?

Answer:

Humans write procedures in natural language.

Dry Run uses the open-weight model as a:

> **natural-language → executable-process compiler**

Without the model, users would have to manually construct the state/process model.

The model should NOT be the final authority on whether the process is broken.

---

# 19. Avoid Becoming “Just a State-Machine Simulator”

A state-machine simulator by itself is not the main innovation.

The important pipeline is:

> **Natural language → executable process → adversarial failure simulation**

The AI/compiler layer is what allows ordinary documents to become executable without requiring the user to understand formal state machines.

---

# 20. Failure Injection Needs to be Meaningful

Scenarios should not just be generic questions like:

> “What if payment fails?”

The system should actually execute the process under failure conditions.

Potential scenarios:

* payment fails
* person does not respond
* deadline expires
* document is rejected
* required document is missing
* approval is denied
* external dependency is unavailable
* retry is attempted
* cancellation occurs
* actor does nothing
* two events occur simultaneously
* recovery is attempted

The engine should simulate these rather than asking the LLM whether they are bad.

---

# 21. Demos May Currently Be Too Simple

University admission / expense reimbursement / travel approval are understandable, which is good.

But at least one demo should contain:

* multiple actors
* rules distributed across the document
* failure + recovery
* cross-referenced clauses
* a non-obvious structural issue

The goal is to demonstrate that Dry Run discovers something that is difficult to spot by simply reading the document.

---

# 22. UI / Demo Flow Risk

The current UI design does not necessarily need a redesign.

But the demo should emphasize:

```text
Document
→ Process Model
→ Scenario
→ Execution Trace
→ Finding
→ Evidence
```

rather than:

```text
Document
→ AI Analysis
→ Risk List
```

The product should visually communicate **execution/debugging**, not document summarization.

---

# 23. Open-Source Requirements

Verify:

* public GitHub repository
* open-source license
* README explains architecture
* model is clearly identified
* model license/terms are respected
* third-party licenses are documented
* no proprietary API is required for the core flow
* open-weight model performs meaningful work
* repository is actually runnable by another developer

Potential polish:

* `.gitignore`
* GitHub Actions
* pytest badge
* clean setup instructions
* example documents
* example outputs
* model setup instructions

---

# 24. Open-Weight AI Must Not Look Decorative

Need to be able to explain:

> Remove the LLM → users must manually create the executable process model.

That establishes why AI is part of the core system.

But:

> Remove the LLM → the deterministic engine can still execute a manually-created model.

That is acceptable and intentional.

The verifier should remain deterministic.

---

# 25. Need an Extraction Benchmark

Since hand-checked models already exist, use them as ground truth.

Measure things such as:

* state extraction accuracy
* transition extraction accuracy
* evidence citation accuracy
* actor accuracy
* prerequisite/effect accuracy
* recovery extraction accuracy
* false positives
* false negatives

Even a small benchmark across 3–5 documents would make the project substantially more credible.

---

# 26. Separate Compiler Evaluation from Engine Evaluation

These are two different tests.

## Compiler

> Did the document become the correct process model?

## Engine

> Given the correct process model, did the engine correctly detect the defect?

Do not mix these.

The current admission example can be used to test both independently.

---

# 27. Add Regression Tests for Known Failure Modes

At minimum, preserve tests for:

### Missing recovery

Retry exists in the document but is not executable.

### Trapdoor

A state can be entered but has no valid exit.

### Silent default

If an actor does nothing, the process silently terminates.

### Ownerless action

A required action has no responsible actor.

### Unreachable transition

A documented transition cannot actually be reached.

### Deadline without consequence

A deadline exists but no defined consequence exists.

### Race / simultaneous events

Two events can happen without defined precedence.

These should become permanent regression tests.

---

# 28. Do Not Hallucinate Repairs

Dry Run's primary role is:

> detect + explain + prove

It should not silently invent policy fixes.

For example, it should not automatically decide:

> “The applicant should be allowed to retry from Payment Pending.”

unless that is clearly presented as a separate suggested repair.

Verification should remain separate from policy generation.

---

# 29. Every Finding Needs Three Layers

Every important finding should ideally contain:

## Evidence

What exact source clauses support the finding?

## Simulation

What sequence of events was executed?

## Conclusion

What structurally happened?

Example:

```text
Evidence:
c6 allows retry.
c7 requires Active for the payment page.
c4 changes Active → Payment Pending.

Simulation:
Accept → Initiate payment → Bank declines.

Result:
Retry is mentioned by the policy but is not executable
under the resulting process state.
```

This is significantly stronger than an LLM-generated explanation.

---

# 30. Test Arbitrary Real-World Documents

Eventually Dry Run should be tested on documents such as:

* university procedures
* reimbursement policies
* HR procedures
* application processes
* refund policies
* government schemes
* compliance procedures

The key question:

> Can Dry Run compile a document that was NOT written specifically for Dry Run?

---

# 31. Graceful Failure When Compilation is Weak

If the model cannot confidently compile a section, Dry Run should not fabricate structure.

Instead:

```text
Compilation uncertainty:
This section could not be mapped confidently enough
to safely simulate.
```

This is preferable to a confident but incorrect finding.

---

# 32. Main Architectural Goal

Current approximate architecture:

```text
Natural language
    ↓
LLM guesses states
    ↓
LLM guesses transitions
    ↓
LLM guesses facts
    ↓
LLM answers follow-ups
    ↓
Engine reasons
```

Target:

```text
Natural language
    ↓
Grounded compilation
    ↓
Validated executable process model
    ↓
Deterministic simulation
    ↓
Structural proof
    ↓
Finding + evidence
```

---

# 33. Priority Order

## P0 — Must Fix

1. Grounded extraction accuracy
2. Recovery / retry representation
3. False `OWNERLESS` findings
4. False `UNREACHABLE` findings
5. Too many inferred transitions
6. Unknown sentence IDs
7. 19-minute latency
8. Evidence/source grounding
9. Compiler uncertainty / fail-safe behavior

## P1 — Must Validate

10. Real public document test
11. Fix → rerun loop
12. Compiler vs engine benchmark
13. Regression tests
14. Demonstrate at least one genuinely discovered bug

## P2 — Product / Demo

15. Make debugger vs analyzer distinction obvious
16. Show simulation trace as the core experience
17. Make open-weight AI's role explicit
18. Sharpen natural-language → executable-process positioning

## P3 — Final Polish

19. README / open-source compliance
20. Model and dependency licensing
21. GitHub Actions / tests
22. Demo flow
23. Final presentation / pitch

---

# 34. What NOT to Do Yet

Do NOT prioritize:

* chatbot
* multi-agent architecture
* generic AI risk score
* large dashboard
* unnecessary UI redesign
* random extra finding types
* AI-generated policy rewriting
* unnecessary features just to make the project look bigger

The priority is:

> **Make the existing core trustworthy.**

---

# 35. Core Product Principle

> **Dry Run is not an AI that reads a policy and tells you whether it looks risky.**
>
> It compiles natural-language procedures into an executable process model, deliberately injects failure scenarios, runs those scenarios through a deterministic verifier, and produces evidence-backed structural findings.

### Shortest version

> **Unit tests for human processes.**

### Technical version

> **Open-weight LLM for grounded compilation + deterministic process verification.**

### Core line

> **The model interprets. The engine proves.**

---

# 36. Questions Claude Must Answer Before Making Changes

1. What is currently causing the extraction mismatch?
2. Why are so many transitions inferred?
3. Why are sentence IDs being hallucinated?
4. How should retry/recovery be represented?
5. How should prerequisites and effects be represented?
6. How can cross-sentence relationships remain grounded?
7. How can LLM calls be reduced substantially?
8. How should compiler uncertainty be represented?
9. How can false structural findings caused by bad extraction be prevented?
10. What is the smallest architecture change that fixes the above?
11. Can the admission model become a regression benchmark?
12. Can the system successfully process a real public document?
13. Can we demonstrate a real bug discovered rather than planted?
14. Does the current architecture genuinely justify the “policy debugger” positioning?
15. Is the open-weight model doing meaningful work?
16. What should be changed before the final demo, and what should explicitly NOT be changed?

---

# Final Constraint

**Do not make Dry Run bigger before making it more trustworthy.**

The target is not more features.

The target is:

```text
Natural language
        ↓
Grounded executable model
        ↓
Failure injection
        ↓
Deterministic execution
        ↓
Evidence-backed structural finding
```

**The model interprets. The engine verifies.**
