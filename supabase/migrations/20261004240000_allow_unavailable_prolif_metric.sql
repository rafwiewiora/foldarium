-- Unknown receptor chemistry cannot receive a fabricated H-bond count.
-- This optional, blind-safe metric may be explicitly unavailable for UNK only.
begin;
create function private.weekly_interaction_count_is_valid(v jsonb)
returns boolean language sql immutable set search_path=pg_catalog as $$
 select coalesce(
  jsonb_typeof(v)='object' and nullif(btrim(v->>'policy'),'') is not null and
  ((v->>'metric' in ('prolif_unique_residue_interaction_type','prolif_hbond_residue_count')
    and jsonb_typeof(v->'value')='number' and v->>'value' ~ '^[0-9]+$'
    and v-array['metric','policy','value']='{}'::jsonb)
   or (v->>'metric'='prolif_hbond_residue_count'
    and v->'value'='null'::jsonb and v->>'policy'='prolif-implicit-hbond-unique-protein-residue/v2'
    and v->>'status'='unavailable' and v->>'availability_policy'='foldarium.prolif-availability/v1'
    and v->>'reason'='unsupported_receptor_residue' and v->'unsupported_residues'='["UNK"]'::jsonb
    and v-array['metric','policy','value','status','availability_policy','reason','unsupported_residues']='{}'::jsonb)),false)
$$;
revoke all on function private.weekly_interaction_count_is_valid(jsonb) from public,anon,authenticated,service_role;
-- Retain the existing complete blind-manifest/window/identity validation.
do $$
declare definition text; begin_marker text:='or jsonb_typeof(choice.value -> ''interaction_count'')';
 end_marker text:='or nullif(choice.value #>> ''{interaction_count,policy}'', '''') is null';
 begin_at integer; end_at integer;
begin
 definition:=pg_get_functiondef('public.open_weekly_quiz_round(text,text,timestamptz,timestamptz,jsonb,text,jsonb,text)'::regprocedure);
 begin_at:=strpos(definition,begin_marker);end_at:=strpos(definition,end_marker);
 if begin_at=0 or end_at<=begin_at or strpos(definition,'prolif_hbond_residue_count')=0 then
  raise exception 'reviewed existing blind metric validation block not found'; end if;
 definition:=substring(definition from 1 for begin_at-1)
  ||'or not private.weekly_interaction_count_is_valid(choice.value -> ''interaction_count'')'
  ||substring(definition from end_at+length(end_marker));
 execute definition;
end $$;
commit;
