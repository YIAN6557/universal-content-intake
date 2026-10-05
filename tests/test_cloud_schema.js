const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const context = vm.createContext({ console, Date, Math, Set, Map, Number, String, Array, Object, JSON });
for (const file of ['monitor.gs', 'discovery.gs', 'stats.gs', 'scoring.gs', 'semantic.gs', 'selection.gs']) {
  const fullPath = path.join(scriptPath, file);
  vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
}

function scriptValue(name) { return vm.runInContext(name, context); }

class MemorySheet {
  constructor(name, rows = []) {
    this.name = name;
    this.rows = rows.map((row) => row.slice());
    this.frozenRows = 0;
    this.writeCount = 0;
  }

  getName() { return this.name; }
  setName(name) { this.name = name; }
  setFrozenRows(count) { this.frozenRows = count; }
  getLastRow() {
    for (let index = this.rows.length - 1; index >= 0; index -= 1) {
      if (this.rows[index].some((value) => value !== '' && value !== null && value !== undefined)) return index + 1;
    }
    return 0;
  }
  getLastColumn() {
    let last = 0;
    for (const row of this.rows) {
      for (let index = row.length - 1; index >= 0; index -= 1) {
        if (row[index] !== '' && row[index] !== null && row[index] !== undefined) {
          last = Math.max(last, index + 1);
          break;
        }
      }
    }
    return last;
  }
  getRange(row, column, rowCount = 1, columnCount = 1) {
    const sheet = this;
    return {
      getValues() {
        return Array.from({ length: rowCount }, (_, rowOffset) =>
          Array.from({ length: columnCount }, (_, columnOffset) =>
            sheet.rows[row + rowOffset - 1]?.[column + columnOffset - 1] ?? ''));
      },
      setValues(values) {
        sheet.writeCount += 1;
        values.forEach((valuesRow, rowOffset) => {
          const targetRow = row + rowOffset - 1;
          sheet.rows[targetRow] ||= [];
          valuesRow.forEach((value, columnOffset) => {
            sheet.rows[targetRow][column + columnOffset - 1] = value;
          });
        });
      },
      setValue(value) {
        sheet.writeCount += 1;
        const targetRow = row - 1;
        sheet.rows[targetRow] ||= [];
        sheet.rows[targetRow][column - 1] = value;
      },
    };
  }
  getDataRange() {
    const rowCount = Math.max(this.getLastRow(), 1);
    const columnCount = Math.max(this.getLastColumn(), 1);
    return this.getRange(1, 1, rowCount, columnCount);
  }
  appendRow(values) {
    this.writeCount += 1;
    this.rows[this.getLastRow()] = values.slice();
  }
  snapshot() { return JSON.parse(JSON.stringify(this.rows)); }
}

class MemorySpreadsheet {
  constructor(sheets) { this.sheets = sheets; }
  getSheetByName(name) { return this.sheets.find((sheet) => sheet.name === name) || null; }
  insertSheet(name) {
    const sheet = new MemorySheet(name);
    this.sheets.push(sheet);
    return sheet;
  }
}

function requiredSheets({ videos, snapshots, config } = {}) {
  const creatorHeaders = ['creator_name', 'channel_id', 'channel_url', 'uploads_playlist_id', 'enabled', 'priority', 'trusted_creator', 'notes'];
  const baselineHeaders = ['creator_name', 'channel_id', 'video_id', 'title', 'published_at', 'view_count', 'like_count', 'comment_count', 'like_rate'];
  return [
    new MemorySheet('Creators', [creatorHeaders, ['Example Space Channel', 'UCexampleCreatorId000000', 'https://www.youtube.com/channel/UCexampleCreatorId000000', 'UUexampleCreatorId000000', true, 1, false, 'fixture']]),
    new MemorySheet('Baseline', [baselineHeaders, ['Example Space Channel', 'UCexampleCreatorId000000', 'old-1', 'old', '2026-09-01T00:00:00Z', 1000, 20, 2, 0.02]]),
    ...(videos ? [videos] : [new MemorySheet('Sheet1')]),
    ...(snapshots ? [snapshots] : []),
    ...(config ? [config] : []),
  ];
}

function runSetup(sheets) {
  const spreadsheet = new MemorySpreadsheet(sheets);
  context.SpreadsheetApp = { getActiveSpreadsheet() { return this.openById(''); }, openById: () => spreadsheet };
  context.Logger = { log: () => {} };
  context.stage6SetupSheets();
  return spreadsheet;
}

