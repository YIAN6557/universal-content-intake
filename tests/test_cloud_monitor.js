const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const context = vm.createContext({
  console, Date, Math, Set, Map, Number, String, Array, Object, JSON,
  Utilities: { formatDate(date, timezone, pattern) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(date);
    const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    const day = `${value.year}-${value.month}-${value.day}`;
    const clock = `${value.hour}:${value.minute}`;
    if (pattern === 'HH:mm') return clock;
    if (pattern === 'HH:mm') return clock;
    if (pattern.includes('|')) return `${day}|${clock}`;
    return day;
  } },
  SpreadsheetApp: { getActiveSpreadsheet() { return this.openById(''); }, openById: () => ({ getSpreadsheetTimeZone: () => 'Etc/GMT' }) },
});
for (const file of ['monitor.gs', 'discovery.gs', 'stats.gs', 'scoring.gs']) {
  const fullPath = path.join(scriptPath, file);
  if (fs.existsSync(fullPath)) vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
}

test('schedule clock parser accepts HH:mm strings, Sheets time serials, and Date values', () => {
  assert.equal(context.stage6ClockMinutes_('04:10', 'final_sweep_time'), 250);
  assert.equal(context.stage6ClockMinutes_(250 / 1440, 'final_sweep_time'), 250);
  assert.equal(context.stage6ClockMinutes_(new Date('1899-12-30T04:10:00Z'), 'final_sweep_time'), 250);
});

test('schedule clock parser rejects invalid or non-minute Sheets time values', () => {
  assert.throws(() => context.stage6ClockMinutes_(1, 'final_sweep_time'), /time of day/);
  assert.throws(() => context.stage6ClockMinutes_(-0.01, 'final_sweep_time'), /time of day/);
  assert.throws(() => context.stage6ClockMinutes_(250.5 / 1440, 'final_sweep_time'), /whole minute/);
  assert.throws(() => context.stage6ClockMinutes_('25:00', 'final_sweep_time'), /HH:mm/);
});

test('daily discovery admits only unseen uploads in the Shanghai batch window', () => {
  const items = [
    { videoId: 'night-before', title: 'night-before', publishedAt: '2026-09-25T15:59:00Z' },
    { videoId: 'known-1', title: 'known', publishedAt: '2026-09-25T16:30:00Z' },
    { videoId: 'valid-1', title: 'valid', publishedAt: '2026-09-25T19:59:00Z' },
    { videoId: 'too-late', title: 'late', publishedAt: '2026-09-25T20:03:00Z' },
  ];
  const config = {
    production_timezone: 'Asia/Shanghai', discovery_window_start: '00:00', discovery_window_end: '04:00',
  };
  const got = context.stage6SelectNewUploads_(items, new Set(['known-1']), '2026-09-26', config);
  assert.deepEqual(Array.from(got, (item) => item.video_id), ['valid-1']);
});

test('IANA batch boundaries include 00:00 and 03:59, exclude 23:59 prior day and 04:00+', () => {
  const config = {
    production_timezone: 'Asia/Shanghai', discovery_window_start: '00:00', discovery_window_end: '04:00',
  };
  const accepts = (time) => context.stage6IsPublishedInDailyBatch_(time, '2026-09-26', config);
  assert.equal(accepts('2026-09-25T15:59:00Z'), false); // prior-day 23:59 Beijing
  assert.equal(accepts('2026-09-25T16:00:00Z'), true); // 00:00
  assert.equal(accepts('2026-09-25T19:59:00Z'), true); // 03:59
  assert.equal(accepts('2026-09-25T20:00:00Z'), false); // 04:00 exclusive
  assert.equal(accepts('2026-09-25T20:03:00Z'), false); // final sweep does not widen window
});

