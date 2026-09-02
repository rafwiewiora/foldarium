import test from 'node:test';
import assert from 'node:assert/strict';
import {
  collectSafeBrowserDiagnostics,
  createPerformanceDiagnosticsCollector,
  summarizeStructureResources,
} from '../performance-diagnostics.js';

test('collects useful coarse capabilities without retaining a raw browser fingerprint', () => {
  const diagnostics = collectSafeBrowserDiagnostics({
    navigatorImpl: {
      userAgent: 'Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36',
      userAgentData: { mobile: false },
      hardwareConcurrency: 11,
      deviceMemory: 8,
      maxTouchPoints: 0,
      onLine: true,
      connection: { effectiveType: '4g', downlink: 12.3, rtt: 83, saveData: false },
    },
    windowImpl: {
      innerWidth: 1437,
      innerHeight: 891,
      devicePixelRatio: 1.998,
      screen: { width: 1512, height: 982, colorDepth: 30 },
      matchMedia: () => ({ matches: false }),
    },
    documentImpl: { visibilityState: 'visible' },
    performanceImpl: { memory: { jsHeapSizeLimit: 3_900 * 1024 * 1024 } },
    gl: {
      MAX_TEXTURE_SIZE: 1,
      MAX_RENDERBUFFER_SIZE: 2,
      texStorage2D() {},
      getParameter: parameter => (parameter === 1 ? 16_384 : 8_192),
    },
  });

  assert.equal(diagnostics.browser_family, 'Chrome');
  assert.equal(diagnostics.os_family, 'macOS');
  assert.deepEqual(diagnostics.viewport, {
    width: 1440,
    height: 890,
    device_pixel_ratio: 2,
  });
  assert.equal(diagnostics.hardware.logical_processors, 12);
  assert.equal(diagnostics.connection.downlink_mbps, 12.5);
  assert.equal(diagnostics.connection.rtt_ms, 100);
  assert.equal(diagnostics.graphics.webgl2, true);
  const serialized = JSON.stringify(diagnostics);
  assert.doesNotMatch(serialized, /Mozilla|140\.0\.0\.0|AppleWebKit|plugins|fonts|vendor|renderer/i);
});

test('summarizes structure transfers without retaining asset URLs', () => {
  const summary = summarizeStructureResources([
    {
      name: 'https://storage.test/storage/v1/object/public/structures/a',
      transferSize: 120,
      encodedBodySize: 100,
      decodedBodySize: 200,
      duration: 40.25,
    },
    {
      name: 'https://storage.test/storage/v1/object/public/structures/b',
      transferSize: 0,
      encodedBodySize: 80,
      decodedBodySize: 160,
      duration: 20,
    },
    { name: 'https://cdn.test/font.woff2', transferSize: 900, duration: 5 },
  ]);

  assert.deepEqual(summary, {
    request_count: 2,
    cache_hit_count: 1,
    transfer_bytes: 120,
    encoded_body_bytes: 180,
    decoded_body_bytes: 360,
    aggregate_duration_ms: 60.3,
    max_duration_ms: 40.3,
  });
  assert.doesNotMatch(JSON.stringify(summary), /storage\.test|structures\/a/);
});

test('collector records startup once and only newly observed resource timings', () => {
  const entries = [{
    name: 'https://storage.test/storage/v1/object/public/structures/a',
    transferSize: 120,
    encodedBodySize: 100,
    decodedBodySize: 200,
    duration: 40,
  }];
  const collector = createPerformanceDiagnosticsCollector({
    performanceImpl: {
      getEntriesByType: () => entries,
    },
    now: () => '2026-09-02T00:00:00.000Z',
  });
  const report = {
    id: 'question-2',
    metadata: {
      itemId: '11HZ',
      questionIndex: 0,
      requestedMode: 'grid',
      clustered: true,
      includesStart: true,
      status: 'ready',
      mode: 'grid',
      gridCards: 9,
      viewerPoolEnabled: true,
      fastGridCameraSyncEnabled: true,
      gridViewerPrewarmEnabled: true,
      gridViewersReused: 9,
      gridViewersPrewarmed: 0,
      gridViewersRecycled: 9,
      gridViewersCreated: 0,
      gridViewerPoolSize: 0,
    },
    totalMs: 2800,
    milestones: { 'first-grid-card-ready': { elapsedMs: 1800 } },
    stageSummary: {
      'trajectory-parse': { count: 28, aggregateMs: 7600, maxMs: 500 },
    },
  };

  const first = collector.capture(report, {
    startupReports: [{ id: 'startup-1', stage: 'weekly-round-rpc', totalMs: 600 }],
  });
  assert.equal(first.schema_version, 'foldarium.viewer-performance-diagnostics/v1');
  assert.equal(first.consent, 'explicit-beta-checkbox');
  assert.deepEqual(first.startup, [{ stage: 'weekly-round-rpc', total_ms: 600 }]);
  assert.equal(first.question.milestones['first-grid-card-ready'], 1800);
  assert.equal(first.question.stages['trajectory-parse'].count, 28);
  assert.equal(first.question.includes_start, true);
  assert.equal(first.question.viewer_pool_enabled, true);
  assert.equal(first.question.fast_grid_camera_sync_enabled, true);
  assert.equal(first.question.grid_viewer_prewarm_enabled, true);
  assert.equal(first.question.grid_viewers_reused, 9);
  assert.equal(first.question.grid_viewers_prewarmed, 0);
  assert.equal(first.question.grid_viewers_recycled, 9);
  assert.equal(first.question.grid_viewers_created, 0);
  assert.equal(first.question.grid_viewer_pool_size, 0);
  assert.equal(first.structures.request_count, 1);

  entries.push({
    name: 'https://storage.test/storage/v1/object/public/structures/b',
    transferSize: 80,
    encodedBodySize: 70,
    decodedBodySize: 140,
    duration: 15,
  });
  const second = collector.capture(report, {
    startupReports: [{ stage: 'weekly-round-rpc', totalMs: 600 }],
  });
  assert.equal(second.startup, undefined);
  assert.equal(second.structures.request_count, 1);
  assert.equal(second.structures.transfer_bytes, 80);
});
