# Generic API Load Test Kit

Docker Compose, k6, Prometheus, Grafana, and lightweight JVM/database collectors for measuring an HTTP API. The request, dashboard, and collectors are not tied to one application. Generated results, local credentials, and application-specific request bodies are kept out of version control.

## Requirements

- Docker Engine and Docker Compose v2
- Java 17+ JDK and Python 3 for local JVM/database collection
- A local Spring Boot process for the optional JVM and Actuator metrics
- An Oracle or MariaDB Docker container for database runtime metrics

The k6 runner can test any HTTP API reachable from its container. The runtime database collector uses `docker exec`, so the database must be a local Docker container with the appropriate SQL client installed.

## Start Prometheus, Pushgateway, and Grafana

```bash
cd k6
cp .env.example .env
```

Change `GRAFANA_ADMIN_PASSWORD` in `k6/.env`, then start the local monitoring services:

```bash
docker compose up -d prometheus pushgateway grafana
```

Open Grafana at <http://localhost:3000>. The provisioned dashboard is in the **Performance** folder and is named **Generic API Load Test**. Prometheus and Pushgateway are at ports 9090 and 9091. All three services bind to localhost. Compose creates fresh, project-local data volumes; it does not reuse another Compose project's volumes.

## Configure the API request

Edit `k6/.env` and set `BASE_URL`, `API_PATH`, and `HTTP_METHOD`. Optional settings include:

- `BODY_FILE` or `BODY_JSON` for a request body
- `HEADERS_JSON` and `AUTH_HEADER_NAME` / `AUTH_HEADER_VALUE` for headers and authentication
- `EXPECTED_STATUS`, `EXPECT_JSON`, `JSON_TYPE`, and `MIN_ITEMS` for response checks
- `PAGE_MODE=rotate`, `PAGE_FIELD`, and `PAGE_COUNT` for paginated list APIs

The API host must be reachable from inside the k6 container. Docker Desktop commonly provides `host.docker.internal`; on Linux, use an address or Docker host-gateway configuration reachable from the container. Keep real tokens and passwords only in the ignored local `.env` file or inject them through a secrets manager. Do not commit generated result files.

## Run a smoke check and load test

Run a single request first:

```bash
SMOKE=1 TEST_ID=sample-api-smoke docker compose run --rm loadtest
```

Then run a constant-arrival-rate test, for example one iteration per second for one minute:

```bash
TEST_ID=sample-api-r1 RATE=1 DURATION=1m PREALLOCATED_VUS=10 MAX_VUS=50 \
  docker compose run --rm loadtest
```

The default scenario makes one HTTP request per iteration, so the iteration rate is the target request rate. Review actual RPS, `dropped_iterations`, HTTP failures, and response checks together. The JSON summary is written under `k6/results/` and ignored by Git. k6 sends its metrics to Prometheus with the `testid` label; use the same `TEST_ID` when collecting runtime metrics.

## Collect JVM and Spring metrics

The exporter attaches to a local Java process using the JDK Attach API. Run it as the same OS user as the application process. Spring Actuator metrics are collected when the metrics endpoint is exposed through JMX.

```bash
jcmd -l
javac --add-modules jdk.attach,jdk.httpserver -d tools tools/JvmMetricsExporter.java
APP_HTTP_METRIC_TAGS='uri:/api/orders,method:GET,status:200' \
  java --add-modules jdk.attach,jdk.httpserver -cp tools JvmMetricsExporter <java-pid> 127.0.0.1 9404
```

Without `APP_HTTP_METRIC_TAGS`, the exporter reports the app-wide HTTP request timer. It also collects JVM heap, CPU, threads, GC, Spring request timers, and available JDBC/Hikari metrics. The collector reads this endpoint at `http://127.0.0.1:9404/metrics`.

## Collect Oracle or MariaDB metrics

Run the runtime collector in another terminal with the same `TEST_ID` used by k6. It reads JVM metrics, database metrics, and Docker container CPU/memory, then pushes the samples to Pushgateway. Stop it with Ctrl-C when the load test is complete.

Oracle example:

```bash
python3 tools/publish-runtime-metrics.py \
  --database oracle \
  --test-id sample-api-r1 \
  --oracle-container oracle-db \
  --interval 10 \
  --output results/sample-api-r1-runtime.jsonl
```

The Oracle container must have SQL*Plus and permit local `sqlplus / as sysdba` access. The collector reads Oracle session/system views and recent SQL statistics.

MariaDB example:

```bash
# Load MARIADB_PASSWORD from a local secrets manager before starting the collector.
python3 tools/publish-runtime-metrics.py \
  --database mariadb \
  --test-id sample-api-r1 \
  --mariadb-container app-mariadb \
  --mariadb-client mariadb \
  --mariadb-user metrics \
  --mariadb-schema app_db \
  --interval 10 \
  --output results/sample-api-r1-runtime.jsonl
```

The MariaDB container must have the `mariadb` client (`--mariadb-client mysql` is also supported). The account needs access to global status values. SQL digest timing additionally needs `SELECT` access to `performance_schema.setup_consumers` and `performance_schema.events_statements_summary_by_digest`, with the `statements_digest` consumer enabled. If digest timing is unavailable, connection and container metrics still publish and the collector reports the reason. The collector does not change database settings or write application data.

The MariaDB password is read from `MARIADB_PASSWORD`. It is sent to the container over Docker stdin, written to a temporary mode-600 client option file inside the container, and removed after each query. It is not put in process arguments, Prometheus labels, or result JSONL. The collector publishes connection totals/active/limits, global query counters, and statement digest execution/time summaries. Digests are represented by normalized hashes rather than SQL text; timer totals are converted from picoseconds to seconds. The global `QUERIES` and `QUESTIONS` counters include the collector's own small number of polling statements.

For either database, container CPU and memory come from `docker stats`. Host CPU and Oracle per-SQL CPU measurements are available only from Oracle's system views. The shared Grafana dashboard keeps the same panels for both database types and retains fallback expressions for older Oracle samples.

## Metrics in Grafana

The dashboard shows k6 request rate, response-time percentiles and failures; Spring request count and latency; JVM CPU, heap, threads and GC; database connections and query timing; and database container CPU and memory. Some Spring and database panels require the optional runtime collectors above.

## Stop the stack

```bash
cd k6
docker compose down
```

This stops the services and keeps their local Prometheus/Grafana volumes for the next run. `docker compose down -v` also deletes those volumes and their local history.
