create function collective_private.can_confirm(target uuid, amount numeric) returns boolean
language sql stable security invoker set search_path = '' as $$
  select collective_private.is_owner(target) or (
    amount is not null and amount >= 0 and exists (
      select 1 from collective.business_memberships m
      join collective.businesses b on b.id=m.business_id
      where m.business_id=target and m.user_id=collective_private.subject_id()
        and m.role='staff' and amount <= b.staff_limit
    )
  );
$$;
revoke all on function collective_private.can_confirm(uuid,numeric) from public,anon,authenticated;
grant execute on function collective_private.can_confirm(uuid,numeric) to collective_tenant;

create table collective.sku_mappings (
  business_id uuid not null references collective.businesses,
  id uuid not null,
  revision integer not null check (revision > 0),
  supplier_id uuid not null references collective.suppliers,
  supplier_sku text not null,
  source_name text not null,
  spec_id uuid,
  spec_revision integer,
  pack_factors jsonb not null default '[]' check (jsonb_typeof(pack_factors)='array'),
  confirmed boolean not null default false,
  confirmed_by uuid references auth.users,
  confirmed_at timestamptz,
  provenance text not null check (provenance in ('synthetic','human','agent')),
  created_at timestamptz not null default now(),
  primary key (business_id,id,revision),
  unique (business_id,supplier_id,supplier_sku,revision),
  foreign key (spec_id,spec_revision) references collective.spec_revisions,
  check ((spec_id is null) = (spec_revision is null)),
  check (not confirmed or (spec_id is not null and
    (provenance='synthetic' or (confirmed_by is not null and confirmed_at is not null))))
);
create index mapping_supplier on collective.sku_mappings (supplier_id);
create index mapping_spec on collective.sku_mappings (spec_id,spec_revision);
create index mapping_confirmer on collective.sku_mappings (confirmed_by) where confirmed_by is not null;

create table collective.sources (
  id uuid primary key,
  business_id uuid not null references collective.businesses,
  kind text not null check (kind in ('image','pdf','text','supplier_reply')),
  status text not null check (status in ('uploading','available','failed')),
  sha256 text check (sha256 ~ '^[a-f0-9]{64}$'),
  original_object_key text unique,
  provider_object_key text unique,
  text_content text,
  mime_type text,
  size_bytes integer check (size_bytes between 1 and 10485760),
  page_count integer check (page_count between 1 and 10),
  price_bearing boolean not null default true,
  uploaded_by uuid references auth.users,
  provenance text not null check (provenance in ('synthetic','human')),
  created_at timestamptz not null default now(),
  unique (business_id,id),
  unique (business_id,sha256),
  check (provenance='synthetic' or uploaded_by is not null),
  check (status <> 'available' or (sha256 is not null and
    (original_object_key is not null or text_content is not null)))
);
create index source_business_cursor on collective.sources (business_id,created_at,id);
create index source_uploader on collective.sources (uploaded_by) where uploaded_by is not null;
create function collective_private.preserve_source() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.status='available' and (
      (to_jsonb(new)-'provider_object_key') <> (to_jsonb(old)-'provider_object_key')
      or (old.provider_object_key is not null and new.provider_object_key is distinct from old.provider_object_key)) then
    raise exception 'Available source is immutable' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.preserve_source() from public,anon,authenticated;
create trigger source_immutable before update on collective.sources
  for each row execute function collective_private.preserve_source();
create table collective.source_pages (
  business_id uuid not null,
  source_id uuid not null,
  page integer not null check (page between 1 and 10),
  object_key text not null unique,
  provider_object_key text unique,
  width integer not null check (width > 0),
  height integer not null check (height > 0),
  transformations jsonb not null default '{}',
  primary key (business_id,source_id,page),
  foreign key (business_id,source_id) references collective.sources (business_id,id)
);

