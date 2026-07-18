-- Schema-v3 compatibility delta captured from commit 94eb333.
-- Apply after routing_schema_v2_ca77600.sql to reconstruct the legacy v3
-- digest columns and guards used by the v3-to-v4 migration tests.

ALTER TABLE execution_attempts ADD COLUMN inventory_digest TEXT;
ALTER TABLE staged_execution_receipts ADD COLUMN inventory_digest TEXT;

CREATE TRIGGER execution_attempt_digest_insert_guard
BEFORE INSERT ON execution_attempts
WHEN NEW.inventory_digest IS NULL
  OR length(NEW.inventory_digest) != 64
  OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'inventory_digest_invalid');
END;

CREATE TRIGGER execution_attempt_digest_update_guard
BEFORE UPDATE ON execution_attempts
WHEN OLD.inventory_digest IS NULL
  OR NEW.inventory_digest IS NULL
  OR length(NEW.inventory_digest) != 64
  OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'inventory_digest_invalid');
END;

CREATE TRIGGER execution_attempt_digest_delete_guard
BEFORE DELETE ON execution_attempts
WHEN OLD.inventory_digest IS NULL
BEGIN
    SELECT RAISE(ABORT, 'inventory_digest_invalid');
END;

CREATE TRIGGER staged_receipt_digest_insert_guard
BEFORE INSERT ON staged_execution_receipts
WHEN NEW.inventory_digest IS NULL
  OR length(NEW.inventory_digest) != 64
  OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'inventory_digest_invalid');
END;

CREATE TRIGGER staged_receipt_digest_update_guard
BEFORE UPDATE ON staged_execution_receipts
WHEN OLD.inventory_digest IS NULL
  OR NEW.inventory_digest IS NULL
  OR length(NEW.inventory_digest) != 64
  OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'inventory_digest_invalid');
END;

CREATE TRIGGER staged_receipt_digest_delete_guard
BEFORE DELETE ON staged_execution_receipts
WHEN OLD.inventory_digest IS NULL
BEGIN
    SELECT RAISE(ABORT, 'inventory_digest_invalid');
END;

UPDATE schema_meta SET value = '3' WHERE key = 'schema_version';
