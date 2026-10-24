drop index collective.invoice_check_recovery_source;
create unique index invoice_check_recovery_source on collective.invoice_checks
  (business_id,recovery_source_id) where recovery_source_id is not null;