create table collective.proposals (
  id uuid primary key,
  business_id uuid not null,
  source_id uuid not null,
  kind text not null check (kind in ('purchase','stock','quote','mapping')),
  current_revision integer not null check (current_revision > 0),
  status text not null default 'pending_review' check (status in ('pending_review','confirmed','rejected')),
  created_at timestamptz not null default now(),
  unique (business_id,id),
  foreign key (business_id,source_id) references collective.sources (business_id,id)
);
create index proposal_business_cursor on collective.proposals (business_id,created_at,id);
create index proposal_source on collective.proposals (business_id,source_id);
create table collective.proposal_revisions (
  business_id uuid not null,
  proposal_id uuid not null,
  revision integer not null check (revision > 0),
  payload jsonb not null check (jsonb_typeof(payload)='object'),
  evidence jsonb not null default '[]' check (jsonb_typeof(evidence)='array'),
  flags jsonb not null default '[]' check (jsonb_typeof(flags)='array'),
  agent text,
  model text,
  prompt_version text,
  schema_version text not null,
  edited_by uuid references auth.users,
  created_at timestamptz not null default now(),
  primary key (business_id,proposal_id,revision),
  foreign key (business_id,proposal_id) references collective.proposals (business_id,id)
);
create index proposal_editor on collective.proposal_revisions (edited_by) where edited_by is not null;
alter table collective.proposals add constraint proposal_current_revision
  foreign key (business_id,id,current_revision)
  references collective.proposal_revisions (business_id,proposal_id,revision)
  deferrable initially deferred;

create table collective.purchases (
  business_id uuid not null references collective.businesses,
  id uuid not null,
  revision integer not null check (revision > 0),
  supplier_id uuid not null references collective.suppliers,
  source_id uuid,
  proposal_id uuid,
  proposal_revision integer,
  document_number text,
  document_date date,
  currency text,
  purpose text not null check (purpose in ('historical','delivered')),
  goods_total numeric(18,2) check (goods_total >= 0),
  delivery_fee numeric(18,2) check (delivery_fee >= 0),
  tax_rate numeric(18,6) check (tax_rate between 0 and 1),
  discount numeric(18,2) check (discount >= 0),
  surcharge numeric(18,2) check (surcharge >= 0),
  total numeric(18,2) check (total >= 0),
  credit_days integer check (credit_days >= 0),
  delivery_mode text check (delivery_mode in ('per_business','collection_point')),
  missing_fields jsonb not null default '{}',
  fees jsonb not null default '[]' check (jsonb_typeof(fees)='array'),
  confirmed_by uuid references auth.users,
  provenance text not null check (provenance in ('synthetic','human')),
  sealed boolean not null default false,
  created_at timestamptz not null default now(),
  primary key (business_id,id,revision),
  unique (business_id,proposal_id,proposal_revision),
  unique (business_id,source_id,proposal_revision),
  foreign key (business_id,source_id) references collective.sources (business_id,id),
  foreign key (business_id,proposal_id,proposal_revision)
    references collective.proposal_revisions (business_id,proposal_id,revision),
  check (provenance='synthetic' or
    (source_id is not null and proposal_id is not null and proposal_revision is not null and confirmed_by is not null))
);
create index purchase_business_cursor on collective.purchases (business_id,created_at,id);
create index purchase_supplier on collective.purchases (supplier_id);
create index purchase_confirmer on collective.purchases (confirmed_by) where confirmed_by is not null;
create table collective.purchase_lines (
  business_id uuid not null,
  purchase_id uuid not null,
  purchase_revision integer not null,
  line integer not null check (line >= 0),
  source_name text not null,
  supplier_sku text,
  original_quantity numeric(18,6) check (original_quantity > 0),
  original_unit text,
  original_unit_price numeric(18,6) check (original_unit_price >= 0),
  quantity numeric(18,6) check (quantity > 0),
  base_unit text check (base_unit in ('piece','ml','g')),
  unit_price numeric(18,6) check (unit_price >= 0),
  line_total numeric(18,2) check (line_total >= 0),
  spec_id uuid,
  spec_revision integer,
  mapping_id uuid,
  mapping_revision integer,
  primary key (business_id,purchase_id,purchase_revision,line),
  foreign key (business_id,purchase_id,purchase_revision) references collective.purchases,
  foreign key (business_id,mapping_id,mapping_revision) references collective.sku_mappings,
  foreign key (spec_id,spec_revision) references collective.spec_revisions,
  check ((spec_id is null) = (spec_revision is null)),
  check ((mapping_id is null) = (mapping_revision is null)),
  check (base_unit <> 'piece' or quantity = trunc(quantity))
);
create index purchase_line_mapping on collective.purchase_lines (business_id,mapping_id,mapping_revision);
create index purchase_line_spec on collective.purchase_lines (spec_id,spec_revision);

