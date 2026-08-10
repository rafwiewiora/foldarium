export const GRID_PAGE_SIZE = 9;

export function gridPageCount(total, pageSize = GRID_PAGE_SIZE) {
  const count = Number.isFinite(total) ? Math.max(0, Math.floor(total)) : 0;
  return Math.max(1, Math.ceil(count / pageSize));
}

export function gridPage(entries, pageIndex, pageSize = GRID_PAGE_SIZE) {
  const pages = gridPageCount(entries.length, pageSize);
  const index = Math.min(Math.max(0, Math.floor(pageIndex) || 0), pages - 1);
  return {
    index,
    pages,
    entries: entries.slice(index * pageSize, (index + 1) * pageSize),
  };
}

export function formatReleaseCountdown(closesAt, now = Date.now()) {
  const target = Date.parse(closesAt || '');
  if (!Number.isFinite(target)) return 'Results Wednesday.';
  const remaining = Math.max(0, target - now);
  if (!remaining) return 'Voting closed · results processing.';
  const minutes = Math.ceil(remaining / 60_000);
  const days = Math.floor(minutes / (24 * 60));
  const hours = Math.floor((minutes % (24 * 60)) / 60);
  const mins = minutes % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours || days) parts.push(`${hours}h`);
  if (!days && mins) parts.push(`${mins}m`);
  return `Results Wednesday · voting closes in ${parts.join(' ')}`;
}

export function reviewChoiceIds(choice, cluster, clustered) {
  const members = clustered ? (cluster?.members || [choice]) : [choice];
  return members.map(member => String(
    member?._weeklyChoiceId || member?.pose_file || member?.label || '',
  )).filter(Boolean);
}

export function rejectedState(rejectedIds, choice, cluster, clustered) {
  const ids = reviewChoiceIds(choice, cluster, clustered);
  return ids.length > 0 && ids.every(id => rejectedIds.has(id));
}

if (typeof window !== 'undefined') {
  window.foldariumDev2Feedback = Object.freeze({
    GRID_PAGE_SIZE,
    gridPageCount,
    gridPage,
    formatReleaseCountdown,
    reviewChoiceIds,
    rejectedState,
  });
}
