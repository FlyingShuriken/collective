create function collective_private.preserve_revision() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if tg_op='INSERT' then
    if new.revision <> 1 or new.history <> '{}'::jsonb then
      raise exception 'Initial workflow revision must be 1' using errcode='23514';
    end if;
  else
    if new.revision <> old.revision+1 then
      raise exception 'Workflow revision must advance once' using errcode='23514';
    end if;
    new.history := old.history || jsonb_build_object(old.revision::text,to_jsonb(old)-'history');
  end if;
  return new;
end $$;
revoke all on function collective_private.preserve_revision() from public,anon,authenticated;

create table collective.reorder_needs (
  id uuid primary key,
  business_id uuid not null references collective.businesses,
  revision integer not null default 1 check (revision > 0),
  history jsonb not null default '{}',
  spec_id uuid not null,
  spec_revision integer not null,
  mapping_id uuid,
  mapping_revision integer,
  quantity numeric(18,6) not null check (quantity > 0),
  base_unit text not null check (base_unit in ('piece','ml','g')),
  pack_multiple numeric(18,6) not null check (pack_multiple > 0),
  zone text not null,
  saving_threshold numeric(18,6) not null check (saving_threshold between 0 and 1),
  min_credit_days integer not null check (min_credit_days >= 0),
  delivery_mode text not null check (delivery_mode in ('per_business','collection_point')),
  delivery_location text,
  collection_share numeric(18,2) check (collection_share >= 0),
  order_start timestamptz not null,
  order_end timestamptz not null,
  arrival_start timestamptz not null,
  arrival_end timestamptz not null,
  state text not null check (state in ('suggested','selected','reserved','released','stale')),
  selected_by uuid references auth.users,
  reserved_pool_id uuid,
  stock_snapshot jsonb,
  usage_input jsonb,
  lead_days numeric(18,6) check (lead_days >= 0),
  buffer_days numeric(18,6) check (buffer_days >= 0),
  max_stock numeric(18,6) check (max_stock > 0),
  shelf_life_days numeric(18,6) check (shelf_life_days > 0),
  provenance text not null check (provenance in ('synthetic','human','system')),
  created_at timestamptz not null default now(),
  unique (business_id,id),
  foreign key (spec_id,spec_revision) references collective.spec_revisions,
  foreign key (business_id,mapping_id,mapping_revision) references collective.sku_mappings,
  check ((mapping_id is null) = (mapping_revision is null)),
  check (quantity % pack_multiple = 0),
  check (base_unit <> 'piece' or quantity=trunc(quantity)),
  check (order_start < order_end and arrival_start < arrival_end),
  check (state not in ('selected','reserved','released') or selected_by is not null or provenance='synthetic')
);
create index need_business_cursor on collective.reorder_needs (business_id,created_at,id);
create index need_spec_zone_window on collective.reorder_needs (spec_id,zone,order_end) where state in ('selected','reserved');
create index need_mapping on collective.reorder_needs (business_id,mapping_id,mapping_revision);
create index need_selector on collective.reorder_needs (selected_by) where selected_by is not null;