create table collective.stock_observations (
  id uuid primary key,
  business_id uuid not null references collective.businesses,
  spec_id uuid not null,
  spec_revision integer not null,
  quantity numeric(18,6) not null check (quantity >= 0),
  observed_at timestamptz not null,
  kind text not null default 'stock' check (kind in ('stock','receipt','inbound_adjustment','waste')),
  review_value numeric(18,2) check (review_value >= 0),
  source_id uuid,
  proposal_id uuid,
  proposal_revision integer,
  confirmed_by uuid references auth.users,
  provenance text not null check (provenance in ('synthetic','human')),
  created_at timestamptz not null default now(),
  unique (business_id,id),
  unique (business_id,proposal_id,proposal_revision,spec_id),
  foreign key (spec_id,spec_revision) references collective.spec_revisions,
  foreign key (business_id,source_id) references collective.sources (business_id,id),
  foreign key (business_id,proposal_id,proposal_revision) references collective.proposal_revisions,
  check (provenance='synthetic' or
    (source_id is not null and proposal_id is not null and proposal_revision is not null and confirmed_by is not null))
);
create index stock_business_spec_time on collective.stock_observations (business_id,spec_id,observed_at,id);
create index stock_source on collective.stock_observations (business_id,source_id);
create index stock_confirmer on collective.stock_observations (confirmed_by) where confirmed_by is not null;

create table collective.quote_revisions (
  business_id uuid not null references collective.businesses,
  id uuid not null,
  revision integer not null check (revision > 0),
  supplier_id uuid not null references collective.suppliers,
  spec_id uuid not null,
  spec_revision integer not null,
  source_id uuid,
  proposal_id uuid,
  proposal_revision integer,
  currency text,
  base_unit text not null check (base_unit in ('piece','ml','g')),
  pack_multiple numeric(18,6) not null check (pack_multiple > 0),
  zone text not null,
  offer_family uuid not null,
  pooled_buyers_permitted boolean,
  tier_kind text not null check (tier_kind in ('all_unit','incremental')),
  per_business_moq numeric(18,6) check (per_business_moq > 0),
  aggregate_moq numeric(18,6) check (aggregate_moq > 0),
  delivery_mode text check (delivery_mode in ('per_business','collection_point')),
  delivery_location text,
  delivery_fee numeric(18,2) check (delivery_fee >= 0),
  free_delivery_threshold numeric(18,2) check (free_delivery_threshold >= 0),
  waive_split_drop_fees boolean,
  credit_days integer check (credit_days >= 0),
  tax_rate numeric(18,6) check (tax_rate between 0 and 1),
  discount numeric(18,2) check (discount >= 0),
  surcharge numeric(18,2) check (surcharge >= 0),
  conditions text,
  conditions_complete boolean not null default false,
  valid_from timestamptz,
  valid_until timestamptz,
  arrival_start timestamptz,
  arrival_end timestamptz,
  review_value numeric(18,2) check (review_value >= 0),
  confirmed_by uuid references auth.users,
  provenance text not null check (provenance in ('synthetic','human')),
  sealed boolean not null default false,
  created_at timestamptz not null default now(),
  primary key (business_id,id,revision),
  unique (business_id,proposal_id,proposal_revision,spec_id),
  foreign key (spec_id,spec_revision) references collective.spec_revisions,
  foreign key (business_id,source_id) references collective.sources (business_id,id),
  foreign key (business_id,proposal_id,proposal_revision) references collective.proposal_revisions,
  check (valid_from < valid_until),
  check (arrival_start < arrival_end),
  check (provenance='synthetic' or
    (source_id is not null and proposal_id is not null and proposal_revision is not null and confirmed_by is not null))
);
create index quote_context on collective.quote_revisions (business_id,supplier_id,spec_id,valid_until);
create index quote_spec on collective.quote_revisions (spec_id,spec_revision);
create index quote_source on collective.quote_revisions (business_id,source_id);
create index quote_confirmer on collective.quote_revisions (confirmed_by) where confirmed_by is not null;
create table collective.quote_tiers (
  business_id uuid not null,
  quote_id uuid not null,
  quote_revision integer not null,
  min_quantity numeric(18,6) not null check (min_quantity > 0),
  unit_price numeric(18,6) not null check (unit_price >= 0),
  primary key (business_id,quote_id,quote_revision,min_quantity),
  foreign key (business_id,quote_id,quote_revision) references collective.quote_revisions
);

