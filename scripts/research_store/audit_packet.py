import hashlib
import json
from uuid import UUID
from typing import Callable

def compute_audit_packet_hash_from_db(run_id: UUID, uow_factory: Callable) -> str:
    """Compute the audit packet hash dynamically from the PostgreSQL projection."""
    from .catalog_export import CatalogExportService
    
    export_service = CatalogExportService(uow_factory)
    projection = export_service._load_projection(run_id)
    run, invocations, events, snapshots, claims, assessments, manifest = export_service._map_records(projection)
    
    target = run
    records = invocations
    
    candidate_cards = []
    used_dossiers = []
    operations = []
    used_ids = {source.get("candidate_id") for source in target.get("used_sources", [])}
    
    for record in records:
        operations.append({
            "invocation_id": record["invocation_id"],
            "operation": record.get("operation"),
            "input": record.get("input"),
            "strategy": record.get("strategy"),
            "planner_provenance": record.get("planner_provenance"),
            "query_plan": record.get("query_plan", []),
            "retry_query_plan": record.get("retry_query_plan", []),
            "execution": record.get("execution"),
            "metrics": record.get("operational_metrics"),
            "events": record.get("events", [])
        })
        for result in record.get("results", []):
            card = {
                key: result.get(key)
                for key in ("candidate_id", "result_index", "url", "title", "snippet", "selected", "targeted", "branches", "facets", "status", "scrape_status", "word_count", "error", "error_class", "constraint", "date_signals", "source_hints")
                if result.get(key) not in (None, "", [], {})
            }
            card["invocation_id"] = record["invocation_id"]
            candidate_cards.append(card)
            if result.get("candidate_id") in used_ids:
                used_dossiers.append({**card, "excerpts": result.get("excerpts", [])})
                
    def estimate_tokens(obj):
        return len(json.dumps(obj)) // 4
        
    filtered_events = [
        e for e in events 
        if e.get("event") not in {"assessment_finished", "artifact_verification"}
        and e.get("research_run_id") == target["research_run_id"]
    ]
        
    packet = {
        "packet_version": "audit-packet-v1",
        "target_id": target["research_run_id"],
        "objective": target.get("objective") or target.get("input", {}).get("topic") or target.get("input", {}).get("query"),
        "profile": target.get("profile"),
        "declared_outcome": target.get("declared_outcome"),
        "annotations": target.get("annotations", []),
        "operations": operations,
        "candidate_cards": candidate_cards,
        "used_source_dossiers": used_dossiers,
        "claims": target.get("claims", []),
        "source_manifest": target.get("used_sources", []),
        "final_answer": target.get("final_answer"),
        "operational_summary": target.get("operational_summary") or target.get("operational_metrics"),
        "timeline": filtered_events,
    }
    packet["context_manifest"] = {
        "candidate_count": len(candidate_cards),
        "used_dossier_count": len(used_dossiers),
        "operation_count": len(operations),
        "input_token_estimate": estimate_tokens(packet),
        "omissions": []
    }
    
    return hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()