test('minute coordinator respects ten-minute Discovery slots, one 04:10 sweep, and Shanghai date serial markers', () => {
  const config = {
    production_timezone: 'Asia/Shanghai', discovery_window_start: '00:00', discovery_window_end: '04:00',
    discovery_cadence_minutes: 10, final_sweep_time: '04:10', daily_selection_time: '06:00',
    rank2_start_cutoff: '08:00', last_discovery_slot: '', last_final_sweep_day: '', last_daily_selection_day: '',
  };
  const at = (hour, minute) => new Date(Date.UTC(2026, 8, 25, hour - 8, minute));
  const serial = (iso) => new Date(iso).getTime() / 86_400_000 + 25569;
  const originalOpenById = context.SpreadsheetApp.openById;
  // Reproduce the production Apps Script runtime, where this method returned
  // blank despite the spreadsheet metadata showing Etc/GMT.
  context.SpreadsheetApp.openById = () => ({ getSpreadsheetTimeZone: () => '' });
  try {
  assert.equal(context.stage6ScheduleDecision_(at(0, 0), config).discovery_slot, '2026-09-25 00:00');
  assert.equal(context.stage6ScheduleDecision_(at(0, 10), { ...config, last_discovery_slot: '2026-09-25 00:00' }).discovery_slot, '2026-09-25 00:10');
  assert.equal(context.stage6ScheduleDecision_(at(3, 50), config).discovery_slot, '2026-09-25 03:50');
  assert.equal(context.stage6ScheduleDecision_(at(3, 50), {
    ...config, last_discovery_slot: serial('2026-09-25T03:50:00Z'),
  }).discovery_slot, null);
  assert.equal(context.stage6ScheduleDecision_(at(4, 0), config).discovery_slot, null);
  assert.equal(context.stage6ScheduleDecision_(at(4, 9), config).discovery_slot, null);
  assert.equal(context.stage6ScheduleDecision_(at(4, 9), config).final_sweep_due, false);
  assert.equal(context.stage6ScheduleDecision_(at(4, 10), config).final_sweep_due, true);
  assert.equal(context.stage6ScheduleDecision_(at(4, 10), {
    ...config, last_final_sweep_day: new Date('2026-09-25T00:00:00Z'),
  }).final_sweep_due, false);
  const swept = { ...config, last_final_sweep_day: new Date('2026-09-25T00:00:00Z') };
  for (const [hour, minute] of [[4, 11], [4, 20], [5, 0], [5, 59]]) {
    const decision = context.stage6ScheduleDecision_(at(hour, minute), swept);
    assert.equal(decision.discovery_slot, null, `${hour}:${minute} should not run normal Discovery`);
    assert.equal(decision.final_sweep_due, false, `${hour}:${minute} should not repeat final sweep`);
    // A delayed or skipped 04:10 trigger is caught up once before Selection.
    assert.equal(context.stage6ScheduleDecision_(at(hour, minute), config).final_sweep_due, true,
      `${hour}:${minute} should catch up a missed final sweep`);
  }
  assert.equal(context.stage6ScheduleDecision_(at(6, 0), config).final_sweep_due, false,
    'a missed sweep is not run after Selection closed the batch');
  assert.equal(context.stage6ScheduleDecision_(at(6, 0), config).selection_due, true);
  assert.equal(context.stage6ScheduleDecision_(at(6, 0), {
    ...config, last_daily_selection_day: serial('2026-09-25T00:00:00Z'),
  }).selection_due, false);
  assert.equal(context.stage6ScheduleDecision_(at(8, 0), config).rank2_cutoff_reached, true);
  // Full work only inside 00:00-08:30; the rest of the day the scheduler idles.
  for (const [hour, minute, active] of [[0, 0, true], [4, 10, true], [6, 0, true], [8, 29, true], [8, 30, false], [13, 0, false], [23, 59, false]]) {
    assert.equal(context.stage6ScheduleDecision_(at(hour, minute), config).active_window, active, `${hour}:${minute} active_window`);
  }
  } finally {
    context.SpreadsheetApp.openById = originalOpenById;
  }
});

test('discovery creates WATCH records once per stable video_id', () => {
  const items = [
    { videoId: 'new-1', title: 'new', publishedAt: '2026-09-23T16:30:00Z' },
    { videoId: 'new-1', title: 'duplicate row', publishedAt: '2026-09-23T16:30:00Z' },
    { videoId: 'old-1', title: 'old', publishedAt: '2026-09-23T15:00:00Z' },
  ];
  const got = context.stage6SelectNewUploads_(items, new Set(), '2026-09-24', {
    production_timezone: 'Asia/Shanghai', discovery_window_start: '00:00', discovery_window_end: '04:00',
  });
  assert.equal(got.length, 1);
  assert.equal(got[0].video_id, 'new-1');
  assert.equal(got[0].lifecycle_state, 'WATCH');
});