-- Sealing prevents a later INSERT from changing an already accepted line/tier set.
create function collective_private.seal_record() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if tg_op='UPDATE' and not old.sealed and new.sealed
      and (to_jsonb(new)-'sealed') = (to_jsonb(old)-'sealed') then
    return new;
  end if;
  raise exception 'Immutable accepted record: %',tg_table_name using errcode='23514';
end $$;
revoke all on function collective_private.seal_record() from public,anon,authenticated;
create function collective_private.open_parent() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare parent_sealed boolean;
begin
  if tg_table_name='quote_tiers' then
    select sealed into parent_sealed from collective.quote_revisions
      where business_id=new.business_id and id=new.quote_id and revision=new.quote_revision for share;
  else
    select sealed into parent_sealed from collective.purchases
      where business_id=new.business_id and id=new.purchase_id and revision=new.purchase_revision for share;
  end if;
  if parent_sealed then
    raise exception 'Accepted parent is sealed' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.open_parent() from public,anon,authenticated;
create trigger purchase_seal before update or delete on collective.purchases
  for each row execute function collective_private.seal_record();
create trigger quote_seal before update or delete on collective.quote_revisions
  for each row execute function collective_private.seal_record();
create trigger purchase_line_open before insert on collective.purchase_lines
  for each row execute function collective_private.open_parent();
create trigger quote_tier_open before insert on collective.quote_tiers
  for each row execute function collective_private.open_parent();

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'sku_mappings','sources','source_pages','proposals','proposal_revisions',
    'purchases','purchase_lines','stock_observations','quote_revisions','quote_tiers'
  ] loop
    execute format('alter table collective.%I enable row level security',table_name);
    execute format('alter table collective.%I force row level security',table_name);
    execute format('grant select on collective.%I to collective_tenant,collective_coordinator',table_name);
    execute format('create policy tenant_read on collective.%I for select to collective_tenant using (collective_private.is_member(business_id))',table_name);
    execute format('create policy coordinator_read on collective.%I for select to collective_coordinator using (true)',table_name);
  end loop;
  foreach table_name in array array['sku_mappings','source_pages','proposal_revisions',
                                    'purchase_lines','stock_observations','quote_tiers'] loop
    execute format('create trigger immutable before update or delete on collective.%I for each row execute function collective_private.immutable_record()',table_name);
  end loop;
  foreach table_name in array array['sources','source_pages','proposals','proposal_revisions'] loop
    execute format('grant insert on collective.%I to collective_tenant,collective_coordinator',table_name);
    execute format('create policy tenant_insert on collective.%I for insert to collective_tenant with check (collective_private.is_member(business_id))',table_name);
    execute format('create policy coordinator_insert on collective.%I for insert to collective_coordinator with check (true)',table_name);
  end loop;
