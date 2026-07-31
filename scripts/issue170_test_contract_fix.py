from pathlib import Path

path = Path("scripts/test_research_store_integration.py")
text = path.read_text(encoding="utf-8")

old = '''    def test_index_build_resume_interrupted_build(self, service):
        """When some manifests are complete and some pending, index-build
        only requeues the pending ones — no duplicate jobs are created."""
'''
new = '''    def test_index_build_resume_interrupted_build(self, service):
        """Index build requeues every manifest whose physical point is absent,
        including a manifest incorrectly marked complete, without duplicate jobs."""
'''
if text.count(old) != 1:
    raise RuntimeError("interrupted-build test docstring target was not unique")
text = text.replace(old, new, 1)

old = '''            # Verify the previously-complete manifest is still complete
            cur.execute(
                """SELECT index_status FROM embedding_manifests
                WHERE id=%s""",
                (complete_manifest_id,),
            )
            assert cur.fetchone()[0] == "complete"
'''
new = '''            # Physical absence invalidates the false-complete PostgreSQL state.
            cur.execute(
                """SELECT m.index_status,j.status
                FROM embedding_manifests m
                JOIN index_jobs j ON j.manifest_id=m.id
                WHERE m.id=%s""",
                (complete_manifest_id,),
            )
            assert cur.fetchone() == ("pending", "pending")
'''
if text.count(old) != 1:
    raise RuntimeError("interrupted-build final assertion target was not unique")

path.write_text(text.replace(old, new, 1), encoding="utf-8")
