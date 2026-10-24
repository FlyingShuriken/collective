alter table collective.sku_mappings
  add column source_id uuid,
  add column source_unit text check (source_unit in ('piece','carton','box','pack')),
  add column match_snapshot jsonb not null default '{}' check (jsonb_typeof(match_snapshot)='object'),
  add foreign key (business_id,source_id) references collective.sources(business_id,id);
create index mapping_source on collective.sku_mappings(business_id,source_id);
