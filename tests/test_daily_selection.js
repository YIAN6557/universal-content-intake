const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const FIXED_NOW = Date.UTC(2026, 8, 26, 6, 30, 0);
const selectionHeaders = ['selected_at', 'selection_day', 'selection_rank', 'selection_hot_strength', 'queue_id', 'queued_at',
  'selection_result', 'selection_closed_at', 'selection_reason'];

class FixedDate extends Date {
  constructor(...args) { super(...(args.length ? args : [FIXED_NOW])); }
  static now() { return FIXED_NOW; }
}

class MemorySheet {
  constructor(name, rows = []) {
    this.name = name;
    this.rows = rows.map((row) => row.slice());
    this.failNextWriteFor = null;
  }
  getName() { return this.name; }
  getLastRow() {
    for (let i = this.rows.length - 1; i >= 0; i -= 1) {
      if (this.rows[i].some((value) => value !== '' && value !== null && value !== undefined)) return i + 1;
    }
    return 0;
  }
  getLastColumn() {
    return this.rows.reduce((last, row) => {
      for (let i = row.length - 1; i >= 0; i -= 1) {
        if (row[i] !== '' && row[i] !== null && row[i] !== undefined) return Math.max(last, i + 1);
      }
      return last;
    }, 0);
  }
  getRange(row, column, rowCount = 1, columnCount = 1) {
    const sheet = this;
    return {
      getValues() {
        return Array.from({ length: rowCount }, (_, r) =>
          Array.from({ length: columnCount }, (_, c) => sheet.rows[row + r - 1]?.[column + c - 1] ?? ''));
      },
      setValues(values) {
        values.forEach((valuesRow, r) => valuesRow.forEach((value, c) => {
          sheet.write(row + r, column + c, value);
        }));
      },
      setValue(value) { sheet.write(row, column, value); },
    };
  }
  write(row, column, value) {
    const header = this.rows[0]?.[column - 1];
    if (this.failNextWriteFor === header) {
      this.failNextWriteFor = null;
      throw new Error('injected sheet write failure');
    }
    this.rows[row - 1] ||= [];
    this.rows[row - 1][column - 1] = value;
  }
  getDataRange() { return this.getRange(1, 1, Math.max(1, this.getLastRow()), Math.max(1, this.getLastColumn())); }
  appendRow(values) { this.rows[this.getLastRow()] = values.slice(); }
}

class MemorySpreadsheet {
  constructor(sheets, timezone = 'America/Los_Angeles') { this.sheets = sheets; this.timezone = timezone; }
  getSheetByName(name) { return this.sheets.find((sheet) => sheet.name === name) || null; }
    getSpreadsheetTimeZone() { return this.timezone; }
}

const context = vm.createContext({
  console, Date: FixedDate, Math, Set, Map, Number, String, Array, Object, JSON, Error, RegExp,
  Logger: { log() {} },
  Logger: { log() {} },
  Utilities: {
    getUuid: () => crypto.randomUUID(),
    formatDate(date, timezone, pattern) {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).formatToParts(date);
      const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
      const day = `${values.year}-${values.month}-${values.day}`;
      const clock = `${values.hour}:${values.minute}`;
      if (pattern === 'HH:mm') return clock;
      return pattern && pattern.includes('|') ? `${day}|${clock}` : day;
    },
  },
  LockService: {
    getScriptLock: () => ({ waitLock() {}, tryLock() { return true; }, releaseLock() {} }),
  },
});
for (const file of ['monitor.gs', 'stats.gs', 'scoring.gs', 'semantic.gs', 'queue_contract.gs', 'queue.gs', 'selection.gs']) {
  const fullPath = path.join(scriptPath, file);
  if (fs.existsSync(fullPath)) vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
}