function rowsByKey(sheet) {
  return Object.fromEntries(sheet.rows.slice(1).filter((row) => row[0]).map((row) => [row[0], row[1]]));
}

test('first setup initializes empty Videos, Snapshots, and Config and is idempotent', () => {
  const spreadsheet = runSetup(requiredSheets());
  assert.deepEqual(spreadsheet.getSheetByName('Videos').rows[0], Array.from(scriptValue('STAGE6_VIDEO_HEADERS_')));
  assert.deepEqual(spreadsheet.getSheetByName('Snapshots').rows[0], Array.from(scriptValue('STAGE6_SNAPSHOT_HEADERS_')));
  const config = spreadsheet.getSheetByName('Config');
  const firstConfig = rowsByKey(config);
  const formalHotKeys = [
    'warm_baseline_min_complete_watch', 'cold_start_final_views_history_window',
    'cold_start_checkpoint_30_ratio', 'cold_start_checkpoint_60_ratio',
    'cold_start_checkpoint_120_ratio', 'cold_start_like_rate_multiplier',
    'warm_baseline_like_rate_multiplier', 'acceleration_min_snapshot_index',
    'warm_baseline_relative_velocity_threshold', 'normal_after_minutes',
  ];
  assert.ok(formalHotKeys.every((key) => Object.hasOwn(firstConfig, key)));
  assert.equal(firstConfig.cold_start_checkpoint_30_ratio, 0.025);
  assert.equal(firstConfig.warm_baseline_like_rate_multiplier, 0.8);
  assert.equal(firstConfig.daily_selection_enabled, true);
  assert.equal(firstConfig.daily_selection_max, 2);
  assert.equal(firstConfig.selection_lookback_hours, undefined);
  assert.equal(firstConfig.production_timezone, 'Asia/Shanghai');
  assert.equal(firstConfig.discovery_window_start, '00:00');
  assert.equal(firstConfig.discovery_window_end, '08:00');
  assert.equal(firstConfig.snapshot_stage_minutes, '30,60,120');
  assert.equal(firstConfig.daily_selection_time, '10:00');
  assert.equal(firstConfig.rank2_start_cutoff, '12:00');
  assert.equal(firstConfig.processing_concurrency, 1);
  assert.ok(firstConfig.monitor_started_at);
  const afterFirstSetup = spreadsheet.sheets.map((sheet) => [sheet.name, sheet.snapshot()]);

  context.stage6SetupSheets();

  assert.deepEqual(spreadsheet.sheets.map((sheet) => [sheet.name, sheet.snapshot()]), afterFirstSetup);
  assert.equal(spreadsheet.getSheetByName('Creators').getLastRow(), 2);
});

test('old schema with data rows gets only missing headers appended and preserves data', () => {
  const oldVideoHeaders = Array.from(scriptValue('STAGE6_VIDEO_HEADERS_')).slice(0, 27);
  const oldSnapshotHeaders = Array.from(scriptValue('STAGE6_SNAPSHOT_HEADERS_')).slice(0, 18);
  const videoData = oldVideoHeaders.map((_, index) => index === 0 ? 'video-1' : `video-value-${index}`);
  const snapshotData = oldSnapshotHeaders.map((_, index) => index === 0 ? 'video-1' : `snapshot-value-${index}`);
  const videos = new MemorySheet('Videos', [oldVideoHeaders, videoData]);
  const snapshots = new MemorySheet('Snapshots', [oldSnapshotHeaders, snapshotData]);
  const beforeVideo = videos.snapshot();
  const beforeSnapshots = snapshots.snapshot();
  const spreadsheet = runSetup(requiredSheets({ videos, snapshots }));

  assert.deepEqual(videos.rows[0].slice(0, oldVideoHeaders.length), oldVideoHeaders);
  assert.deepEqual(videos.rows[1].slice(0, oldVideoHeaders.length), beforeVideo[1]);
  assert.deepEqual(snapshots.rows[0].slice(0, oldSnapshotHeaders.length), oldSnapshotHeaders);
  assert.deepEqual(snapshots.rows[1].slice(0, oldSnapshotHeaders.length), beforeSnapshots[1]);
  assert.deepEqual(videos.rows[0], Array.from(scriptValue('STAGE6_VIDEO_HEADERS_')));
  assert.deepEqual(snapshots.rows[0], Array.from(scriptValue('STAGE6_SNAPSHOT_HEADERS_')));
  assert.equal(spreadsheet.getSheetByName('Videos'), videos);
});

