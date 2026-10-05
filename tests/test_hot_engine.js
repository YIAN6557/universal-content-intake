const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const context = vm.createContext({ console, Date, Math, Set, Map, Number, String, Array, Object, JSON, Error });
for (const file of ['monitor.gs', 'discovery.gs', 'stats.gs', 'scoring.gs']) {
  const fullPath = path.join(scriptPath, file);
  vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
}

function validConfig(overrides = {}) {
  return {
    snapshot_stage_minutes: '30,60,120',
    warm_baseline_min_complete_watch: 10,
    cold_start_final_views_history_window: 20,
    cold_start_checkpoint_30_ratio: 0.025,
    cold_start_checkpoint_60_ratio: 0.05,
    cold_start_checkpoint_120_ratio: 0.10,
    cold_start_like_rate_multiplier: 0.80,
    acceleration_min_snapshot_index: 2,
    warm_baseline_relative_velocity_threshold: 1.50,
    warm_baseline_like_rate_multiplier: 0.80,
    normal_after_minutes: 120,
    ...overrides,
  };
}

function evaluate(input, config = validConfig()) {
  return context.stage6EvaluateHot_(input, config);
}

function coldInput(checkpoint, overrides = {}) {
  return {
    video_id: 'cold-video',
    creator_id: 'creator-1',
    complete_watch_count: 0,
    checkpoint_minutes: checkpoint,
    actual_elapsed_seconds: checkpoint * 60 + 17,
    current_view_count: 10_000,
    current_like_rate: 0.08,
    acceleration: checkpoint === 30 ? null : 1,
    baseline_final_views_median: 10_000,
    historical_median_like_rate: 0.10,
    ...overrides,
  };
}

function warmInput(overrides = {}) {
  return {
    video_id: 'warm-video',
    creator_id: 'creator-1',
    complete_watch_count: 10,
    checkpoint_minutes: 60,
    actual_elapsed_seconds: 3_617,
    current_view_count: 1_500,
    current_like_rate: 0.08,
    acceleration: 1,
    historical_same_checkpoint_median_views: 1_000,
    historical_same_checkpoint_median_like_rate: 0.10,
    ...overrides,
  };
}

test('Cold/Warm boundary reads its threshold from validated Config', () => {
  const config = context.stage6ReadHotConfig_(validConfig());
  assert.equal(context.stage6HotPhase_(0, config.completeWatchThreshold), 'COLD_START');
  assert.equal(context.stage6HotPhase_(9, config.completeWatchThreshold), 'COLD_START');
  assert.equal(context.stage6HotPhase_(10, config.completeWatchThreshold), 'WARM_BASELINE');
  assert.throws(() => context.stage6HotPhase_(null, config.completeWatchThreshold), /complete WATCH count/i);
});

test('Stage 6 Config seeds the formal HOT defaults in the existing key/value table', () => {
  const seeded = Object.fromEntries(context.stage6ConfigDefaults_().map((row) => [row[0], row[1]]));
  const config = context.stage6ReadHotConfig_(seeded);
  assert.equal(config.completeWatchThreshold, 10);
  assert.equal(config.finalViewsHistoryWindow, 20);
  assert.deepEqual(JSON.parse(JSON.stringify(config.snapshotCheckpoints)), [30, 60, 120]);
  assert.deepEqual(JSON.parse(JSON.stringify(config.checkpointRatios)), { 30: 0.025, 60: 0.05, 120: 0.1 });
  assert.equal(config.coldLikeRateMultiplier, 0.8);
  assert.equal(config.accelerationMinSnapshotIndex, 2);
  assert.equal(config.warmRelativeVelocityThreshold, 1.5);
  assert.equal(config.warmLikeRateMultiplier, 0.8);
  assert.equal(config.normalAfterMinutes, 120);
});

test('schema growth appends Stage 6 fields without rewriting existing columns', () => {
  const originalHeaders = ['video_id', 'channel_id', 'lifecycle_state'];
  const nextHeaders = originalHeaders.concat(['data_hot_at', 'hot_mode', 'hot_checkpoint']);
  const writes = [];
  const sheet = {
    getLastRow: () => 4,
    getLastColumn: () => originalHeaders.length,
    getRange: (row, column) => row === 1 && column === 1
      ? { getValues: () => [originalHeaders] }
      : { setValues: (values) => writes.push({ row, column, values }) },
  };
  context.stage6EnsureHeaders_(sheet, nextHeaders);
  assert.deepEqual(JSON.parse(JSON.stringify(writes)), [{ row: 1, column: 4, values: [['data_hot_at', 'hot_mode', 'hot_checkpoint']] }]);
});

