const DIAGNOSTICS_SCHEMA = 'foldarium.viewer-performance-diagnostics/v1';
const STRUCTURE_PATH = '/storage/v1/object/public/';

function rounded(value, precision = 1) {
  if (!Number.isFinite(value)) return null;
  const scale = 10 ** precision;
  return Math.round(value * scale) / scale;
}

function bucket(value, step, maximum = Infinity) {
  if (!Number.isFinite(value) || value < 0) return null;
  return Math.min(maximum, Math.round(value / step) * step);
}

function browserFamily(userAgent = '') {
  if (/Edg\//.test(userAgent)) return 'Edge';
  if (/OPR\//.test(userAgent)) return 'Opera';
  if (/Firefox\//.test(userAgent)) return 'Firefox';
  if (/CriOS\//.test(userAgent)) return 'Chrome iOS';
  if (/Chrome\//.test(userAgent)) return 'Chrome';
  if (/Safari\//.test(userAgent)) return 'Safari';
  return 'Other';
}

function operatingSystemFamily(userAgent = '') {
  if (/CrOS/.test(userAgent)) return 'ChromeOS';
  if (/Android/.test(userAgent)) return 'Android';
  if (/iPhone|iPad|iPod/.test(userAgent)) return 'iOS';
  if (/Windows/.test(userAgent)) return 'Windows';
  if (/Mac OS X|Macintosh/.test(userAgent)) return 'macOS';
  if (/Linux/.test(userAgent)) return 'Linux';
  return 'Other';
}

function graphicsCapabilities(gl) {
  if (!gl?.getParameter) return { available: false };
  const capabilities = {
    available: true,
    webgl2: typeof gl.texStorage2D === 'function',
    max_texture_size: Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || null,
    max_renderbuffer_size: Number(gl.getParameter(gl.MAX_RENDERBUFFER_SIZE)) || null,
  };
  return capabilities;
}

export function collectSafeBrowserDiagnostics({
  navigatorImpl = globalThis.navigator,
  windowImpl = globalThis.window,
  documentImpl = globalThis.document,
  performanceImpl = globalThis.performance,
  gl = null,
} = {}) {
  const connection = navigatorImpl?.connection
    || navigatorImpl?.mozConnection
    || navigatorImpl?.webkitConnection;
  const screen = windowImpl?.screen;
  const viewport = windowImpl?.visualViewport;
  return {
    browser_family: browserFamily(navigatorImpl?.userAgent),
    os_family: operatingSystemFamily(navigatorImpl?.userAgent),
    mobile: navigatorImpl?.userAgentData?.mobile === true,
    viewport: {
      width: bucket(viewport?.width ?? windowImpl?.innerWidth, 10, 10_000),
      height: bucket(viewport?.height ?? windowImpl?.innerHeight, 10, 10_000),
      device_pixel_ratio: rounded(windowImpl?.devicePixelRatio),
    },
    screen: {
      width_bucket: bucket(screen?.width, 100, 10_000),
      height_bucket: bucket(screen?.height, 100, 10_000),
      color_depth: bucket(screen?.colorDepth, 8, 64),
    },
    hardware: {
      logical_processors: bucket(navigatorImpl?.hardwareConcurrency, 2, 64),
      device_memory_gb: bucket(navigatorImpl?.deviceMemory, 1, 64),
      touch_points: bucket(navigatorImpl?.maxTouchPoints, 2, 20),
      js_heap_limit_mb: bucket(
        performanceImpl?.memory?.jsHeapSizeLimit / (1024 * 1024),
        128,
        16_384,
      ),
    },
    connection: {
      effective_type: typeof connection?.effectiveType === 'string'
        ? connection.effectiveType.slice(0, 16)
        : null,
      downlink_mbps: bucket(connection?.downlink, 0.5, 10_000),
      rtt_ms: bucket(connection?.rtt, 50, 60_000),
      save_data: connection?.saveData === true,
      online: navigatorImpl?.onLine !== false,
    },
    graphics: graphicsCapabilities(gl),
    preferences: {
      reduced_motion: windowImpl?.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true,
      visibility: typeof documentImpl?.visibilityState === 'string'
        ? documentImpl.visibilityState
        : null,
    },
  };
}

export function summarizeStructureResources(entries = []) {
  const structures = entries.filter(entry => (
    typeof entry?.name === 'string' && entry.name.includes(STRUCTURE_PATH)
  ));
  const sum = key => structures.reduce((total, entry) => (
    total + (Number.isFinite(entry?.[key]) ? Math.max(0, entry[key]) : 0)
  ), 0);
  return {
    request_count: structures.length,
    cache_hit_count: structures.filter(entry => (
      entry.transferSize === 0 && entry.decodedBodySize > 0
    )).length,
    transfer_bytes: Math.round(sum('transferSize')),
    encoded_body_bytes: Math.round(sum('encodedBodySize')),
    decoded_body_bytes: Math.round(sum('decodedBodySize')),
    aggregate_duration_ms: rounded(sum('duration')),
    max_duration_ms: rounded(Math.max(0, ...structures.map(entry => (
      Number.isFinite(entry?.duration) ? entry.duration : 0
    )))),
  };
}

function normalizedQuestionReport(report) {
  const metadata = report?.metadata || {};
  return {
    report_id: typeof report?.id === 'string' ? report.id.slice(0, 64) : null,
    item_id: typeof metadata.itemId === 'string' ? metadata.itemId.slice(0, 128) : null,
    question_index: Number.isInteger(metadata.questionIndex) ? metadata.questionIndex : null,
    requested_mode: typeof metadata.requestedMode === 'string'
      ? metadata.requestedMode.slice(0, 24)
      : null,
    clustered: metadata.clustered === true,
    includes_start: metadata.includesStart === true,
    status: typeof metadata.status === 'string' ? metadata.status.slice(0, 24) : null,
    final_mode: typeof metadata.mode === 'string' ? metadata.mode.slice(0, 24) : null,
    grid_cards: Number.isInteger(metadata.gridCards) ? metadata.gridCards : null,
    viewer_pool_enabled: metadata.viewerPoolEnabled === true,
    fast_grid_camera_sync_enabled: metadata.fastGridCameraSyncEnabled === true,
    grid_viewer_prewarm_enabled: metadata.gridViewerPrewarmEnabled === true,
    grid_viewers_reused: Number.isInteger(metadata.gridViewersReused)
      ? metadata.gridViewersReused
      : null,
    grid_viewers_prewarmed: Number.isInteger(metadata.gridViewersPrewarmed)
      ? metadata.gridViewersPrewarmed
      : null,
    grid_viewers_recycled: Number.isInteger(metadata.gridViewersRecycled)
      ? metadata.gridViewersRecycled
      : null,
    grid_viewers_created: Number.isInteger(metadata.gridViewersCreated)
      ? metadata.gridViewersCreated
      : null,
    grid_viewer_pool_size: Number.isInteger(metadata.gridViewerPoolSize)
      ? metadata.gridViewerPoolSize
      : null,
    total_ms: rounded(report?.totalMs),
    milestones: Object.fromEntries(Object.entries(report?.milestones || {}).map(([name, row]) => [
      name.slice(0, 64),
      rounded(row?.elapsedMs),
    ])),
    stages: Object.fromEntries(Object.entries(report?.stageSummary || {}).map(([name, row]) => [
      name.slice(0, 64),
      {
        count: Number.isInteger(row?.count) ? row.count : null,
        aggregate_ms: rounded(row?.aggregateMs),
        max_ms: rounded(row?.maxMs),
      },
    ])),
  };
}

function normalizedStartupReports(reports = []) {
  return reports.filter(report => typeof report?.stage === 'string').map(report => ({
    stage: report.stage.slice(0, 64),
    total_ms: rounded(report.totalMs),
  })).slice(0, 12);
}

export function createPerformanceDiagnosticsCollector({
  performanceImpl = globalThis.performance,
  now = () => new Date().toISOString(),
} = {}) {
  let resourceIndex = 0;
  let startupCaptured = false;
  return {
    capture(report, { startupReports = [], gl = null } = {}) {
      const resources = performanceImpl?.getEntriesByType?.('resource') || [];
      const newResources = resources.slice(resourceIndex);
      resourceIndex = resources.length;
      const payload = {
        schema_version: DIAGNOSTICS_SCHEMA,
        captured_at: now(),
        consent: 'explicit-beta-checkbox',
        setup: collectSafeBrowserDiagnostics({ performanceImpl, gl }),
        question: normalizedQuestionReport(report),
        structures: summarizeStructureResources(newResources),
      };
      if (!startupCaptured) {
        payload.startup = normalizedStartupReports(startupReports);
        startupCaptured = true;
      }
      return payload;
    },
  };
}