test('partially migrated schema appends only absent HOT and baseline fields', () => {
  const targetVideos = Array.from(scriptValue('STAGE6_VIDEO_HEADERS_'));
  const partialVideoHeaders = targetVideos.slice(0, 27).concat(['data_hot_at', 'hot_checkpoint']);
  const partialSnapshotHeaders = Array.from(scriptValue('STAGE6_SNAPSHOT_HEADERS_')).slice(0, 18)
    .concat(['creator_id', 'velocity', 'historical_same_checkpoint_median_like_rate']);
  const videos = new MemorySheet('Videos', [partialVideoHeaders, partialVideoHeaders.map((_, i) => `v${i}`)]);
  const snapshots = new MemorySheet('Snapshots', [partialSnapshotHeaders, partialSnapshotHeaders.map((_, i) => `s${i}`)]);
  const beforeVideoRow = videos.rows[1].slice();
  const beforeSnapshotRow = snapshots.rows[1].slice();
  runSetup(requiredSheets({ videos, snapshots }));

  assert.deepEqual(videos.rows[0].slice(0, partialVideoHeaders.length), partialVideoHeaders);
  assert.deepEqual(videos.rows[1].slice(0, partialVideoHeaders.length), beforeVideoRow);
  assert.deepEqual(snapshots.rows[0].slice(0, partialSnapshotHeaders.length), partialSnapshotHeaders);
  assert.deepEqual(snapshots.rows[1].slice(0, partialSnapshotHeaders.length), beforeSnapshotRow);
  assert.equal(new Set(videos.rows[0]).size, videos.rows[0].length);
  assert.equal(new Set(snapshots.rows[0]).size, snapshots.rows[0].length);
  assert.ok(videos.rows[0].includes('hot_mode'));
  assert.ok(snapshots.rows[0].includes('baseline_final_views_median'));
});

test('manual Config values survive setup and missing formal keys receive defaults', () => {
  const config = new MemorySheet('Config', [
    ['key', 'value', 'description'],
    ['warm_baseline_min_complete_watch', 14, 'user tuned'],
    ['cold_start_checkpoint_30_ratio', 0.031, 'user tuned'],
    ['daily_selection_enabled', true, 'operator-controlled'],
    ['selection_lookback_hours', 48, 'operator-controlled'],
    ['snapshot_stage_minutes', '30,60,120,240', 'operator tuned'],
    ['normal_after_minutes', 240, 'operator tuned'],
  ]);
  const spreadsheet = runSetup(requiredSheets({ config }));
  const values = rowsByKey(spreadsheet.getSheetByName('Config'));
  assert.equal(values.warm_baseline_min_complete_watch, 14);
  assert.equal(values.cold_start_checkpoint_30_ratio, 0.031);
  assert.equal(values.cold_start_checkpoint_60_ratio, 0.05);
  assert.equal(values.daily_selection_enabled, true);
  assert.equal(values.selection_lookback_hours, 48);
  assert.equal(values.daily_selection_max, 2);
  assert.equal(values.snapshot_stage_minutes, '30,60,120,240');
  assert.equal(values.normal_after_minutes, 240);
});

test('legacy six-hour WATCH Config migrates only retired schedule values and remains idempotent', () => {
  const config = new MemorySheet('Config', [
    ['key', 'value', 'description'],
    ['snapshot_stage_minutes', '30,60,120,180,360', 'legacy default'],
    ['normal_after_minutes', 360, 'legacy default'],
    ['daily_selection_enabled', false, 'operator-controlled'],
    ['cold_start_checkpoint_30_ratio', 0.027, 'operator-controlled'],
  ]);
  const spreadsheet = runSetup(requiredSheets({ config }));
  const first = config.snapshot();
  const values = rowsByKey(config);
  assert.equal(values.snapshot_stage_minutes, '30,60,120');
  assert.equal(values.normal_after_minutes, 120);
  assert.equal(values.daily_selection_enabled, false);
  assert.equal(values.cold_start_checkpoint_30_ratio, 0.027);

  context.stage6SetupSheets();
  assert.deepEqual(config.snapshot(), first);
  assert.equal(rowsByKey(spreadsheet.getSheetByName('Config')).snapshot_stage_minutes, '30,60,120');
});

