const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const context = vm.createContext({ console, Date, Math, Set, Map, Number, String, Array, Object, JSON, Error });
for (const file of ['monitor.gs', 'discovery.gs', 'stats.gs', 'scoring.gs', 'baseline.gs']) {
  const fullPath = path.join(scriptPath, file);
  vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
}

class MemorySheet {
  constructor(name, rows) { this.name = name; this.rows = rows.map((row) => row.slice()); }
  getName() { return this.name; }
  getLastRow() { return this.rows.length; }
  getLastColumn() { return Math.max(0, ...this.rows.map((row) => row.length)); }
  getDataRange() { return this.getRange(1, 1, this.getLastRow(), this.getLastColumn()); }
  getRange(row, column, rowCount = 1, columnCount = 1) {
    const sheet = this;
    return {
      getValues() { return Array.from({ length: rowCount }, (_, r) => Array.from({ length: columnCount }, (_, c) => sheet.rows[row + r - 1]?.[column + c - 1] ?? '')); },
      setValues(values) { values.forEach((valuesRow, r) => { sheet.rows[row + r - 1] ||= []; valuesRow.forEach((value, c) => { sheet.rows[row + r - 1][column + c - 1] = value; }); }); },
      setValue(value) { sheet.rows[row - 1] ||= []; sheet.rows[row - 1][column - 1] = value; },
      setNumberFormat() {},
    };
  }
}

function hotConfig(overrides = {}) {
  return {
    production_timezone: 'Asia/Shanghai', snapshot_stage_minutes: '30,60,120', warm_baseline_min_complete_watch: 10,
    cold_start_final_views_history_window: 20, cold_start_checkpoint_30_ratio: 0.025,
    cold_start_checkpoint_60_ratio: 0.05, cold_start_checkpoint_120_ratio: 0.1,
    cold_start_like_rate_multiplier: 0.8, acceleration_min_snapshot_index: 2,
    warm_baseline_relative_velocity_threshold: 1.5, warm_baseline_like_rate_multiplier: 0.8,
    normal_after_minutes: 120, ...overrides,
  };
}

test('Cold baseline seed summaries use every available view sample and valid Like Rates only', () => {
  const headers = ['creator_name', 'channel_id', 'video_id', 'published_at', 'view_count', 'like_count', 'like_rate'];
  const rows = [
    ['C', 'creator-1', 'v1', '2026-09-01T00:00:00Z', 100, '', ''],
    ['C', 'creator-1', 'v2', '2026-09-02T00:00:00Z', 300, 30, 0.1],
  ];
  const summary = context.stage6SummarizeCreatorBaseline_(rows, headers, 20);
  assert.equal(summary.sample_count, 2);
  assert.equal(summary.valid_like_rate_sample_count, 1);
  assert.equal(summary.final_views_median, 200);
  assert.equal(summary.like_rate_median, 0.1);
  assert.equal(summary.status, 'AVAILABLE');
  assert.equal(context.stage6SummarizeCreatorBaseline_([], headers, 20).status, 'BASELINE_UNAVAILABLE');
});

