import com.sun.net.httpserver.HttpServer;
import com.sun.tools.attach.VirtualMachine;

import javax.management.MBeanServerConnection;
import javax.management.ObjectName;
import javax.management.openmbean.CompositeData;
import javax.management.remote.JMXConnector;
import javax.management.remote.JMXConnectorFactory;
import javax.management.remote.JMXServiceURL;
import java.io.IOException;
import java.lang.management.MemoryUsage;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executors;

public final class JvmMetricsExporter {
    private JvmMetricsExporter() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 3) {
            throw new IllegalArgumentException("usage: JvmMetricsExporter <pid> <bind-host> <port>");
        }

        String address;
        VirtualMachine vm = VirtualMachine.attach(args[0]);
        try {
            address = vm.startLocalManagementAgent();
        } finally {
            vm.detach();
        }

        JMXConnector connector = JMXConnectorFactory.connect(new JMXServiceURL(address));
        MBeanServerConnection mbeans = connector.getMBeanServerConnection();
        HttpServer server = HttpServer.create(
                new InetSocketAddress(InetAddress.getByName(args[1]), Integer.parseInt(args[2])), 0);
        server.createContext("/metrics", exchange -> {
            byte[] body;
            try {
                body = render(mbeans).getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "text/plain; version=0.0.4; charset=utf-8");
                exchange.sendResponseHeaders(200, body.length);
            } catch (Exception e) {
                body = ("# JVM metrics scrape failed: " + e.getClass().getSimpleName() + "\n")
                        .getBytes(StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(500, body.length);
            }
            try (var out = exchange.getResponseBody()) {
                out.write(body);
            }
        });
        server.setExecutor(Executors.newSingleThreadExecutor(r -> {
            Thread thread = new Thread(r, "jvm-metrics-http");
            thread.setDaemon(true);
            return thread;
        }));
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.stop(1);
            try {
                connector.close();
            } catch (IOException ignored) {
            }
        }));
        server.start();
        System.out.println("JVM metrics exporter listening on http://" + args[1] + ":" + args[2] + "/metrics");
    }

    private static String render(MBeanServerConnection mbeans) throws Exception {
        StringBuilder out = new StringBuilder(4096);
        out.append("# TYPE jvm_memory_used_bytes gauge\n");
        out.append("# TYPE jvm_memory_committed_bytes gauge\n");
        out.append("# TYPE jvm_memory_max_bytes gauge\n");
        out.append("# TYPE jvm_threads_live_threads gauge\n");
        out.append("# TYPE jvm_threads_peak_threads gauge\n");
        out.append("# TYPE jvm_gc_collection_count_total counter\n");
        out.append("# TYPE jvm_gc_collection_time_seconds_total counter\n");
        out.append("# TYPE jvm_process_cpu_usage_ratio gauge\n");
        out.append("# TYPE jvm_process_cpu_time_seconds_total counter\n");

        ObjectName memory = new ObjectName("java.lang:type=Memory");
        appendMemory(out, mbeans, memory, "HeapMemoryUsage", "heap");
        appendMemory(out, mbeans, memory, "NonHeapMemoryUsage", "nonheap");

        ObjectName threading = new ObjectName("java.lang:type=Threading");
        metric(out, "jvm_threads_live_threads", number(mbeans.getAttribute(threading, "ThreadCount")), "");
        metric(out, "jvm_threads_peak_threads", number(mbeans.getAttribute(threading, "PeakThreadCount")), "");

        ObjectName os = new ObjectName("java.lang:type=OperatingSystem");
        try {
            double cpu = number(mbeans.getAttribute(os, "ProcessCpuLoad"));
            if (cpu >= 0) metric(out, "jvm_process_cpu_usage_ratio", cpu, "");
        } catch (Exception ignored) {
        }
        try {
            double cpuNanos = number(mbeans.getAttribute(os, "ProcessCpuTime"));
            if (cpuNanos >= 0) metric(out, "jvm_process_cpu_time_seconds_total", cpuNanos / 1_000_000_000d, "");
        } catch (Exception ignored) {
        }

        Set<ObjectName> gcBeans = mbeans.queryNames(new ObjectName("java.lang:type=GarbageCollector,*"), null);
        for (ObjectName bean : gcBeans) {
            String gcName = safeLabel(attributeText(mbeans, bean, "Name", bean.getKeyProperty("name")));
            String labels = "{gc=\"" + gcName + "\"}";
            try {
                metric(out, "jvm_gc_collection_count_total",
                        number(mbeans.getAttribute(bean, "CollectionCount")), labels);
                metric(out, "jvm_gc_collection_time_seconds_total",
                        number(mbeans.getAttribute(bean, "CollectionTime")) / 1000d, labels);
            } catch (Exception ignored) {
            }
        }

        appendActuator(out, mbeans);
        appendTomcat(out, mbeans);
        appendHikari(out, mbeans);
        return out.toString();
    }

    private static void appendActuator(StringBuilder out, MBeanServerConnection mbeans) {
        ObjectName endpoint;
        try {
            endpoint = new ObjectName("org.springframework.boot:type=Endpoint,name=Metrics");
            if (!mbeans.isRegistered(endpoint)) return;
        } catch (Exception ignored) {
            return;
        }

        out.append("# TYPE spring_http_server_requests_total counter\n");
        out.append("# TYPE spring_http_server_request_duration_seconds_total counter\n");
        out.append("# TYPE spring_http_server_request_duration_seconds_max gauge\n");
        out.append("# TYPE spring_http_server_requests_active gauge\n");

        String requestMetric = System.getenv().getOrDefault("APP_HTTP_METRIC_NAME", "http.server.requests");
        String rawTags = System.getenv().getOrDefault("APP_HTTP_METRIC_TAGS", "");
        List<String> tags = new ArrayList<>();
        for (String tag : rawTags.split(",")) {
            if (!tag.isBlank()) tags.add(tag.trim());
        }
        Map<String, Double> request = actuatorMeasurements(mbeans, requestMetric, tags);
        String labels = actuatorLabels(tags);
        addMeasurement(out, request, "COUNT", "spring_http_server_requests_total", labels);
        addMeasurement(out, request, "TOTAL_TIME", "spring_http_server_request_duration_seconds_total", labels);
        addMeasurement(out, request, "MAX", "spring_http_server_request_duration_seconds_max", labels);
        Map<String, Double> active = actuatorMeasurements(
                mbeans, requestMetric + ".active", tags);
        addMeasurement(out, active, "VALUE", "spring_http_server_requests_active", labels);

        out.append("# TYPE spring_process_cpu_usage_ratio gauge\n");
        out.append("# TYPE spring_system_cpu_usage_ratio gauge\n");
        out.append("# TYPE spring_jdbc_connections_active gauge\n");
        out.append("# TYPE spring_jdbc_connections_idle gauge\n");
        out.append("# TYPE spring_jdbc_connections_max gauge\n");
        appendSimpleActuatorGauge(out, mbeans, "process.cpu.usage",
                "spring_process_cpu_usage_ratio");
        appendSimpleActuatorGauge(out, mbeans, "system.cpu.usage",
                "spring_system_cpu_usage_ratio");
        appendTaggedActuatorGauge(out, mbeans, "jdbc.connections.active", "name",
                "spring_jdbc_connections_active");
        appendTaggedActuatorGauge(out, mbeans, "jdbc.connections.idle", "name",
                "spring_jdbc_connections_idle");
        appendTaggedActuatorGauge(out, mbeans, "jdbc.connections.max", "name",
                "spring_jdbc_connections_max");

        out.append("# TYPE spring_hikaricp_connections_active gauge\n");
        out.append("# TYPE spring_hikaricp_connections_idle gauge\n");
        out.append("# TYPE spring_hikaricp_connections_pending gauge\n");
        out.append("# TYPE spring_hikaricp_connections_max gauge\n");
        out.append("# TYPE spring_hikaricp_connection_acquire_count_total counter\n");
        out.append("# TYPE spring_hikaricp_connection_acquire_seconds_total counter\n");
        out.append("# TYPE spring_hikaricp_connection_acquire_seconds_max gauge\n");
        appendTaggedActuatorGauge(out, mbeans, "hikaricp.connections.active", "pool",
                "spring_hikaricp_connections_active");
        appendTaggedActuatorGauge(out, mbeans, "hikaricp.connections.idle", "pool",
                "spring_hikaricp_connections_idle");
        appendTaggedActuatorGauge(out, mbeans, "hikaricp.connections.pending", "pool",
                "spring_hikaricp_connections_pending");
        appendTaggedActuatorGauge(out, mbeans, "hikaricp.connections.max", "pool",
                "spring_hikaricp_connections_max");
        for (String pool : actuatorTagValues(mbeans, "hikaricp.connections.acquire", "pool")) {
            List<String> poolTag = List.of("pool:" + pool);
            Map<String, Double> acquire = actuatorMeasurements(
                    mbeans, "hikaricp.connections.acquire", poolTag);
            String poolLabels = "{pool=\"" + safeLabel(pool) + "\"}";
            addMeasurement(out, acquire, "COUNT",
                    "spring_hikaricp_connection_acquire_count_total", poolLabels);
            addMeasurement(out, acquire, "TOTAL_TIME",
                    "spring_hikaricp_connection_acquire_seconds_total", poolLabels);
            addMeasurement(out, acquire, "MAX",
                    "spring_hikaricp_connection_acquire_seconds_max", poolLabels);
        }
    }

    private static void appendSimpleActuatorGauge(StringBuilder out, MBeanServerConnection mbeans,
                                                   String source, String metric) {
        addMeasurement(out, actuatorMeasurements(mbeans, source, List.of()),
                "VALUE", metric, "");
    }

    private static void appendTaggedActuatorGauge(StringBuilder out, MBeanServerConnection mbeans,
                                                   String source, String tagName, String metric) {
        for (String tagValue : actuatorTagValues(mbeans, source, tagName)) {
            List<String> tags = List.of(tagName + ":" + tagValue);
            String labels = "{" + tagName + "=\"" + safeLabel(tagValue) + "\"}";
            addMeasurement(out, actuatorMeasurements(mbeans, source, tags),
                    "VALUE", metric, labels);
        }
    }

    private static List<String> actuatorTagValues(MBeanServerConnection mbeans,
                                                   String metricName, String tagName) {
        try {
            ObjectName endpoint = new ObjectName("org.springframework.boot:type=Endpoint,name=Metrics");
            Object result = mbeans.invoke(endpoint, "metric",
                    new Object[]{metricName, List.of()}, new String[]{"java.lang.String", "java.util.List"});
            if (!(result instanceof Map<?, ?> map)) return List.of();
            Object available = map.get("availableTags");
            if (!(available instanceof List<?> tagList)) return List.of();
            for (Object item : tagList) {
                if (!(item instanceof Map<?, ?> tag) || !tagName.equals(String.valueOf(tag.get("tag")))) continue;
                Object values = tag.get("values");
                if (!(values instanceof List<?> valueList)) return List.of();
                return valueList.stream().map(String::valueOf).toList();
            }
        } catch (Exception ignored) {
        }
        return List.of();
    }

    private static Map<String, Double> actuatorMeasurements(MBeanServerConnection mbeans,
                                                             String metricName, List<String> tags) {
        try {
            ObjectName endpoint = new ObjectName("org.springframework.boot:type=Endpoint,name=Metrics");
            Object result = mbeans.invoke(endpoint, "metric",
                    new Object[]{metricName, tags}, new String[]{"java.lang.String", "java.util.List"});
            if (!(result instanceof Map<?, ?> map)) return Map.of();
            Object measurements = map.get("measurements");
            if (!(measurements instanceof List<?> measurementList)) return Map.of();
            Map<String, Double> values = new java.util.HashMap<>();
            for (Object item : measurementList) {
                if (!(item instanceof Map<?, ?> measurement)) continue;
                String statistic = String.valueOf(measurement.get("statistic"));
                double value = number(measurement.get("value"));
                if (Double.isFinite(value)) values.put(statistic, value);
            }
            return values;
        } catch (Exception ignored) {
            return Map.of();
        }
    }

    private static void addMeasurement(StringBuilder out, Map<String, Double> measurements,
                                       String statistic, String metric, String labels) {
        Double value = measurements.get(statistic);
        if (value != null) metric(out, metric, value, labels);
    }

    private static String actuatorLabels(List<String> tags) {
        List<String> labels = new ArrayList<>();
        for (String tag : tags) {
            int colon = tag.indexOf(':');
            if (colon <= 0) continue;
            labels.add(tag.substring(0, colon) + "=\"" + safeLabel(tag.substring(colon + 1)) + "\"");
        }
        return labels.isEmpty() ? "" : "{" + String.join(",", labels) + "}";
    }

    private static void appendMemory(StringBuilder out, MBeanServerConnection mbeans, ObjectName bean,
                                     String attribute, String area) throws Exception {
        Object value = mbeans.getAttribute(bean, attribute);
        if (!(value instanceof CompositeData data)) return;
        metric(out, "jvm_memory_used_bytes", number(data.get("used")), "{area=\"" + area + "\"}");
        metric(out, "jvm_memory_committed_bytes", number(data.get("committed")), "{area=\"" + area + "\"}");
        metric(out, "jvm_memory_max_bytes", number(data.get("max")), "{area=\"" + area + "\"}");
    }

    private static void appendTomcat(StringBuilder out, MBeanServerConnection mbeans) throws Exception {
        out.append("# TYPE tomcat_global_request_total counter\n");
        out.append("# TYPE tomcat_global_error_total counter\n");
        out.append("# TYPE tomcat_global_processing_time_milliseconds_total counter\n");
        out.append("# TYPE tomcat_global_max_time_milliseconds gauge\n");
        out.append("# TYPE tomcat_global_bytes_received_total counter\n");
        out.append("# TYPE tomcat_global_bytes_sent_total counter\n");
        Set<ObjectName> beans = mbeans.queryNames(new ObjectName("*:type=GlobalRequestProcessor,*"), null);
        for (ObjectName bean : beans) {
            String connector = safeLabel(bean.getKeyProperty("name"));
            String labels = "{connector=\"" + connector + "\"}";
            appendAttribute(out, mbeans, bean, "RequestCount", "tomcat_global_request_total", labels);
            appendAttribute(out, mbeans, bean, "ErrorCount", "tomcat_global_error_total", labels);
            appendAttribute(out, mbeans, bean, "ProcessingTime", "tomcat_global_processing_time_milliseconds_total", labels);
            appendAttribute(out, mbeans, bean, "MaxTime", "tomcat_global_max_time_milliseconds", labels);
            appendAttribute(out, mbeans, bean, "BytesReceived", "tomcat_global_bytes_received_total", labels);
            appendAttribute(out, mbeans, bean, "BytesSent", "tomcat_global_bytes_sent_total", labels);
        }
    }

    private static void appendHikari(StringBuilder out, MBeanServerConnection mbeans) throws Exception {
        out.append("# TYPE hikari_pool_active_connections gauge\n");
        out.append("# TYPE hikari_pool_idle_connections gauge\n");
        out.append("# TYPE hikari_pool_total_connections gauge\n");
        out.append("# TYPE hikari_pool_pending_threads gauge\n");
        for (ObjectName bean : mbeans.queryNames(new ObjectName("com.zaxxer.hikari:*"), null)) {
            if (!bean.toString().contains("type=Pool")) continue;
            String pool = safeLabel(bean.getKeyProperty("type"));
            String labels = "{pool=\"" + pool + "\"}";
            appendAttribute(out, mbeans, bean, "ActiveConnections", "hikari_pool_active_connections", labels);
            appendAttribute(out, mbeans, bean, "IdleConnections", "hikari_pool_idle_connections", labels);
            appendAttribute(out, mbeans, bean, "TotalConnections", "hikari_pool_total_connections", labels);
            appendAttribute(out, mbeans, bean, "ThreadsAwaitingConnection", "hikari_pool_pending_threads", labels);
        }
    }

    private static void appendAttribute(StringBuilder out, MBeanServerConnection mbeans, ObjectName bean,
                                        String attribute, String metric, String labels) {
        try {
            metric(out, metric, number(mbeans.getAttribute(bean, attribute)), labels);
        } catch (Exception ignored) {
        }
    }

    private static String attributeText(MBeanServerConnection mbeans, ObjectName bean,
                                        String attribute, String fallback) {
        try {
            Object value = mbeans.getAttribute(bean, attribute);
            return value == null ? fallback : value.toString();
        } catch (Exception ignored) {
            return fallback;
        }
    }

    private static double number(Object value) {
        if (value instanceof Number number) return number.doubleValue();
        return Double.NaN;
    }

    private static void metric(StringBuilder out, String name, double value, String labels) {
        if (Double.isFinite(value)) out.append(name).append(labels).append(' ').append(value).append('\n');
    }

    private static String safeLabel(String value) {
        if (value == null) return "unknown";
        if (value.length() >= 2 && value.startsWith("\"") && value.endsWith("\"")) {
            value = value.substring(1, value.length() - 1);
        }
        return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ");
    }
}