create table collective.learned_rules (
  id uuid primary key,
  revision integer not null default 1 check (revision > 0),
  history jsonb not null default '{}',
  business_id uuid references collective.businesses,
  network_id uuid references collective.networks,
  scope text not null check (scope in ('private','network')),
  kind text not null check (kind in ('usage','observed_fee','quote_accuracy')),
  context_key text not null,
  evidence_digest text not null,
  proposed_value jsonb not null check (jsonb_typeof(proposed_value)='object'),
  sample_count integer not null check (sample_count >= 0),
  distinct_businesses integer not null check (distinct_businesses >= 0),
  state text not null default 'proposed' check (state in ('proposed','accepted','rejected','revoked','superseded')),
  accepted_by uuid references auth.users,
  accepted_at timestamptz,
  created_at timestamptz not null default now(),
  check ((scope='private' and business_id is not null and network_id is null)
      or (scope='network' and network_id is not null and business_id is null)),
  check (state <> 'accepted' or (accepted_by is not null and accepted_at is not null)),
  check (state <> 'accepted' or scope <> 'network' or distinct_businesses >= 5),
  check (state <> 'accepted' or kind <> 'observed_fee' or sample_count >= 3)
);
create unique index private_rule_digest on collective.learned_rules (business_id,kind,context_key,evidence_digest) where scope='private';
create unique index network_rule_digest on collective.learned_rules (network_id,kind,context_key,evidence_digest) where scope='network';
create unique index private_rule_active on collective.learned_rules (business_id,kind,context_key) where state='accepted';
create unique index network_rule_active on collective.learned_rules (network_id,kind,context_key) where state='accepted';
create index rule_acceptor on collective.learned_rules (accepted_by) where accepted_by is not null;
create table collective.rule_evidence (
  rule_id uuid not null references collective.learned_rules,
  business_id uuid not null references collective.businesses,
  source_id uuid not null,
  purchase_id uuid,
  purchase_revision integer,
  primary key (rule_id,business_id,source_id),
  foreign key (business_id,source_id) references collective.sources (business_id,id),
  foreign key (business_id,purchase_id,purchase_revision) references collective.purchases
);
create index rule_evidence_source on collective.rule_evidence (business_id,source_id);
create index rule_evidence_purchase on collective.rule_evidence (business_id,purchase_id,purchase_revision);

create table collective.pools (
  id uuid primary key,
  supplier_id uuid not null references collective.suppliers,
  spec_id uuid not null,
  spec_revision integer not null,
  offer_family uuid not null,
  delivery_mode text not null check (delivery_mode in ('per_business','collection_point')),
  deadline timestamptz not null,
  state text not null check (state in ('offered','awaiting_approvals','ready','ineligible','released')),
  revision integer not null default 1 check (revision > 0),
  history jsonb not null default '{}',
  created_at timestamptz not null default now(),
  foreign key (spec_id,spec_revision) references collective.spec_revisions
);
create index pool_supplier_spec on collective.pools (supplier_id,spec_id,state);
alter table collective.reorder_needs add constraint need_pool foreign key (reserved_pool_id) references collective.pools;
create index need_pool on collective.reorder_needs (reserved_pool_id) where reserved_pool_id is not null;
create table collective.pool_members (
  pool_id uuid not null references collective.pools,
  business_id uuid not null,
  need_id uuid not null,
  state text not null check (state in ('offered','approved','awaiting_reapproval','withdrawn','expired','released')),
  created_at timestamptz not null default now(),
  primary key (pool_id,business_id),
  foreign key (business_id,need_id) references collective.reorder_needs (business_id,id)
);
create unique index need_active_reservation on collective.pool_members (need_id)
  where state in ('offered','approved','awaiting_reapproval','released');
create index member_business on collective.pool_members (business_id,pool_id);
create table collective.pool_evaluations (
  pool_id uuid not null references collective.pools,
  revision integer not null check (revision > 0),
  evaluated_at timestamptz not null,
  snapshot jsonb not null check (jsonb_typeof(snapshot)='object'),
  primary key (pool_id,revision)
);
create table collective.member_evaluations (
  pool_id uuid not null,
  business_id uuid not null,
  revision integer not null,
  snapshot jsonb not null check (jsonb_typeof(snapshot)='object'),
  primary key (pool_id,business_id,revision),
  foreign key (pool_id,business_id) references collective.pool_members,
  foreign key (pool_id,revision) references collective.pool_evaluations
);
create index member_evaluation_parent on collective.member_evaluations (pool_id,revision);
create index member_evaluation_business on collective.member_evaluations (business_id,pool_id,revision);
create table collective.bound_approvals (
  id uuid primary key,
  business_id uuid not null,
  need_id uuid not null,
  owner_id uuid not null references auth.users,
  pool_id uuid,
  terms jsonb not null check (jsonb_typeof(terms)='object'),
  cap numeric(18,2) not null check (cap >= 0),
  expires_at timestamptz not null,
  approved_at timestamptz not null,
  evaluation_revision integer not null check (evaluation_revision > 0),
  quote_id uuid not null,
  quote_revision integer not null,
  state text not null default 'valid' check (state in ('valid','void','expired','withdrawn','released')),
  latest_evaluation_revision integer,
  revision integer not null default 1 check (revision > 0),
  history jsonb not null default '{}',
  unique (business_id,id),
  foreign key (business_id,need_id) references collective.reorder_needs (business_id,id),
  foreign key (pool_id,business_id) references collective.pool_members,
  foreign key (business_id,quote_id,quote_revision) references collective.quote_revisions,
  check (expires_at > approved_at)
);
create unique index need_valid_approval on collective.bound_approvals (business_id,need_id) where state='valid';
create index approval_owner on collective.bound_approvals (owner_id);
create index approval_pool on collective.bound_approvals (pool_id,business_id);
create index approval_quote on collective.bound_approvals (business_id,quote_id,quote_revision);
create index approval_expiry on collective.bound_approvals (expires_at) where state='valid';

