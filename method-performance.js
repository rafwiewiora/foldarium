const METHOD_NAMES = {
  boltz2: 'Boltz-2',
  openfold3: 'OpenFold3',
};

export function methodName(method) {
  return METHOD_NAMES[method] || method;
}

export function scoreMethodPoses(poses) {
  const candidates = Array.isArray(poses) ? poses.filter(Boolean) : [];
  const ranked = candidates
    .filter(pose => (
      pose.confidence?.metric === 'ligand_plddt'
      && Number.isFinite(pose.confidence.value)
    ))
    .sort((left, right) => (
      right.confidence.value - left.confidence.value
      || String(left.id).localeCompare(String(right.id))
    ));
  const top = ranked[0] || null;
  return {
    oracle_success: candidates.some(pose => pose.correct === true),
    top1_success: top ? top.correct === true : null,
    top1_choice_id: top?.id || null,
    top1_plddt: top?.confidence.value ?? null,
  };
}

export function validateMethodStats(data) {
  if (data?.schema_version !== 1 || !Array.isArray(data.weeks)) {
    throw new Error('Method performance data is unavailable.');
  }
  const seen = new Set();
  const rows = data.weeks.map(row => {
    const key = `${row?.week}|${row?.method}`;
    const counts = [
      row?.targets,
      row?.oracle_successes,
      row?.top1_evaluated,
      row?.top1_successes,
    ];
    if (!/^\d{4}-\d{2}-\d{2}$/.test(row?.week)
        || typeof row?.method !== 'string'
        || !row.method
        || counts.some(value => !Number.isInteger(value) || value < 0)
        || row.oracle_successes > row.targets
        || row.top1_evaluated > row.targets
        || row.top1_successes > row.top1_evaluated
        || seen.has(key)) {
      throw new Error('Method performance data is invalid.');
    }
    seen.add(key);
    return { ...row };
  });
  return rows.sort((left, right) => (
    left.week.localeCompare(right.week) || left.method.localeCompare(right.method)
  ));
}

export function successRate(successes, evaluated) {
  return evaluated ? (successes / evaluated) * 100 : null;
}

export function aggregateMethodStats(data) {
  const totals = new Map();
  for (const row of validateMethodStats(data)) {
    const total = totals.get(row.method) || {
      method: row.method,
      targets: 0,
      oracle_successes: 0,
      top1_evaluated: 0,
      top1_successes: 0,
    };
    total.targets += row.targets;
    total.oracle_successes += row.oracle_successes;
    total.top1_evaluated += row.top1_evaluated;
    total.top1_successes += row.top1_successes;
    totals.set(row.method, total);
  }
  return [...totals.values()].map(total => ({
    ...total,
    oracle_rate: successRate(total.oracle_successes, total.targets),
    top1_rate: successRate(total.top1_successes, total.top1_evaluated),
  })).sort((left, right) => (
    (right.oracle_rate ?? -1) - (left.oracle_rate ?? -1)
    || (right.top1_rate ?? -1) - (left.top1_rate ?? -1)
    || left.method.localeCompare(right.method)
  ));
}

export function methodTrend(data, method) {
  return validateMethodStats(data)
    .filter(row => row.method === method)
    .map(row => ({
      ...row,
      oracle_rate: successRate(row.oracle_successes, row.targets),
      top1_rate: successRate(row.top1_successes, row.top1_evaluated),
    }));
}
