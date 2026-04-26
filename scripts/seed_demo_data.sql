-- Seed demo proposals
INSERT INTO _aegisdb_proposals (
    proposal_id, event_id, table_fqn, table_name,
    failure_categories, root_cause, confidence,
    fix_sql, fix_description, rollback_sql,
    estimated_rows, sandbox_passed,
    rows_before, rows_after, rows_affected,
    sample_before, sample_after,
    status, created_at, diagnosis_json, event_json
) VALUES
(
    'a1b2c3d4-0001-0001-0001-000000000001',
    'e1b2c3d4-0001-0001-0001-000000000001',
    'aegisDB.default.public.orders',
    'orders',
    ARRAY['null_violation'],
    'NULL values found in customer_id column violating NOT NULL constraint. 2 orphaned order records have no associated customer.',
    0.95,
    'DELETE FROM "orders" WHERE customer_id IS NULL;',
    'Remove 2 orders with NULL customer_id that violate referential integrity',
    'INSERT INTO "orders" (customer_id, amount, status) VALUES (NULL, 99.99, ''completed''), (NULL, 0.00, NULL);',
    2, true, 6, 4, 2,
    '[{"order_id":3,"customer_id":null,"amount":99.99,"status":"completed"},{"order_id":6,"customer_id":null,"amount":0.00,"status":null}]',
    '[]',
    'pending_approval',
    NOW() - INTERVAL '2 hours',
    '{}', '{}'
),
(
    'a1b2c3d4-0002-0002-0002-000000000002',
    'e1b2c3d4-0002-0002-0002-000000000002',
    'aegisDB.default.public.orders',
    'orders',
    ARRAY['range_violation'],
    'Negative amount value found in orders table. Order #4 has amount=-500.00 which violates business rule that amounts must be positive.',
    0.92,
    'UPDATE "orders" SET amount = ABS(amount) WHERE amount < 0;',
    'Convert negative amount to absolute value for order with invalid negative amount',
    'UPDATE "orders" SET amount = -ABS(amount) WHERE amount > 0 AND order_id = 4;',
    1, true, 6, 6, 1,
    '[{"order_id":4,"customer_id":1,"amount":-500.00,"status":"completed"}]',
    '[{"order_id":4,"customer_id":1,"amount":500.00,"status":"completed"}]',
    'approved',
    NOW() - INTERVAL '5 hours',
    '{}', '{}'
),
(
    'a1b2c3d4-0003-0003-0003-000000000003',
    'e1b2c3d4-0003-0003-0003-000000000003',
    'aegisDB.default.public.customers',
    'customers',
    ARRAY['uniqueness_violation'],
    'Duplicate email found: alice@example.com appears twice in customers table. One record has invalid age=-5 suggesting it is a dirty duplicate.',
    0.88,
    'DELETE FROM "customers" WHERE ctid NOT IN (SELECT MIN(ctid) FROM "customers" GROUP BY email) AND email IS NOT NULL;',
    'Remove duplicate customer records keeping the first occurrence of each email',
    NULL,
    1, true, 6, 5, 1,
    '[{"customer_id":4,"email":"alice@example.com","age":-5,"country":null}]',
    '[]',
    'rejected',
    NOW() - INTERVAL '1 day',
    '{}', '{}'
),
(
    'a1b2c3d4-0004-0004-0004-000000000004',
    'e1b2c3d4-0004-0004-0004-000000000004',
    'aegisDB.default.public.customers',
    'customers',
    ARRAY['range_violation'],
    'Invalid age values detected: customer with age=-5 and customer with age=200 both fall outside valid human age range [0,150].',
    0.91,
    'DELETE FROM "customers" WHERE age < 0 OR age > 150;',
    'Remove customers with biologically impossible age values',
    NULL,
    2, true, 6, 4, 2,
    '[{"customer_id":4,"email":"alice@example.com","age":-5,"country":null},{"customer_id":5,"email":null,"age":200,"country":"India"}]',
    '[]',
    'pending_approval',
    NOW() - INTERVAL '30 minutes',
    '{}', '{}'
);

-- Seed demo audit entries
INSERT INTO _aegisdb_audit (
    event_id, table_fqn, table_name, action,
    fix_sql, rollback_sql, rows_affected,
    dry_run, sandbox_passed, confidence,
    failure_categories, applied_at,
    post_apply_json, error
) VALUES
(
    'e1b2c3d4-0002-0002-0002-000000000002',
    'aegisDB.default.public.orders',
    'orders',
    'applied',
    'UPDATE "orders" SET amount = ABS(amount) WHERE amount < 0;',
    'UPDATE "orders" SET amount = -ABS(amount) WHERE amount > 0 AND order_id = 4;',
    1, false, true, 0.92,
    ARRAY['range_violation'],
    NOW() - INTERVAL '4 hours',
    '[]', NULL
),
(
    'e1b2c3d4-0003-0003-0003-000000000003',
    'aegisDB.default.public.customers',
    'customers',
    'rejected',
    'DELETE FROM "customers" WHERE ctid NOT IN (SELECT MIN(ctid) FROM "customers" GROUP BY email) AND email IS NOT NULL;',
    NULL,
    0, false, true, 0.88,
    ARRAY['uniqueness_violation'],
    NOW() - INTERVAL '23 hours',
    '[]', NULL
),
(
    'e1b2c3d4-0005-0005-0005-000000000005',
    'aegisDB.default.public.orders',
    'orders',
    'dry_run',
    'DELETE FROM "orders" WHERE status NOT IN (''completed'', ''pending'', ''cancelled'');',
    NULL,
    1, true, true, 0.85,
    ARRAY['null_violation'],
    NOW() - INTERVAL '6 hours',
    '[]', NULL
);

-- Seed demo profiling report
INSERT INTO _aegisdb_profiling_reports (
    report_id, connection_id, table_name, schema_name,
    row_count, anomaly_count, null_violations,
    uniqueness_violations, range_violations,
    format_violations, referential_violations,
    anomalies, profiled_at
) VALUES (
    'rpt-0001-0001-0001-000000000001',
    'conn-0001-0001-0001-000000000001',
    'orders',
    'public',
    6, 3, 1, 0, 1, 0, 1,
    '[
        {"column":"customer_id","type":"null_violation","severity":"high","detail":"2 NULL values found","count":2},
        {"column":"amount","type":"range_violation","severity":"medium","detail":"1 negative value found","count":1},
        {"column":"customer_id","type":"referential_integrity","severity":"high","detail":"1 FK violation found","count":1}
    ]'::jsonb,
    NOW() - INTERVAL '3 hours'
);