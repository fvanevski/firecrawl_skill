from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex


class FakeQdrant(QdrantIndex):
    def __init__(self, responses):
        super().__init__("http://qdrant", "", "physical", 4)
        self.responses = list(responses)
        self.requests = []

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        return self.responses.pop(0)


def _collection(payload_schema):
    return {
        "result": {
            "payload_schema": payload_schema,
            "config": {
                "params": {
                    "vectors": {"dense": {"size": 4, "distance": "Cosine"}}
                }
            },
        }
    }


def test_payload_index_inspection_reads_payload_schema_without_writes():
    qdrant = FakeQdrant(
        [
            _collection(
                {
                    "snapshot_id": {"data_type": "uuid"},
                    "document_id": {"data_type": "uuid"},
                    "source_id": {"data_type": "uuid"},
                    "domain": {"data_type": "keyword"},
                    "published_at": {"data_type": "datetime"},
                }
            )
        ]
    )
    status = qdrant.inspect_payload_indexes(PAYLOAD_INDEX_SCHEMAS)
    assert all(detail["compatible"] for detail in status.values())
    assert qdrant.requests == [("GET", "/collections/physical", None)]


def test_payload_index_provisioning_uses_semantic_field_types_and_is_idempotent():
    missing = _collection({})
    complete = _collection(
        {
            "snapshot_id": {"data_type": "uuid"},
            "document_id": {"data_type": "uuid"},
            "source_id": {"data_type": "uuid"},
            "domain": {"data_type": "keyword"},
            "published_at": {"data_type": "datetime"},
        }
    )
    qdrant = FakeQdrant([missing, {}, {}, {}, {}, {}, complete, complete])
    first = qdrant.ensure_payload_indexes(PAYLOAD_INDEX_SCHEMAS, create_missing=True)
    second = qdrant.ensure_payload_indexes(PAYLOAD_INDEX_SCHEMAS, create_missing=True)
    assert all(first.values()) and all(second.values())
    put_payloads = [request[2] for request in qdrant.requests if request[0] == "PUT"]
    assert put_payloads == [
        {"field_name": field, "field_schema": schema}
        for field, schema in PAYLOAD_INDEX_SCHEMAS.items()
    ]
    assert qdrant.requests[-1] == ("GET", "/collections/physical", None)


def test_incompatible_payload_index_is_reported_not_overwritten():
    wrong = _collection({"snapshot_id": {"data_type": "text"}})
    qdrant = FakeQdrant([wrong, wrong])
    status = qdrant.inspect_payload_indexes({"snapshot_id": "uuid"})
    assert status["snapshot_id"] == {
        "present": True,
        "compatible": False,
        "expected_type": "uuid",
        "actual_type": "text",
    }
    ensured = qdrant.ensure_payload_indexes(
        {"snapshot_id": "uuid"}, create_missing=True
    )
    assert ensured == {"snapshot_id": False}
    assert not any(request[0] == "PUT" for request in qdrant.requests)


def test_shard_state_uses_cluster_endpoint_and_preserves_real_states():
    qdrant = FakeQdrant(
        [
            {
                "result": {
                    "peer_id": 1,
                    "shard_count": 2,
                    "local_shards": [
                        {"shard_id": 0, "points_count": 10, "state": "Active"},
                        {"shard_id": 1, "points_count": 5, "state": "Dead"},
                    ],
                    "remote_shards": [],
                    "shard_transfers": [],
                    "resharding_operations": [],
                }
            }
        ]
    )
    health = qdrant.inspect_shard_health()
    assert health["healthy"] is False
    assert [item["state"] for item in health["shards"]] == ["active", "dead"]
    assert qdrant.requests == [("GET", "/collections/physical/cluster", None)]


def test_empty_cluster_topology_fails_closed_instead_of_fabricating_active_shard():
    qdrant = FakeQdrant(
        [
            {
                "result": {
                    "peer_id": 1,
                    "shard_count": 0,
                    "local_shards": [],
                    "remote_shards": [],
                    "shard_transfers": [],
                    "resharding_operations": [],
                }
            }
        ]
    )
    health = qdrant.inspect_shard_health()
    assert health["healthy"] is False
    assert health["shards"] == []


def test_reconcile_cli_returns_nonzero_for_observed_discrepancies(monkeypatch, capsys):
    import research_store.cli as cli

    fake_config = object()
    monkeypatch.setattr(
        cli.StoreConfig, "from_env", classmethod(lambda cls: fake_config)
    )
    monkeypatch.setattr(
        cli,
        "reconcile_run",
        lambda config, run, repair=False: {
            "schema_version": "qdrant-reconciliation-v2",
            "scope": "run",
            "ok": False,
            "discrepancies": ["missing point"],
        },
    )
    assert cli.main(["index-reconcile", "fr_test"]) == 1
    assert '"ok": false' in capsys.readouterr().out.lower()


def test_reconcile_cli_returns_two_when_authoritative_scope_is_unavailable(
    monkeypatch, capsys
):
    import research_store.cli as cli

    fake_config = object()
    monkeypatch.setattr(
        cli.StoreConfig, "from_env", classmethod(lambda cls: fake_config)
    )
    monkeypatch.setattr(
        cli,
        "reconcile_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cli.ReconciliationError("historical membership unavailable")
        ),
    )
    assert cli.main(["index-reconcile", "fr_legacy"]) == 2
    assert "historical membership unavailable" in capsys.readouterr().err