test('snapshot metrics use captured actual elapsed time and observed prior samples', () => {
  const got = context.stage6BuildSnapshotMetrics_({
    viewCount: 1000,
    likeCount: 20,
    publishedAt: '2026-09-24T12:00:00Z',
    capturedAt: '2026-09-24T13:02:00Z',
    previous: {
      view_count: 700,
      captured_at: '2026-09-24T12:32:00Z',
      actual_elapsed_seconds: 1920,
      interval_growth_per_hour: 400,
      velocity: 400,
    },
  });
  assert.equal(got.actual_elapsed_seconds, 3720);
  assert.equal(got.interval_growth, 300);
  assert.equal(got.like_rate, 0.02);
  assert.equal(got.views_per_hour, 1000 / (3720 / 3600));
  assert.equal(got.interval_growth_per_hour, 600);
  assert.equal(got.acceleration, 200);
});

test('Warm Baseline is unavailable below ten complete WATCH videos', () => {
  assert.equal(context.stage6HotPhase_(9, 10), 'COLD_START');
  assert.equal(context.stage6HotPhase_(10, 10), 'WARM_BASELINE');
});

test('HOT config rejects missing production parameters instead of evaluator fallbacks', () => {
  assert.throws(() => context.stage6ReadHotConfig_({}), /HOT Config/i);
});

test('formal Cold decision evaluates unchanged checkpoint threshold and secondary acceleration', () => {
  const got = context.stage6EvaluateHot_({
    complete_watch_count: 0,
    checkpoint_minutes: 60,
    current_view_count: 500,
    current_like_rate: 0.03,
    acceleration: 200,
    baseline_final_views_median: 10000,
    historical_median_like_rate: 0.04,
  }, {
    snapshot_stage_minutes: '30,60,120',
    warm_baseline_min_complete_watch: 10,
    cold_start_final_views_history_window: 20,
    cold_start_checkpoint_30_ratio: 0.025,
    cold_start_checkpoint_60_ratio: 0.05,
    cold_start_checkpoint_120_ratio: 0.1,
    cold_start_like_rate_multiplier: 0.8,
    acceleration_min_snapshot_index: 2,
    warm_baseline_relative_velocity_threshold: 1.5,
    warm_baseline_like_rate_multiplier: 0.8,
    normal_after_minutes: 120,
  });
  assert.equal(got.matched, true);
  assert.equal(got.reason, 'cold_views_acceleration');
});

test('missing observed metrics cannot pass a zero threshold as fabricated zero', () => {
  const got = context.stage6EvaluateHot_({
    complete_watch_count: 0,
    checkpoint_minutes: 30,
    current_view_count: 1,
    current_like_rate: null,
    acceleration: null,
    baseline_final_views_median: 1,
    historical_median_like_rate: null,
  }, {
    snapshot_stage_minutes: '30,60,120',
    warm_baseline_min_complete_watch: 10,
    cold_start_final_views_history_window: 20,
    cold_start_checkpoint_30_ratio: 0,
    cold_start_checkpoint_60_ratio: 0.05,
    cold_start_checkpoint_120_ratio: 0.1,
    cold_start_like_rate_multiplier: 0.8,
    acceleration_min_snapshot_index: 2,
    warm_baseline_relative_velocity_threshold: 1.5,
    warm_baseline_like_rate_multiplier: 0.8,
    normal_after_minutes: 120,
  });
  assert.equal(got.matched, false);
  assert.equal(got.like_rate_pass, false);
  assert.equal(got.acceleration_pass, false);
});

test('batch Stats request groups never exceed fifty IDs', () => {
  const got = context.stage6ChunkIds_(Array.from({ length: 121 }, (_, i) => `v${i}`), 50);
  assert.deepEqual(Array.from(got, (chunk) => chunk.length), [50, 50, 21]);
});

test('due snapshot selection returns only T+30/T+60/T+120 milestones', () => {
  const got = context.stage6NextDueStage_([30, 60, 120], [30],
    '2026-09-24T12:00:00Z', '2026-09-24T14:00:00Z');
  assert.equal(got, 60);
  assert.equal(context.stage6NextDueStage_([30, 60, 120], [], null, '2026-09-24T14:00:00Z'), null);
});