function value(name) { return vm.runInContext(name, context); }
function defaultConfig() {
  const rows = typeof context.stage6ConfigDefaults_ === 'function' ? context.stage6ConfigDefaults_() : [];
  return Object.fromEntries(rows.map(([key, val]) => [key, val]));
}
function videoRow(headers, values = {}) {
  const row = headers.map(() => '');
  for (const [key, val] of Object.entries(values)) {
    const index = headers.indexOf(key);
    if (index >= 0) row[index] = val;
  }
  return row;
}
function snapshotRow(headers, { videoId, checkpoint = 30, views = 250, baseline = 1000, relativeVelocity = 1.8 } = {}) {
  const row = headers.map(() => '');
  for (const [key, val] of Object.entries({
    video_id: videoId,
    checkpoint_minutes: checkpoint,
    view_count: views,
    baseline_final_views_median: baseline,
    relative_velocity: relativeVelocity,
  })) {
    const index = headers.indexOf(key);
    if (index >= 0) row[index] = val;
  }
  return row;
}
function makeRuntime({ videos = [], snapshots = [], config = {}, queue = [], timezone = 'America/Los_Angeles' } = {}) {
  const videoHeaders = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  for (const header of selectionHeaders) if (!videoHeaders.includes(header)) videoHeaders.push(header);
  const snapshotHeaders = Array.from(value('STAGE6_SNAPSHOT_HEADERS_'));
  const queueHeaders = Array.from(value('STAGE7_QUEUE_HEADERS_'));
  const mergedConfig = {
    ...defaultConfig(),
    daily_selection_enabled: false,
    daily_selection_max: 2,
    ...config,
  };
  const sheets = [
    new MemorySheet('Config', [['key', 'value', 'description'], ...Object.entries(mergedConfig).map(([key, val]) => [key, val, 'test'])]),
    new MemorySheet('Videos', [videoHeaders, ...videos.map((record) => videoRow(videoHeaders, record))]),
    new MemorySheet('Snapshots', [snapshotHeaders, ...snapshots.map((record) => snapshotRow(snapshotHeaders, record))]),
    new MemorySheet('Queue', [queueHeaders, ...queue.map((record) => queueHeaders.map((header) => record[header] ?? ''))]),
  ];
  const spreadsheet = new MemorySpreadsheet(sheets, timezone);
  context.SpreadsheetApp = { getActiveSpreadsheet() { return this.openById(''); }, openById: () => spreadsheet };
  let lockHeld = false;
  context.LockService = {
    getScriptLock: () => ({
      waitLock() {
        if (lockHeld) throw new Error('ScriptLock is already held');
        lockHeld = true;
      },
      tryLock() {
        if (lockHeld) return false;
        lockHeld = true;
        return true;
      },
      releaseLock() {
        if (!lockHeld) throw new Error('ScriptLock is not held');
        lockHeld = false;
      },
    }),
  };
  return { spreadsheet, sheets, videoHeaders, snapshotHeaders, queueHeaders };
}

test('Stage 9 clock parsing reuses Stage 6 handling for Sheet time values', () => {
  makeRuntime({ timezone: 'America/Los_Angeles' });
  assert.equal(context.stage9ClockMinutes_('06:00', 'daily_selection_time'), 360);
  assert.equal(context.stage9ClockMinutes_(360 / 1440, 'daily_selection_time'), 360);
  assert.equal(context.stage9ClockMinutes_(new FixedDate('1899-12-30T14:00:00Z'), 'daily_selection_time'), 360);
  assert.throws(() => context.stage9ClockMinutes_(1, 'daily_selection_time'), /Stage 9 selection Config invalid/);
});

function candidate(videoId, overrides = {}) {
  return {
    video_id: videoId,
    channel_id: 'creator-1',
    creator_name: 'Creator One',
    video_url: `https://www.youtube.com/watch?v=${videoId}`,
    published_at: '2026-09-25T17:00:00.000Z',
    lifecycle_state: 'CANDIDATE',
    candidate_at: '2026-09-26T06:00:00.000Z',
    hot_mode: 'cold',
    hot_checkpoint: 30,
    ...overrides,
  };
}
function queueRecord(videoId, queueId = `queue-${videoId}`, status = 'PENDING') {
  return { queue_id: queueId, video_id: videoId, url: `https://www.youtube.com/watch?v=${videoId}`, status, attempts: 0 };
}
function runSelection(runtime) {
  return context.stage9RunDailySelection_();
}

test('selection defaults are disabled and selection config rejects invalid values', () => {
  const defaults = defaultConfig();
  assert.equal(defaults.daily_selection_enabled, true);
  assert.equal(defaults.daily_selection_max, 2);
  for (const config of [
    { daily_selection_enabled: 'maybe' },
    { daily_selection_enabled: true, daily_selection_max: 3 },
    { daily_selection_enabled: true, production_timezone: '' },
  ]) {
    const runtime = makeRuntime({ config });
    assert.throws(() => runSelection(runtime), /selection Config/i);
  }
});