create table collective.releases (
  id uuid primary key,
  pool_id uuid unique,
  business_id uuid,
  need_id uuid,
  evaluation_revision integer not null check (evaluation_revision > 0),
  snapshot jsonb not null check (jsonb_typeof(snapshot)='object'),
  created_at timestamptz not null default now(),
  foreign key (pool_id,evaluation_revision) references collective.pool_evaluations,
  foreign key (business_id,need_id) references collective.reorder_needs (business_id,id),
  check ((pool_id is not null and business_id is null and need_id is null)
      or (pool_id is null and business_id is not null and need_id is not null)),
  unique (business_id,need_id)
);
create table collective.purchase_orders (
  id uuid primary key,
  business_id uuid not null,
  release_id uuid not null references collective.releases,
  need_id uuid not null,
  approval_id uuid not null unique,
  supplier_id uuid not null references collective.suppliers,
  spec_id uuid not null,
  spec_revision integer not null,
  quantity numeric(18,6) not null check (quantity > 0),
  amount numeric(18,2) not null check (amount >= 0),
  decision_amount numeric(18,2) not null check (decision_amount >= amount),
  snapshot jsonb not null check (jsonb_typeof(snapshot)='object'),
  implementation text not null default 'simulated' check (implementation='simulated'),
  issued_at timestamptz not null default now(),
  unique (business_id,id),
  unique (release_id,business_id),
  unique (business_id,need_id),
  foreign key (business_id,need_id) references collective.reorder_needs (business_id,id),
  foreign key (business_id,approval_id) references collective.bound_approvals (business_id,id),
  foreign key (spec_id,spec_revision) references collective.spec_revisions
);
create index order_business_cursor on collective.purchase_orders (business_id,issued_at,id);
create index order_supplier on collective.purchase_orders (supplier_id);
create index order_spec on collective.purchase_orders (spec_id,spec_revision);
alter table collective.purchases add column po_id uuid;
alter table collective.purchases add constraint purchase_order foreign key (business_id,po_id) references collective.purchase_orders (business_id,id);
create index purchase_po on collective.purchases (business_id,po_id) where po_id is not null;
alter table collective.stock_observations add column received_po_id uuid;
alter table collective.stock_observations add constraint stock_received_order foreign key (business_id,received_po_id) references collective.purchase_orders (business_id,id);
create index stock_received_order on collective.stock_observations (business_id,received_po_id) where received_po_id is not null;

