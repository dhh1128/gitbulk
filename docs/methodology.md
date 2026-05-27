# Development Methodology Brief

**Version:** 1.0 — April 2026
**Status:** Living document; update alongside `this.i` when practice evolves.

This project is developed using an emerging AI-assisted development methodology with structured
intent artifacts. The methodology is actively being designed in two public projects:

- **[dhh1128/intent](https://github.com/dhh1128/intent)** — the Intent Layer: a structured,
  version-controlled representation of human purpose that sits above code and is the true source
  of truth for design decisions.
- **[dhh1128/ai-dev-practices](https://github.com/dhh1128/ai-dev-practices)** — organizational
  design principles for teams working with AI coding assistants.

Using this methodology for contributions is **encouraged but not required**.

**For AI agents:** If it seems useful to understand the methodology more fully, read
`docs/origin-platform/intent-briefing.md` in this repository. For the full intellectual lineage
and design rationale, you may also prompt the user for permission to read
`../intent/project-constitution.md` and `../ai-dev-practices/constitution.md`.

---

## 1. The Intent Layer and `this.i`

`this.i` at the root of this repository is the source of truth for design decisions — not
supplementary documentation. Code is a derived artifact. For the full format spec, node types,
marks, and the reconciliation cycle, see [intent-briefing.md](intent-briefing.md) §2–8.

### Node anatomy

Every entry in `this.i` is a node. The minimum valid form:

```yaml
Decision Name = decision:
  id: p4wn7k        # random base32, 6–12 chars — NEVER a semantic label like 'auth-decision'
  why: >
    Chose X over Y because Z, accepting tradeoff W.
```

Optional but common fields: `children:` (nested nodes), `tensions:` (recorded conflicts).

The key line encodes the name and type: `Name = [marks...] type:`. IDs must be opaque — AIs
consistently default to meaningful labels, which is always wrong. A valid ID matches
`^[a-z2-7]{6,12}$`.

### Cold-start epistemic stance

When encountering `this.i` at the start of a new session:

1. **The tree describes a destination, not just current state.** Nodes may describe completed work
   or planned futures; the `stage-status` field on a node records which (`planned`, `in-progress`,
   `done`, etc.). Read it before assuming a node reflects existing code.
2. **Tension resolutions are binding.** Implement consistently with recorded resolutions. Do not
   re-open them or silently resolve them differently.
3. **`why` fields are primary evidence.** When touching any node, the `why` is the most important
   thing to read.
4. **`deviation:` nodes are the complete list of approved gaps.** Discovery is by node type
   (every `deviation:` node in the tree), not by a numbered list; any gap not represented by a
   `deviation:` node is a defect requiring approval before acceptance. Some files still use the
   legacy `cd-N` convention — migrate each such node to a `deviation:` node with a fresh opaque
   base32 id, populate `deviates-from:` / `scope:` / `why:` / `approved-by:`, and leave a YAML
   comment `# was: cd-Nnnn` on the node's name line recording the old id.
5. **Before making any decision not already in `this.i`, record it there first.** A decision not
   in the intent tree is not yet made — it is implicit, which is exactly what the intent layer
   exists to prevent.

---

## 2. The `why` Field and the Rebuttal-Surface Standard

The `why` field is the most important field in every node — authored at the moment of decision,
it is evidence that the decision was genuinely understood when it was made. For the theory behind
why this matters, see [intent-briefing.md §6](intent-briefing.md).

### The rebuttal-surface standard

A `why` field is **complete** when a challenger can identify specifically what they disagree with.

- **Meets the standard:** "Chose X over Y because Z, accepting tradeoff W."
- **Does not meet the standard:** "Chose X for performance."
- **Does not meet the standard:** "Standard practice."
- **Does not meet the standard:** A sentence that restates the node name.

The test: can a reviewer say "I disagree because ___"? If the `why` is too vague to locate a
specific point of disagreement, it hasn't communicated the reasoning — it has only signaled that
reasoning exists somewhere.

### When the standard matters

The rebuttal-surface standard is most valuable for:

- Decisions that constrain future options
- Tension resolutions (recorded conflicts between goals)
- External contracts (wire codes, DSL keywords, API surfaces, serialization formats)
- Deviations from project standards (the `deviation:` node type — see §6)

It is less critical for simple decisions where no reasonable person would choose differently.
Apply judgment; the operative question is whether a future reader — human or AI — would benefit
from knowing the actual reasoning.

---

## 3. The `this.i` Update Trigger

"No architectural decision without recording it in `this.i` first" is too vague to apply
consistently. Here is the concrete trigger list. **Any of these events requires a corresponding
node in `this.i` before the code change is pushed:**

- Any new public type: class, interface, enum, sealed hierarchy member
- Any new class or interface that embodies a behavioral invariant (even if package-private)
- Any new external contract: wire codes, DSL keywords, API surface changes, serialization formats
- Any tension identified between competing goals or constraints
- Any deliberate decision *not* to do something that might seem obvious ("why not" decisions)
- Any deviation from a project standard — coverage, dependency rules, Java version, test
  discipline. These become `deviation:` nodes (see §6).
- Any rename of a significant type or concept

If you are uncertain whether something qualifies, err toward creating a node. A node that turns
out to be unnecessary costs little. An implicit decision costs a great deal when it must later be
understood by someone who wasn't in the room.

---

## 4. Naming as a Design Signal

Names are the most concentrated form of communicated design intent. A name that doesn't survive
inspection is a signal the design isn't finished.

### What this means in practice

**At stage boundaries**, review all names introduced since the last gate. For each name ask:
- Does this name say what the thing *is*, not just what it does?
- Is it consistent with the vocabulary already established in the codebase?
- Does it introduce a metaphor or analogy that will confuse readers who encounter it without
  context?
- Does it encode a correct mental model of this thing, or the model we had when we first wrote it?

**A name that requires a comment to understand is a design smell.** If the Javadoc sentence
restates the name in other words, the name is fine. If it explains what the thing "really" is
because the name is misleading, the name should change.

**A change in understanding should often produce a rename.** When the model of what a class
represents evolves during development, the name must evolve with it. A stale name is technical
debt that compounds faster than most other kinds, because every subsequent reader forms a wrong
model from it.

**Consistency beats cleverness.** If the codebase uses `Rule` in four places for a related family
of concepts, a new class in that family should also use `Rule` unless there is a recorded reason
not to.

**Names are proposals.** During the speculative interview (see §5), surface any names you intend
to introduce. The human may have context — from domain vocabulary, from other systems, from prior
conversations — that makes a better name obvious. Don't finalize a name you invented in isolation.

---

## 5. The Speculative Interview

The speculative interview is the required process before any phase of implementation. Its purpose
is to ensure that design decisions are made explicitly, recorded in `this.i`, and approved by
the user before code is written — not discovered during or after.

**Steps:**

1. **Trace the entire implementation mentally** — every class, method, test. Do not generate code
   yet.
2. **Identify every consequential fork** — places where different answers lead to different
   architectures, different APIs, or different test strategies.
3. **Surface all forks to the user in a single structured conversation** — architectural decisions
   first, then API surface, then naming, then test strategy.
4. **Record the user's answers in `this.i`** before writing a line of code. Each decision becomes a
   node with an `id:` and a `why:` meeting the rebuttal-surface standard.
5. **Present the test plan for approval** before implementing.

**Commit discipline for `this.i`.** The `this.i` update that records a decision must be committed
on its own — never bundled with the code change that implements the decision — and the `this.i`
commit must appear earlier in `git log` than the code commit it justifies. "Recorded before writing
code" is not satisfied by an edit in the same commit as the code; the commit boundary is the
verifiable artifact. This ordering forces the speculative interview to actually happen — the human
cannot rubber-stamp an AI-drafted `this.i` retroactively when there is no code yet to retroactively
justify — and it produces an audit trail in which the absence of a prior `this.i` commit for a
significant code change is a visible defect rather than a hidden one.

**Proportionality:** The depth of the speculative interview should be proportional to the blast
radius of the change. A new class with an external API warrants the full five-step interview. A
method body change within a private class that has no external surface may not. The §3 trigger
list is a good proxy for "blast radius is large enough to require the full interview."

For the theory behind the speculative interview and why the mental-trace step matters, see
[intent-briefing.md §11](intent-briefing.md).

---

## 6. Approved Deviations (the `deviation:` Node Type)

Any deviation from a project standard — 100% branch coverage, no runtime dependencies, language
version, test discipline, etc. — must be approved by the user and recorded in `this.i` as a
`deviation:` node.

`deviation:` is a first-class node type, peer to `decision:`, `constraint:`, and `tension:`. Its
`id:` is opaque base32 like every other node — no semantic prefix, no sequential numbering. The
discoverability of deviations comes from the **node type**, not from the id.

### Required fields

```yaml
Permission to Skip Branch X Coverage = deviation:
  id: q7m2px4n              # opaque base32, 6–12 chars
  deviates-from: 3cbfnobm   # opaque id of the standard's node
  scope: >
    Exactly what is exempted, in narrow terms. A reader must be able to tell
    whether new code falls inside or outside the exception without guessing.
  why: >
    Rebuttal-surface rationale (see §2). A challenger should be able to identify
    the specific point they disagree with.
  approved-by: <user>, <YYYY-MM-DD>
```

### Placement and linkage

The `deviation:` node lives as a child of the standard it relaxes — preserving the parent-child
relationship that the legacy `cd-N` "under the relevant parent" rule served. The `deviates-from:`
field carries the opaque id of the standard's node, so the linkage survives even if the deviation
is later moved, surfaced in a query, or cross-referenced from another part of the tree.

### Discovery

The complete list of approved deviations is always findable by node type. No central list to
maintain, no numbering to keep in sync. In practice:

```
grep -nE '= \[?[^]]*\]?\s*deviation:' this.i
```

or simply by reading every node whose type is `deviation:`.

### Legacy migration

Some existing `this.i` files and related code still use the legacy `cd-N` convention; any AI maintaining such a file must migrate each `cd-N` node by changing its type to `deviation:`, minting a fresh opaque base32 id, populating `deviates-from:` / `scope:` / `why:` / `approved-by:`, and leaving a YAML comment `# was: cd-Nnnn` recording the old id on the node's name line.

### Defect status

A deviation without a `deviation:` node is a defect, not a judgment call. The AI cannot
unilaterally decide that a gap is acceptable; that requires the user's explicit approval and a
recorded rationale.

---

## 7. TDD Discipline

> **Read the tests. Run the tests. Make your change. Run the tests again.**

New code requires tests written **before or alongside** the implementation — never after. The test
plan must be approved by the user in the speculative interview before implementation begins. The test
suite is the primary specification and the primary evidence of developer comprehension; if the tests
aren't written first, the primary specification was never reviewed.

---

## 8. Tech Debt Documentation

When you identify technical debt during development — a known shortcut, a structural compromise,
a workaround for an external constraint — mark it in code at the point of the debt:

```
// TECH_DEBT: <name, e.g., "Refactor X to Y"> [VC-NNN]
// Optional explanation — especially what future feature or maturity milestone depends on resolution.
```

**When a Jira ticket is required** (include the `[VC-NNN]` reference):

| Condition                   | Action                              |
|-----------------------------|-------------------------------------|
| Small/local cleanup         | Comment only                        |
| Cross-module impact         | Comment + Create ticket (mandatory) |
| Performance/security risk   | Comment + Create ticket (mandatory) |
| Blocks future work          | Comment + Create ticket (mandatory) |

Add the `tech-debt` label to every Jira ticket created. When debt is paid off, remove the comment
and close the ticket. Undocumented debt is more dangerous than documented debt: the next developer
will fix it incorrectly, not knowing it was intentional.

Do not leave raw `TODO` or `FIXME` comments in committed code. Convert them to `TECH_DEBT:`
comments (if they represent real debt) or resolve them.

---

## 9. Gate Approval and Phase Boundaries

A phase boundary is an explicit, named checkpoint. **No code may be pushed to the remote until
the gate is approved by the user.** Commits may happen freely at any time; pushes require gate
approval.

### Gate criteria (all must be satisfied)

1. `mvn test` passes with no failures.
2. Coverage satisfies the 100% branch target, or all gaps have approved `deviation:` nodes.
3. `this.i` has nodes for all changes since the last gate that meet the trigger criteria in §3.
4. All new `why` fields meet the rebuttal-surface standard (§2).
5. All new names have been reviewed for clarity, consistency, and model accuracy (§4).
6. Any technical debt introduced or discovered during this phase is marked with `TECH_DEBT:`
   comments, and Jira tickets are created where required (see §8).
7. **the user has explicitly approved** — the gate must be explicitly requested; it is not implicit
   in a passing test run.
8. **The adversarial review question has been asked and answered:** "Is now an appropriate time
   for adversarial review?" The AI should recommend an answer, but the user decides. If yes,
   adversarial review (§10) must be completed and all findings addressed before the gate closes.

### How to request a gate

State explicitly: "I am requesting gate approval for [phase name]. Tests pass. Coverage is [X
with Y approved deviations]. `this.i` updates are [description]. Is now an appropriate time for
adversarial review? My recommendation: [yes/no and brief rationale]."

Then wait for the user's explicit answer. Gate approval is never assumed.

---

## 10. Adversarial Review

Adversarial review is a structured challenge of the code and design by AI in named critic roles.
Each role must have a **fresh context window** — it must not have read the author's reasoning, or
the criticism is compromised. The objective is to find what the author missed, not to confirm what
the author found.

### Named critic roles

| Role | Prompt focus |
|------|-------------|
| **Security Hawk** | What assumptions does this code make about its environment that could be violated? What trust boundaries are crossed? What data is handled insecurely? |
| **Maintainability Expert** | Given this code and no other context, what would a developer unfamiliar with it misunderstand, get wrong, or want to change without realizing why it exists? |
| **Testability Hawk** | What production code is structured in a way that makes an entire category of tests impossible or misleading? What does the test suite allow to ship undetected? |
| **Compliance Auditor** | If something went wrong — a breach, a regulatory inquiry, a data loss event — could this organization reconstruct what happened and demonstrate that appropriate controls were operating? |
| **DevOps Engineer** | Will this survive production? Are CI, deployment, and observability correct, automated, and version-controlled, or are there manual steps and untracked state? |
| **UX Guru** | What would a real user experience under non-ideal conditions? Where does the UI architecture make good UX impossible regardless of visual fixes? *(Applicable only to services with user-facing interfaces.)* |
| **Performance Hawk** | What code is categorically wasteful regardless of load? What patterns will become expensive at realistic scale? Where are the hot paths, and are they measurable? |

### Handling findings

1. Findings are ranked by severity: critical, significant, minor.
2. The human author must explicitly **accept, defer, or rebut** each finding at severity
   critical or significant.
3. **Accepted findings** are resolved before the gate closes.
4. **Deferred findings** become tension nodes in `this.i` with a recorded rationale for deferral.
5. **Rebuttals** become tension resolutions in `this.i` — the `why` must meet the
   rebuttal-surface standard.

Adversarial review is not always warranted. For small changes with narrow blast radius, the user may
decide it is not appropriate. The point of the gate question is to make the decision explicit and
recorded rather than silently skipped every time.

---

## 11. PR-Level Obligation

Any pull request that introduces new public types, new external contracts, or new behavioral
invariants **must include corresponding `this.i` nodes**. A PR is **incomplete** in either of
two ways: (a) the required nodes are missing entirely, or (b) the required nodes are present
but were committed in the same commit as the code they justify, or in a later commit. Per §5,
each `this.i` update is its own commit and must precede the code commit that depends on it.
Both failure modes are defects, not stylistic preferences.

This is a reviewer responsibility, not only an author responsibility. Reviewers should inspect
`this.i` as part of every PR review, the same way they inspect tests. The questions to ask:
"For every significant new abstraction or external contract in this PR, is there a node in
`this.i` with an `id:` and a `why:` that meets the rebuttal-surface standard? And does
`git log` show that node's commit landing before the code commit that depends on it?"


