alter table collective.invoice_checks
  add column result jsonb not null default '{}' check (jsonb_typeof(result)='object'),
  add column input_snapshot jsonb not null default '{}' check (jsonb_typeof(input_snapshot)='object'),
  add column confirmed_amount numeric(18,2) check (confirmed_amount>=0);
alter table collective.invoice_checks add constraint invoice_confirmed_amount
  check (confirmed_amount is null or (review_status='confirmed' and reviewed_by is not null));
alter table collective.invoice_checks add constraint invoice_recovery_bound
  check (recovery_amount is null or (confirmed_amount is not null and recovery_amount<=confirmed_amount));
grant update(confirmed_amount) on collective.invoice_checks to collective_tenant;

create function collective_private.protect_invoice_result() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if tg_op='INSERT' then
    if new.review_status<>'pending' or new.reviewed_by is not null
      or new.confirmed_amount is not null or new.recovery_amount is not null
      or new.recovery_source_id is not null or new.resolution is not null then
      raise exception 'Human invoice review must be separate' using errcode='23514';
    end if;
    return new;
  end if;
  if (to_jsonb(new)-array['review_status','reviewed_by','confirmed_amount','recovery_amount',
       'recovery_source_id','resolution','revision','history']) is distinct from
     (to_jsonb(old)-array['review_status','reviewed_by','confirmed_amount','recovery_amount',
       'recovery_source_id','resolution','revision','history']) then
    raise exception 'Invoice comparison inputs and result are immutable' using errcode='23514';
  end if;
  if old.recovery_amount is not null or
     (old.review_status<>'pending' and (new.review_status<>old.review_status
       or new.confirmed_amount is distinct from old.confirmed_amount)) then
    raise exception 'Invoice review or resolution is already final' using errcode='23514';
  end if;
  return new;
end $$;
revoke all on function collective_private.protect_invoice_result() from public,anon,authenticated;
create trigger invoice_result_immutable before insert or update on collective.invoice_checks
  for each row execute function collective_private.protect_invoice_result();
