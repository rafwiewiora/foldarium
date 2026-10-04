const content = document.querySelector('#research-content');
const status = document.querySelector('#research-status');
const query = new URLSearchParams(location.search);
const roundId = query.get('round_id');
const api = '/api/historical-preview-research';
function element(tag, text, className) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (className) e.className = className; return e; }
function date(value) { return new Date(value).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }); }
function percentage(correct, total) { return total ? `${(100 * correct / total).toFixed(1)}%` : 'Not scorable'; }
function table(caption, columns, rows) {
  const wrap = element('div', null, 'table-wrap'), t = element('table'), head = element('thead'), h = element('tr');
  t.append(element('caption', caption)); columns.forEach(label => { const th = element('th', label); th.scope = 'col'; h.append(th); }); head.append(h); t.append(head);
  const body = element('tbody'); rows.forEach(values => { const r = element('tr'); values.forEach(value => r.append(element('td', value))); body.append(r); });
  t.append(body); wrap.append(t); return wrap;
}
async function request(params) {
  const response = await fetch(`${api}?${params}`, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error('unavailable'); return response.json();
}
function summaryCards(values) {
  const cards = element('div', null, 'cards');
  values.forEach(([label, value]) => { const card = element('div', null, 'card'); card.append(element('span', value, 'stat'), element('span', label, 'muted')); cards.append(card); });
  return cards;
}
function renderDetail(artifact) {
  document.title = `${artifact.source.round_id} · Foldarium research`;
  document.querySelector('h1').textContent = artifact.source.round_id.replace(/^preview-weekly-/, '').replace(/-nextweekly-.*/, '');
  content.append(element('p', `Original Preview window: ${date(artifact.source.opens_at)} – ${date(artifact.source.closes_at)}.`, 'muted'));
  const scorable = artifact.counts.scorable_item_count ?? artifact.counts.item_count;
  content.append(summaryCards([['questions preserved', artifact.counts.item_count], ['poses preserved', artifact.counts.choice_count], ['scorable questions', scorable], ['unscorable references', artifact.counts.excluded_item_count ?? 0]]));
  const download = element('a', 'View verified research data'); download.href = `${api}?${new URLSearchParams({ round_id: artifact.source.round_id })}`; content.append(download);
  content.append(element('h2', 'Model decisions'));
  if (!artifact.benchmarks.length) content.append(element('p', 'This published cohort has no registered model decisions.', 'muted'));
  else {
    const rows = artifact.benchmarks.map(b => {
      const clustered = b.decisions.filter(d => typeof d.clustered_correct === 'boolean');
      const exact = b.decisions.filter(d => typeof d.unclustered_correct === 'boolean');
      return [b.model_id, b.driver, `${exact.filter(d => d.unclustered_correct).length} / ${exact.length}`, percentage(exact.filter(d => d.unclustered_correct).length, exact.length), percentage(clustered.filter(d => d.clustered_correct).length, clustered.length)];
    });
    content.append(table('Full cohort model accuracy', ['Model', 'Driver', 'Exact correct / scored', 'Exact accuracy', 'Cluster accuracy'], rows));
    content.append(element('p', 'Only scorable references contribute to accuracy. Cluster accuracy accepts any correct pose in the selected cluster.', 'muted'));
  }
  content.append(element('h2', 'Structures and poses'));
  for (const item of artifact.items) {
    const details = element('details'); details.append(element('summary', `${item.target_id} · ${item.choices.length} poses${item.evaluation_status === 'unscorable' ? ' · Unscorable reference' : ''}`));
    if (item.reference_disposition) {
      const d = item.reference_disposition;
      details.append(element('p', `${d.observed_heavy_atoms} of ${d.expected_heavy_atoms} ligand heavy atoms were observed in the released reference, below the ${d.minimum_reference_coverage * 100}% coverage requirement. All poses remain in this archive with no assigned score.`, 'note'));
    }
    details.append(table('Pose evaluation', ['Method', 'Version', 'RMSD (Å)', 'Accepted'], item.choices.map(c => [c.method, c.method_version, c.rmsd == null ? 'Unscorable' : c.rmsd.toFixed(3), c.accepted_correct == null ? 'Not scored' : c.accepted_correct ? 'Yes' : 'No'])));
    content.append(details);
  }
}
async function renderList(offset = 0) {
  const result = await request(new URLSearchParams({ offset: String(offset) }));
  if (!result.items.length && !offset) { content.append(element('p', 'No historical research weeks have been published yet.')); return; }
  const cards = element('div', null, 'cards');
  result.items.forEach(row => {
    const card = element('article', null, 'card'), link = element('a', row.round_id.replace(/^preview-weekly-/, '').replace(/-nextweekly-.*/, ''));
    link.href = `?${new URLSearchParams({ round_id: row.round_id })}`;
    card.append(link, element('p', `${row.item_count} questions · ${row.choice_count} poses · ${row.benchmark_count} model runs`, 'muted'), element('p', `Closed ${date(row.closes_at)}`, 'muted')); cards.append(card);
  }); content.append(cards);
  if (result.next_offset != null) {
    const more = element('button', 'Load older weeks'); more.type = 'button';
    more.addEventListener('click', async () => { more.disabled = true; try { await renderList(result.next_offset); more.remove(); } catch { more.disabled = false; status.textContent = 'Could not load older research. Please try again.'; } }); content.append(more);
  }
}
try {
  if (roundId) renderDetail(await request(new URLSearchParams({ round_id: roundId })));
  else await renderList();
  status.textContent = '';
} catch { status.textContent = 'This research archive is unavailable. Please try again later.'; }
