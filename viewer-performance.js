function rounded(value) {
  return Math.round(value * 10) / 10;
}

function formatSeconds(value) {
  return Number.isFinite(value) ? `${(value / 1000).toFixed(2)} s` : 'waiting';
}

export function createViewerPerformanceOverlay({
  documentImpl = globalThis.document,
  clock = () => globalThis.performance?.now?.() ?? Date.now(),
  setIntervalImpl = globalThis.setInterval?.bind(globalThis),
  clearIntervalImpl = globalThis.clearInterval?.bind(globalThis),
} = {}) {
  if (!documentImpl?.body || !setIntervalImpl || !clearIntervalImpl) {
    return { begin: () => {}, milestone: () => {}, finish: () => {} };
  }

  const root = documentImpl.createElement('aside');
  root.id = 'foldarium-performance-clock';
  root.setAttribute('aria-label', 'Viewer loading diagnostics');
  Object.assign(root.style, {
    position: 'fixed',
    top: '12px',
    right: '12px',
    zIndex: '10000',
    minWidth: '205px',
    padding: '11px 12px',
    border: '1px solid rgba(255,255,255,.25)',
    borderRadius: '9px',
    background: 'rgba(14,20,27,.9)',
    boxShadow: '0 5px 18px rgba(0,0,0,.24)',
    color: '#fff',
    font: '600 12px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace',
    fontVariantNumeric: 'tabular-nums',
    pointerEvents: 'none',
  });

  const title = documentImpl.createElement('div');
  title.textContent = 'VIEWER LOAD CLOCK';
  Object.assign(title.style, {
    marginBottom: '3px',
    color: '#9bdcf5',
    fontSize: '10px',
    letterSpacing: '.08em',
  });
  const total = documentImpl.createElement('div');
  total.textContent = '0.00 s';
  Object.assign(total.style, {
    marginBottom: '8px',
    fontSize: '24px',
    lineHeight: '1.1',
  });
  const status = documentImpl.createElement('div');
  status.textContent = 'Click Start to measure';
  Object.assign(status.style, {
    marginBottom: '8px',
    color: '#d7e2ea',
    fontSize: '10px',
  });
  const rows = new Map([
    ['first-grid-card-ready', ['First card', documentImpl.createElement('span')]],
    ['all-grid-cards-ready', ['Full Grid', documentImpl.createElement('span')]],
    ['question-ready', ['Interactive', documentImpl.createElement('span')]],
  ]);
  for (const [, [label, value]] of rows) {
    const row = documentImpl.createElement('div');
    Object.assign(row.style, {
      display: 'flex',
      justifyContent: 'space-between',
      gap: '16px',
      paddingTop: '3px',
      color: '#c6d0d8',
    });
    const name = documentImpl.createElement('span');
    name.textContent = label;
    value.textContent = 'waiting';
    value.style.color = '#fff';
    row.append(name, value);
    root.appendChild(row);
  }
  root.prepend(title, total, status);
  documentImpl.body.appendChild(root);

  let startedAt = null;
  let interval = null;

  function stopClock() {
    if (interval !== null) clearIntervalImpl(interval);
    interval = null;
  }

  function renderElapsed() {
    if (startedAt !== null) total.textContent = formatSeconds(Math.max(0, clock() - startedAt));
  }

  return {
    begin(report) {
      stopClock();
      startedAt = clock();
      status.textContent = Number.isInteger(report?.metadata?.questionIndex)
        ? `Loading question ${report.metadata.questionIndex + 1}`
        : 'Loading question';
      for (const [, [, value]] of rows) value.textContent = 'waiting';
      total.textContent = '0.00 s';
      interval = setIntervalImpl(renderElapsed, 50);
    },
    milestone(_report, name, elapsedMs) {
      const row = rows.get(name);
      if (row) row[1].textContent = formatSeconds(elapsedMs);
      renderElapsed();
    },
    finish(report) {
      stopClock();
      startedAt = null;
      total.textContent = formatSeconds(report?.totalMs);
      status.textContent = report?.metadata?.status === 'failed' ? 'Load failed' : 'Load complete';
    },
  };
}