test('disabled Selection leaves CANDIDATE and Queue untouched', () => {
  const runtime = makeRuntime({
    config: { daily_selection_max: 99 },
    videos: [candidate('disabled')], snapshots: [{ videoId: 'disabled' }],
  });
  const result = runSelection(runtime);
  assert.equal(result.enabled, false);
  assert.equal(runtime.spreadsheet.getSheetByName('Queue').rows.length, 1);
  assert.equal(runtime.spreadsheet.getSheetByName('Videos').rows[1][runtime.videoHeaders.indexOf('selected_at')], '');
});

test('WATCH and DATA_HOT are ineligible while valid CANDIDATE enqueues PENDING without Selection state coupling', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [candidate('watch', { lifecycle_state: 'WATCH' }), candidate('hot', { lifecycle_state: 'DATA_HOT' }), candidate('candidate')],
    snapshots: [{ videoId: 'candidate' }],
  });
  const result = runSelection(runtime);
  const queue = runtime.spreadsheet.getSheetByName('Queue');
  assert.equal(result.enqueued, 1);
  assert.equal(queue.rows.length, 2);
  assert.equal(queue.rows[1][runtime.queueHeaders.indexOf('video_id')], 'candidate');
  assert.equal(queue.rows[1][runtime.queueHeaders.indexOf('status')], 'PENDING');
  assert.equal(runtime.spreadsheet.getSheetByName('Videos').rows[1][runtime.videoHeaders.indexOf('selected_at')], '');
  assert.equal(runtime.spreadsheet.getSheetByName('Videos').rows[2][runtime.videoHeaders.indexOf('selected_at')], '');
  assert.equal(runtime.spreadsheet.getSheetByName('Videos').rows[3][runtime.videoHeaders.indexOf('selected_at')], '2026-09-26T06:30:00.000Z');
});

test('semantic metadata is optional when disabled and REJECT is excluded when enabled', () => {
  let runtime = makeRuntime({
    config: { daily_selection_enabled: true, semantic_judge_enabled: false },
    videos: [candidate('no-semantic-result')], snapshots: [{ videoId: 'no-semantic-result' }],
  });
  assert.equal(runSelection(runtime).enqueued, 1);

  runtime = makeRuntime({
    config: { daily_selection_enabled: true, semantic_judge_enabled: true },
    videos: [candidate('rejected', { semantic_decision: 'REJECT' })], snapshots: [{ videoId: 'rejected' }],
  });
  const result = runSelection(runtime);
  assert.equal(result.enqueued, 0);
  assert.deepEqual(Array.from(result.skipped, (item) => item.reason), ['SEMANTIC_REJECT']);
  assert.equal(runtime.spreadsheet.getSheetByName('Queue').rows.length, 1);
});

test('current Daily Batch permanently excludes prior-day and after-window-end (08:00) Candidates', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [
      candidate('stale', { published_at: '2026-09-24T17:00:00.000Z' }),
      candidate('late', { published_at: '2026-09-26T00:03:00.000Z' }), // 08:03 Asia/Shanghai
    ],
    snapshots: [{ videoId: 'stale' }, { videoId: 'late' }],
  });
  const result = runSelection(runtime);
  assert.equal(result.enqueued, 0);
  assert.deepEqual(Array.from(result.skipped, (item) => item.reason), ['OUTSIDE_DAILY_BATCH', 'OUTSIDE_DAILY_BATCH']);
  const rows = runtime.spreadsheet.getSheetByName('Videos').rows;
  assert.deepEqual(rows.slice(1).map((row) => row[runtime.videoHeaders.indexOf('selection_result')]), ['ELIMINATED', 'ELIMINATED']);
});

test('Candidate identity, URL, candidate_at, and unique video_id are validated before sorting', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [
      candidate(''),
      candidate('bad-url', { video_url: 'ftp://example.test/video' }),
      candidate('bad-date', { candidate_at: 'not-a-timestamp' }),
      candidate('duplicate-row'),
      candidate('duplicate-row'),
    ],
    snapshots: [
      { videoId: 'bad-url' }, { videoId: 'bad-date' }, { videoId: 'duplicate-row' },
    ],
  });
  const result = runSelection(runtime);
  assert.equal(result.enqueued, 0);
  assert.deepEqual(Array.from(result.skipped, (item) => item.reason), [
    'INVALID_VIDEO_ID', 'INVALID_REQUEST', 'INVALID_CANDIDATE_AT',
    'DUPLICATE_VIDEO_ROW', 'DUPLICATE_VIDEO_ROW',
  ]);
});

