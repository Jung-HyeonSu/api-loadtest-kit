#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ORACLE_SQL = """set pages 0 feedback off heading off lines 250 trimspool on verify off define off
select 'session_user_total|'||count(*) from v$session where type='USER' and username is not null and username not in ('SYS','SYSTEM');
select 'session_user_active|'||count(*) from v$session where type='USER' and status='ACTIVE' and username is not null and username not in ('SYS','SYSTEM');
select 'session_jdbc_total|'||count(*) from v$session where type='USER' and (lower(program) like '%jdbc%' or lower(program) like '%java%');
select 'session_jdbc_active|'||count(*) from v$session where type='USER' and status='ACTIVE' and (lower(program) like '%jdbc%' or lower(program) like '%java%');
select 'connection_limit|'||limit_value from v$resource_limit where resource_name='sessions';
select 'sysmetric|'||replace(metric_name,' ','_')||'|'||round(value,6)||'|'||replace(metric_unit,'|',' ') from v$sysmetric where group_id=2 and (metric_name in ('Host CPU Utilization (%)','CPU Usage Per Sec','SQL Service Response Time'));
select 'sql|'||sql_id||'|'||executions||'|'||elapsed_time||'|'||cpu_time||'|'||rows_processed from (select sql_id, sum(executions) executions, sum(elapsed_time) elapsed_time, sum(cpu_time) cpu_time, sum(rows_processed) rows_processed, max(last_active_time) last_active_time from v$sqlstats where executions>0 group by sql_id order by last_active_time desc) where rownum<=50;
exit
"""

MARIADB_STATUS_SQL = """SELECT CONCAT('status|', UPPER(VARIABLE_NAME), '|', VARIABLE_VALUE)
FROM information_schema.GLOBAL_STATUS
WHERE UPPER(VARIABLE_NAME) IN ('THREADS_CONNECTED', 'THREADS_RUNNING', 'QUERIES', 'QUESTIONS');
SELECT CONCAT('limit|MAX_CONNECTIONS|', VARIABLE_VALUE)
FROM information_schema.GLOBAL_VARIABLES
WHERE UPPER(VARIABLE_NAME) = 'MAX_CONNECTIONS';"""

MARIADB_DIGEST_SQL = """SELECT CONCAT('consumer|', ENABLED)
FROM performance_schema.setup_consumers
WHERE NAME = 'statements_digest';
SELECT CONCAT('query|', COALESCE(DIGEST, 'NO_DIGEST'), '|', COUNT_STAR, '|',
  COALESCE(SUM_TIMER_WAIT, 0), '|', COALESCE(SUM_ROWS_EXAMINED, 0))
