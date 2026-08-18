# Local disposable PostgreSQL and Qdrant test services

Use `scripts/disposable-test-services` for local integration tests that are allowed to reset PostgreSQL state or mutate Qdrant projections. The helper intentionally mirrors the CI service images while isolating local tests from persistent personal services.

## Safety boundary

The local persistent services are **not** test targets:

| Service | Persistent endpoint | Test helper default |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:55432` | `127.0.0.1:55436` |
| Qdrant | `127.0.0.1:6333` | `127.0.0.1:55437` |

The helper refuses both known persistent ports, binds test services to `127.0.0.1` only, and labels every created container. `down` and `reset-qdrant` refuse to remove a same-named container unless those ownership labels match the requested namespace.

The service images are pinned to the same versions used by the integration workflows:

- PostgreSQL: `postgres:16-alpine`
- Qdrant: `qdrant/qdrant:v1.18.3-unprivileged`

## Start a fresh pair

Choose a short namespace for the validation campaign. `fc263` is only an example:

```bash
eval "$(scripts/disposable-test-services --namespace fc263 up)"
```

The helper:

1. refuses to reuse existing `${namespace}_pg` or `${namespace}_qdrant` containers;
2. starts PostgreSQL and waits for `pg_isready`;
3. creates `${namespace}_test` after normalizing `-` to `_`;
4. starts a brand-new Qdrant container and waits for `/healthz`;
5. prints the exact test environment exports.

The generated PostgreSQL database always contains a standalone `test` segment, which satisfies `require_disposable_database_reset()`. The generated Qdrant reset acknowledgement exactly equals the disposable Qdrant URL, which satisfies `require_disposable_qdrant_url()`.

To inspect the exports without starting services:

```bash
scripts/disposable-test-services --namespace fc263 env
```

Default output is equivalent to:

```bash
export RESEARCH_STORE_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:55436/fc263_test'
export RESEARCH_STORE_TEST_ALLOW_RESET='fc263_test'
export QDRANT_URL='http://127.0.0.1:55437'
export RESEARCH_STORE_TEST_QDRANT_URL='http://127.0.0.1:55437'
export RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET='http://127.0.0.1:55437'
```

Custom test ports are supported when the defaults are occupied:

```bash
eval "$(scripts/disposable-test-services \
  --namespace fc263 \
  --pg-port 55446 \
  --qdrant-port 55447 \
  up)"
```

Do not hand-edit the emitted reset acknowledgements to point at persistent services.

## PostgreSQL reset semantics

The repository test bootstrap may destructively rebuild schema in the dedicated disposable database when the reset guard permits it. A running PostgreSQL server is not enough: the database itself must exist before pytest starts. The helper creates it during `up`.

A disposable PostgreSQL container normally does **not** need recreation between repository integration-test batches when the suite's gated reset bootstrap runs successfully against the dedicated test database. Recreate the pair when diagnosing bootstrap failures or state that is outside the reset-managed schema/database.

## Qdrant clean-state semantics

`docker restart` is **not** a Qdrant reset. Container-local Qdrant storage survives restart and can retain points created by earlier test batches. Point-count, reconciliation, and orphan-detection tests can therefore become nondeterministic if a disposable Qdrant container is reused.

For an authoritative clean Qdrant identity, run:

```bash
eval "$(scripts/disposable-test-services --namespace fc263 reset-qdrant)"
```

`reset-qdrant` removes the helper-owned Qdrant container and creates a new one. It does not touch PostgreSQL.

Interpret count/coverage mismatches in two stages:

1. If Qdrant may have been reused, recreate it and rerun the failing test.
2. Treat the result as a candidate implementation defect only if it reproduces against a freshly created Qdrant container and the expected disposable PostgreSQL test database.

A collection reset performed by a particular test may be sufficient for that test, but container recreation is the strongest clean-state control and should be the baseline for diagnosing point-count-sensitive failures.

## Cleanup

Always remove the disposable pair after the campaign:

```bash
scripts/disposable-test-services --namespace fc263 down
```

Do not leave validation containers running between campaigns. A long-lived nominally disposable Qdrant container can silently become persistent test state and contaminate later runs.