test('Cold baseline uses exactly the latest configured history window and does not guess missing history', () => {
  const originalReadTable = context.stage6ReadTable_;
  const headers = ['creator_name', 'channel_id', 'video_id', 'published_at', 'view_count', 'like_rate'];
  const rows = Array.from({ length: 21 }, (_, index) => [
    'Creator', 'creator-1', `v${index}`,
    new Date(Date.UTC(2026, 0, index + 1)).toISOString(),
    index + 1, (index + 1) / 100,
  ]);
  context.stage6ReadTable_ = () => ({ headers, rows });
  try {
    const recent20 = context.stage6CreatorBaseline_('creator-1', 20);
    assert.equal(recent20.history_complete, true);
    assert.equal(recent20.sample_count, 20);
    assert.equal(recent20.final_views_median, 11.5);
    assert.ok(Math.abs(recent20.historical_median_like_rate - 0.115) < 1e-12);
    const missingHistory = context.stage6CreatorBaseline_('creator-1', 22);
    assert.equal(missingHistory.history_complete, false);
    assert.equal(missingHistory.selected_history_count, 21);
    assert.equal(missingHistory.sample_count, 21);
    assert.equal(missingHistory.final_views_median, 11);
  } finally {
    context.stage6ReadTable_ = originalReadTable;
  }
});

test('Cold baseline accepts fewer than twenty historical videos and uses valid Like Rates only', () => {
  const originalReadTable = context.stage6ReadTable_;
  const headers = ['creator_name', 'channel_id', 'video_id', 'published_at', 'view_count', 'like_rate'];
  const rows = [
    ['Creator', 'creator-1', 'older-1', '2026-01-01T00:00:00Z', 100, null],
    ['Creator', 'creator-1', 'older-2', '2026-01-02T00:00:00Z', 300, 0.2],
  ];
  context.stage6ReadTable_ = () => ({ headers, rows });
  try {
    const oneValidLike = context.stage6CreatorBaseline_('creator-1', 20, 'not-in-baseline');
    assert.equal(oneValidLike.sample_count, 2);
    assert.equal(oneValidLike.final_views_median, 200);
    assert.equal(oneValidLike.valid_like_rate_sample_count, 1);
    assert.equal(oneValidLike.historical_median_like_rate, 0.2);

    rows[1][5] = null;
    const noValidLikes = context.stage6CreatorBaseline_('creator-1', 20, 'not-in-baseline');
    assert.equal(noValidLikes.sample_count, 2);
    assert.equal(noValidLikes.final_views_median, 200);
    assert.equal(noValidLikes.valid_like_rate_sample_count, 0);
    assert.equal(noValidLikes.historical_median_like_rate, null);
  } finally {
    context.stage6ReadTable_ = originalReadTable;
  }
});

test('Warm baseline helper uses completed same-checkpoint view_count and like_rate samples', () => {
  const videoHeaders = ['video_id', 'channel_id'];
  const videoRows = [
    ['target', 'creator-1'], ['peer-a', 'creator-1'], ['peer-b', 'creator-1'], ['other', 'creator-2'],
  ];
  const snapshotHeaders = ['video_id', 'channel_id', 'snapshot_stage_minutes', 'view_count', 'like_rate'];
  const snapshotRows = [
    ['target', 'creator-1', 30, 100, 0.1], ['target', 'creator-1', 60, 1_500, 0.08],
    ['peer-a', 'creator-1', 30, 50, 0.1], ['peer-a', 'creator-1', 60, 1_000, 0.06],
    ['peer-b', 'creator-1', 30, 80, 0.1], ['peer-b', 'creator-1', 60, 3_000, 0.04],
    ['other', 'creator-2', 30, 100, 0.1], ['other', 'creator-2', 60, 99_000, 0.9],
  ];
  const got = context.stage6CompletePeerStats_(
    'target', 'creator-1', 60, videoHeaders, videoRows, snapshotHeaders, snapshotRows, [30, 60],
  );
  assert.equal(got.sample_count, 2);
  assert.equal(got.median_views, 2_000);
  assert.equal(got.median_like_rate, 0.05);
});

