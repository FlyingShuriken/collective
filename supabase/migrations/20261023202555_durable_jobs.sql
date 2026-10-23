create extension if not exists pg_cron;

alter table collective.jobs add constraint job_result_complete
  check ((status='succeeded' and result is not null and jsonb_typeof(result)='object'
          and completed_at is not null) or (status<>'succeeded' and result is null));

create function collective_private.protect_job() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if old.status='succeeded'
     or (to_jsonb(new)-array['status','lease_until','worker_id','attempts','next_attempt_at',
                            'result','error_code','completed_at'])
       <> (to_jsonb(old)-array['status','lease_until','worker_id','attempts','next_attempt_at',
                              'result','error_code','completed_at'])
     or new.attempts<old.attempts then
    raise exception 'Immutable job input or completed result' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_job() from public,anon,authenticated;
create trigger job_immutable before update on collective.jobs
  for each row execute function collective_private.protect_job();

create function collective_private.enqueue_due(as_of timestamptz default now()) returns integer
language plpgsql security invoker set search_path='' as $$
declare inserted integer;
begin
  with latest as (
    select distinct on (business_id,need_id) * from collective.bound_approvals
    order by business_id,need_id,approved_at desc,id desc
  ), due as (
    select n.business_id,n.id as need_id,n.revision,'reorder'::text as kind,
      'reorder:'||n.id||':'||n.revision||':'||extract(epoch from n.order_start)||':'||
        extract(epoch from n.order_end) as key,
      'reorder'::text as reason,n.id as reference_id,n.order_start as due_at
    from collective.reorder_needs n
    where n.state in ('suggested','selected') and n.order_start<=as_of and n.order_end>as_of
      and not exists(select 1 from latest a where a.business_id=n.business_id
                     and a.need_id=n.id and a.state='valid')
    union all
    select n.business_id,n.id,n.revision,'expiry',
      'expiry:approval:'||a.id||':'||extract(epoch from a.expires_at)||':'||n.revision,
      'approval',a.id,a.expires_at
    from latest a join collective.reorder_needs n
      on (n.business_id,n.id)=(a.business_id,a.need_id)
    where a.state in ('valid','void') and a.expires_at<=as_of
      and n.state in ('selected','reserved')
      and (a.terms->>'need_revision')::integer=n.revision
    union all
    select n.business_id,n.id,n.revision,'expiry',
      'expiry:pool:'||p.id||':'||extract(epoch from p.deadline)||':'||n.id||':'||n.revision,
      'pool',p.id,p.deadline
    from collective.pools p join collective.reorder_needs n on n.reserved_pool_id=p.id
    where p.state<>'released' and p.deadline<=as_of and n.state='reserved'
    union all
    select n.business_id,n.id,n.revision,'expiry',
      'expiry:window:'||n.id||':'||n.revision||':'||extract(epoch from n.order_end),
      'window',n.id,n.order_end
    from collective.reorder_needs n
    where n.state in ('suggested','selected') and n.reserved_pool_id is null and n.order_end<=as_of
  )
  insert into collective.jobs(id,business_id,kind,input_revision,dedup_key,payload,next_attempt_at)
    select gen_random_uuid(),business_id,kind,revision,key,
      jsonb_build_object('need_id',need_id,'reason',reason,'reference_id',reference_id,'due_at',due_at),
      due_at from due order by due_at,need_id,reason
    on conflict(dedup_key) do nothing;
  get diagnostics inserted = row_count;
  return inserted;
end $$;
revoke all on function collective_private.enqueue_due(timestamptz) from public,anon,authenticated;
grant execute on function collective_private.enqueue_due(timestamptz) to collective_coordinator;

select cron.schedule('collective-due-work','* * * * *',
  'begin; set local role collective_coordinator; select collective_private.enqueue_due(); commit;');
