# PostgreSQL initialization

Deliberately empty of SQL. The schema is owned entirely by Alembic
(`services/backend/alembic/`), so there is exactly one place a table can be
created and exactly one history to audit.

Runtime tuning is passed as `-c` flags in `compose.yaml` (development) and
`compose.prod.yaml` (production, sized for an 8 GiB instance):

| Setting | Dev | Prod | Why |
| --- | --- | --- | --- |
| `shared_buffers` | 512MB | 2GB | ~25% of instance memory |
| `work_mem` | 16MB | 24MB | Bounded per sort/hash |
| `maintenance_work_mem` | 256MB | 512MB | Index builds, VACUUM |
| `effective_cache_size` | 2GB | 5GB | Planner hint |
| `max_connections` | 200 | 200 | See the connection budget in docs/DATABASE.md |
| `wal_compression` | on | on | Smaller WAL on a small volume |
| `shared_preload_libraries` | - | pg_stat_statements | Query analysis |

If a future change genuinely needs an extension or a role created before Alembic
runs, add a numbered `.sql` file here: the official image executes
`/docker-entrypoint-initdb.d/*.sql` on first start only.
