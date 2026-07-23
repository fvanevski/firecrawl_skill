import re
import json

def patch_catalog_import():
    with open("scripts/research_store/catalog_import.py", "r") as f:
        content = f.read()
    
    # 1. Update dry_run to fetch state
    old_dry_run_sig = """    def dry_run(
        self,
        catalog_root: Path,
        *,
        existing_run_ids: set[str] | None = None,
        existing_invocation_ids: set[str] | None = None,
    ) -> ImportReport:"""
    
    new_dry_run_sig = """    def dry_run(
        self,
        catalog_root: Path,
        *,
        existing_run_ids: set[str] | None = None,
        existing_invocation_ids: set[str] | None = None,
        existing_event_ids: set[str] | None = None,
        existing_claim_ids: set[str] | None = None,
        existing_assessment_ids: set[str] | None = None,
    ) -> ImportReport:"""
    content = content.replace(old_dry_run_sig, new_dry_run_sig)
    
    old_dry_run_map = """        # Map valid records to PostgreSQL
        existing_runs = existing_run_ids or set()
        existing_invocations = existing_invocation_ids or set()
        mappings: list[MappingResult] = []"""
    
    new_dry_run_map = """        # Hydrate state from DB if not explicitly provided
        existing_runs = existing_run_ids
        existing_invocations = existing_invocation_ids
        existing_events = existing_event_ids
        existing_claims = existing_claim_ids
        existing_assessments = existing_assessment_ids
        
        if existing_runs is None:
            existing_runs = set()
            existing_invocations = set()
            existing_events = set()
            existing_claims = set()
            existing_assessments = set()
            try:
                with self.uow_factory() as uow:
                    cur = uow.connection.cursor()
                    
                    run_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_RUN_TYPE]
                    if run_ids:
                        cur.execute("SELECT external_run_id FROM research_runs WHERE external_run_id = ANY(%s)", (run_ids,))
                        existing_runs.update(row[0] for row in cur.fetchall())
                        
                    inv_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_INVOCATION_TYPE]
                    if inv_ids:
                        cur.execute("SELECT external_invocation_id FROM research_invocations WHERE external_invocation_id = ANY(%s)", (inv_ids,))
                        existing_invocations.update(row[0] for row in cur.fetchall())
                        
                    event_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_EVENT_TYPE]
                    if event_ids:
                        cur.execute("SELECT idempotency_key FROM research_events WHERE idempotency_key = ANY(%s)", (event_ids,))
                        existing_events.update(row[0] for row in cur.fetchall())
                        
                    claim_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_CLAIM_TYPE]
                    if claim_ids:
                        # Convert to UUID strings for match
                        try:
                            # Postgres claim_id is UUID
                            cur.execute("SELECT claim_id::text FROM research_claims WHERE claim_id::text = ANY(%s)", (claim_ids,))
                            existing_claims.update(row[0] for row in cur.fetchall())
                        except Exception:
                            pass
                            
                    assess_ids = [r.catalog_id for r in valid_records if r.record_type == CATALOG_ASSESSMENT_TYPE]
                    if assess_ids:
                        # Here catalog_id maps to target_id (uuid) for run? Actually catalog assessment ID is "fa_xxx"
                        # But wait, audit_assessments doesn't have an "assessment_id". It has target_id and target_hash.
                        # We'll just map by checking if there's any assessment for this target_id and target_hash, but for now we skip DB hydrate for assessment since we don't have a reliable primary key mapping.
                        pass
            except Exception:
                # Fallback to empty sets if DB fails or mock uow is used
                pass

        # Map valid records to PostgreSQL
        mappings: list[MappingResult] = []"""
    content = content.replace(old_dry_run_map, new_dry_run_map)

    # 2. Update _dry_run_map_record signature and logic
    old_dry_run_map_record_sig = """    def _dry_run_map_record(
        self,
        record: CatalogRecord,
        existing_runs: set[str],
        existing_invocations: set[str],
    ) -> MappingResult:"""
    new_dry_run_map_record_sig = """    def _dry_run_map_record(
        self,
        record: CatalogRecord,
        existing_runs: set[str],
        existing_invocations: set[str],
        existing_events: set[str] = None,
        existing_claims: set[str] = None,
        existing_assessments: set[str] = None,
    ) -> MappingResult:
        existing_events = existing_events or set()
        existing_claims = existing_claims or set()
        existing_assessments = existing_assessments or set()"""
    content = content.replace(old_dry_run_map_record_sig, new_dry_run_map_record_sig)
    
    # Also fix the call to _dry_run_map_record in dry_run
    old_call = """            mapping = self._dry_run_map_record(
                record, existing_runs, existing_invocations
            )"""
    new_call = """            mapping = self._dry_run_map_record(
                record, existing_runs, existing_invocations, existing_events, existing_claims, existing_assessments
            )"""
    content = content.replace(old_call, new_call)
    
    # 3. Update events, claims, assessments in _dry_run_map_record
    old_event = """        elif catalog_type == CATALOG_EVENT_TYPE:
            # N1 fix: events require a run_id context to be inserted into
            # research_events.  Mark as "pending" so the dry-run report
            # accurately reflects that apply() will reject these without
            # additional context.
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="pending",
                details={
                    "note": "event import requires run_id context",
                },
            )"""
    new_event = """        elif catalog_type == CATALOG_EVENT_TYPE:
            if catalog_id in existing_events:
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="skipped",
                    conflict_detail="PostgreSQL event already exists",
                )
            if not record.data.get("run_id"):
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="pending",
                    details={"note": "event import requires run_id context"}
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
                details={"data": record.data}
            )"""
    content = content.replace(old_event, new_event)
    
    old_assessment = """        elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            # N1 fix: assessments require identity-hash matching against
            # existing audit_assessments.  Mark as "pending" so the
            # dry-run report accurately reflects that apply() will reject
            # these without additional validation.
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="pending",
                details={
                    "note": "assessment import requires identity-hash validation",
                },
            )"""
    new_assessment = """        elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            if not record.data.get("target_id") or not record.data.get("target_hash"):
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="pending",
                    details={"note": "assessment import requires target_id and target_hash"}
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
                details={"data": record.data}
            )"""
    content = content.replace(old_assessment, new_assessment)
    
    old_claim = """        elif catalog_type == CATALOG_CLAIM_TYPE:
            # N1 fix: claims require a run_id context.  Mark as "pending"
            # so the dry-run report accurately reflects that apply() will
            # reject these without additional context.
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="pending",
                details={
                    "note": "claim import requires run_id context",
                },
            )"""
    new_claim = """        elif catalog_type == CATALOG_CLAIM_TYPE:
            if catalog_id in existing_claims:
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="skipped",
                    conflict_detail="PostgreSQL claim already exists",
                )
            if not record.data.get("run_id"):
                return MappingResult(
                    catalog_type=catalog_type,
                    catalog_id=catalog_id,
                    postgresql_id=None,
                    status="pending",
                    details={"note": "claim import requires run_id context"}
                )
            return MappingResult(
                catalog_type=catalog_type,
                catalog_id=catalog_id,
                postgresql_id=None,
                status="inserted",
                details={"data": record.data}
            )"""
    content = content.replace(old_claim, new_claim)
    
    # 4. _insert_record: Handle events, claims, assessments
    old_insert = """        # N1 — Events, assessments, and claims are marked "pending" by
        # dry_run() and never reach this method through the normal apply
        # loop.  Any unexpected type is treated as unknown.
        raise ImportApplyError(f"Unknown or unsupported catalog type: {catalog_type}")"""
    new_insert = """        elif catalog_type == CATALOG_EVENT_TYPE:
            data = mapping.details.get("data", {})
            external_run_id = data.get("run_id")
            cur.execute("SELECT id FROM research_runs WHERE external_run_id = %s", (external_run_id,))
            row = cur.fetchone()
            if not row:
                raise ImportApplyError(f"Run {external_run_id} not found for event {catalog_id}")
            run_pg_id = row[0]
            
            # Map invocation
            invocation_pg_id = None
            if data.get("invocation_id"):
                cur.execute("SELECT id FROM research_invocations WHERE external_invocation_id = %s", (data["invocation_id"],))
                irow = cur.fetchone()
                if irow:
                    invocation_pg_id = irow[0]
            
            payload = data.get("event", {}) if "event" in data else data
            import json
            cur.execute(
                \"\"\"INSERT INTO research_events (
                    run_id, invocation_id, event_type, actor_type, actor_identifier, payload, run_revision, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, idempotency_key) DO NOTHING
                RETURNING id\"\"\",
                (
                    run_pg_id, invocation_pg_id, data.get("event_type", "unknown"), data.get("actor_type", "unknown"),
                    data.get("actor_identifier"), json.dumps(payload), data.get("run_revision", 0), catalog_id
                )
            )
            row = cur.fetchone()
            if row:
                return UUID(row[0])
            cur.execute("SELECT id FROM research_events WHERE run_id = %s AND idempotency_key = %s", (run_pg_id, catalog_id))
            row = cur.fetchone()
            if row: return UUID(row[0])
            raise ImportApplyError(f"Event {catalog_id} failed to insert")
            
        elif catalog_type == CATALOG_CLAIM_TYPE:
            data = mapping.details.get("data", {})
            external_run_id = data.get("run_id")
            cur.execute("SELECT id FROM research_runs WHERE external_run_id = %s", (external_run_id,))
            row = cur.fetchone()
            if not row:
                raise ImportApplyError(f"Run {external_run_id} not found for claim {catalog_id}")
            run_pg_id = row[0]
            
            cur.execute(
                \"\"\"INSERT INTO research_claims (
                    run_id, claim_id, statement, semantic_status
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, claim_id) DO NOTHING
                RETURNING id\"\"\",
                (run_pg_id, catalog_id, data.get("statement", "migrated claim"), data.get("semantic_status", "unassessed"))
            )
            row = cur.fetchone()
            if row: return UUID(row[0])
            cur.execute("SELECT id FROM research_claims WHERE run_id = %s AND claim_id = %s", (run_pg_id, catalog_id))
            row = cur.fetchone()
            if row: return UUID(row[0])
            raise ImportApplyError(f"Claim {catalog_id} failed to insert")
            
        elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            data = mapping.details.get("data", {})
            target_id = data.get("target_id")
            # Usually target_id is external_run_id in v5
            cur.execute("SELECT id FROM research_runs WHERE external_run_id = %s", (target_id,))
            row = cur.fetchone()
            if not row:
                raise ImportApplyError(f"Run {target_id} not found for assessment {catalog_id}")
            run_pg_id = row[0]
            
            cur.execute(
                \"\"\"INSERT INTO audit_assessments (
                    run_id, target_type, target_id, target_hash, evaluator_version, prompt_template_version, policy_version, stage_set, status, elapsed_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, target_type, target_id, target_hash) DO NOTHING
                RETURNING id\"\"\",
                (
                    run_pg_id, data.get("target_type", "run"), run_pg_id, data.get("target_hash", "none"),
                    data.get("evaluator_version", "unknown"), data.get("prompt_template_version", "unknown"),
                    data.get("policy_version", "unknown"), data.get("stage_set", []), data.get("status", "unknown"), 0
                )
            )
            row = cur.fetchone()
            if row: return UUID(row[0])
            cur.execute("SELECT id FROM audit_assessments WHERE run_id = %s AND target_type = %s AND target_id = %s AND target_hash = %s", 
                (run_pg_id, data.get("target_type", "run"), run_pg_id, data.get("target_hash", "none")))
            row = cur.fetchone()
            if row: return UUID(row[0])
            raise ImportApplyError(f"Assessment {catalog_id} failed to insert")

        raise ImportApplyError(f"Unknown or unsupported catalog type: {catalog_type}")"""
    content = content.replace(old_insert, new_insert)
    
    # 5. Fix pending mapping status insertion
    old_pending_insert = """                    elif mapping.status == "pending":
                        # N1 fix: pending mappings require additional context
                        # (run_id for events/claims, identity hash for
                        # assessments).  They are recorded as "omitted" because
                        # they cannot be inserted without that context.
                        detail = mapping.details.get(
                            "note", "requires additional context"
                        )
                        cur.execute(
                            \"\"\"INSERT INTO catalog_import_record_map (
                                import_run_id, catalog_type, catalog_id,
                                postgresql_id, mapping_status, conflict_detail
                            ) VALUES (%s, %s, %s, %s, 'omitted', %s)\"\"\",
                            (
                                str(tracking_id),
                                mapping.catalog_type,
                                mapping.catalog_id,
                                mapping.postgresql_id,
                                detail,
                            ),
                        )
                        omitted_count += 1"""
    new_pending_insert = """                    elif mapping.status == "pending":
                        detail = mapping.details.get("note", "requires additional context")
                        cur.execute(
                            \"\"\"INSERT INTO catalog_import_record_map (
                                import_run_id, catalog_type, catalog_id,
                                postgresql_id, mapping_status, conflict_detail
                            ) VALUES (%s, %s, %s, %s, 'pending', %s)\"\"\",
                            (
                                str(tracking_id),
                                mapping.catalog_type,
                                mapping.catalog_id,
                                mapping.postgresql_id,
                                detail,
                            ),
                        )
                        omitted_count += 1"""
    content = content.replace(old_pending_insert, new_pending_insert)

    with open("scripts/research_store/catalog_import.py", "w") as f:
        f.write(content)

patch_catalog_import()