test('API quota, permission, and temporary failures get sanitized pause classes', () => {
  assert.equal(context.stage6ClassifyApiFailure_(new Error('Quota exceeded')), 'QUOTA_OR_RATE_LIMIT');
  assert.equal(context.stage6ClassifyApiFailure_(new Error('403 permission denied')), 'PERMISSION_OR_AUTH');
  assert.equal(context.stage6ClassifyApiFailure_(new Error('backend temporarily unavailable')), 'TRANSIENT_API_OR_SERVICE');
  assert.equal(context.stage6ClassifyApiFailure_(new Error("We're sorry, a server error occurred. Please wait a bit and try again.")), 'TRANSIENT_API_OR_SERVICE');
  assert.equal(context.stage6ClassifyApiFailure_(new Error('Empty response')), 'TRANSIENT_API_OR_SERVICE');
  assert.equal(context.stage6ClassifyApiFailure_(new Error('playlistNotFound')), 'API_OR_APPS_SCRIPT_ERROR');
  // One-off failures retry on the next run; quota/permission pause at once; three in a row pause.
  assert.deepEqual({ ...context.stage6ApiFailureDecision_('API_OR_APPS_SCRIPT_ERROR', '') }, { failures: 1, pause: false });
  assert.deepEqual({ ...context.stage6ApiFailureDecision_('TRANSIENT_API_OR_SERVICE', 1) }, { failures: 2, pause: false });
  assert.deepEqual({ ...context.stage6ApiFailureDecision_('TRANSIENT_API_OR_SERVICE', 2) }, { failures: 3, pause: true });
  assert.deepEqual({ ...context.stage6ApiFailureDecision_('QUOTA_OR_RATE_LIMIT', 0) }, { failures: 1, pause: true });
  assert.deepEqual({ ...context.stage6ApiFailureDecision_('PERMISSION_OR_AUTH', 0) }, { failures: 1, pause: true });
});

test('only configured WATCH HOT results transition DATA_HOT then CANDIDATE', () => {
  const decision = context.stage6EvaluateHot_({
    complete_watch_count: 0,
    checkpoint_minutes: 60,
    current_view_count: 600,
    current_like_rate: 0.08,
    acceleration: 0,
    baseline_final_views_median: 10000,
    historical_median_like_rate: 0.1,
  }, {
    snapshot_stage_minutes: '30,60,120',
    warm_baseline_min_complete_watch: 10,
    cold_start_final_views_history_window: 20,
    cold_start_checkpoint_30_ratio: 0.025,
    cold_start_checkpoint_60_ratio: 0.05,
    cold_start_checkpoint_120_ratio: 0.1,
    cold_start_like_rate_multiplier: 0.8,
    acceleration_min_snapshot_index: 2,
    warm_baseline_relative_velocity_threshold: 1.5,
    warm_baseline_like_rate_multiplier: 0.8,
    normal_after_minutes: 120,
  });
  assert.equal(context.stage6HotLifecycleTransition_('WATCH', { target_state: 'WATCH' }), 'WATCH');
  assert.equal(context.stage6HotLifecycleTransition_('WATCH', decision), 'DATA_HOT');
  assert.equal(context.stage6CandidateLifecycleTransition_('DATA_HOT'), 'CANDIDATE');
  assert.equal(context.stage6HotLifecycleTransition_('WATCH', { target_state: 'NORMAL' }), 'NORMAL');
  assert.equal(context.stage6CandidateLifecycleTransition_('NORMAL'), 'NORMAL');
});

test('a ten-minute trigger at any offset still covers every Discovery slot, one sweep and one Selection', () => {
  for (const offset of [0, 3, 7, 9]) {
    const config = {
      production_timezone: 'Asia/Shanghai', discovery_window_start: '00:00', discovery_window_end: '04:00',
      discovery_cadence_minutes: 10, final_sweep_time: '04:10', daily_selection_time: '06:00',
      rank2_start_cutoff: '08:00', last_discovery_slot: '', last_final_sweep_day: '', last_daily_selection_day: '',
    };
    const slots = [];
    let sweeps = 0;
    let selections = 0;
    let cutoffSeenBy = null;
    for (let minute = offset; minute < 24 * 60; minute += 10) {
      const at = new Date(Date.UTC(2026, 8, 24, 16, 0) + minute * 60_000); // 2026-09-25 00:00 Shanghai + minute
      const decision = context.stage6ScheduleDecision_(at, config);
      if (decision.discovery_slot) { slots.push(decision.discovery_slot); config.last_discovery_slot = decision.discovery_slot; }
      if (decision.final_sweep_due) { sweeps += 1; config.last_final_sweep_day = decision.day; }
      if (decision.selection_due) { selections += 1; config.last_daily_selection_day = decision.day; }
      if (decision.rank2_cutoff_reached && cutoffSeenBy === null) cutoffSeenBy = minute;
    }
    assert.equal(slots.length, 24, `offset ${offset}: every 10-minute Discovery slot runs once`);
    assert.equal(new Set(slots).size, 24);
    assert.equal(sweeps, 1, `offset ${offset}: one final sweep`);
    assert.equal(selections, 1, `offset ${offset}: one Selection`);
    assert.ok(cutoffSeenBy >= 480 && cutoffSeenBy < 490, `offset ${offset}: Rank 2 cutoff observed within ten minutes`);
  }
});

