from __future__ import annotations

from pathlib import Path


smoke_path = Path("scripts/smoke_test.py")
smoke_text = smoke_path.read_text(encoding="utf-8")

anchor = '''class RunEvidenceInspector:
    """Inspect exact-run assets, reports, and persisted semantic authority."""

    _COUNT_QUERIES: ClassVar[dict[str, str]] = {
'''
replacement = '''class RunEvidenceInspector:
    """Inspect exact-run assets, reports, and persisted semantic authority."""

    _SEMANTIC_CALL_COUNT_QUERY: ClassVar[str] = (
        "SELECT COUNT(*) FROM semantic_calls "
        "WHERE run_id=%s AND call_status='complete'"
    )
    _AUTHORITY_COUNT_QUERY: ClassVar[str] = """
        SELECT semantic_authority, COUNT(*) FROM semantic_calls
        WHERE run_id=%s AND call_status='complete'
        GROUP BY semantic_authority ORDER BY semantic_authority
    """
    _HOST_METADATA_QUERY: ClassVar[str] = """
        SELECT response_metadata FROM semantic_calls
        WHERE run_id=%s AND semantic_authority='host-agent'
          AND call_status='complete'
    """

    _COUNT_QUERIES: ClassVar[dict[str, str]] = {
'''
if smoke_text.count(anchor) != 1:
    raise RuntimeError("RunEvidenceInspector class anchor was not unique")
smoke_text = smoke_text.replace(anchor, replacement, 1)

old = '''        "semantic_calls": "SELECT COUNT(*) FROM semantic_calls WHERE run_id=%s AND status='complete'",
'''
new = '''        "semantic_calls": _SEMANTIC_CALL_COUNT_QUERY,
'''
if smoke_text.count(old) != 1:
    raise RuntimeError("semantic call count query target was not unique")
smoke_text = smoke_text.replace(old, new, 1)

old = '''            cur.execute(
                """SELECT authority, COUNT(*) FROM semantic_calls
                   WHERE run_id=%s AND status='complete'
                   GROUP BY authority ORDER BY authority""",
                (run_id,),
            )
'''
new = '''            cur.execute(self._AUTHORITY_COUNT_QUERY, (run_id,))
'''
if smoke_text.count(old) != 1:
    raise RuntimeError("semantic authority count query target was not unique")
smoke_text = smoke_text.replace(old, new, 1)

old = '''            cur.execute(
                """SELECT response_metadata FROM semantic_calls
                   WHERE run_id=%s AND authority='host-agent' AND status='complete'""",
                (run_id,),
            )
'''
new = '''            cur.execute(self._HOST_METADATA_QUERY, (run_id,))
'''
if smoke_text.count(old) != 1:
    raise RuntimeError("host metadata query target was not unique")
smoke_text = smoke_text.replace(old, new, 1)

smoke_path.write_text(smoke_text, encoding="utf-8")


test_path = Path("scripts/test_smoke_test.py")
test_text = test_path.read_text(encoding="utf-8")

anchor = '''def test_longest_text_finds_nested_report_body():
    artifact = {"metadata": {"short": "x"}, "report": {"body": "z" * 250}}

    assert smoke_test.RunEvidenceInspector._longest_text(artifact) == "z" * 250


'''
replacement = anchor + '''def test_run_evidence_inspector_uses_current_semantic_calls_schema():
    queries = (
        smoke_test.RunEvidenceInspector._COUNT_QUERIES["semantic_calls"],
        smoke_test.RunEvidenceInspector._AUTHORITY_COUNT_QUERY,
        smoke_test.RunEvidenceInspector._HOST_METADATA_QUERY,
    )
    combined = "\\n".join(queries)

    assert all("call_status" in query for query in queries)
    assert "semantic_authority" in queries[1]
    assert "semantic_authority" in queries[2]
    assert "SELECT authority" not in combined
    assert "AND authority=" not in combined
    assert " status=" not in combined


'''
if test_text.count(anchor) != 1:
    raise RuntimeError("smoke inspector test insertion anchor was not unique")
test_path.write_text(test_text.replace(anchor, replacement, 1), encoding="utf-8")
