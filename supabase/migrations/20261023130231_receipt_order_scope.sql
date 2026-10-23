alter table collective.purchase_orders
  add constraint order_receipt_spec unique (business_id,id,spec_id,spec_revision);
alter table collective.stock_observations add column po_id uuid;
alter table collective.stock_observations add constraint receipt_order_scope
  foreign key (business_id,po_id,spec_id,spec_revision)
  references collective.purchase_orders (business_id,id,spec_id,spec_revision);
alter table collective.stock_observations add constraint receipt_order_kind
  check (po_id is null or kind='receipt');
create index receipt_order on collective.stock_observations (business_id,po_id) where po_id is not null;