create table collective.invoice_checks (
  id uuid primary key,
  business_id uuid not null,
  purchase_id uuid not null,
  purchase_revision integer not null,
  po_id uuid,
  status text not null check (status in ('unmatched','passed','flagged')),
  differences jsonb not null check (jsonb_typeof(differences)='array'),
  review_status text not null default 'pending' check (review_status in ('pending','confirmed','rejected')),
  reviewed_by uuid references auth.users,
  recovery_amount numeric(18,2) check (recovery_amount >= 0),
  recovery_source_id uuid,
  resolution jsonb,
  revision integer not null default 1 check (revision > 0),
  history jsonb not null default '{}',
  created_at timestamptz not null default now(),
  unique (business_id,id),
  foreign key (business_id,purchase_id,purchase_revision) references collective.purchases,
  foreign key (business_id,po_id) references collective.purchase_orders (business_id,id),
  foreign key (business_id,recovery_source_id) references collective.sources (business_id,id),
  check (recovery_amount is null or (recovery_source_id is not null and reviewed_by is not null and review_status='confirmed'))
);
create unique index invoice_check_inputs on collective.invoice_checks (business_id,purchase_id,purchase_revision,po_id) nulls not distinct;
create index invoice_check_po on collective.invoice_checks (business_id,po_id);
create index invoice_check_reviewer on collective.invoice_checks (reviewed_by) where reviewed_by is not null;
create index invoice_check_recovery_source on collective.invoice_checks (business_id,recovery_source_id) where recovery_source_id is not null;

create table collective.conversations (
  id uuid primary key,
  business_id uuid not null references collective.businesses,
  supplier_id uuid not null references collective.suppliers,
  created_at timestamptz not null default now(),
  unique (business_id,id),
  unique (business_id,supplier_id)
);
create index conversation_supplier on collective.conversations (supplier_id);
create table collective.message_drafts (
  id uuid primary key,
  business_id uuid not null,
  conversation_id uuid not null,
  kind text not null check (kind in ('rfq','invoice_query')),
  text_content text not null check (length(text_content) between 1 and 16000),
  recipient text not null,
  input_context jsonb not null,
  status text not null default 'draft' check (status in ('draft','sent_simulated')),
  edited_by uuid references auth.users,
  sent_by uuid references auth.users,
  sent_at timestamptz,
  revision integer not null default 1 check (revision > 0),
  history jsonb not null default '{}',
  created_at timestamptz not null default now(),
  unique (business_id,id),
  foreign key (business_id,conversation_id) references collective.conversations (business_id,id),
  check (status <> 'sent_simulated' or (sent_by is not null and sent_at is not null))
);
create index draft_conversation on collective.message_drafts (business_id,conversation_id);
create index draft_editor on collective.message_drafts (edited_by) where edited_by is not null;
create index draft_sender on collective.message_drafts (sent_by) where sent_by is not null;
create table collective.messages (
  id uuid primary key,
  business_id uuid not null,
  conversation_id uuid not null,
  direction text not null check (direction in ('inbound','outbound_simulated')),
  text_content text not null check (length(text_content) between 1 and 16000),
  source_id uuid,
  draft_id uuid unique,
  draft_revision integer,
  recipient text,
  sent_by uuid references auth.users,
  receipt_id uuid unique,
  created_at timestamptz not null default now(),
  unique (business_id,id),
  foreign key (business_id,conversation_id) references collective.conversations (business_id,id),
  foreign key (business_id,source_id) references collective.sources (business_id,id),
  foreign key (business_id,draft_id) references collective.message_drafts (business_id,id),
  check (direction <> 'outbound_simulated' or
    (draft_id is not null and draft_revision is not null and sent_by is not null and receipt_id is not null))
);
create index message_conversation on collective.messages (business_id,conversation_id,created_at,id);
create index message_source on collective.messages (business_id,source_id) where source_id is not null;
create index message_sender on collective.messages (sent_by) where sent_by is not null;
create table collective.jobs (
  id uuid primary key,
  business_id uuid references collective.businesses,
  kind text not null,
  input_revision integer check (input_revision > 0),
  dedup_key text not null unique,
  payload jsonb not null check (jsonb_typeof(payload)='object'),
  status text not null default 'queued' check (status in ('queued','running','succeeded','failed')),
  lease_until timestamptz,
  worker_id uuid,
  attempts integer not null default 0 check (attempts >= 0),
  next_attempt_at timestamptz not null default now(),
  result jsonb,
  error_code text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  check (status <> 'running' or (lease_until is not null and worker_id is not null))
);
create index job_due on collective.jobs (status,next_attempt_at);
create index job_lease on collective.jobs (lease_until) where status='running';
create index job_business on collective.jobs (business_id,created_at,id);

