create unique index intake_source_once on collective.proposals(business_id,source_id)
  where kind in ('purchase','stock');
create index job_source_lookup on collective.jobs(business_id,(payload->>'source_id'),created_at desc)
  where kind='intake';

grant update(kind) on collective.proposals to collective_tenant;
create function collective_private.protect_proposal_kind() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if new.kind is distinct from old.kind and (
      old.kind not in ('purchase','stock') or new.kind not in ('purchase','stock')
      or old.status<>'pending_review' or new.status<>'pending_review'
      or new.current_revision<>old.current_revision+1
      or not exists(select 1 from collective.proposal_revisions r
        where r.business_id=new.business_id and r.proposal_id=new.id
          and r.revision=new.current_revision and r.edited_by=collective_private.subject_id())) then
    raise exception 'Human pending revision required for record kind' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_proposal_kind() from public,anon,authenticated;
create trigger proposal_kind before update on collective.proposals
  for each row execute function collective_private.protect_proposal_kind();