end $$;
drop policy coordinator_insert on collective.proposals;
create policy coordinator_insert on collective.proposals for insert to collective_coordinator
  with check (status='pending_review');
drop policy coordinator_insert on collective.proposal_revisions;
create policy coordinator_insert on collective.proposal_revisions for insert to collective_coordinator
  with check (edited_by is null);
drop policy tenant_insert on collective.proposal_revisions;
create policy tenant_insert on collective.proposal_revisions for insert to collective_tenant
  with check (collective_private.is_member(business_id) and edited_by=collective_private.subject_id());
grant update (status,provider_object_key) on collective.sources to collective_coordinator;
grant update (status,original_object_key,sha256,size_bytes,page_count,mime_type) on collective.sources to collective_tenant;
create policy source_edit on collective.sources for update to collective_tenant
  using (collective_private.is_member(business_id)) with check (collective_private.is_member(business_id));
create policy source_worker_edit on collective.sources for update to collective_coordinator using (true) with check (true);
grant update (current_revision,status) on collective.proposals to collective_tenant;
grant update (current_revision) on collective.proposals to collective_coordinator;
create policy proposal_edit on collective.proposals for update to collective_tenant
  using (collective_private.is_member(business_id) and status='pending_review')
  with check (collective_private.is_member(business_id));
create policy proposal_worker_edit on collective.proposals for update to collective_coordinator
  using (status='pending_review') with check (status='pending_review');
create policy mapping_insert on collective.sku_mappings for insert to collective_tenant
  with check (collective_private.is_owner(business_id) and confirmed_by=collective_private.subject_id() and provenance='human');
grant insert on collective.sku_mappings to collective_tenant;
create policy mapping_proposal on collective.sku_mappings for insert to collective_coordinator
  with check (not confirmed and confirmed_by is null and provenance='agent');
grant insert on collective.sku_mappings to collective_coordinator;
create policy purchase_confirm on collective.purchases for insert to collective_tenant
  with check (collective_private.can_confirm(business_id,total) and confirmed_by=collective_private.subject_id() and provenance='human');
create policy purchase_seal on collective.purchases for update to collective_tenant
  using (collective_private.is_member(business_id) and confirmed_by=collective_private.subject_id())
  with check (collective_private.can_confirm(business_id,total));
grant insert on collective.purchases to collective_tenant;
grant update (sealed) on collective.purchases to collective_tenant;
create policy purchase_line_insert on collective.purchase_lines for insert to collective_tenant
  with check (exists (select 1 from collective.purchases p where p.business_id=purchase_lines.business_id
    and p.id=purchase_id and p.revision=purchase_revision and not p.sealed
    and p.confirmed_by=collective_private.subject_id()));
grant insert on collective.purchase_lines to collective_tenant;
create policy stock_confirm on collective.stock_observations for insert to collective_tenant
  with check (collective_private.can_confirm(business_id,review_value) and confirmed_by=collective_private.subject_id() and provenance='human');
grant insert on collective.stock_observations to collective_tenant;
create policy quote_confirm on collective.quote_revisions for insert to collective_tenant
  with check (collective_private.can_confirm(business_id,review_value) and confirmed_by=collective_private.subject_id() and provenance='human');
create policy quote_seal on collective.quote_revisions for update to collective_tenant
  using (collective_private.is_member(business_id) and confirmed_by=collective_private.subject_id())
  with check (collective_private.can_confirm(business_id,review_value));
grant insert on collective.quote_revisions to collective_tenant;
grant update (sealed) on collective.quote_revisions to collective_tenant;
create policy quote_tier_insert on collective.quote_tiers for insert to collective_tenant
  with check (exists (select 1 from collective.quote_revisions q where q.business_id=quote_tiers.business_id
    and q.id=quote_id and q.revision=quote_revision and not q.sealed
    and q.confirmed_by=collective_private.subject_id()));
grant insert on collective.quote_tiers to collective_tenant;
