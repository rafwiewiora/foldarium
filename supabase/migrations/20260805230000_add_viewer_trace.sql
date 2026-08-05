alter table public.quiz_answers
  add column viewer_trace jsonb;

alter table public.quiz_answers
  add constraint quiz_answers_viewer_trace_shape
  check (
    viewer_trace is null
    or (
      jsonb_typeof(viewer_trace) = 'object'
      and viewer_trace ->> 'version' = '1'
      and jsonb_typeof(viewer_trace -> 'snapshots') = 'array'
    )
  );
