import re

with open("scripts/research_store/catalog_import.py", "r") as f:
    content = f.read()

# 1. Update dry_run signature
content = re.sub(
    r'(def dry_run\(\s*self,\s*catalog_root: Path,\s*\*,.*?)(existing_invocation_ids: set\[str\] \| None = None,\s*)(\) -> ImportReport:)',
    r'\1\2existing_event_ids: set[str] | None = None,\n        existing_claim_ids: set[str] | None = None,\n        existing_assessment_ids: set[str] | None = None,\n    \3',
    content, flags=re.DOTALL
)

# 2. Update state hydration
content = re.sub(
    r'(\s*# Map valid records to PostgreSQL\s*)existing_runs = existing_run_ids or set\(\)\s*existing_invocations = existing_invocation_ids or set\(\)\s*mappings: list\[MappingResult\] = \[\]',
    r'''\n        # Hydrate state from DB if not explicitly provided
        existing_runs = existing_run_ids if existing_run_ids is not None else set()
        existing_invocations = existing_invocation_ids if existing_invocation_ids is not None else set()
        existing_events = existing_event_ids if existing_event_ids is not None else set()
        existing_claims = existing_claim_ids if existing_claim_ids is not None else set()
        existing_assessments = existing_assessment_ids if existing_assessment_ids is not None else set()
        
        if existing_run_ids is None or existing_invocation_ids is None or existing_event_ids is None or existing_claim_ids is None or existing_assessment_ids is None:
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
                        try:
                            cur.execute("SELECT claim_id::text FROM research_claims WHERE claim_id::text = ANY(%s)", (claim_ids,))
                            existing_claims.update(row[0] for row in cur.fetchall())
                        except Exception:
                            pass
            except Exception:
                pass

\1mappings: list[MappingResult] = []''',
    content
)

# 3. Update _dry_run_map_record signature
content = re.sub(
    r'(def _dry_run_map_record\(\s*self,\s*record: CatalogRecord,\s*existing_runs: set\[str\],\s*existing_invocations: set\[str\],\s*)(\) -> MappingResult:)',
    r'\1existing_events: set[str] = None,\n        existing_claims: set[str] = None,\n        existing_assessments: set[str] = None,\n    \2\n        existing_events = existing_events or set()\n        existing_claims = existing_claims or set()\n        existing_assessments = existing_assessments or set()',
    content, flags=re.DOTALL
)

# 4. Fix call to _dry_run_map_record
content = re.sub(
    r'(mapping = self._dry_run_map_record\(\s*record, existing_runs, existing_invocations\s*\))',
    r'mapping = self._dry_run_map_record(\n                record, existing_runs, existing_invocations, existing_events, existing_claims, existing_assessments\n            )',
    content
)

# 5. Fix _dry_run_map_record bodies
content = re.sub(
    r'elif catalog_type == CATALOG_EVENT_TYPE:.*?return MappingResult\(.*?catalog_type=catalog_type,.*?catalog_id=catalog_id,.*?postgresql_id=None,.*?status="pending",.*?details=\{.*?"note": "event import requires run_id context",.*?\},.*?\)',
    r'''elif catalog_type == CATALOG_EVENT_TYPE:
            if catalog_id in existing_events:
                return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="skipped", conflict_detail="PostgreSQL event already exists")
            if not record.data.get("run_id"):
                return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="pending", details={"note": "event import requires run_id context"})
            return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="inserted", details={"data": record.data})''',
    content, flags=re.DOTALL
)

content = re.sub(
    r'elif catalog_type == CATALOG_ASSESSMENT_TYPE:.*?return MappingResult\(.*?catalog_type=catalog_type,.*?catalog_id=catalog_id,.*?postgresql_id=None,.*?status="pending",.*?details=\{.*?"note": "assessment import requires identity-hash validation",.*?\},.*?\)',
    r'''elif catalog_type == CATALOG_ASSESSMENT_TYPE:
            if not record.data.get("target_id") or not record.data.get("target_hash"):
                return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="pending", details={"note": "assessment import requires target_id and target_hash"})
            return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="inserted", details={"data": record.data})''',
    content, flags=re.DOTALL
)

content = re.sub(
    r'elif catalog_type == CATALOG_CLAIM_TYPE:.*?return MappingResult\(.*?catalog_type=catalog_type,.*?catalog_id=catalog_id,.*?postgresql_id=None,.*?status="pending",.*?details=\{.*?"note": "claim import requires run_id context",.*?\},.*?\)',
    r'''elif catalog_type == CATALOG_CLAIM_TYPE:
            if catalog_id in existing_claims:
                return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="skipped", conflict_detail="PostgreSQL claim already exists")
            if not record.data.get("run_id"):
                return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="pending", details={"note": "claim import requires run_id context"})
            return MappingResult(catalog_type=catalog_type, catalog_id=catalog_id, postgresql_id=None, status="inserted", details={"data": record.data})''',
    content, flags=re.DOTALL
)

