# Authoritative workflows

This document defines current operational examples. `SKILL.md` is the active runtime contract. Historical issue/remediation documents are non-normative when they conflict with this file or `SKILL.md`.

## Canonical autonomous research

The normal agent supplies a high-level objective and lets application code own progression:

```bash
scripts/fresearch run "Research objective"
```

Persist the returned public `fr_<uuid>` only. Follow the typed disposition rather than free-text guidance:

```bash
scripts/fresearch continue fr_<uuid>
scripts/fresearch status fr_<uuid>
scripts/fresearch result fr_<uuid>
```

Do not insert low-level lifecycle operations between those commands. The controller performs retained-first review, bounded acquisition when required, evidence preparation, coverage evaluation, and completion progression through application services.

A terminal partial result remains partial and has `objective_satisfied=false`. Process exit `0` is not evidence that the objective was satisfied.

## Canonical final delivery

`host_handoff` is the default normal-agent delivery mode:

```bash
scripts/fresearch run --delivery-mode host_handoff "Research objective"
scripts/fresearch result fr_<uuid>
```

The terminal result returns/references a bounded citation-ready handoff tied to the exact public run, persisted ResearchSpec, coverage/evidence revisions, and completion provenance. The host agent may draft prose from that handoff without invoking a redundant inner full-prose synthesis model.

For an explicit standalone internally generated report, select the existing synthesis path at run creation:

```bash
scripts/fresearch run --delivery-mode self_synthesized "Research objective"
```

Delivery choice does not alter completion authority.

## Operator actions

When the directive is `operator_action_required`, inspect the returned public action:

```bash
scripts/fresearch action oa_<uuid>
```

Resolve only the human decision represented by that action. Examples:

```bash
scripts/fresearch approve oa_<uuid> --reason "approved bounded soft exception" --authorized-by "operator"
scripts/fresearch curate oa_<uuid> --retain <subject-uuid> --reject-rest --reason "curated evidence" --authorized-by "operator"
scripts/fresearch fork oa_<uuid> "Revised research objective" --reason "material scope change" --authorized-by "operator"
```

Then continue the public run returned by the controller. Hard budget violations are not approvable. Material scope change creates a child run; there is no canonical in-place mutation of the parent ResearchSpec meaning.

## Retained-only and curated research

Use high-level policy at run creation rather than outer-agent lifecycle choreography:

```bash
scripts/fresearch run --retained-only "Research objective"
scripts/fresearch run --curated "Research objective"
```

Retained-only never opens provider acquisition. Curated mode pauses at the durable `oa_` selection boundary and controller-owned progression resumes after the human submission.

## Specialist direct acquisition

`frun`, `fsearch`, `fscrape`, `finspect`, `research-db`, and `candidate-budget` remain supported specialist/operator/debug surfaces. They are not the canonical autonomous workflow and are intentionally omitted from normal agent choreography here. Consult the dedicated lifecycle/runbook references before using them for explicit low-level integration work.

Direct Firecrawl MCP/SDK/raw-HTTP/provider calls are never an alternate authoritative skill path.

## Projection recovery

Qdrant is rebuildable and cannot become workflow/corpus authority. For an operator-directed projection rebuild:

```bash
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db index-activate <index-id>
```

Verify PostgreSQL job/manifest completion before activation. Valkey loss must not strand durable work.

## Authority summary

- PostgreSQL: workflow, provenance, specs/plans, decisions, evidence membership, claims/bindings, durable jobs.
- `BLOB_ROOT`: immutable payload bytes.
- Qdrant: rebuildable vector projection.
- Valkey: transient coordination.
- Currentness: ResearchSpec obligations plus canonical publication/update provenance, never retrieval time or agent judgment.