test('Config edits are read on the next invocation without in-memory caching', () => {
  const spreadsheet = runSetup(requiredSheets());
  const config = spreadsheet.getSheetByName('Config');
  const index = config.rows.findIndex((row) => row[0] === 'cold_start_checkpoint_30_ratio');
  config.getRange(index + 1, 2).setValue(0.041);
  assert.equal(context.stage6ReadHotConfig_().checkpointRatios[30], 0.041);
});

test('duplicate schema headers fail before migration writes', () => {
  const duplicated = Array.from(scriptValue('STAGE6_VIDEO_HEADERS_')).slice(0, 27).concat(['data_hot_at', 'data_hot_at']);
  const videos = new MemorySheet('Videos', [duplicated, ['v1', ...Array(duplicated.length - 1).fill('keep')]]);
  const before = videos.snapshot();
  const writes = videos.writeCount;
  assert.throws(() => context.stage6EnsureHeaders_(videos, Array.from(scriptValue('STAGE6_VIDEO_HEADERS_'))), /duplicate header/i);
  assert.deepEqual(videos.snapshot(), before);
  assert.equal(videos.writeCount, writes);
});

test('duplicate Config keys are rejected instead of selecting an ambiguous value', () => {
  const config = new MemorySheet('Config', [
    ['key', 'value', 'description'],
    ['warm_baseline_min_complete_watch', 10, 'first'],
    ['warm_baseline_min_complete_watch', 12, 'second'],
  ]);
  const spreadsheet = new MemorySpreadsheet([config]);
  context.SpreadsheetApp = { getActiveSpreadsheet() { return this.openById(''); }, openById: () => spreadsheet };
  assert.throws(() => context.stage6ReadConfig_(), /duplicate Config key/i);
});

test('Cold Baseline excludes the current WATCH video before choosing the latest history window', () => {
  const originalReadTable = context.stage6ReadTable_;
  const headers = ['creator_name', 'channel_id', 'video_id', 'published_at', 'view_count', 'like_rate'];
  const rows = [
    ['Creator', 'creator-1', 'old-a', '2026-09-01T00:00:00Z', 100, 0.01],
    ['Creator', 'creator-1', 'old-b', '2026-09-02T00:00:00Z', 200, 0.02],
    ['Creator', 'creator-1', 'current-watch', '2026-09-03T00:00:00Z', 999999, 0.99],
    ['Other', 'creator-2', 'other', '2026-09-04T00:00:00Z', 800000, 0.8],
  ];
  context.stage6ReadTable_ = () => ({ headers, rows });
  try {
    const got = context.stage6CreatorBaseline_('creator-1', 2, 'current-watch');
    assert.equal(got.sample_count, 2);
    assert.equal(got.final_views_median, 150);
    assert.equal(got.historical_median_like_rate, 0.015);
  } finally {
    context.stage6ReadTable_ = originalReadTable;
  }
});

test('Cold Like Rate uses valid values inside the selected 20-row Baseline window', () => {
  const originalReadTable = context.stage6ReadTable_;
  const headers = ['creator_name', 'channel_id', 'video_id', 'published_at', 'view_count', 'like_rate'];
  const rows = Array.from({ length: 20 }, (_, index) => [
    'Creator', 'creator-1', `history-${index}`,
    new Date(Date.UTC(2026, 0, index + 1)).toISOString(),
    100 + index * 10, index === 19 ? '' : (index + 1) / 1000,
  ]);
  context.stage6ReadTable_ = () => ({ headers, rows });
  try {
    const got = context.stage6CreatorBaseline_('creator-1', 20, 'current');
    assert.equal(got.history_complete, true);
    assert.equal(got.sample_count, 20);
    assert.equal(got.final_views_median, 195);
    assert.equal(got.historical_median_like_rate, 0.01);
  } finally {
    context.stage6ReadTable_ = originalReadTable;
  }
});