test('daily cap selects 0, 1, or at most 2 items and counts existing selected_at in spreadsheet timezone', () => {
  for (const count of [0, 1, 2, 3]) {
    const ids = Array.from({ length: count }, (_, index) => `v${index}`);
    const runtime = makeRuntime({
      config: { daily_selection_enabled: true },
      videos: ids.map((id) => candidate(id)),
      snapshots: ids.map((id) => ({ videoId: id })),
    });
    const result = runSelection(runtime);
    assert.equal(result.enqueued, Math.min(count, 2));
    if (count === 3) {
      assert.equal(Array.from(result.skipped, (item) => item.reason).includes('DAILY_LIMIT'), true);
      const videos = runtime.spreadsheet.getSheetByName('Videos');
      const loser = videos.rows.find((row) => row[runtime.videoHeaders.indexOf('video_id')] === 'v2');
      assert.equal(loser[runtime.videoHeaders.indexOf('selection_result')], 'ELIMINATED');
      assert.equal(loser[runtime.videoHeaders.indexOf('selection_day')], '2026-09-26');
      assert.ok(loser[runtime.videoHeaders.indexOf('selection_closed_at')]);
      const nextDay = context.stage9RunDailySelection_(false, new Date('2026-09-26T22:00:00.000Z'));
      assert.equal(nextDay.enqueued, 0);
      assert.equal(runtime.spreadsheet.getSheetByName('Queue').rows.length, 3);
    }
  }

  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [
      candidate('already-today', { selected_at: '2026-09-26T02:00:00.000Z', selection_day: '2026-09-25' }),
      candidate('another'), candidate('third'),
    ],
    snapshots: [{ videoId: 'already-today' }, { videoId: 'another' }, { videoId: 'third' }],
    queue: [queueRecord('already-today')],
  });
  const result = runSelection(runtime);
  assert.equal(result.enqueued, 1);
  assert.equal(result.selection_day, '2026-09-26');
  assert.equal(runtime.spreadsheet.getSheetByName('Queue').rows.length, 3);

  const fullDay = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [
      candidate('used-1', { selected_at: '2026-09-26T02:00:00.000Z' }),
      candidate('used-2', { selected_at: '2026-09-26T03:00:00.000Z' }),
      candidate('unused'),
    ],
    snapshots: [{ videoId: 'used-1' }, { videoId: 'used-2' }, { videoId: 'unused' }],
    queue: [queueRecord('used-1'), queueRecord('used-2')],
  });
  assert.equal(runSelection(fullDay).enqueued, 0);
  assert.equal(fullDay.spreadsheet.getSheetByName('Queue').rows.length, 3);
});

test('Cold HOT strength normalizes views by required views; strongest candidate sorts first', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [candidate('a', { hot_checkpoint: 60 }), candidate('b', { hot_checkpoint: 30 })],
    snapshots: [
      { videoId: 'a', checkpoint: 60, views: 550, baseline: 10000 },
      { videoId: 'a', checkpoint: 120, views: 999999, baseline: 10000 },
      { videoId: 'b', checkpoint: 30, views: 450, baseline: 10000 },
    ],
  });
  const result = runSelection(runtime);
  const rows = runtime.spreadsheet.getSheetByName('Videos');
  const a = rows.rows[1], b = rows.rows[2];
  assert.equal(result.enqueued, 2);
  assert.equal(a[runtime.videoHeaders.indexOf('selection_hot_strength')], 1.1);
  assert.equal(b[runtime.videoHeaders.indexOf('selection_hot_strength')], 1.8);
  assert.equal(b[runtime.videoHeaders.indexOf('selection_rank')], 1);
  assert.equal(a[runtime.videoHeaders.indexOf('selection_rank')], 2);
});

test('Warm HOT strength divides recorded checkpoint relative velocity by the configured threshold', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true, warm_baseline_relative_velocity_threshold: 1.5 },
    videos: [candidate('warm-a', { hot_mode: 'warm' }), candidate('warm-b', { hot_mode: 'warm' })],
    snapshots: [
      { videoId: 'warm-a', relativeVelocity: 2.7 },
      { videoId: 'warm-b', relativeVelocity: 1.8 },
    ],
  });
  runSelection(runtime);
  const rows = runtime.spreadsheet.getSheetByName('Videos');
  assert.equal(rows.rows[1][runtime.videoHeaders.indexOf('selection_hot_strength')], 1.8);
  assert.equal(rows.rows[2][runtime.videoHeaders.indexOf('selection_hot_strength')], 1.2);
});