test('Snapshot resume does not replace an explicitly missing velocity with a legacy alias', () => {
  const currentHeaders = ['video_id', 'captured_at', 'actual_elapsed_seconds', 'view_count', 'velocity', 'interval_growth_per_hour', 'views_per_hour'];
  const currentRows = [['v1', '2026-09-24T12:30:00Z', 1_800, 1_000, '', 600, 2_000]];
  assert.equal(context.stage6LatestSnapshot_(currentHeaders, currentRows, 'v1').velocity, null);

  const legacyHeaders = ['video_id', 'captured_at', 'actual_elapsed_seconds', 'view_count', 'interval_growth_per_hour', 'views_per_hour'];
  const legacyRows = [['v1', '2026-09-24T12:30:00Z', 1_800, 1_000, '', 2_000]];
  assert.equal(context.stage6LatestSnapshot_(legacyHeaders, legacyRows, 'v1').velocity, 2_000);
});

test('Cold checkpoint ratios use the planned checkpoint, not actual elapsed time', async (t) => {
  const ratios = { 30: 0.025, 60: 0.05, 120: 0.10 };
  for (const [checkpoint, ratio] of Object.entries(ratios)) {
    await t.test(`T+${checkpoint} threshold - 1, exact, +1`, async (st) => {
      const required = 10_000 * ratio;
      for (const [label, views, expected] of [
        ['below', required - 1, false],
        ['exact', required, true],
        ['above', required + 1, true],
      ]) {
        await st.test(label, () => {
          const decision = evaluate(coldInput(Number(checkpoint), {
            current_view_count: views,
            actual_elapsed_seconds: Number(checkpoint) * 60 + 899,
          }));
          assert.equal(decision.required_views, required);
          assert.equal(decision.views_pass, expected);
          assert.equal(decision.mode, 'cold');
          assert.equal(decision.checkpoint, Number(checkpoint));
        });
      }
    });
  }
});

test('Cold secondary gate requires views and at least one secondary pass', async (t) => {
  const cases = [
    ['views and like rate pass', { current_view_count: 600, current_like_rate: 0.08, acceleration: 0 }, true],
    ['views and acceleration pass', { current_view_count: 600, current_like_rate: 0.079, acceleration: 0.01 }, true],
    ['both secondary gates fail', { current_view_count: 600, current_like_rate: 0.079, acceleration: 0 }, false],
    ['views fail though like rate passes', { current_view_count: 499, current_like_rate: 0.08, acceleration: 1 }, false],
    ['views fail though acceleration passes', { current_view_count: 499, current_like_rate: 0.01, acceleration: 1 }, false],
  ];
  for (const [name, overrides, expected] of cases) {
    await t.test(name, () => {
      const decision = evaluate(coldInput(60, overrides));
      assert.equal(decision.matched, expected);
      assert.equal(decision.target_state, expected ? 'DATA_HOT' : 'WATCH');
    });
  }
});

test('Cold Like Rate threshold equality passes; first-checkpoint acceleration is false', () => {
  const first = evaluate(coldInput(30, {
    current_view_count: 250,
    current_like_rate: 0.08,
    acceleration: null,
  }));
  assert.equal(first.acceleration_pass, false);
  assert.equal(first.like_rate_pass, true);
  assert.equal(first.matched, true);
  assert.equal(first.reason, 'cold_views_like_rate');
});

test('Acceleration passes only when the configured snapshot index is reached and value is positive', () => {
  const first = evaluate(coldInput(30, { current_view_count: 600, current_like_rate: 0.01, acceleration: 99 }));
  const positive = evaluate(coldInput(60, { current_view_count: 600, current_like_rate: 0.01, acceleration: 0.01 }));
  const zero = evaluate(coldInput(60, { current_view_count: 600, current_like_rate: 0.01, acceleration: 0 }));
  const negative = evaluate(coldInput(60, { current_view_count: 600, current_like_rate: 0.01, acceleration: -0.01 }));
  assert.equal(first.acceleration_pass, false);
  assert.equal(positive.acceleration_pass, true);
  assert.equal(zero.acceleration_pass, false);
  assert.equal(negative.acceleration_pass, false);
});

