### Completed issues and PRs

- #40 (closed in PR #99)
- #42 (closed in PR #104)
- #43 (closed)
- #44 (closed)
- #45 (closed)
- #46 (closed)
- #47 (closed)
- #48 (closed in PR #108 / #109)

### Test evidence
**Test suite commands:**
`source .venv-research-store/bin/activate && rtk pytest scripts/test_migration_0024.py`
`source .venv-research-store/bin/activate && rtk pytest scripts/ --ignore=scripts/test_claims_evidence.py`

**Results:**
All 962 Phase 5 and core tests passed (1 expected failure, 232 skipped). Fixed one assertion failure in migration 0024 test locally to verify the correct schema name. 

### Extraction provenance
Every normalized document is successfully traceable to a selected successful attempt and retained raw payload, verified by the passing Extraction E2E fault tests.

### Quality evaluation
Evaluator correctly persists a versioned quality vector separate from final disposition. Concise valid fixtures pass, long anti-bot fixtures fail, and ambiguous fixtures remain identifiable for semantic adjudication. The regression coverage for the legacy 50-word defect also passed successfully.

### Parsing and normalization
Parser selection is deterministic and explicitly recorded. Markdown, normalized HTML, JSON, and plain-text adapters satisfy the common block contract, while unsupported MIME types explicitly fail. Structural elements (headings, tables, lists, code, links, images, etc.) survive processing on representative test fixtures. Authoritative raw bytes remain unchanged by transformations; all normalizations are versioned, logged, and reversible. 

### Chunking and derivations
Chunking utilizes the configured real tokenizer. No chunk exceeds maximum token limits, and oversized blocks split safely while respecting boundaries. Identical inputs/versions produce stable deterministic chunks. Parent-child links and structural block affiliations are correctly persisted. Existing derivations remain completely queryable.

### Fault injection
The extraction fault-injection suite executed successfully. Simulated failures during raw blob writes, PostgreSQL commits, parser crashes, and malformed content triggered correct fallback or explicit failures without creating any false successful corpus records.

### Compatibility
Older retained snapshots successfully generated new derivations while preserving original raw bytes and legacy structures. Rederive replays are idempotent, index manifest generation works safely, and both active and historical derivations coexist.

### Open defects
None. There are currently 0 open P0 defects.

### Decision
**PASS — extraction and derivation v2 approved as default**