test('Backfill stores only recent public baseline evidence and excludes existing WATCH videos', () => {
  const creatorHeaders = [
    'creator_name', 'channel_id', 'channel_url', 'uploads_playlist_id', 'enabled', 'priority', 'trusted_creator', 'notes',
    ...Array.from(vm.runInContext('STAGE6_CREATOR_BASELINE_HEADERS_', context)),
  ];
  const baselineHeaders = ['creator_name', 'channel_id', 'video_id', 'title', 'published_at', 'view_count', 'like_count', 'comment_count', 'like_rate', 'creator_id'];
  const videoHeaders = ['video_id', 'channel_id', 'lifecycle_state', 'complete_watch'];
  const snapshotHeaders = ['video_id', 'channel_id', 'checkpoint_minutes', 'view_count', 'baseline_final_views_median', 'historical_median_like_rate', 'creator_scale_median_views'];
  const configHeaders = ['key', 'value', 'description'];
  const sheets = [
    new MemorySheet('Creators', [creatorHeaders, ['Creator A', 'creator-1', 'https://youtube.com/c/a', 'uploads-1', true, 1, false, '', 'creator-1', '', '', '', '', '', '', '']]),
    new MemorySheet('Baseline', [baselineHeaders]),
    new MemorySheet('Videos', [videoHeaders, ['watch-today', 'creator-1', 'WATCH', false]]),
    new MemorySheet('Snapshots', [snapshotHeaders, ['watch-today', 'creator-1', 30, 42, '', '', '']]),
    new MemorySheet('Config', [configHeaders, ...Object.entries(hotConfig()).map(([key, value]) => [key, value, ''])]),
  ];
  const spreadsheet = { getSheetByName: (name) => sheets.find((sheet) => sheet.name === name) };
  context.SpreadsheetApp = { getActiveSpreadsheet() { return this.openById(''); }, openById: () => ({ getSpreadsheetTimeZone: () => 'Etc/GMT' }) };
  let lockReleased = false;
  context.LockService = { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => { lockReleased = true; } }) };
  context.Logger = { log() {} };
  context.Utilities = { formatDate(date, timezone, pattern) {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(date);
    const p = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return pattern.includes('|') ? `${p.year}-${p.month}-${p.day}|${p.hour}:${p.minute}` : `${p.year}-${p.month}-${p.day}`;
  } };
  context.stage6ReadTable_ = (name) => {
    const sheet = spreadsheet.getSheetByName(name);
    const rows = sheet.rows;
    return { sheet, headers: rows[0].map(String), rows: rows.slice(1).filter((row) => row.some((value) => value !== '' && value !== null && value !== undefined)) };
  };
  context.YouTube = {
    PlaylistItems: { list: () => ({ items: [
      { contentDetails: { videoId: 'watch-today', videoPublishedAt: '2026-09-28T00:00:00Z' }, snippet: { title: 'Today' } },
      { contentDetails: { videoId: 'private-1', videoPublishedAt: '2026-09-20T00:00:00Z' }, snippet: { title: 'Private' } },
      { contentDetails: { videoId: 'public-1', videoPublishedAt: '2026-09-18T00:00:00Z' }, snippet: { title: 'Public 1' } },
      { contentDetails: { videoId: 'public-2', videoPublishedAt: '2026-09-17T00:00:00Z' }, snippet: { title: 'Public 2' } },
    ] }) },
    Videos: { list: () => ({ items: [
      { id: 'private-1', snippet: { title: 'Private' }, statistics: { viewCount: '999', likeCount: '9' }, status: { privacyStatus: 'private' } },
      { id: 'public-1', snippet: { title: 'Public 1', publishedAt: '2026-09-18T00:00:00Z' }, statistics: { viewCount: '100', likeCount: '10' }, status: { privacyStatus: 'public' } },
      { id: 'public-2', snippet: { title: 'Public 2', publishedAt: '2026-09-17T00:00:00Z' }, statistics: { viewCount: '300' }, status: { privacyStatus: 'public' } },
    ] }) },
  };

  const result = context.stage6BackfillEnabledCreatorBaselines();
  const baseline = spreadsheet.getSheetByName('Baseline');
  const creator = spreadsheet.getSheetByName('Creators');
  const snapshots = spreadsheet.getSheetByName('Snapshots');
  assert.equal(result.processed, 1);
  assert.equal(result.available, 1);
  assert.equal(result.creators[0].sample_count, 2);
  assert.equal(result.creators[0].baseline_final_views_median, 200);
  assert.equal(result.creators[0].valid_like_rate_sample_count, 1);
  assert.equal(result.creators[0].baseline_like_rate_median, 0.1);
  assert.equal(baseline.rows.length, 3);
  assert.deepEqual(baseline.rows.slice(1).map((row) => row[baselineHeaders.indexOf('video_id')]), ['public-1', 'public-2']);
  assert.equal(creator.rows[1][creatorHeaders.indexOf('cold_baseline_status')], 'AVAILABLE');
  assert.equal(creator.rows[1][creatorHeaders.indexOf('creator_id')], 'creator-1');
  assert.equal(snapshots.rows.length, 2);
  assert.equal(snapshots.rows[1][snapshotHeaders.indexOf('baseline_final_views_median')], 200);
  assert.equal(snapshots.rows[1][snapshotHeaders.indexOf('historical_median_like_rate')], 0.1);
  assert.equal(spreadsheet.getSheetByName('Videos').rows.length, 2);
  assert.equal(lockReleased, true);
});