test('Warm Relative Velocity uses current views divided by same-checkpoint median views', async (t) => {
  for (const [views, expected] of [[1_490, false], [1_500, true], [1_510, true]]) {
    await t.test(String(views), () => {
      const decision = evaluate(warmInput({ current_view_count: views, current_like_rate: 0.079, acceleration: 0 }));
      assert.equal(decision.relative_velocity, views / 1_000);
      assert.equal(decision.relative_velocity_pass, expected);
    });
  }
});

test('Warm secondary gate accepts like rate or positive acceleration, and rejects both failing', async (t) => {
  const cases = [
    ['like rate', { current_like_rate: 0.08, acceleration: 0 }, true, 'warm_relative_velocity_like_rate'],
    ['acceleration', { current_like_rate: 0.079, acceleration: 0.01 }, true, 'warm_relative_velocity_acceleration'],
    ['neither', { current_like_rate: 0.079, acceleration: 0 }, false, 'warm_secondary_gate_failed'],
  ];
  for (const [name, overrides, expected, reason] of cases) {
    await t.test(name, () => {
      const decision = evaluate(warmInput(overrides));
      assert.equal(decision.matched, expected);
      assert.equal(decision.reason, reason);
    });
  }
});

test('Warm Like Rate equality at configured multiplier passes', () => {
  const decision = evaluate(warmInput({
    current_like_rate: 0.10 * 0.80,
    acceleration: 0,
  }));
  assert.equal(decision.like_rate_pass, true);
  assert.equal(decision.matched, true);
});

test('both passing secondary gates are preserved in the machine reason without priority', () => {
  const cold = evaluate(coldInput(60, { current_view_count: 600, current_like_rate: 0.08, acceleration: 1 }));
  const warm = evaluate(warmInput({ current_like_rate: 0.08, acceleration: 1 }));
  assert.equal(cold.reason, 'cold_views_like_rate_acceleration');
  assert.equal(warm.reason, 'warm_relative_velocity_like_rate_acceleration');
});

test('T+120 miss becomes NORMAL and retired T+180/T+360 are rejected', () => {
  const base = {
    current_view_count: 0,
    current_like_rate: 1,
    acceleration: 0,
    baseline_final_views_median: 10_000,
    historical_median_like_rate: 1,
  };
  assert.equal(evaluate(coldInput(120, base)).target_state, 'NORMAL');
  assert.throws(() => evaluate(coldInput(180, base)), /checkpoint is not configured/i);
  assert.throws(() => evaluate(coldInput(360, base)), /checkpoint is not configured/i);
});

test('Warm threshold change from 1.50 to 1.60 changes the decision', () => {
  const input = warmInput({ current_view_count: 1_550, current_like_rate: 0.08, acceleration: 0 });
  assert.equal(evaluate(input, validConfig()).matched, true);
  assert.equal(evaluate(input, validConfig({ warm_baseline_relative_velocity_threshold: 1.60 })).matched, false);
});

test('Cold checkpoint ratio change changes the required views and decision', () => {
  const input = coldInput(30, { current_view_count: 275, current_like_rate: 0.08, acceleration: null });
  assert.equal(evaluate(input, validConfig()).matched, true);
  const changed = evaluate(input, validConfig({ cold_start_checkpoint_30_ratio: 0.03 }));
  assert.equal(changed.required_views, 300);
  assert.equal(changed.matched, false);
});

test('Missing historical baselines do not invent values or match HOT', () => {
  const cold = evaluate(coldInput(60, { baseline_final_views_median: null, historical_median_like_rate: null }));
  const warm = evaluate(warmInput({ historical_same_checkpoint_median_views: null, historical_same_checkpoint_median_like_rate: null }));
  assert.equal(cold.required_views, null);
  assert.equal(cold.matched, false);
  assert.equal(warm.relative_velocity, null);
  assert.equal(warm.matched, false);
  assert.equal(evaluate(coldInput(120, { baseline_final_views_median: null })).target_state, 'BASELINE_UNAVAILABLE');
});

test('Unavailable Cold baseline has an explicit terminal result at T+120 without changing HOT thresholds', () => {
  const missing = { baseline_final_views_median: null, historical_median_like_rate: null, current_view_count: 0, acceleration: 0 };
  assert.equal(evaluate(coldInput(60, missing)).target_state, 'WATCH');
  const final = evaluate(coldInput(120, missing));
  assert.equal(final.target_state, 'BASELINE_UNAVAILABLE');
  assert.equal(final.reason, 'BASELINE_UNAVAILABLE');
  assert.equal(context.stage6HotLifecycleTransition_('WATCH', final), 'BASELINE_UNAVAILABLE');
});