FROM performance_schema.events_statements_summary_by_digest
WHERE COUNT_STAR > 0
  AND (DIGEST_TEXT IS NULL OR (
    LOWER(DIGEST_TEXT) NOT LIKE '%events_statements_summary_by_digest%'
    AND LOWER(DIGEST_TEXT) NOT LIKE '%performance_schema.setup_consumers%'
    AND LOWER(DIGEST_TEXT) NOT LIKE '%information_schema.global_status%'
    AND LOWER(DIGEST_TEXT) NOT LIKE '%information_schema.global_variables%'
  ))
  AND (DATABASE() IS NULL OR SCHEMA_NAME <=> DATABASE())
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 50;"""

SAMPLE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+([-+0-9.eE]+)$')
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"')


def parse_prometheus(text):
    result = []
    for line in text.splitlines():
        match = SAMPLE_RE.match(line.strip())
        if not match:
            continue
        labels = {}
        if match.group(2):
            for key, value in LABEL_RE.findall(match.group(2)):
                labels[key] = value.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        result.append({"name": match.group(1), "labels": labels, "value": value})
    return result


def query_oracle(container):
    result = subprocess.run(
        ["docker", "exec", "-i", container, "bash", "-lc", "sqlplus -s / as sysdba"],
        input=ORACLE_SQL,
        text=True,
        capture_output=True,
        check=True,
        timeout=25,
    )
    metrics = {}
    sql_rows = []
    for raw in result.stdout.splitlines():
        fields = raw.strip().split("|")
        if len(fields) == 2 and (fields[0].startswith("session_") or fields[0] == "connection_limit"):
            try:
                metrics[fields[0]] = int(float(fields[1]))
            except ValueError:
                pass
        elif len(fields) == 4 and fields[0] == "sysmetric":
            metrics[fields[1]] = {"value": float(fields[2]), "unit": fields[3]}
        elif len(fields) == 6 and fields[0] == "sql":
            sql_rows.append({
                "sql_id": fields[1],
                "executions": int(float(fields[2])),
                "elapsed_time_microseconds": int(float(fields[3])),
                "cpu_time_microseconds": int(float(fields[4])),
                "rows_processed": int(float(fields[5])),
            })
    required = ("session_user_total", "session_jdbc_total")
    if any(key not in metrics for key in required):
        raise RuntimeError("Oracle SQL*Plus did not return the expected metrics")
    if "connection_limit" in metrics:
        metrics["connections_limit"] = metrics["connection_limit"]
    metrics["sql"] = sql_rows
    metrics["query_metrics_available"] = 1
    return metrics


def mariadb_option_value(value):
    if any(char in value for char in ("\0", "\r", "\n")):
        raise ValueError("MariaDB credentials must not contain line breaks or null bytes")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t") + '"'


def run_mariadb_sql(container, client, user, password, schema, sql):
    option_file = "[client]\nuser=" + mariadb_option_value(user) + "\n"
    if password:
        option_file += "password=" + mariadb_option_value(password) + "\n"
    config_path = "/tmp/.api-loadtest-mariadb-" + uuid.uuid4().hex + ".cnf"
    shell = (
        "set -eu\n"
        "umask 077\n"
        "config='" + config_path + "'\n"
        "trap 'rm -f \"$config\"' 0\n"
        "trap 'exit 129' HUP\n"
        "trap 'exit 130' INT\n"
        "trap 'exit 143' TERM\n"
        "cat > \"$config\"\n"
        "client=\"$1\"\n"
        "shift\n"
        "\"$client\" --defaults-extra-file=\"$config\" \"$@\"\n"
    )
    command = ["docker", "exec", "-i", container, "sh", "-c", shell, "sh", client,
               "--batch", "--skip-column-names", "--raw"]
    if schema:
        command.append("--database=" + schema)
    command.extend(["--execute", sql])
    result = subprocess.run(command, input=option_file, text=True, capture_output=True,
                            check=True, timeout=25)
    return result.stdout


def query_mariadb(container, client, user, password, schema):
    status_text = run_mariadb_sql(container, client, user, password, schema, MARIADB_STATUS_SQL)
    metrics = {"sql": [], "query_metrics_available": 0}
    status = {}
    for raw in status_text.splitlines():
        fields = raw.strip().split("|")
        if len(fields) != 3:
            continue
        if fields[0] == "status":
            try:
                status[fields[1]] = int(float(fields[2]))
            except ValueError:
                continue
        elif fields[0] == "limit" and fields[1] == "MAX_CONNECTIONS":
            try:
                metrics["connections_limit"] = int(float(fields[2]))
            except ValueError:
                pass

    required = ("THREADS_CONNECTED", "THREADS_RUNNING")
    if any(key not in status for key in required):
        raise RuntimeError("MariaDB did not return the expected global status values")

    # The collector's own client connection and query are included in these global counters.
    metrics["connections_total"] = max(0, status["THREADS_CONNECTED"] - 1)
    metrics["connections_active"] = max(0, status["THREADS_RUNNING"] - 1)
    metrics["queries_total"] = status.get("QUERIES")
    metrics["questions_total"] = status.get("QUESTIONS")

    try:
        digest_text = run_mariadb_sql(container, client, user, password, schema, MARIADB_DIGEST_SQL)
        sql_rows = []
        digest_consumer_enabled = None
        for raw in digest_text.splitlines():
            fields = raw.strip().split("|")
            if len(fields) == 2 and fields[0] == "consumer":
                digest_consumer_enabled = fields[1].upper() == "YES"
                continue
            if len(fields) != 5 or fields[0] != "query":
                continue
            try:
                sql_rows.append({
                    "query_id": fields[1] or "NO_DIGEST",
                    "executions": int(float(fields[2])),
                    "elapsed_time_picoseconds": int(float(fields[3])),
                    "rows_processed": int(float(fields[4])),
                })
            except ValueError:
                continue
        if digest_consumer_enabled:
            metrics["sql"] = sql_rows
            metrics["query_metrics_available"] = 1
        else:
            metrics["query_metrics_error"] = "Performance Schema statements_digest consumer is disabled or unavailable"
    except subprocess.CalledProcessError as exc:
        metrics["query_metrics_error"] = (exc.stderr or "Performance Schema digest table unavailable").strip()[:500]
    return metrics


def parse_bytes(value):
    match = re.fullmatch(r'\s*([0-9.]+)\s*([KMGT]?i?B)\s*', value, re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).upper()
    factors = {
        "B": 1, "KB": 1000, "KIB": 1024,
        "MB": 1000 ** 2, "MIB": 1024 ** 2,
        "GB": 1000 ** 3, "GIB": 1024 ** 3,
        "TB": 1000 ** 4, "TIB": 1024 ** 4,
    }
    return int(number * factors[unit])


def docker_stats(container):
    output = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", container],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    cpu_text, memory_text = output.split("|", 1)
    used_text, limit_text = memory_text.split("/", 1)
    return {
        "cpu_percent": float(cpu_text.strip().rstrip("%")),
        "memory_used_bytes": parse_bytes(used_text),
        "memory_limit_bytes": parse_bytes(limit_text),
    }


def database_prometheus(database, metrics, stats):
    database_label = '{database="' + database + '"}'
    lines = [
        "# TYPE database_connections_total gauge",
        "# TYPE database_connections_active gauge",
        "# TYPE database_connections_limit gauge",
        "# TYPE database_queries_total counter",
        "# TYPE database_questions_total counter",
        "# TYPE database_query_metrics_available gauge",
        "# TYPE database_container_cpu_usage_percent gauge",
        "# TYPE database_container_memory_used_bytes gauge",
        "# TYPE database_container_memory_limit_bytes gauge",
        "# TYPE database_container_memory_usage_ratio gauge",
        "# TYPE database_host_cpu_usage_percent gauge",
        "# TYPE database_cpu_usage_cores gauge",
        "# TYPE database_sql_service_response_time_seconds gauge",
        "# TYPE database_query_executions_total counter",
        "# TYPE database_query_elapsed_seconds_total counter",
        "# TYPE database_query_cpu_seconds_total counter",
        "# TYPE database_query_rows_processed_total counter",
    ]

    if database == "oracle":
        connections_total = metrics.get("session_jdbc_total", 0)
        connections_active = metrics.get("session_jdbc_active", 0)
    else:
        connections_total = metrics.get("connections_total", 0)
        connections_active = metrics.get("connections_active", 0)
    lines.append("database_connections_total" + database_label + " " + str(connections_total))
    lines.append("database_connections_active" + database_label + " " + str(connections_active))
    if metrics.get("connections_limit") is not None:
        lines.append("database_connections_limit" + database_label + " " + str(metrics["connections_limit"]))
    if metrics.get("queries_total") is not None:
        lines.append("database_queries_total" + database_label + " " + str(metrics["queries_total"]))
    if metrics.get("questions_total") is not None:
        lines.append("database_questions_total" + database_label + " " + str(metrics["questions_total"]))
    lines.append("database_query_metrics_available" + database_label + " " + str(metrics.get("query_metrics_available", 0)))

    lines.append("database_container_cpu_usage_percent" + database_label + " " + str(stats["cpu_percent"]))
    lines.append("database_container_memory_used_bytes" + database_label + " " + str(stats["memory_used_bytes"]))
    lines.append("database_container_memory_limit_bytes" + database_label + " " + str(stats["memory_limit_bytes"]))
    ratio = stats["memory_used_bytes"] / stats["memory_limit_bytes"] if stats["memory_limit_bytes"] else 0
    lines.append("database_container_memory_usage_ratio" + database_label + " " + str(ratio))

    host_cpu = metrics.get("Host_CPU_Utilization_(%)")
    if isinstance(host_cpu, dict):
        lines.append("database_host_cpu_usage_percent" + database_label + " " + str(host_cpu["value"]))
    cpu_per_sec = metrics.get("CPU_Usage_Per_Sec")
    if isinstance(cpu_per_sec, dict):
        lines.append("database_cpu_usage_cores" + database_label + " " + str(cpu_per_sec["value"] / 100.0))
    sql_response = metrics.get("SQL_Service_Response_Time")
    if isinstance(sql_response, dict):
        # Oracle reports centiseconds per call.
        lines.append("database_sql_service_response_time_seconds" + database_label + " " + str(sql_response["value"] / 100.0))

    for row in metrics.get("sql", []):
        query_id = row.get("query_id", row.get("sql_id", "NO_DIGEST"))
        safe_query_id = str(query_id).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        labels = '{database="' + database + '",query_id="' + safe_query_id + '"}'
        elapsed_seconds = row.get("elapsed_time_microseconds", 0) / 1_000_000.0
        if database == "mariadb":
            elapsed_seconds = row.get("elapsed_time_picoseconds", 0) / 1_000_000_000_000.0
        lines.append("database_query_executions_total" + labels + " " + str(row["executions"]))
        lines.append("database_query_elapsed_seconds_total" + labels + " " + str(elapsed_seconds))
        if row.get("cpu_time_microseconds") is not None:
            lines.append("database_query_cpu_seconds_total" + labels + " " + str(row["cpu_time_microseconds"] / 1_000_000.0))
        lines.append("database_query_rows_processed_total" + labels + " " + str(row.get("rows_processed", 0)))
    return "\n".join(lines) + "\n"


def oracle_legacy_prometheus(metrics, stats):
    lines = [
        "# TYPE oracle_sessions_user_total gauge",
        "# TYPE oracle_sessions_user_active gauge",
        "# TYPE oracle_sessions_jdbc_total gauge",
        "# TYPE oracle_sessions_jdbc_active gauge",
        "# TYPE oracle_host_cpu_usage_percent gauge",
        "# TYPE oracle_cpu_usage_cores gauge",
        "# TYPE oracle_sql_service_response_time_seconds gauge",
        "# TYPE oracle_container_cpu_usage_percent gauge",
        "# TYPE oracle_container_memory_used_bytes gauge",
        "# TYPE oracle_container_memory_limit_bytes gauge",
        "# TYPE oracle_container_memory_usage_ratio gauge",
        "# TYPE oracle_sql_executions_total counter",
        "# TYPE oracle_sql_elapsed_seconds_total counter",
        "# TYPE oracle_sql_cpu_seconds_total counter",
        "# TYPE oracle_sql_rows_processed_total counter",
    ]
    names = {
        "session_user_total": "oracle_sessions_user_total",
        "session_user_active": "oracle_sessions_user_active",
        "session_jdbc_total": "oracle_sessions_jdbc_total",
        "session_jdbc_active": "oracle_sessions_jdbc_active",
    }
    for source, metric in names.items():
        lines.append(metric + " " + str(metrics.get(source, 0)))

    host_cpu = metrics.get("Host_CPU_Utilization_(%)")
    if isinstance(host_cpu, dict):
        lines.append("oracle_host_cpu_usage_percent " + str(host_cpu["value"]))
    cpu_per_sec = metrics.get("CPU_Usage_Per_Sec")
    if isinstance(cpu_per_sec, dict):
        lines.append("oracle_cpu_usage_cores " + str(cpu_per_sec["value"] / 100.0))
    sql_response = metrics.get("SQL_Service_Response_Time")
    if isinstance(sql_response, dict):
        # Oracle reports centiseconds per call.
        lines.append("oracle_sql_service_response_time_seconds " + str(sql_response["value"] / 100.0))

    lines.append("oracle_container_cpu_usage_percent " + str(stats["cpu_percent"]))
    lines.append("oracle_container_memory_used_bytes " + str(stats["memory_used_bytes"]))
    lines.append("oracle_container_memory_limit_bytes " + str(stats["memory_limit_bytes"]))
    ratio = stats["memory_used_bytes"] / stats["memory_limit_bytes"] if stats["memory_limit_bytes"] else 0
    lines.append("oracle_container_memory_usage_ratio " + str(ratio))

    for row in metrics["sql"]:
        labels = '{sql_id="' + row["sql_id"] + '"}'
        lines.append("oracle_sql_executions_total" + labels + " " + str(row["executions"]))
        lines.append("oracle_sql_elapsed_seconds_total" + labels + " " + str(row["elapsed_time_microseconds"] / 1_000_000.0))
        lines.append("oracle_sql_cpu_seconds_total" + labels + " " + str(row["cpu_time_microseconds"] / 1_000_000.0))
        lines.append("oracle_sql_rows_processed_total" + labels + " " + str(row["rows_processed"]))
    return "\n".join(lines) + "\n"


def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def push(url, payload):
    request = urllib.request.Request(url, data=payload.encode("utf-8"), method="PUT",
                                     headers={"Content-Type": "text/plain; version=0.0.4"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def main():
    parser = argparse.ArgumentParser(description="Publish local JVM and Oracle/MariaDB measurements to Pushgateway.")
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--database", "--database-type", choices=("oracle", "mariadb"),
                        default=os.environ.get("DATABASE_TYPE", "oracle"))
    parser.add_argument("--oracle-container", default=os.environ.get("ORACLE_CONTAINER", "oracle"))
    parser.add_argument("--mariadb-container", default=os.environ.get("MARIADB_CONTAINER", ""))
    parser.add_argument("--mariadb-client", default=os.environ.get("MARIADB_CLIENT", "mariadb"))
    parser.add_argument("--mariadb-user", default=os.environ.get("MARIADB_USER", "metrics"))
    parser.add_argument("--mariadb-schema", default=os.environ.get("MARIADB_SCHEMA", ""))
    parser.add_argument("--jvm-url", default="http://127.0.0.1:9404/metrics")
    parser.add_argument("--pushgateway", default="http://127.0.0.1:9091")
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.database == "mariadb" and not args.mariadb_container:
        parser.error("--database mariadb requires --mariadb-container or MARIADB_CONTAINER")
    if args.database == "mariadb" and not args.mariadb_user:
        parser.error("MariaDB collector requires --mariadb-user or MARIADB_USER")

    group_url = (args.pushgateway.rstrip("/") + "/metrics/job/runtime-metrics/test_id/"
                 + urllib.parse.quote(args.test_id, safe=""))
    output = args.output.open("a", encoding="utf-8") if args.output else None
    warned_digest_unavailable = False
    try:
        while True:
            timestamp = datetime.now(timezone.utc).isoformat()
            jvm_text = fetch(args.jvm_url)
            jvm_series = parse_prometheus(jvm_text)
            if args.database == "oracle":
                database_metrics = query_oracle(args.oracle_container)
                database_container = args.oracle_container
            else:
                database_metrics = query_mariadb(
                    args.mariadb_container,
                    args.mariadb_client,
                    args.mariadb_user,
                    os.environ.get("MARIADB_PASSWORD", ""),
                    args.mariadb_schema,
                )
                database_container = args.mariadb_container
            stats = docker_stats(database_container)
            payload = jvm_text.rstrip() + "\n" + database_prometheus(args.database, database_metrics, stats)
            if args.database == "oracle":
                payload += oracle_legacy_prometheus(database_metrics, stats)
            push(group_url, payload)
            if output:
                sample = {
                    "timestamp": timestamp,
                    "test_id": args.test_id,
                    "database_type": args.database,
                    "jvm": jvm_series,
                    "database": database_metrics,
                    "database_container": stats,
                }
                if args.database == "oracle":
                    sample["oracle"] = database_metrics
                    sample["oracle_container"] = stats
                output.write(json.dumps(sample, ensure_ascii=False) + "\n")
                output.flush()
            if args.database == "mariadb" and not database_metrics["query_metrics_available"] and not warned_digest_unavailable:
                detail = database_metrics.get("query_metrics_error", "Performance Schema digest table is unavailable")
                print("warning: MariaDB connection metrics are active; SQL timing is unavailable: " + detail,
                      file=sys.stderr, flush=True)
                warned_digest_unavailable = True
            print(timestamp + " published " + str(len(jvm_series)) +
                  " JVM/Actuator series and " + str(len(database_metrics.get("sql", []))) +
                  " " + args.database + " query IDs", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        if output:
            output.close()


if __name__ == "__main__":
    main()
