alter table collective.jobs drop constraint jobs_status_check;
alter table collective.jobs add constraint jobs_status_check
  check (status in ('waiting_review','queued','running','succeeded','failed'));
alter table collective.jobs add constraint job_business_id unique(business_id,id);

create table collective.source_preparations (
  business_id uuid not null,
  source_id uuid not null,
  status text not null default 'pending_review' check(status in ('pending_review','ready')),
  ingestion_job_id uuid not null,
  review_hash text check(review_hash ~ '^[a-f0-9]{64}$'),
  provider_sha256 text check(provider_sha256 ~ '^[a-f0-9]{64}$'),
  reviewed_by uuid references auth.users,
  reviewed_at timestamptz,
  metadata jsonb not null default '{}' check(jsonb_typeof(metadata)='object'),
  primary key(business_id,source_id),
  foreign key(business_id,source_id) references collective.sources(business_id,id),
  foreign key(business_id,ingestion_job_id) references collective.jobs(business_id,id),
  check(status<>'ready' or (review_hash is not null and provider_sha256 is not null
                          and reviewed_by is not null and reviewed_at is not null))
);
alter table collective.source_preparations enable row level security;
alter table collective.source_preparations force row level security;
grant select on collective.source_preparations to collective_tenant,collective_coordinator;
grant insert,update on collective.source_preparations to collective_coordinator;
create policy source_preparation_member on collective.source_preparations for select to collective_tenant
  using(collective_private.is_member(business_id));
create policy source_preparation_worker on collective.source_preparations for all to collective_coordinator
  using(true) with check(true);
create index preparation_job on collective.source_preparations(ingestion_job_id);
create index preparation_reviewer on collective.source_preparations(reviewed_by);

create function collective_private.protect_source_preparation() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if old.status='ready' or new.business_id<>old.business_id or new.source_id<>old.source_id
     or new.ingestion_job_id<>old.ingestion_job_id then
    raise exception 'Immutable prepared source' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_source_preparation() from public,anon,authenticated;
create trigger preparation_immutable before update on collective.source_preparations
  for each row execute function collective_private.protect_source_preparation();

alter table collective.sources add constraint source_original_scope check(
  original_object_key is null or starts_with(original_object_key,business_id::text||'/'||id::text||'/'));
alter table collective.sources add constraint source_provider_scope check(
  provider_object_key is null or starts_with(provider_object_key,business_id::text||'/'||id::text||'/'));
alter table collective.source_pages add constraint source_page_scope check(
  starts_with(object_key,business_id::text||'/'||source_id::text||'/') and
  (provider_object_key is null or starts_with(provider_object_key,business_id::text||'/'||source_id::text||'/')));
drop trigger immutable on collective.source_pages;
create function collective_private.protect_source_page() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if tg_op='DELETE' or old.provider_object_key is not null or new.provider_object_key is null
     or (to_jsonb(new)-'provider_object_key')<>(to_jsonb(old)-'provider_object_key') then
    raise exception 'Immutable source page' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_source_page() from public,anon,authenticated;
create trigger immutable before update or delete on collective.source_pages
  for each row execute function collective_private.protect_source_page();
grant update(provider_object_key) on collective.source_pages to collective_coordinator;
create policy source_page_prepare on collective.source_pages for update to collective_coordinator
  using(true) with check(true);
