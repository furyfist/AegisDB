INSERT INTO _aegisdb_profiling_reports (
    report_id, connection_hint, status,
    tables_scanned, total_anomalies, critical_count,
    warning_count, duration_ms, report_data, created_at
) VALUES (
    'rpt-0001-0001-0001-000000000001',
    'localhost:5433/aegisdb',
    'completed',
    2, 4, 2, 2, 3420,
    '{"tables": [{"table_name": "orders", "schema": "public", "row_count": 6, "anomaly_count": 3, "anomalies": [{"column": "customer_id", "type": "null_violation", "severity": "critical", "detail": "2 NULL values found", "count": 2}, {"column": "amount", "type": "range_violation", "severity": "warning", "detail": "1 negative value found", "count": 1}, {"column": "customer_id", "type": "referential_integrity", "severity": "critical", "detail": "1 FK violation to customers", "count": 1}]}, {"table_name": "customers", "schema": "public", "row_count": 6, "anomaly_count": 2, "anomalies": [{"column": "email", "type": "uniqueness_violation", "severity": "warning", "detail": "1 duplicate email found", "count": 1}, {"column": "age", "type": "range_violation", "severity": "critical", "detail": "2 out-of-range values found", "count": 2}]}]}',
    NOW() - INTERVAL '3 hours'
);