test('late snapshots within tolerance are captured; checkpoints missed by an outage are skipped', () => {
  const plan = (existing, now) => JSON.parse(JSON.stringify(context.stage6SnapshotPlan_([30, 60, 120], existing, '2026-09-28T16:40:00Z', now, 20)));
  // On time and normal scheduling delay (up to the 20-minute tolerance).
  assert.deepEqual(plan([], '2026-09-28T17:10:00Z'), { stage: 30, all_missed: false, missed: [] });
  assert.deepEqual(plan([], '2026-09-28T17:30:00Z'), { stage: 30, all_missed: false, missed: [] });
  // Not yet due.
  assert.deepEqual(plan([], '2026-09-28T17:05:00Z'), { stage: null, all_missed: false, missed: [] });
  // T+30 missed by 25 minutes, T+60 not yet due: skip T+30 and wait.
  assert.deepEqual(plan([], '2026-09-28T17:35:00Z'), { stage: null, all_missed: false, missed: [30] });
  // T+30 missed, T+60 on time: capture T+60 without back-filling T+30.
  assert.deepEqual(plan([], '2026-09-28T17:45:00Z'), { stage: 60, all_missed: false, missed: [30] });
  // Outage of 11 hours (the 2026-09-29 incident): everything missed.
  assert.deepEqual(plan([], '2026-09-29T03:25:00Z'), { stage: null, all_missed: true, missed: [30, 60, 120] });
  // Earlier checkpoints captured, only the last one missed.
  assert.deepEqual(plan([30, 60], '2026-09-28T19:20:00Z'), { stage: null, all_missed: true, missed: [120] });
  // Complete observations are not reported as missed.
  assert.deepEqual(plan([30, 60, 120], '2026-09-29T03:25:00Z'), { stage: null, all_missed: false, missed: [] });
});

test('snapshot capture ends WATCH as SNAPSHOT_MISSED instead of judging hours-late data', () => {
  const videoHeaders = ['video_id', 'channel_id', 'published_at', 'lifecycle_state', 'hot_reason', 'first_snapshot_at'];
  const snapshotHeaders = ['video_id', 'snapshot_stage_minutes', 'captured_at'];
  const videoRows = [
    ['outage-video', 'c1', '2026-09-28T16:40:00.000Z', 'WATCH', '', ''],
    ['fresh-video', 'c1', new Date(Date.now() - 5 * 60000).toISOString(), 'WATCH', '', ''],
  ];
  const writes = [];
  const sheet = { getRange: (row, col) => ({ setValue: (value) => writes.push([row, col, value]), setValues() {} }), getLastRow: () => 1 };
  const saved = { read: context.stage6ReadTable_, cfg: context.stage6ReadConfig_, hot: context.stage6ReadHotConfig_, yt: context.YouTube };
  context.stage6ReadTable_ = (name) => name === 'Videos'
    ? { sheet, headers: videoHeaders, rows: videoRows.map((r) => r.slice()) }
    : { sheet, headers: snapshotHeaders, rows: [] };
  context.stage6ReadConfig_ = () => ({});
  context.stage6ReadHotConfig_ = () => ({ snapshotCheckpoints: [30, 60, 120] });
  let youtubeCalls = 0;
  context.YouTube = { Videos: { list: () => { youtubeCalls += 1; return { items: [] }; } } };
  try {
    assert.equal(context.stage6CaptureDueSnapshots(), 0);
  } finally {
    Object.assign(context, { stage6ReadTable_: saved.read, stage6ReadConfig_: saved.cfg, stage6ReadHotConfig_: saved.hot, YouTube: saved.yt });
  }
  assert.equal(youtubeCalls, 0);
  assert.deepEqual(writes, [[2, 4, 'SNAPSHOT_MISSED'], [2, 5, 'SNAPSHOT_MISSED']]);
});