export function createViewerPerformanceReporter({
  enabled = false,
  clock = () => globalThis.performance?.now?.() ?? Date.now(),
  performanceImpl = globalThis.performance,
  observer = null,
  logger = report => console.info('FOLDARIUM_PERFORMANCE', {
    id: report.id,
    stage: report.stage,
    metadata: report.metadata,
    totalMs: report.totalMs,
    milestones: report.milestones,
    stageSummary: report.stageSummary,
  }),
} = {}) {
  const reports = [];
  let active = null;
  let sequence = 0;

  function mark(name) {
    if (!enabled) return;
    try { performanceImpl?.mark?.(name); } catch (_) {}
  }

  function beginQuestion(metadata = {}) {
    if (!enabled) return null;
    const id = `question-${++sequence}`;
    const report = {
      id,
      metadata: { ...metadata },
      startedAt: clock(),
      stages: [],
      milestones: {},
      completed: false,
    };
    active = report;
    mark(`foldarium:${id}:start`);
    try { observer?.begin?.(report); } catch (_) {}
    return report;
  }

  async function measure(report, stage, operation, details = {}) {
    if (!enabled || !report) return operation();
    const measurementId = `${report.id}:${stage}:${report.stages.length + 1}`;
    const startedAt = clock();
    mark(`foldarium:${measurementId}:start`);
    try {
      return await operation();
    } finally {
      const durationMs = Math.max(0, clock() - startedAt);
      report.stages.push({ stage, durationMs, details: { ...details } });
      mark(`foldarium:${measurementId}:end`);
      try {
        performanceImpl?.measure?.(
          `foldarium:${measurementId}`,
          `foldarium:${measurementId}:start`,
          `foldarium:${measurementId}:end`,
        );
      } catch (_) {}
    }
  }

  function milestone(report, name, details = {}) {
    if (!enabled || !report || report.milestones[name]) return;
    const elapsedMs = Math.max(0, clock() - report.startedAt);
    report.milestones[name] = {
      elapsedMs,
      details: { ...details },
    };
    mark(`foldarium:${report.id}:${name}`);
    try { observer?.milestone?.(report, name, elapsedMs); } catch (_) {}
  }

  function finishQuestion(report, metadata = {}) {
    if (!enabled || !report || report.completed) return null;
    report.completed = true;
    report.metadata = { ...report.metadata, ...metadata };
    report.totalMs = Math.max(0, clock() - report.startedAt);
    const stageSummary = {};
    for (const row of report.stages) {
      const summary = stageSummary[row.stage] ||= { count: 0, totalMs: 0, maxMs: 0 };
      summary.count += 1;
      summary.totalMs += row.durationMs;
      summary.maxMs = Math.max(summary.maxMs, row.durationMs);
    }
    report.stageSummary = Object.fromEntries(Object.entries(stageSummary).map(([stage, summary]) => [
      stage,
      {
        count: summary.count,
        aggregateMs: rounded(summary.totalMs),
        maxMs: rounded(summary.maxMs),
      },
    ]));
    report.totalMs = rounded(report.totalMs);
    report.milestones = Object.fromEntries(Object.entries(report.milestones).map(([name, row]) => [
      name,
      { ...row, elapsedMs: rounded(row.elapsedMs) },
    ]));
    delete report.startedAt;
    reports.push(report);
    mark(`foldarium:${report.id}:complete`);
    logger(report);
    try { observer?.finish?.(report); } catch (_) {}
    if (active === report) active = null;
    return report;
  }

  async function measureStartup(stage, operation, metadata = {}) {
    if (!enabled) return operation();
    const startedAt = clock();
    try {
      return await operation();
    } finally {
      const report = {
        id: `startup-${++sequence}`,
        stage,
        totalMs: rounded(Math.max(0, clock() - startedAt)),
      };
      if (Object.keys(metadata).length) report.metadata = { ...metadata };
      reports.push(report);
      logger(report);
    }
  }

  return {
    enabled,
    reports,
    current: () => active,
    beginQuestion,
    measure,
    milestone,
    finishQuestion,
    measureStartup,
  };
}