test('Cold Like Rate returns one valid sample or null when no valid samples exist', () => {
  const originalReadTable = context.stage6ReadTable_;
  const headers = ['creator_name', 'channel_id', 'video_id', 'published_at', 'view_count', 'like_rate'];
  const makeRows = (validValues) => Array.from({ length: 20 }, (_, index) => [
    'Creator', 'creator-1', `history-${index}`,
    new Date(Date.UTC(2026, 0, index + 1)).toISOString(),
    100 + index * 10, validValues[index] ?? '',
  ]);
  let rows = makeRows({ 7: 0.073 });
  context.stage6ReadTable_ = () => ({ headers, rows });
  try {
    const one = context.stage6CreatorBaseline_('creator-1', 20, 'current');
    assert.equal(one.historical_median_like_rate, 0.073);
    const viewsMedian = one.final_views_median;
    rows = makeRows({});
    const none = context.stage6CreatorBaseline_('creator-1', 20, 'current');
    assert.equal(none.historical_median_like_rate, null);
    assert.equal(none.final_views_median, viewsMedian);
  } finally {
    context.stage6ReadTable_ = originalReadTable;
  }
});

test('Cold Like Rate remains available when the same row is unusable for Views baseline', () => {
  const originalReadTable = context.stage6ReadTable_;
  const headers = ['creator_name', 'channel_id', 'video_id', 'published_at', 'view_count', 'like_rate'];
  const rows = Array.from({ length: 20 }, (_, index) => [
    'Creator', 'creator-1', `history-${index}`,
    new Date(Date.UTC(2026, 0, index + 1)).toISOString(),
    index === 19 ? '' : 100 + index * 10, 0.02,
  ]);
  context.stage6ReadTable_ = () => ({ headers, rows });
  try {
    const got = context.stage6CreatorBaseline_('creator-1', 20, 'current');
    assert.equal(got.history_complete, false);
    assert.equal(got.sample_count, 19);
    assert.equal(got.final_views_median, 190);
    assert.equal(got.historical_median_like_rate, 0.02);
  } finally {
    context.stage6ReadTable_ = originalReadTable;
  }
});

test('Warm Baseline excludes the current video and incomplete or cross-Creator WATCH samples', () => {
  const videoHeaders = ['video_id', 'channel_id'];
  const videoRows = [
    ['target', 'creator-1'], ['peer-a', 'creator-1'], ['peer-b', 'creator-1'],
    ['peer-incomplete', 'creator-1'], ['other', 'creator-2'],
  ];
  const snapshotHeaders = ['video_id', 'channel_id', 'snapshot_stage_minutes', 'view_count', 'like_rate'];
  const stages = [30, 60, 120];
  const snapshotRows = [];
  for (const stage of stages) snapshotRows.push(['target', 'creator-1', stage, 900000, 0.9]);
  for (const stage of stages) snapshotRows.push(['peer-a', 'creator-1', stage, stage * 10, 0.05]);
  for (const stage of stages) snapshotRows.push(['peer-b', 'creator-1', stage, stage * 30, stage === 60 ? '' : 0.07]);
  snapshotRows.push(['peer-incomplete', 'creator-1', 30, 5000, 0.5], ['peer-incomplete', 'creator-1', 60, 6000, 0.6]);
  for (const stage of stages) snapshotRows.push(['other', 'creator-2', stage, 800000, 0.8]);

  const got = context.stage6CompletePeerStats_('target', 'creator-1', 60,
    videoHeaders, videoRows, snapshotHeaders, snapshotRows, stages);
  assert.equal(got.sample_count, 2);
  assert.equal(got.median_views, 1200);
  assert.equal(got.median_like_rate, 0.05);
  assert.equal(2400 / got.median_views, 2);
  assert.equal(context.stage6CompleteWatchSampleCount_('creator-1', videoHeaders, videoRows,
    snapshotHeaders, snapshotRows, stages, 'target'), 2);
});

test('Warm Like Rate is null when all complete peer rates are unavailable', () => {
  const videoHeaders = ['video_id', 'channel_id'];
  const videoRows = [['target', 'creator-1'], ['peer-a', 'creator-1'], ['peer-b', 'creator-1']];
  const snapshotHeaders = ['video_id', 'channel_id', 'snapshot_stage_minutes', 'view_count', 'like_rate'];
  const stages = [30, 60];
  const snapshotRows = [];
  for (const id of ['target', 'peer-a', 'peer-b']) {
    for (const stage of stages) snapshotRows.push([id, 'creator-1', stage, id === 'target' ? 10000 : stage * 10, '']);
  }
  const got = context.stage6CompletePeerStats_('target', 'creator-1', 60,
    videoHeaders, videoRows, snapshotHeaders, snapshotRows, stages);
  assert.equal(got.sample_count, 2);
  assert.equal(got.median_views, 600);
  assert.equal(got.median_like_rate, null);
});

