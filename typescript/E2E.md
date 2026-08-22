# Live TypeScript SDK → control-plane E2E

`tests/e2e.test.ts` is the live release gate. The normal hermetic suite skips
its six tests; `DEXCOST_E2E_LOCAL=1` enables them against a real Worker, local
Cloudflare queues, PostgreSQL, and ClickHouse.

The control-plane repository includes `server/wrangler.e2e.jsonc`. It is wired
only to the disposable container names, credentials, and loopback ports below.
Do not use it with a shared or production database.

## 1. Start disposable databases

```powershell
docker run --name dexcost-e2e-postgres `
  -e POSTGRES_USER=dexcost_e2e `
  -e POSTGRES_PASSWORD=dexcost_e2e_local_only `
  -e POSTGRES_DB=dexcost `
  -p 127.0.0.1:55432:5432 -d postgres:16-alpine

docker run --name dexcost-e2e-clickhouse `
  -e CLICKHOUSE_USER=dexcost_e2e `
  -e CLICKHOUSE_PASSWORD=dexcost_e2e_local_only `
  -e CLICKHOUSE_DB=dexcost `
  -p 127.0.0.1:58123:8123 -d clickhouse/clickhouse-server:latest
```

The validated run used PostgreSQL 16 and ClickHouse 26.3.8.4. Wait for both
containers to report ready before migrating.

## 2. Migrate and seed from `control-plane/server`

```powershell
$env:DATABASE_URL = "postgres://dexcost_e2e:dexcost_e2e_local_only@127.0.0.1:55432/dexcost"
$env:CLICKHOUSE_URL = "http://127.0.0.1:58123"
$env:CLICKHOUSE_USER = "dexcost_e2e"
$env:CLICKHOUSE_PASSWORD = "dexcost_e2e_local_only"
$env:CLICKHOUSE_DATABASE = "dexcost"

node --import tsx src/db/migrate.ts
node --import tsx src/db/migrate-clickhouse.ts
node --import tsx src/db/seed.ts
```

The deterministic local seed key is `dx_test_e2e_seed_12345`. A correct fresh
bootstrap applies all 85 PostgreSQL migration files, initializes 112
ClickHouse statements, preserves five global plan-entitlement rows, creates
210 operational tasks, and inserts 875 ClickHouse seed events.

## 3. Start the real Worker and local queues

```powershell
npx wrangler dev --config wrangler.e2e.jsonc --local --ip 127.0.0.1 --port 58787
```

Wait for `/health` to return `{ "status": "ok" }`.

## 4. Run from `dexcost-sdk/typescript`

```powershell
$env:DEXCOST_E2E_LOCAL = "1"
$env:DEXCOST_ENDPOINT = "http://127.0.0.1:58787"
$env:DEXCOST_API_KEY = "dx_test_e2e_seed_12345"
npm run test -- tests/e2e.test.ts
```

The gate is green only when all six tests pass and the Worker reports each
ingest queue batch as fully successful (for example `1/1` or `3/3`) with no
retry or dead-letter messages. The test deliberately polls the task API so a
mere HTTP 202 is insufficient.

## 5. Clean up

Stop Wrangler, then remove only the two disposable containers:

```powershell
docker rm -f dexcost-e2e-postgres dexcost-e2e-clickhouse
```

Unit, joint-contract, type, build, and package tests remain mandatory; none of
them replaces this operational gate.
