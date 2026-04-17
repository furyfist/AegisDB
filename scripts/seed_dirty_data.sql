-- aegisdb/scripts/seed_dirty_data.sql
-- Intentionally dirty data to trigger OpenMetadata test failures

CREATE TABLE IF NOT EXISTS orders (
    order_id    SERIAL PRIMARY KEY,
    customer_id INT,                          -- will have NULLs (violation)
    amount      NUMERIC(10, 2),               -- will have negatives (violation)
    status      VARCHAR(50),                  -- will have invalid values
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    email       VARCHAR(255),                 -- will have duplicates (violation)
    age         INT,                          -- will have out-of-range values
    country     VARCHAR(100)
);

-- Clean rows
INSERT INTO customers (email, age, country) VALUES
    ('alice@example.com', 28, 'India'),
    ('bob@example.com', 34, 'India'),
    ('carol@example.com', 22, 'India');

-- Dirty rows - these will trigger OM test failures
INSERT INTO customers (email, age, country) VALUES
    ('alice@example.com', -5, NULL),          -- duplicate email + invalid age
    (NULL, 200, 'India'),                     -- null email + impossible age
    ('dave@example.com', 17, 'India');

INSERT INTO orders (customer_id, amount, status) VALUES
    (1, 250.00, 'completed'),
    (2, 180.50, 'pending'),
    (NULL, 99.99, 'completed'),               -- null FK violation
    (1, -500.00, 'completed'),               -- negative amount
    (99, 300.00, 'INVALID_STATUS'),           -- bad FK + invalid enum
    (3, 0.00, NULL);                          -- null status