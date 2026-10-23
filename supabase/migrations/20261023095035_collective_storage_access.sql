create function collective_private.can_read_source_object(object_name text) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from collective.sources s
    join collective.business_memberships m on m.business_id=s.business_id
    join collective.businesses b on b.id=s.business_id
    where m.user_id=auth.uid() and s.status='available'
      and (m.role='owner' or b.staff_can_view_prices or not s.price_bearing)
      and (s.original_object_key=object_name or s.provider_object_key=object_name or exists (
        select 1 from collective.source_pages p where p.business_id=s.business_id and p.source_id=s.id
          and (p.object_key=object_name or p.provider_object_key=object_name)
      ))
  );
$$;
create function collective_private.can_upload_source_object(object_name text) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from collective.sources s
    join collective.business_memberships m on m.business_id=s.business_id
    where m.user_id=auth.uid() and s.uploaded_by=auth.uid()
      and s.status='uploading' and s.original_object_key=object_name
  );
$$;
revoke all on function collective_private.can_read_source_object(text),
  collective_private.can_upload_source_object(text) from public,anon,authenticated;
grant usage on schema collective_private to authenticated;
grant execute on function collective_private.can_read_source_object(text),
  collective_private.can_upload_source_object(text) to authenticated;
create policy collective_source_read on storage.objects for select to authenticated
  using (bucket_id='collective-sources' and collective_private.can_read_source_object(name));
create policy collective_source_upload on storage.objects for insert to authenticated
  with check (bucket_id='collective-sources' and collective_private.can_upload_source_object(name));