create function collective_private.check_order_approval() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare approval collective.bound_approvals;
declare release collective.releases;
begin
  select * into approval from collective.bound_approvals
    where business_id=new.business_id and id=new.approval_id for share;
  select * into release from collective.releases where id=new.release_id;
  if approval.id is null or approval.state <> 'valid' or approval.expires_at <= clock_timestamp()
      or approval.need_id <> new.need_id or new.decision_amount > approval.cap
      or release.pool_id is distinct from approval.pool_id
      or (approval.pool_id is null and release.need_id is distinct from new.need_id)
      or (approval.terms->>'supplier_id')::uuid is distinct from new.supplier_id
      or (approval.terms->>'spec_id')::uuid is distinct from new.spec_id
      or (approval.terms->>'spec_revision')::integer is distinct from new.spec_revision
      or (approval.terms->>'quantity')::numeric is distinct from new.quantity then
    raise exception 'Current bound approval required for PO' using errcode='23514';
  end if;
  if approval.pool_id is not null and (
      not exists (select 1 from collective.pools p where p.id=approval.pool_id
                  and p.state='ready' and p.deadline > clock_timestamp())
      or exists (select 1 from collective.pool_members m
                 where m.pool_id=approval.pool_id and m.state not in ('approved','withdrawn','expired'))
  ) then
    raise exception 'All current pool approvals required' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.check_order_approval() from public,anon,authenticated;
create trigger order_approval before insert on collective.purchase_orders
  for each row execute function collective_private.check_order_approval();

create function collective_private.complete_release() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare expected integer;
declare issued integer;
begin
  if new.pool_id is null then
    expected := 1;
  else
    select count(*) into expected from collective.pool_members
      where pool_id=new.pool_id and state not in ('withdrawn','expired');
  end if;
  select count(*) into issued from collective.purchase_orders where release_id=new.id;
  if expected=0 or issued <> expected then
    raise exception 'Incomplete release' using errcode='23514';
  end if;
  if exists (
    select 1 from collective.purchase_orders po
    join collective.bound_approvals a on a.business_id=po.business_id and a.id=po.approval_id
    where po.release_id=new.id and
      (a.state not in ('valid','released') or a.expires_at <= clock_timestamp() or po.decision_amount > a.cap)
  ) then
    raise exception 'Invalid release approvals' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.complete_release() from public,anon,authenticated;
create constraint trigger release_complete after insert on collective.releases
  deferrable initially deferred for each row execute function collective_private.complete_release();

create function collective_private.protect_binding() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if tg_op='DELETE' or old.state='released' then
    raise exception 'Immutable approval binding' using errcode='23514';
  end if;
  if (to_jsonb(new)-array['state','latest_evaluation_revision','revision','history'])
      <> (to_jsonb(old)-array['state','latest_evaluation_revision','revision','history'])
      or (old.state <> 'valid' and new.state <> old.state) then
    raise exception 'Immutable approval binding' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_binding() from public,anon,authenticated;
create trigger binding_immutable before update or delete on collective.bound_approvals
  for each row execute function collective_private.protect_binding();

create function collective_private.protect_sent_draft() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.status='sent_simulated' then
    raise exception 'Sent message is immutable' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_sent_draft() from public,anon,authenticated;
create trigger sent_draft_immutable before update on collective.message_drafts
  for each row execute function collective_private.protect_sent_draft();

create function collective_private.check_outbound() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare draft collective.message_drafts;
begin
  if new.direction='outbound_simulated' then
    select * into draft from collective.message_drafts
      where business_id=new.business_id and id=new.draft_id for update;
    if draft.id is null or draft.status <> 'draft' or draft.revision <> new.draft_revision
      or draft.text_content <> new.text_content or draft.recipient <> new.recipient
      or draft.conversation_id <> new.conversation_id then
      raise exception 'Exact approved draft required' using errcode='23514';
    end if;
  end if;
  return new;