test('lexicographic ties resolve by checkpoint, candidate_at, then video_id', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true, daily_selection_max: 2 },
    videos: [
      candidate('z', { hot_checkpoint: 60, candidate_at: '2026-09-26T05:40:00.000Z' }),
      candidate('b', { hot_checkpoint: 30, candidate_at: '2026-09-26T05:40:00.000Z' }),
      candidate('a', { hot_checkpoint: 30, candidate_at: '2026-09-26T05:50:00.000Z' }),
      candidate('c', { hot_checkpoint: 30, candidate_at: '2026-09-26T05:40:00.000Z' }),
    ],
    snapshots: ['z', 'b', 'a', 'c'].map((id) => ({ videoId: id, checkpoint: id === 'z' ? 60 : 30, views: id === 'z' ? 900 : 450, baseline: 10000 })),
  });
  runSelection(runtime);
  const queueIds = runtime.spreadsheet.getSheetByName('Queue').rows.slice(1)
    .map((row) => row[runtime.queueHeaders.indexOf('video_id')]);
  assert.deepEqual(queueIds, ['b', 'c']);
});

test('missing, duplicate, or invalid matching HOT Snapshot makes a Candidate ineligible', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [candidate('missing'), candidate('duplicate'), candidate('invalid')],
    snapshots: [
      { videoId: 'duplicate' }, { videoId: 'duplicate' },
      { videoId: 'invalid', views: 0, baseline: 0 },
    ],
  });
  const result = runSelection(runtime);
  assert.equal(result.enqueued, 0);
  assert.equal(result.skipped_invalid_hot_evidence, 3);
  assert.deepEqual(Array.from(result.skipped, (item) => item.reason), [
    'HOT_SNAPSHOT_NOT_FOUND', 'DUPLICATE_HOT_SNAPSHOT', 'INVALID_COLD_HOT_EVIDENCE',
  ]);
});

test('existing Queue video is reconciled and Selector reruns do not create duplicate rows', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [candidate('existing'), candidate('new')],
    snapshots: [{ videoId: 'existing' }, { videoId: 'new' }],
    queue: [queueRecord('existing', 'stable-queue-id', 'FAILED')],
  });
  const first = runSelection(runtime);
  const second = runSelection(runtime);
  const queue = runtime.spreadsheet.getSheetByName('Queue');
  const rows = runtime.spreadsheet.getSheetByName('Videos');
  assert.equal(first.recovered, 1);
  assert.equal(second.enqueued, 0);
  assert.equal(queue.rows.length, 3);
  assert.equal(rows.rows[1][runtime.videoHeaders.indexOf('queue_id')], 'stable-queue-id');
  assert.equal(rows.rows[2][runtime.videoHeaders.indexOf('queue_id')], queue.rows[2][runtime.queueHeaders.indexOf('queue_id')]);
});

test('persisted queue_id prevents re-enqueue even if its Queue row is absent', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [candidate('already-has-id', { queue_id: 'old-queue-id' })],
    snapshots: [{ videoId: 'already-has-id' }],
  });
  const result = runSelection(runtime);
  assert.equal(result.enqueued, 0);
  assert.equal(runtime.spreadsheet.getSheetByName('Queue').rows.length, 1);
  assert.deepEqual(Array.from(result.skipped, (item) => item.reason), ['PERSISTED_SELECTION_WITHOUT_QUEUE']);
});

test('Queue enqueue succeeds before metadata write failure and next run repairs metadata idempotently', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [candidate('recover-write')],
    snapshots: [{ videoId: 'recover-write' }],
  });
  const videos = runtime.spreadsheet.getSheetByName('Videos');
  videos.failNextWriteFor = 'selected_at';
  assert.throws(() => runSelection(runtime), /injected sheet write failure/);
  assert.equal(runtime.spreadsheet.getSheetByName('Queue').rows.length, 2);
  const recovered = runSelection(runtime);
  assert.equal(recovered.recovered, 1);
  assert.equal(runtime.spreadsheet.getSheetByName('Queue').rows.length, 2);
  assert.ok(videos.rows[1][runtime.videoHeaders.indexOf('selected_at')]);
});

