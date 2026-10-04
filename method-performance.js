import { isUnscorableReference } from './lib/reference-disposition.js';

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

// Callers must verify that each manifest pair belongs to a published, revealed
// production round before passing it here. Only aggregate counts are returned.
export function buildPublishedMethodStats(contexts) {
  const totals = new Map();
  const rounds = new Set();
  for (const { blindProjection: blind, revealProjection: reveal } of contexts) {
    if (!blind?.round_id || blind.round_id !== reveal?.round_id
        || rounds.has(blind.round_id)
        || !Array.isArray(blind.items) || !Array.isArray(reveal.items)
        || blind.items.length !== reveal.items.length) {
      throw new Error('Published method performance manifests are invalid.');
    }
    rounds.add(blind.round_id);
    const revealItems = uniqueById(reveal.items);
    uniqueById(blind.items);
    for (const item of blind.items) {
      const answer = revealItems.get(item.id);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(item.week)
          || !Array.isArray(item.choices) || !item.choices.length
          || !Array.isArray(answer?.choices)
          || item.choices.length !== answer.choices.length) {
        throw new Error('Published method performance target is invalid.');
      }
      const answers = uniqueById(answer.choices);
      const unscorable = isUnscorableReference(answer, { publicProjection: true });
      uniqueById(item.choices);
      const methods = new Map();
      for (const choice of item.choices) {
        const pose = answers.get(choice.id);
        if (typeof choice.method !== 'string' || !choice.method
            || !pose || (!unscorable && (!Number.isFinite(pose.rmsd) || pose.rmsd < 0
            || typeof pose.correct !== 'boolean'
            || pose.correct !== (pose.rmsd < 1.5)))) {
          throw new Error('Published raw pose correctness is invalid.');
        }
        const poses = methods.get(choice.method) || [];
        poses.push({ id: choice.id, confidence: choice.confidence, correct: pose.rmsd < 1.5 });
        methods.set(choice.method, poses);
      }
      for (const [method, poses] of methods) {
        const key = JSON.stringify([item.week, method]);
        const row = totals.get(key) || {
          week: item.week, method, targets: 0, oracle_successes: 0,
          top1_evaluated: 0, top1_successes: 0,
        };
        if (unscorable) {
          row.excluded_targets = (row.excluded_targets || 0) + 1;
          totals.set(key, row);
          continue;
        }
        const score = scoreMethodPoses(poses);
        row.targets += 1;
        row.oracle_successes += Number(score.oracle_success);
        row.top1_evaluated += Number(score.top1_success !== null);
        row.top1_successes += Number(score.top1_success === true);
        totals.set(key, row);
      }
    }
  }
  return {
    schema_version: 1,
    population: 'published_weekly_quiz',
    publication_count: rounds.size,
    metric: {
      correctness: 'Raw pose RMSD < 1.5 A',
      oracle: 'Any raw-correct pose produced by the method',
      top1: 'Highest ligand pLDDT pose; choice ID ascending breaks ties',
      missing_confidence: 'Excluded from the top-1 denominator',
      target: 'One published quiz question with at least one pose from the method',
    },
    weeks: validateMethodStats({ schema_version: 1, weeks: [...totals.values()] }),
  };
}

function uniqueById(values) {
  const result = new Map();
  for (const value of values) {
    if (typeof value?.id !== 'string' || !value.id || result.has(value.id)) {
      throw new Error('Published method performance identity is invalid.');
    }
    result.set(value.id, value);
  }
  return result;
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
        || (Object.hasOwn(row, 'excluded_targets') && (!Number.isSafeInteger(row.excluded_targets) || row.excluded_targets < 0))
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
    if (Object.hasOwn(row, 'excluded_targets')) total.excluded_targets = (total.excluded_targets || 0) + row.excluded_targets;
    totals.set(row.method, total);
  }
  return [...totals.values()].map(total => ({
    ...total,
    ...(Object.hasOwn(total, 'excluded_targets') ? { full_targets: total.targets + total.excluded_targets } : {}),
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
