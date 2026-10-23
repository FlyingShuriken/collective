create schema collective;
create schema collective_private;
revoke all on schema collective, collective_private from public, anon, authenticated;

do $$
begin
  if not exists (select from pg_roles where rolname = 'collective_tenant') then
    create role collective_tenant nologin noinherit nobypassrls;
  end if;
  if not exists (select from pg_roles where rolname = 'collective_coordinator') then
    create role collective_coordinator nologin noinherit nobypassrls;
  end if;
  if not exists (select from pg_roles where rolname = 'collective_backend') then
    create role collective_backend nologin noinherit nobypassrls;
  end if;
end $$;
grant collective_tenant, collective_coordinator to collective_backend;
grant collective_tenant, collective_coordinator to postgres with inherit false, set true;
grant usage on schema collective, collective_private to collective_tenant, collective_coordinator;
alter default privileges in schema collective revoke all on tables from public, anon, authenticated;
alter default privileges in schema collective_private revoke execute on functions from public;

create table collective.networks (
  id uuid primary key,
  name text not null,
  created_at timestamptz not null default now()
);
create table collective.businesses (
  id uuid primary key,
  network_id uuid references collective.networks,
  name text not null,
  zone text not null,
  locale text not null check (locale in ('en', 'ms', 'zh')),
  staff_limit numeric(18,2) not null default 0 check (staff_limit >= 0),
  staff_can_view_prices boolean not null default false,
  pool_opt_in boolean not null default false,
  created_at timestamptz not null default now()
);
create table collective.business_memberships (
  business_id uuid not null references collective.businesses,
  user_id uuid not null references auth.users,
  role text not null check (role in ('owner', 'staff')),
  primary key (business_id, user_id)
);
create index membership_user_business on collective.business_memberships (user_id, business_id);
create index business_network on collective.businesses (network_id);
create table collective.network_admins (
  network_id uuid not null references collective.networks,
  user_id uuid not null references auth.users,
  primary key (network_id, user_id)
);
create index network_admin_user on collective.network_admins (user_id, network_id);

-- Auth is owned by Supabase; expose only the caller's claim-derived UUID to these roles.
create function collective_private.subject_id() returns uuid
language sql stable security definer set search_path = '' as $$
  select auth.uid();
$$;
revoke all on function collective_private.subject_id() from public, anon, authenticated;
grant execute on function collective_private.subject_id()
  to collective_tenant, collective_coordinator;

create function collective_private.is_member(target uuid) returns boolean
language sql stable security invoker set search_path = '' as $$
  select exists (select 1 from collective.business_memberships
                 where business_id = target and user_id = (select collective_private.subject_id()));
$$;
create function collective_private.is_owner(target uuid) returns boolean
language sql stable security invoker set search_path = '' as $$
  select exists (select 1 from collective.business_memberships
                 where business_id = target and user_id = (select collective_private.subject_id()) and role = 'owner');
$$;
revoke all on function collective_private.is_member(uuid), collective_private.is_owner(uuid)
  from public, anon, authenticated;
grant execute on function collective_private.is_member(uuid), collective_private.is_owner(uuid)
  to collective_tenant, collective_coordinator;

create table collective.suppliers (
  id uuid primary key,
  name text not null,
  created_at timestamptz not null default now()
);
create table collective.business_suppliers (
  business_id uuid not null references collective.businesses,
  supplier_id uuid not null references collective.suppliers,
  contact text,
  account_reference text,
  credit_days integer check (credit_days >= 0),
  primary key (business_id, supplier_id)
);
create index business_supplier_supplier on collective.business_suppliers (supplier_id);
create table collective.canonical_specs (
  id uuid primary key,
  name text not null,
  created_at timestamptz not null default now()
);
create table collective.spec_revisions (
  spec_id uuid not null references collective.canonical_specs,
  revision integer not null check (revision > 0),
  base_unit text not null check (base_unit in ('piece', 'ml', 'g')),
  pack_multiple numeric(18,6) not null check (pack_multiple > 0),
  perishable boolean not null,
  attributes jsonb not null check (jsonb_typeof(attributes) = 'object'),
  primary key (spec_id, revision),
  check (base_unit <> 'piece' or pack_multiple = trunc(pack_multiple))
);
create table collective.events (
  id uuid primary key,
  business_id uuid not null references collective.businesses,
  actor_id uuid references auth.users,
  actor_kind text not null check (actor_kind in ('owner', 'staff', 'system')),
  event_type text not null,
  entity_id uuid not null,
  entity_revision integer check (entity_revision > 0),
  correlation_id uuid not null,
  summary text not null check (length(summary) between 1 and 500),
  restricted_details_reference uuid,
  occurred_at timestamptz not null default now(),
  check (actor_kind = 'system' or actor_id is not null)
);
create index event_business_cursor on collective.events (business_id, occurred_at, id);
create index event_actor on collective.events (actor_id) where actor_id is not null;
create table collective.mutation_keys (
  principal_id uuid not null,
  business_id uuid not null references collective.businesses,
  route text not null,
  key text not null check (length(key) between 1 and 200),
  request_hash text not null check (request_hash ~ '^[a-f0-9]{64}$'),
  response jsonb not null,
  created_at timestamptz not null default now(),
  primary key (principal_id, business_id, route, key)
);
create index mutation_business on collective.mutation_keys (business_id);