test('minute scheduler runs Selection only when the configured one-shot slot is due', () => {
  const calls = [];
  const configWrites = [];
  let selectionEnabled = true;
  const saved = {
    readConfig: context.stage6ReadConfig_,
    discover: context.stage6DiscoverUploads,
    snapshots: context.stage6CaptureDueSnapshots,
    hot: context.stage6RunHotEngine,
    baseline: context.stage6EnsureCreatorBaselines_,
    candidates: context.stage6RefreshCandidatePool_,
    selection: context.stage9RunDailySelection_,
    schedule: context.stage6ScheduleDecision_,
    setConfig: context.stage6SetConfig_,
  };
  context.stage6ReadConfig_ = () => ({ monitor_status: 'ACTIVE', production_timezone: 'Asia/Shanghai' });
  context.stage6ScheduleDecision_ = () => ({
    day: '2026-09-26', timezone: 'Asia/Shanghai', discovery_slot: null,
    final_sweep_due: false, selection_due: true, rank2_cutoff_reached: false,
  });
  context.stage6DiscoverUploads = () => { calls.push('discovery'); return { found: 0 }; };
  context.stage6CaptureDueSnapshots = () => { calls.push('snapshots'); return 0; };
  context.stage6RunHotEngine = () => { calls.push('hot'); return { checked: 0 }; };
  context.stage6EnsureCreatorBaselines_ = () => { calls.push('baseline-bootstrap'); return { processed: 0 }; };
  context.stage6RefreshCandidatePool_ = () => { calls.push('candidate-refresh'); return 0; };
  context.stage9RunDailySelection_ = (scriptLockAlreadyHeld) => {
    calls.push('daily-selection');
    assert.equal(scriptLockAlreadyHeld, true);
    return { enabled: selectionEnabled };
  };
  context.stage6SetConfig_ = (key, value) => configWrites.push([key, value]);
  try {
    const result = context.stage6RunMonitor();
    assert.deepEqual(calls, ['baseline-bootstrap', 'snapshots', 'hot', 'candidate-refresh', 'daily-selection']);
    assert.deepEqual(JSON.parse(JSON.stringify(result.selection)), { enabled: true });
    assert.deepEqual(configWrites, [['last_daily_selection_day', '2026-09-26']]);

    calls.length = 0;
    configWrites.length = 0;
    selectionEnabled = false;
    context.stage6RunMonitor();
    assert.deepEqual(calls, ['baseline-bootstrap', 'snapshots', 'hot', 'candidate-refresh', 'daily-selection']);
    assert.deepEqual(configWrites, []);
  } finally {
    context.stage6ReadConfig_ = saved.readConfig;
    context.stage6DiscoverUploads = saved.discover;
    context.stage6CaptureDueSnapshots = saved.snapshots;
    context.stage6RunHotEngine = saved.hot;
    context.stage6EnsureCreatorBaselines_ = saved.baseline;
    context.stage6RefreshCandidatePool_ = saved.candidates;
    context.stage9RunDailySelection_ = saved.selection;
    context.stage6ScheduleDecision_ = saved.schedule;
    context.stage6SetConfig_ = saved.setConfig;
  }
});

test('real Monitor→Selection→Stage 7 bridge shares the Monitor lock and creates one PENDING row', () => {
  const runtime = makeRuntime({
    config: { daily_selection_enabled: true },
    videos: [candidate('locked-pipeline')],
    snapshots: [{ videoId: 'locked-pipeline' }],
  });
  const saved = {
    discover: context.stage6DiscoverUploads,
    snapshots: context.stage6CaptureDueSnapshots,
    hot: context.stage6RunHotEngine,
    baseline: context.stage6EnsureCreatorBaselines_,
    candidates: context.stage6RefreshCandidatePool_,
  };
  context.stage6DiscoverUploads = () => ({ found: 0 });
  context.stage6CaptureDueSnapshots = () => 0;
  context.stage6RunHotEngine = () => ({ checked: 0 });
  context.stage6EnsureCreatorBaselines_ = () => ({ processed: 0 });
  context.stage6RefreshCandidatePool_ = () => 0;
  try {
    const result = context.stage6RunMonitor();
    assert.equal(result.status, 'ACTIVE');
    assert.equal(result.selection.enqueued, 1);
    const queue = runtime.spreadsheet.getSheetByName('Queue');
    assert.equal(queue.rows.length, 2);
    assert.equal(queue.rows[1][runtime.queueHeaders.indexOf('status')], 'PENDING');
  } finally {
    context.stage6DiscoverUploads = saved.discover;
    context.stage6CaptureDueSnapshots = saved.snapshots;
    context.stage6RunHotEngine = saved.hot;
    context.stage6EnsureCreatorBaselines_ = saved.baseline;
    context.stage6RefreshCandidatePool_ = saved.candidates;
  }
});