test('Unavailable Like Rate fails only its gate; positive acceleration still passes', () => {
  const config = Object.fromEntries(scriptValue('STAGE6_CONFIG_ROWS_').map((row) => [row[0], row[1]]));
  const got = context.stage6EvaluateHot_({
    complete_watch_count: 0, checkpoint_minutes: 60, current_view_count: 1000,
    current_like_rate: null, acceleration: 1, baseline_final_views_median: 10000,
    historical_median_like_rate: null,
  }, config);
  assert.equal(got.like_rate_pass, false);
  assert.equal(got.acceleration_pass, true);
  assert.equal(got.matched, true);

  const noHistoricalRates = context.stage6EvaluateHot_({
    complete_watch_count: 0, checkpoint_minutes: 60, current_view_count: 1000,
    current_like_rate: 0.1, acceleration: 0, baseline_final_views_median: 10000,
    historical_median_like_rate: null,
  }, config);
  assert.equal(noHistoricalRates.like_rate_pass, false);
  assert.equal(noHistoricalRates.acceleration_pass, false);
  assert.equal(noHistoricalRates.matched, false);
});

test('complete WATCH count excludes its current video and requires a valid view value at every checkpoint', () => {
  const videoHeaders = ['video_id', 'channel_id'];
  const videoRows = [['target', 'creator-1'], ['peer-a', 'creator-1'], ['peer-b', 'creator-1'], ['other', 'creator-2']];
  const snapshotHeaders = ['video_id', 'channel_id', 'snapshot_stage_minutes', 'view_count'];
  const stages = [30, 60];
  const snapshotRows = [
    ['target', 'creator-1', 30, 100], ['target', 'creator-1', 60, 200],
    ['peer-a', 'creator-1', 30, 100], ['peer-a', 'creator-1', 60, 200],
    ['peer-b', 'creator-1', 30, 100], ['peer-b', 'creator-1', 60, ''],
    ['other', 'creator-2', 30, 100], ['other', 'creator-2', 60, 200],
  ];
  assert.equal(context.stage6CompleteWatchSampleCount_('creator-1', videoHeaders, videoRows,
    snapshotHeaders, snapshotRows, stages, 'target'), 1);
  const completeIds = { 'creator-1': new Set(['target', 'peer-a', 'peer-b']) };
  assert.equal(context.stage6CompleteWatchCountExcludingVideo_(completeIds, 'creator-1', 'target'), 2);
  assert.equal(context.stage6CompleteWatchCountExcludingVideo_(completeIds, 'creator-1', 'peer-a'), 2);
  assert.equal(context.stage6CompleteWatchCountExcludingVideo_(completeIds, 'creator-2', 'other'), 0);
});