create function collective_private.immutable_record() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  raise exception 'Immutable record: %', tg_table_name using errcode = '23514';
end $$;
revoke all on function collective_private.immutable_record() from public, anon, authenticated;
create trigger event_immutable before update or delete on collective.events
  for each row execute function collective_private.immutable_record();
create trigger spec_revision_immutable before update or delete on collective.spec_revisions
  for each row execute function collective_private.immutable_record();

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'networks', 'businesses', 'business_memberships', 'network_admins', 'suppliers',
    'business_suppliers', 'canonical_specs', 'spec_revisions', 'events', 'mutation_keys'
  ] loop
    execute format('alter table collective.%I enable row level security', table_name);
    execute format('alter table collective.%I force row level security', table_name);
    execute format('create policy coordinator_read on collective.%I for select to collective_coordinator using (true)', table_name);
    execute format('grant select on collective.%I to collective_coordinator, collective_tenant', table_name);
  end loop;
end $$;
create policy membership_own on collective.business_memberships
  for select to collective_tenant using (user_id = (select collective_private.subject_id()));
create policy admin_own on collective.network_admins
  for select to collective_tenant using (user_id = (select collective_private.subject_id()));
create policy business_read on collective.businesses
  for select to collective_tenant using (collective_private.is_member(id));
create policy business_edit on collective.businesses
  for update to collective_tenant using (collective_private.is_owner(id))
  with check (collective_private.is_owner(id));
grant update (name, locale, staff_limit, staff_can_view_prices, pool_opt_in)
  on collective.businesses to collective_tenant;
create policy network_read on collective.networks for select to collective_tenant using (
  exists (select 1 from collective.businesses where network_id = networks.id)
  or exists (select 1 from collective.network_admins where network_id = networks.id)
);
create policy supplier_read on collective.suppliers for select to collective_tenant using (true);
create policy spec_read on collective.canonical_specs for select to collective_tenant using (true);
create policy spec_revision_read on collective.spec_revisions
  for select to collective_tenant using (true);
create policy supplier_account_read on collective.business_suppliers for select to collective_tenant
  using (collective_private.is_member(business_id));
create policy supplier_account_write on collective.business_suppliers for insert to collective_tenant
  with check (collective_private.is_owner(business_id));
create policy supplier_account_edit on collective.business_suppliers for update to collective_tenant
  using (collective_private.is_owner(business_id))
  with check (collective_private.is_owner(business_id));
grant insert, update on collective.business_suppliers to collective_tenant;
create policy event_read on collective.events for select to collective_tenant
  using (collective_private.is_member(business_id));
create policy event_insert on collective.events for insert to collective_tenant
  with check (collective_private.is_member(business_id) and actor_id = (select collective_private.subject_id())
    and (actor_kind = 'staff' or (actor_kind = 'owner' and collective_private.is_owner(business_id))));
create policy event_system_insert on collective.events for insert to collective_coordinator
  with check (true);
grant insert on collective.events to collective_tenant, collective_coordinator;
create policy mutation_read on collective.mutation_keys for select to collective_tenant
  using (principal_id = (select collective_private.subject_id()) and collective_private.is_member(business_id));
create policy mutation_insert on collective.mutation_keys for insert to collective_tenant
  with check (principal_id = (select collective_private.subject_id()) and collective_private.is_member(business_id));
create policy mutation_system_insert on collective.mutation_keys for insert to collective_coordinator
  with check (true);
grant insert on collective.mutation_keys to collective_tenant, collective_coordinator;