test('Config rejects missing, nonnumeric, negative, illegal, or incomplete parameters', async (t) => {
  const cases = [
    ['missing threshold', { warm_baseline_min_complete_watch: undefined }],
    ['nonnumeric multiplier', { cold_start_like_rate_multiplier: 'bad' }],
    ['negative ratio', { cold_start_checkpoint_30_ratio: -0.1 }],
    ['illegal checkpoint', { snapshot_stage_minutes: '30,60,100' }],
    ['missing checkpoint ratio', { cold_start_checkpoint_120_ratio: undefined }],
    ['zero history window', { cold_start_final_views_history_window: 0 }],
    ['expiration not a checkpoint', { normal_after_minutes: 240 }],
  ];
  for (const [name, overrides] of cases) {
    await t.test(name, () => assert.throws(() => context.stage6ReadHotConfig_(validConfig(overrides)), /HOT Config/i));
  }
});

test('Snapshot metrics preserve canonical aliases and avoid non-finite arithmetic', async (t) => {
  const first = context.stage6BuildSnapshotMetrics_({
    viewCount: 1_200,
    likeCount: 24,
    publishedAt: '2026-09-24T12:00:00Z',
    capturedAt: '2026-09-24T12:30:00Z',
    previous: null,
  });
  assert.equal(first.actual_elapsed_seconds, 1_800);
  assert.equal(first.velocity, 2_400);
  assert.equal(first.interval_view_growth, null);
  assert.equal(first.acceleration, null);
  assert.equal(first.like_rate, 0.02);

  const invalidDenominator = context.stage6BuildSnapshotMetrics_({
    viewCount: 0,
    likeCount: null,
    publishedAt: '2026-09-24T12:00:00Z',
    capturedAt: '2026-09-24T12:00:00Z',
  });
  assert.equal(invalidDenominator.velocity, null);
  assert.equal(invalidDenominator.like_rate, null);
  for (const value of Object.values(invalidDenominator)) {
    if (typeof value === 'number') assert.equal(Number.isFinite(value), true);
  }

  const invalidElapsed = context.stage6BuildSnapshotMetrics_({
    viewCount: 10,
    likeCount: 1,
    publishedAt: null,
    capturedAt: '2026-09-24T12:30:00Z',
  });
  assert.equal(invalidElapsed.actual_elapsed_seconds, null);
  assert.equal(invalidElapsed.velocity, null);
  assert.equal(invalidElapsed.views_per_hour, null);

  const noInterval = context.stage6BuildSnapshotMetrics_({
    viewCount: 100,
    likeCount: 2,
    publishedAt: '2026-09-24T12:00:00Z',
    capturedAt: '2026-09-24T12:10:00Z',
    previous: { view_count: 90, captured_at: '2026-09-24T12:10:00Z', actual_elapsed_seconds: 600, velocity: 60 },
  });
  assert.equal(noInterval.interval_view_growth, 10);
  assert.equal(noInterval.velocity, null);
  assert.equal(noInterval.acceleration, null);

  await t.test('negative growth and acceleration stay negative', () => {
    const declining = context.stage6BuildSnapshotMetrics_({
      viewCount: 80,
      likeCount: 1,
      publishedAt: '2026-09-24T12:00:00Z',
      capturedAt: '2026-09-24T13:00:00Z',
      previous: { view_count: 100, captured_at: '2026-09-24T12:30:00Z', actual_elapsed_seconds: 1_800, velocity: 120 },
    });
    assert.equal(declining.interval_view_growth, -20);
    assert.equal(declining.velocity, -40);
    assert.equal(declining.acceleration, -160);
  });
});

test('HOT decision feeds DATA_HOT then existing Candidate transition without extra strategy', () => {
  const decision = evaluate(coldInput(60));
  assert.equal(context.stage6HotLifecycleTransition_('WATCH', decision), 'DATA_HOT');
  assert.equal(context.stage6CandidateLifecycleTransition_('DATA_HOT'), 'CANDIDATE');
  assert.equal(context.stage6HotLifecycleTransition_('CANDIDATE', decision), 'CANDIDATE');
});