test('HOT engine determines Warm eligibility from other completed WATCH videos only', () => {
  const originalReadTable = context.stage6ReadTable_;
  const originalReadConfig = context.stage6ReadConfig_;
  const videoHeaders = Array.from(scriptValue('STAGE6_VIDEO_HEADERS_'));
  const snapshotHeaders = Array.from(scriptValue('STAGE6_SNAPSHOT_HEADERS_'));
  const videoRecords = Array.from({ length: 10 }, (_, index) => {
    const row = videoHeaders.map(() => '');
    row[videoHeaders.indexOf('video_id')] = `video-${index}`;
    row[videoHeaders.indexOf('channel_id')] = 'creator-1';
    row[videoHeaders.indexOf('lifecycle_state')] = 'WATCH';
    row[videoHeaders.indexOf('complete_watch')] = true;
    return row;
  });
  const snapshotRecords = videoRecords.map((row, index) => {
    const snapshot = snapshotHeaders.map(() => '');
    snapshot[snapshotHeaders.indexOf('video_id')] = `video-${index}`;
    snapshot[snapshotHeaders.indexOf('channel_id')] = 'creator-1';
    snapshot[snapshotHeaders.indexOf('checkpoint_minutes')] = 60;
    snapshot[snapshotHeaders.indexOf('captured_at')] = '2026-09-25T12:00:00Z';
    snapshot[snapshotHeaders.indexOf('view_count')] = 600;
    snapshot[snapshotHeaders.indexOf('like_rate')] = 0.08;
    snapshot[snapshotHeaders.indexOf('acceleration')] = 0;
    snapshot[snapshotHeaders.indexOf('baseline_final_views_median')] = 10000;
    snapshot[snapshotHeaders.indexOf('historical_median_like_rate')] = 0.1;
    snapshot[snapshotHeaders.indexOf('historical_same_checkpoint_median_views')] = 1000;
    snapshot[snapshotHeaders.indexOf('historical_same_checkpoint_median_like_rate')] = 0.1;
    return snapshot;
  });
  const videosSheet = new MemorySheet('Videos', [videoHeaders, ...videoRecords]);
  const snapshotsSheet = new MemorySheet('Snapshots', [snapshotHeaders, ...snapshotRecords]);
  context.stage6ReadTable_ = (name) => name === 'Videos'
    ? { sheet: videosSheet, headers: videoHeaders, rows: videosSheet.rows.slice(1) }
    : { sheet: snapshotsSheet, headers: snapshotHeaders, rows: snapshotsSheet.rows.slice(1) };
  context.stage6ReadConfig_ = () => Object.fromEntries(scriptValue('STAGE6_CONFIG_ROWS_').map((row) => [row[0], row[1]]));
  try {
    const result = context.stage6RunHotEngine();
    assert.equal(result.evaluated_hot, 10);
    assert.ok(videosSheet.rows.slice(1).every((row) => row[videoHeaders.indexOf('hot_mode')] === 'cold'));
  } finally {
    context.stage6ReadTable_ = originalReadTable;
    context.stage6ReadConfig_ = originalReadConfig;
  }
});

test('video tail update stores the Cold Baseline median without undefined aliases', () => {
  const headers = Array.from(scriptValue('STAGE6_VIDEO_HEADERS_'));
  const row = headers.map((header) => header === 'video_id' ? 'v1' : '');
  const record = {
    video_id: 'v1', captured_at: '2026-09-25T00:30:00Z', snapshot_stage_minutes: 30,
    metrics: { actual_elapsed_seconds: 1800, view_count: 500, like_count: 10, comment_count: 1,
      views_per_hour: 1000, interval_growth: null, interval_growth_per_hour: null, acceleration: null, like_rate: 0.02 },
    coldBaseline: { final_views_median: 10000 }, warmBaseline: { median_views: null }, relativeVelocity: null,
  };
  const tail = context.stage6UpdatedVideoTail_(headers, row, record, [30, 60], []);
  assert.equal(tail[headers.indexOf('creator_scale_median_views') - headers.indexOf('first_snapshot_at')], 10000);
});

test('Snapshot row writes Stage 6 baseline fields through the migrated header names', () => {
  const headers = Array.from(scriptValue('STAGE6_SNAPSHOT_HEADERS_'));
  const row = context.stage6SnapshotAsRow_(headers, {
    video_id: 'video-1', channel_id: 'creator-1', snapshot_stage_minutes: 60,
    published_at: '2026-09-25T10:00:00Z', captured_at: '2026-09-25T11:00:00Z',
    metrics: { actual_elapsed_seconds: 3600, view_count: 2400, like_count: 120,
      comment_count: 5, views_per_hour: 2400, interval_growth: 900,
      interval_growth_per_hour: 1800, acceleration: 400, like_rate: 0.05,
      interval_view_growth: 900, velocity: 1800 },
    coldBaseline: { final_views_median: 50000, historical_median_like_rate: 0.04 },
    warmBaseline: { median_views: 1000, median_like_rate: 0.03 },
    relativeVelocity: 2.4, warmCount: 10,
  });
  const get = (name) => row[headers.indexOf(name)];
  assert.equal(get('creator_id'), 'creator-1');
  assert.equal(get('checkpoint_minutes'), 60);
  assert.equal(get('interval_view_growth'), 900);
  assert.equal(get('velocity'), 1800);
  assert.equal(get('baseline_final_views_median'), 50000);
  assert.equal(get('historical_median_like_rate'), 0.04);
  assert.equal(get('historical_same_checkpoint_median_views'), 1000);
  assert.equal(get('historical_same_checkpoint_median_like_rate'), 0.03);
});