# 6. Update _insert_record
content = re.sub(
    r'        # N1 — Events, assessments, and claims are marked "pending" by\s*# dry_run\(\) and never reach this method through the normal apply\s*# loop\.  Any unexpected type is treated as unknown\.\s*raise ImportApplyError\(f"Unknown or unsupported catalog type: \{catalog_type\}"\)',
    r'''        elif catalog_type == CATALOG_EVENT_TYPE:
            data = mapping.details.get("data", {})
            external_run_id = data.get("run_id")
            cur.execute("SELECT id FROM research_runs WHERE external_run_id = %s", (external_run_id,))
            row = cur.fetchone()
            if not row:
                raise ImportApplyError(f"Run {external_run_id} not found for event {catalog_id}")
            run_pg_id = row[0]
            
            invocation_pg_id = None
            if data.get("invocation_id"):
                cur.execute("SELECT id FROM research_invocations WHERE external_invocation_id = %s", (data["invocation_id"],))
                irow = cur.fetchone()
                if irow: invocation_pg_id = irow[0]
            
            payload = data.get("event", {}) if "event" in data else data
            import json
            cur.execute(
                """INSERT INTO research_events (run_id, invocation_id, event_type, actor_type, actor_identifier, payload, run_revision, idempotency_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_id, idempotency_key) DO NOTHING RETURNING id""",
                (run_pg_id, invocation_pg_id, data.get("event_type", "unknown"), data.get("actor_type", "unknown"), data.get("actor_identifier"), json.dumps(payload), data.get("run_revision", 0), catalog_id)
            )
            row = cur.fetchone()
            if row: return UUID(row[0])
            cur.execute("SELECT id FROM research_events WHERE run_id = %s AND idempotency_key = %s", (run_pg_id, catalog_id))
            row = cur.fetchone()
            if row: return UUID(row[0])
            raise ImportApplyError(f"Event {catalog_id} failed to insert")
            
        elif catalog_type == CATALOG_CLAIM_TYPE:
            data = mapping.details.get("data", {})
            external_run_id = data.get("run_id")
            cur.execute("SELECT id FROM research_runs WHERE external_run_id = %s", (external_run_id,))
            row = cur.fetchone()
            if not row: raise ImportApplyError(f"Run {external_run_id} not found for claim {catalog_id}")
            run_pg_id = row[0]
            
            cur.execute(
                """INSERT INTO research_claims (run_id, claim_id, statement, semantic_status) VALUES (%s, %s, %s, %s) ON CONFLICT (run_id, claim_id) DO NOTHING RETURNING id""",
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
            cur.execute("SELECT id FROM research_runs WHERE external_run_id = %s", (target_id,))
            row = cur.fetchone()
            if not row: raise ImportApplyError(f"Run {target_id} not found for assessment {catalog_id}")
            run_pg_id = row[0]
            
            cur.execute(
                """INSERT INTO audit_assessments (run_id, target_type, target_id, target_hash, evaluator_version, prompt_template_version, policy_version, stage_set, status, elapsed_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_id, target_type, target_id, target_hash) DO NOTHING RETURNING id""",
                (run_pg_id, data.get("target_type", "run"), run_pg_id, data.get("target_hash", "none"), data.get("evaluator_version", "unknown"), data.get("prompt_template_version", "unknown"), data.get("policy_version", "unknown"), data.get("stage_set", []), data.get("status", "unknown"), 0)
            )
            row = cur.fetchone()
            if row: return UUID(row[0])
            cur.execute("SELECT id FROM audit_assessments WHERE run_id = %s AND target_type = %s AND target_id = %s AND target_hash = %s", (run_pg_id, data.get("target_type", "run"), run_pg_id, data.get("target_hash", "none")))
            row = cur.fetchone()
            if row: return UUID(row[0])
            raise ImportApplyError(f"Assessment {catalog_id} failed to insert")

        raise ImportApplyError(f"Unknown or unsupported catalog type: {catalog_type}")''',
    content
)

# 7. Update pending mapping to 'pending' instead of 'omitted'
content = re.sub(
    r'(mapping_status, conflict_detail\s*\)\s*VALUES\s*\(\s*%s,\s*%s,\s*%s,\s*%s,\s*\')omitted(\',\s*%s\s*\))',
    r'\1pending\2',
    content
)

with open("scripts/research_store/catalog_import.py", "w") as f:
    f.write(content)

