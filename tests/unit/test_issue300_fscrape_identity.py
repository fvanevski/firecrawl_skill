"""Issue #300 AC2 canonical logical identity regressions."""

from __future__ import annotations

from uuid import uuid4

from firecrawl_skill.research_store.acquisition.models import DirectScrapeRequest
from firecrawl_skill.research_store.acquisition.replay_safe_direct_scrape import (
    ReplaySafeDirectScrapeService,
)


def _service() -> ReplaySafeDirectScrapeService:
    return object.__new__(ReplaySafeDirectScrapeService)


def test_logical_key_reuses_direct_service_normalization() -> None:
    run_id = uuid4()
    service = _service()
    first = service.logical_idempotency_key(
        run_id,
        [
            DirectScrapeRequest(
                url="  https://example.test/article  ",
                options={"wait_for": 1000},
            )
        ],
    )
    replay = service.logical_idempotency_key(
        run_id,
        [
            DirectScrapeRequest(
                url="https://example.test/article",
                options={"wait_for": 1000},
            )
        ],
    )
    changed = service.logical_idempotency_key(
        run_id,
        [
            DirectScrapeRequest(
                url="https://example.test/article",
                options={"wait_for": 2000},
            )
        ],
    )

    assert replay == first
    assert changed != first


def test_logical_key_includes_candidate_identity_and_never_crosses_runs() -> None:
    service = _service()
    candidate_id = uuid4()
    request = [DirectScrapeRequest(candidate_id=candidate_id)]
    first_run = service.logical_idempotency_key(uuid4(), request)
    second_run = service.logical_idempotency_key(uuid4(), request)

    assert first_run != second_run


class _BudgetCursor:
    def __init__(self, executed):
        self.executed = executed
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self._last = str(sql)
        self.executed.append((self._last, params))
        if "count(*) FROM extraction_attempts" in self._last:
            raise AssertionError("terminal replay must not be charged as fresh work")

    def fetchone(self):
        if "SELECT status,output FROM research_invocations" in self._last:
            return ("complete", {"items": []})
        return None


class _BudgetConnection:
    def __init__(self, executed):
        self.executed = executed

    def cursor(self):
        return _BudgetCursor(self.executed)

    def commit(self):
        return None


class _BudgetUow:
    def __init__(self, executed):
        self.connection = _BudgetConnection(executed)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_terminal_replay_is_rechecked_inside_budget_lock_before_projection() -> None:
    from types import SimpleNamespace

    service = _service()
    executed = []
    service.budget = SimpleNamespace(max_exploratory_extraction_attempts=0)
    service.uow_factory = lambda: _BudgetUow(executed)
    token = service._active_batch_key.set("same-logical-request")
    try:
        with service._budget_guard(uuid4(), 1):
            pass
    finally:
        service._active_batch_key.reset(token)

    assert any("pg_advisory_lock" in sql for sql, _params in executed)
    assert any("research_invocations" in sql for sql, _params in executed)
    assert not any(
        "count(*) FROM extraction_attempts" in sql for sql, _params in executed
    )


class _ResumeBudgetCursor(_BudgetCursor):
    def execute(self, sql, params=()):
        # A nonterminal resume legitimately charges the extraction-count query,
        # so the terminal-replay guardrail in the base cursor does not apply.
        self._last = str(sql)
        self.executed.append((self._last, params))

    def fetchone(self):
        if "SELECT status,output FROM research_invocations" in self._last:
            return (
                "running",
                {"items": [{"item_key": "already-persisted", "status": "succeeded"}]},
            )
        if "count(*) FROM extraction_attempts" in self._last:
            return (1,)
        return None


class _ResumeBudgetConnection(_BudgetConnection):
    def cursor(self):
        return _ResumeBudgetCursor(self.executed)


class _ResumeBudgetUow(_BudgetUow):
    def __init__(self, executed):
        self.connection = _ResumeBudgetConnection(executed)


def test_nonterminal_resume_projects_only_unpersisted_items() -> None:
    from types import SimpleNamespace

    service = _service()
    executed = []
    service.budget = SimpleNamespace(max_exploratory_extraction_attempts=2)
    service.uow_factory = lambda: _ResumeBudgetUow(executed)
    token = service._active_batch_key.set("crashed-batch")
    try:
        # Two logical items were requested, but one already has an authoritative
        # item result/attempt. Only one additional attempt may be projected.
        with service._budget_guard(uuid4(), 2):
            pass
    finally:
        service._active_batch_key.reset(token)

    assert any("count(*) FROM extraction_attempts" in sql for sql, _ in executed)


class _LineageCursor:
    def __init__(self, queries):
        self.queries = queries
        self._last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.queries.append((str(sql), params))
        self._last_params = params

    def fetchone(self):
        return None


class _LineageConnection:
    def __init__(self, queries):
        self.queries = queries

    def cursor(self):
        return _LineageCursor(self.queries)


class _LineageUow:
    def __init__(self, queries):
        self.connection = _LineageConnection(queries)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_fresh_parent_lookup_excludes_selected_fresh_identity() -> None:
    service = _service()
    queries = []
    service.uow_factory = lambda: _LineageUow(queries)
    service.latest_terminal_logical_invocation(
        uuid4(),
        [DirectScrapeRequest(url="https://example.test/repeat-fresh")],
        exclude_idempotency_key="fscrape:fresh:stable",
    )

    sql, params = queries[-1]
    assert "idempotency_key<>%s" in sql
    assert params[-2:] == ("fscrape:fresh:stable", "fscrape:fresh:stable")