end $$;
revoke all on function collective_private.check_outbound() from public,anon,authenticated;
create trigger outbound_draft_check before insert on collective.messages
  for each row execute function collective_private.check_outbound();

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'reorder_needs','learned_rules','rule_evidence','pools','pool_members','pool_evaluations',
    'member_evaluations','bound_approvals','releases','purchase_orders','invoice_checks',
    'conversations','message_drafts','messages','jobs'
  ] loop
    execute format('alter table collective.%I enable row level security',table_name);
    execute format('alter table collective.%I force row level security',table_name);
    execute format('grant select on collective.%I to collective_coordinator',table_name);
    execute format('create policy coordinator_read on collective.%I for select to collective_coordinator using (true)',table_name);
  end loop;
  foreach table_name in array array[
    'reorder_needs','pool_members','member_evaluations','bound_approvals','purchase_orders',
    'invoice_checks','conversations','message_drafts','messages','jobs'
  ] loop
    execute format('grant select on collective.%I to collective_tenant',table_name);
    execute format('create policy tenant_read on collective.%I for select to collective_tenant using (collective_private.is_member(business_id))',table_name);
  end loop;
  foreach table_name in array array['reorder_needs','learned_rules','pools','bound_approvals','invoice_checks','message_drafts'] loop
    execute format('create trigger workflow_history before insert or update on collective.%I for each row execute function collective_private.preserve_revision()',table_name);
  end loop;
  foreach table_name in array array['rule_evidence','pool_evaluations','member_evaluations','releases','purchase_orders','messages'] loop
    execute format('create trigger immutable before update or delete on collective.%I for each row execute function collective_private.immutable_record()',table_name);
  end loop;
  foreach table_name in array array['pools','pool_members','pool_evaluations','member_evaluations','releases','purchase_orders','invoice_checks','conversations','jobs'] loop
    execute format('grant insert on collective.%I to collective_coordinator',table_name);
    execute format('create policy coordinator_insert on collective.%I for insert to collective_coordinator with check (true)',table_name);
  end loop;
  foreach table_name in array array['pools','pool_members'] loop
    execute format('grant update on collective.%I to collective_coordinator',table_name);
    execute format('create policy coordinator_edit on collective.%I for update to collective_coordinator using (true) with check (true)',table_name);
  end loop;
end $$;
grant insert on collective.reorder_needs to collective_tenant,collective_coordinator;
create policy need_insert on collective.reorder_needs for insert to collective_tenant
  with check (collective_private.is_owner(business_id) and selected_by=collective_private.subject_id() and provenance='human');
create policy need_suggest on collective.reorder_needs for insert to collective_coordinator
  with check (state='suggested' and selected_by is null and provenance='system');
grant update (quantity,state,selected_by,revision,mapping_id,mapping_revision,order_start,order_end,
              arrival_start,arrival_end,stock_snapshot,usage_input) on collective.reorder_needs to collective_tenant;
create policy need_edit on collective.reorder_needs for update to collective_tenant
  using (collective_private.is_owner(business_id) and state in ('suggested','selected','stale'))
  with check (collective_private.is_owner(business_id) and state='selected' and selected_by=collective_private.subject_id());
grant update (state,reserved_pool_id,revision) on collective.reorder_needs to collective_coordinator;
create policy need_coordinator_edit on collective.reorder_needs for update to collective_coordinator using (true) with check (true);

