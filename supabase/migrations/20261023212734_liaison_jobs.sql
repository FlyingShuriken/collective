create unique index proposal_source_kind on collective.proposals(business_id,source_id,kind);

create table collective_private.model_attempts (
  id uuid primary key,
  business_id uuid not null,
  job_id uuid not null,
  attempt jsonb not null check(jsonb_typeof(attempt)='object'),
  validated jsonb check(validated is null or jsonb_typeof(validated)='object'),
  created_at timestamptz not null default now(),
  foreign key(business_id,job_id) references collective.jobs(business_id,id)
);
alter table collective_private.model_attempts enable row level security;
alter table collective_private.model_attempts force row level security;
grant select,insert on collective_private.model_attempts to collective_coordinator;
create policy attempt_worker on collective_private.model_attempts for all to collective_coordinator
  using(true) with check(true);
create index model_attempt_job on collective_private.model_attempts(business_id,job_id);
create trigger immutable before update or delete on collective_private.model_attempts
  for each row execute function collective_private.immutable_record();
