-- Capture the business delivery zone at confirmation; older unknown zones stay unknown.
alter table collective.purchases add column delivery_zone text;
alter table collective.learned_rules
  add column sealed boolean not null default false,
  add column reviewed_by uuid references auth.users,
  add column reviewed_at timestamptz;
create index rule_reviewer on collective.learned_rules(reviewed_by) where reviewed_by is not null;
revoke update(proposed_value,sample_count,distinct_businesses,evidence_digest,revision)
  on collective.learned_rules from collective_coordinator;
grant update(sealed,revision) on collective.learned_rules to collective_coordinator;
grant update(reviewed_by,reviewed_at) on collective.learned_rules to collective_tenant;

create function collective_private.protect_rule() returns trigger
language plpgsql security definer set search_path='' as $$
declare evidence_businesses integer; evidence_sources integer;
begin
  if tg_op='INSERT' then
    if new.state<>'proposed' or new.sealed or new.accepted_by is not null
      or new.accepted_at is not null or new.reviewed_by is not null or new.reviewed_at is not null then
      raise exception 'Rule proposal and human review must be separate' using errcode='23514';
    end if;
    return new;
  end if;
  if (to_jsonb(new)-array['state','accepted_by','accepted_at','reviewed_by','reviewed_at',
                        'sealed','revision','history']) is distinct from
     (to_jsonb(old)-array['state','accepted_by','accepted_at','reviewed_by','reviewed_at',
                        'sealed','revision','history']) then
    raise exception 'Rule inputs are immutable' using errcode='23514';
  end if;
  if not old.sealed and new.sealed and old.state=new.state and
     (new.accepted_by,new.accepted_at,new.reviewed_by,new.reviewed_at) is not distinct from
     (old.accepted_by,old.accepted_at,old.reviewed_by,old.reviewed_at) then
    return new;
  end if;
  if not old.sealed or not new.sealed or
     not ((old.state='proposed' and new.state in ('accepted','rejected')) or
          (old.state='accepted' and new.state in ('revoked','superseded'))) or
     new.reviewed_by is distinct from collective_private.subject_id() or
     new.reviewed_at is null or new.reviewed_at>clock_timestamp() then
    raise exception 'Invalid human rule transition' using errcode='23514';
  end if;
  if new.state='accepted' then
    select count(distinct e.business_id),count(*) into evidence_businesses,evidence_sources
      from collective.rule_evidence e join collective.businesses b on b.id=e.business_id
      where e.rule_id=new.id and
        ((new.scope='private' and b.id=new.business_id) or
         (new.scope='network' and b.network_id=new.network_id));
    if new.accepted_by is distinct from new.reviewed_by or
       new.accepted_at is distinct from new.reviewed_at or
       evidence_businesses<>new.distinct_businesses or evidence_businesses<1 or
       (new.scope='network' and evidence_businesses<5) or
       (new.kind in ('observed_fee','quote_accuracy') and new.sample_count>evidence_sources) or
       (new.kind='usage' and evidence_sources<2) or
       new.proposed_value->>'version' is distinct from 'learning-v1' or
       new.proposed_value->'value'->>'kind' is distinct from new.kind then
      raise exception 'Rule evidence does not meet its activation gate' using errcode='23514';
    end if;
  elsif (new.accepted_by,new.accepted_at) is distinct from (old.accepted_by,old.accepted_at) then
    raise exception 'Original rule acceptance is immutable' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_rule() from public,anon,authenticated;
create trigger rule_immutable before insert or update on collective.learned_rules
  for each row execute function collective_private.protect_rule();

create function collective_private.protect_rule_evidence() returns trigger
language plpgsql security invoker set search_path='' as $$
declare rule_state text; rule_sealed boolean;
begin
  select state,sealed into rule_state,rule_sealed from collective.learned_rules
    where id=new.rule_id for update;
  if rule_state is distinct from 'proposed' or rule_sealed is distinct from false then
    raise exception 'Rule evidence is already sealed' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_rule_evidence() from public,anon,authenticated;
create trigger rule_evidence_sealed before insert on collective.rule_evidence
  for each row execute function collective_private.protect_rule_evidence();

drop policy rule_read on collective.learned_rules;
create policy rule_read on collective.learned_rules for select to collective_tenant using (
  (scope='private' and collective_private.is_member(business_id)) or
  (scope='network' and distinct_businesses>=5 and (
    exists(select 1 from collective.network_admins a where a.network_id=learned_rules.network_id) or
    (state='accepted' and exists(select 1 from collective.businesses b
      where b.network_id=learned_rules.network_id and collective_private.is_member(b.id)))
  ))
);