def _fr_id() -> str:
    return f"fr_{uuid4().hex}"


class _CanonicalDirectFake:
    def __init__(self, *, replayed: bool = False):
        from types import SimpleNamespace

        self.replayed = replayed
        self.calls = []
        self.lineage = []
        self.parent = uuid4()
        self.persisted_parent = self.parent
        self.batch_id = uuid4()
        self._namespace = SimpleNamespace

    def logical_idempotency_key(self, run_id, _requests):
        return f"direct-scrape:{run_id}:logical"

    def latest_terminal_logical_invocation(
        self, _run_id, _requests, *, exclude_idempotency_key=None
    ):
        self.excluded_fresh_key = exclude_idempotency_key
        return self.parent

    def execute(self, run_id, _requests, **kwargs):
        self.calls.append(kwargs)
        return self._namespace(
            run_id=run_id,
            invocation_id=self.batch_id,
            idempotency_key=kwargs["idempotency_key"],
            status="complete",
            items=(),
            replayed=self.replayed,
        )

    def invocation_parent(self, _run_id, _invocation_id):
        return self.persisted_parent

    def record_fresh_invocation_lineage(self, run_id, invocation_id, **kwargs):
        self.lineage.append((run_id, invocation_id, kwargs))


class _RunFake:
    def __init__(self, run_id):
        self.run_id = run_id

    def status(self, *, external_id):
        from types import SimpleNamespace

        assert external_id.startswith("fr_")
        return SimpleNamespace(id=self.run_id)


def _canonical_service(direct):
    from firecrawl_skill.research_store.fscrape_authority import CanonicalFScrapeService

    run_id = uuid4()
    service = CanonicalFScrapeService(direct, _RunFake(run_id))
    service._authoritative_external_invocation_id = lambda _batch: f"fc_{uuid4().hex}"
    service._index_job_ids = lambda _batch: {}
    return service, run_id


def test_effective_fresh_links_to_latest_logical_invocation_and_reports_mode() -> None:
    from firecrawl_skill.research_store.fscrape_contract import FScrapeRequest

    direct = _CanonicalDirectFake()
    service, run_id = _canonical_service(direct)
    result = service.execute(
        FScrapeRequest(
            urls=("https://example.test/fresh",),
            research_run_id=_fr_id(),
            fresh=True,
        )
    )

    assert direct.calls[0]["parent_invocation_id"] == direct.parent
    assert direct.calls[0]["idempotency_key"].startswith("fscrape:fresh:")
    assert direct.excluded_fresh_key == direct.calls[0]["idempotency_key"]
    assert direct.lineage == [
        (
            run_id,
            direct.batch_id,
            {
                "logical_idempotency_key": f"direct-scrape:{run_id}:logical",
                "parent_invocation_id": direct.parent,
            },
        )
    ]
    output = result.to_dict()
    assert output["fresh_requested"] is True
    assert output["fresh_effective"] is True
    assert output["fresh_parent_invocation_id"] == str(direct.parent)
    assert output["work_mode"] == "fresh"


def test_explicit_idempotency_key_remains_authoritative_over_fresh_flag() -> None:
    from firecrawl_skill.research_store.fscrape_contract import FScrapeRequest

    direct = _CanonicalDirectFake()
    service, _run_id = _canonical_service(direct)
    result = service.execute(
        FScrapeRequest(
            urls=("https://example.test/explicit",),
            research_run_id=_fr_id(),
            fresh=True,
            idempotency_key="caller-owned-key",
        )
    )

    assert direct.calls[0]["idempotency_key"] == "caller-owned-key"
    assert direct.calls[0]["parent_invocation_id"] is None
    assert direct.lineage == []
    output = result.to_dict()
    assert output["fresh_requested"] is True
    assert output["fresh_effective"] is False
    assert output["fresh_parent_invocation_id"] is None
    assert output["work_mode"] == "new"


def test_fresh_replay_is_reported_as_replay_even_when_fresh_key_was_selected() -> None:
    from firecrawl_skill.research_store.fscrape_contract import FScrapeRequest

    direct = _CanonicalDirectFake(replayed=True)
    service, _run_id = _canonical_service(direct)
    result = service.execute(
        FScrapeRequest(
            urls=("https://example.test/fresh-replay",),
            research_run_id=_fr_id(),
            fresh=True,
            external_invocation_id=f"fc_{'1' * 32}",
        )
    )

    output = result.to_dict()
    assert output["fresh_effective"] is True
    assert output["work_mode"] == "replay"


def test_fresh_replay_reports_persisted_parent_not_recomputed_latest_parent() -> None:
    from firecrawl_skill.research_store.fscrape_contract import FScrapeRequest

    direct = _CanonicalDirectFake(replayed=True)
    direct.persisted_parent = uuid4()
    assert direct.persisted_parent != direct.parent
    service, _run_id = _canonical_service(direct)
    result = service.execute(
        FScrapeRequest(
            urls=("https://example.test/fresh-replay-lineage",),
            research_run_id=_fr_id(),
            fresh=True,
            external_invocation_id=f"fc_{'2' * 32}",
        )
    )

    output = result.to_dict()
    assert output["work_mode"] == "replay"
    assert output["fresh_parent_invocation_id"] == str(direct.persisted_parent)
    assert direct.lineage[-1][2]["parent_invocation_id"] == direct.persisted_parent
