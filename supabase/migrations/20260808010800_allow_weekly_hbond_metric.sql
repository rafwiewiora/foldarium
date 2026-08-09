-- Preserve immutable v3 rounds while allowing v4 manifests to replace the
-- coarse VdW-contact count with a ProLIF implicit-hydrogen H-bond residue count.

begin;

do $$
declare
  function_signature constant regprocedure :=
    'public.open_weekly_quiz_round(text,text,timestamptz,timestamptz,jsonb,text,jsonb,text)'::regprocedure;
  definition text;
  repaired_definition text;
begin
  select pg_get_functiondef(function_signature::oid) into definition;
  repaired_definition := replace(
    definition,
    '<> ''prolif_unique_residue_interaction_type''',
    'not in (''prolif_unique_residue_interaction_type'', ''prolif_hbond_residue_count'')'
  );
  if repaired_definition = definition then
    raise exception 'expected legacy weekly interaction metric validator in %',
      function_signature;
  end if;
  execute repaired_definition;
end;
$$;

comment on function public.open_weekly_quiz_round(
  text, text, timestamptz, timestamptz, jsonb, text, jsonb, text
) is
  'Idempotently opens an immutable blind round in an explicit deployment environment. Allows legacy v3 VdW counts or v4 implicit-H H-bond residue counts, while rejecting released-coordinate answers and execution identities.';

commit;