grant select,update (state,accepted_by,accepted_at,revision) on collective.learned_rules to collective_tenant;
create policy rule_read on collective.learned_rules for select to collective_tenant using (
  (scope='private' and collective_private.is_member(business_id)) or
  (scope='network' and distinct_businesses >= 5 and exists (
    select 1 from collective.network_admins a where a.network_id=learned_rules.network_id
  ))
);
create policy rule_accept on collective.learned_rules for update to collective_tenant using (
  (scope='private' and collective_private.is_owner(business_id)) or
  (scope='network' and distinct_businesses >= 5 and exists (
    select 1 from collective.network_admins a where a.network_id=learned_rules.network_id
  ))
) with check (
  ((scope='private' and collective_private.is_owner(business_id)) or
   (scope='network' and distinct_businesses >= 5 and exists (
     select 1 from collective.network_admins a where a.network_id=learned_rules.network_id)))
  and (state <> 'accepted' or accepted_by=collective_private.subject_id())
);
grant insert on collective.learned_rules to collective_coordinator;
create policy rule_propose on collective.learned_rules for insert to collective_coordinator
  with check (state='proposed' and accepted_by is null and accepted_at is null);
grant update (proposed_value,sample_count,distinct_businesses,evidence_digest,revision) on collective.learned_rules to collective_coordinator;
create policy rule_refresh on collective.learned_rules for update to collective_coordinator
  using (state='proposed') with check (state='proposed');
grant select on collective.rule_evidence to collective_tenant;
create policy evidence_private on collective.rule_evidence for select to collective_tenant
  using (collective_private.is_member(business_id) and exists (
    select 1 from collective.learned_rules r where r.id=rule_id and r.scope='private' and r.business_id=rule_evidence.business_id
  ));
grant insert on collective.rule_evidence to collective_coordinator;
create policy evidence_insert on collective.rule_evidence for insert to collective_coordinator
  with check (exists (select 1 from collective.learned_rules r join collective.businesses b on b.id=rule_evidence.business_id
    where r.id=rule_id and r.state='proposed' and
      ((r.scope='private' and r.business_id=b.id) or (r.scope='network' and r.network_id=b.network_id))));

grant insert on collective.bound_approvals to collective_tenant;
create policy approval_owner_insert on collective.bound_approvals for insert to collective_tenant
  with check (collective_private.is_owner(business_id) and owner_id=collective_private.subject_id() and state='valid');
grant update (state,latest_evaluation_revision,revision) on collective.bound_approvals to collective_coordinator,collective_tenant;
create policy approval_owner_edit on collective.bound_approvals for update to collective_tenant
  using (collective_private.is_owner(business_id)) with check (collective_private.is_owner(business_id));
create policy approval_lifecycle on collective.bound_approvals for update to collective_coordinator using (true) with check (true);
grant update (review_status,reviewed_by,recovery_amount,recovery_source_id,resolution,revision) on collective.invoice_checks to collective_tenant;
create policy invoice_review on collective.invoice_checks for update to collective_tenant
  using (collective_private.is_owner(business_id))
  with check (collective_private.is_owner(business_id) and reviewed_by=collective_private.subject_id());

grant insert on collective.message_drafts to collective_coordinator;
create policy draft_propose on collective.message_drafts for insert to collective_coordinator
  with check (status='draft' and sent_by is null and sent_at is null);
grant update (text_content,recipient,revision) on collective.message_drafts to collective_coordinator;
create policy draft_worker_edit on collective.message_drafts for update to collective_coordinator using (status='draft') with check (status='draft');
grant update (text_content,recipient,edited_by,status,sent_by,sent_at,revision) on collective.message_drafts to collective_tenant;
create policy draft_owner_edit on collective.message_drafts for update to collective_tenant
  using (collective_private.is_owner(business_id) and status='draft')
  with check (collective_private.is_owner(business_id) and
    (status='draft' or sent_by=collective_private.subject_id()));
grant insert on collective.messages to collective_tenant,collective_coordinator;
create policy message_send on collective.messages for insert to collective_tenant
  with check (collective_private.is_owner(business_id) and direction='outbound_simulated' and sent_by=collective_private.subject_id());
create policy message_inbound on collective.messages for insert to collective_coordinator with check (direction='inbound' and sent_by is null);
grant update (status,lease_until,worker_id,attempts,next_attempt_at,result,error_code,completed_at) on collective.jobs to collective_coordinator;
create policy job_update on collective.jobs for update to collective_coordinator using (true) with check (true);
