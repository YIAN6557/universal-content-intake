const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const context = vm.createContext({ console, Date, Math, Set, Map, Number, String, Array, Object, JSON, Error });
for (const file of ['monitor.gs', 'discovery.gs']) {
  const fullPath = path.join(scriptPath, file);
  vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
}
context.Utilities = { formatDate(date, timezone, pattern) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(date);
  const p = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  if (pattern === 'HH:mm') return `${p.hour}:${p.minute}`;
  return pattern.includes('|') ? `${p.year}-${p.month}-${p.day}|${p.hour}:${p.minute}` : `${p.year}-${p.month}-${p.day}`;
} };
context.SpreadsheetApp = { getActiveSpreadsheet() { return this.openById(''); }, openById: () => ({ getSpreadsheetTimeZone: () => 'Etc/GMT' }) };

class MemorySheet {
  constructor(name, rows) { this.name = name; this.rows = rows.map((row) => row.slice()); }
  getName() { return this.name; }
  getLastRow() { return this.rows.length; }
  getLastColumn() { return Math.max(0, ...this.rows.map((row) => row.length)); }
  getDataRange() { return this.getRange(1, 1, this.getLastRow(), this.getLastColumn()); }
  setFrozenRows() {}
  getRange(row, column, rowCount = 1, columnCount = 1) {
    const sheet = this;
    return {
      getValues() { return Array.from({ length: rowCount }, (_, r) => Array.from({ length: columnCount }, (_, c) => sheet.rows[row + r - 1]?.[column + c - 1] ?? '')); },
      setValues(values) { values.forEach((valuesRow, r) => { sheet.rows[row + r - 1] ||= []; valuesRow.forEach((value, c) => { sheet.rows[row + r - 1][column + c - 1] = value; }); }); },
      setValue(value) { sheet.rows[row - 1] ||= []; sheet.rows[row - 1][column - 1] = value; },
    };
  }
}

const CONFIG = { production_timezone: 'Asia/Shanghai', discovery_window_start: '00:00', discovery_window_end: '04:00', content_title_filters: 'PODCAST' };
const DAY = '2026-09-29';
const IN_WINDOW = '2026-09-28T17:00:00Z'; // 01:00 Shanghai
const AFTER_WINDOW = '2026-09-28T20:10:00Z'; // 04:10 Shanghai

const upcomingLive = { snippet: { liveBroadcastContent: 'upcoming' }, contentDetails: { duration: 'P0D' }, liveStreamingDetails: { scheduledStartTime: '2026-09-28T18:00:00Z' } };
const liveNow = { snippet: { liveBroadcastContent: 'live' }, contentDetails: { duration: 'P0D' }, liveStreamingDetails: { actualStartTime: '2026-09-28T16:50:00Z' } };
const endedBroadcast = { snippet: { liveBroadcastContent: 'none' }, contentDetails: { duration: 'PT1H2M3S' }, liveStreamingDetails: { actualStartTime: '2026-09-28T16:10:00Z', actualEndTime: '2026-09-28T17:12:00Z' } };
const upcomingPremiere = { snippet: { liveBroadcastContent: 'upcoming' }, contentDetails: { duration: 'PT12M30S' }, liveStreamingDetails: { scheduledStartTime: '2026-09-28T17:30:00Z' } };
const playingPremiere = { snippet: { liveBroadcastContent: 'live' }, contentDetails: { duration: 'PT12M30S' }, liveStreamingDetails: { actualStartTime: '2026-09-28T17:31:00Z' } };
const endedPremiere = { snippet: { liveBroadcastContent: 'none' }, contentDetails: { duration: 'PT12M30S' }, liveStreamingDetails: { actualStartTime: '2026-09-28T17:31:00Z', actualEndTime: '2026-09-28T17:44:00Z' } };
const ordinary = { snippet: { liveBroadcastContent: 'none' }, contentDetails: { duration: 'PT8M' } };

const classify = (video, now = IN_WINDOW, known = false) => ({ ...context.stage6ClassifyBroadcast_(video, DAY, CONFIG, now, known) });

test('ISO 8601 durations parse to seconds; live placeholders are zero', () => {
  assert.equal(context.stage6IsoDurationSeconds_('P0D'), 0);
  assert.equal(context.stage6IsoDurationSeconds_('PT0S'), 0);
  assert.equal(context.stage6IsoDurationSeconds_('PT1H2M3S'), 3723);
  assert.equal(context.stage6IsoDurationSeconds_('P1DT1S'), 86401);
  assert.equal(context.stage6IsoDurationSeconds_(''), null);
  assert.equal(context.stage6IsoDurationSeconds_('garbage'), null);
});

test('ordinary uploads enter WATCH unchanged', () => {
  assert.deepEqual(classify(ordinary), { lifecycle_state: 'WATCH', broadcast_type: 'VIDEO' });
  assert.deepEqual(classify(undefined), { lifecycle_state: 'WATCH', broadcast_type: 'UNKNOWN' });
});

test('live streams are rejected permanently whether upcoming, live or already ended', () => {
  for (const video of [upcomingLive, liveNow, endedBroadcast]) {
    assert.deepEqual(classify(video), { lifecycle_state: 'LIVE_REJECTED', broadcast_type: 'LIVE' });
  }
  const missingDuration = { snippet: { liveBroadcastContent: 'upcoming' }, liveStreamingDetails: {} };
  assert.equal(classify(missingDuration).lifecycle_state, 'LIVE_REJECTED');
});

test('Premieres wait, then start WATCH at their actual start time', () => {
  assert.deepEqual(classify(upcomingPremiere), { lifecycle_state: 'PREMIERE_PENDING', broadcast_type: 'PREMIERE' });
  assert.deepEqual(classify(playingPremiere), { lifecycle_state: 'WATCH', broadcast_type: 'PREMIERE', published_at: '2026-09-28T17:31:00.000Z' });
  // A known Premiere that finished between two Discovery runs is not mistaken for a live stream.
  assert.deepEqual(classify(endedPremiere, IN_WINDOW, true), { lifecycle_state: 'WATCH', broadcast_type: 'PREMIERE', published_at: '2026-09-28T17:31:00.000Z' });
});

test('Premieres that do not start inside the Discovery window are closed', () => {
  assert.equal(classify(upcomingPremiere, AFTER_WINDOW, true).lifecycle_state, 'PREMIERE_OUT_OF_WINDOW');
  const lateStart = { ...playingPremiere, liveStreamingDetails: { actualStartTime: '2026-09-28T20:05:00Z' } };
  assert.equal(classify(lateStart, AFTER_WINDOW, true).lifecycle_state, 'PREMIERE_OUT_OF_WINDOW');
});

function runDiscovery({ videoRows = [], playlist, details, now = IN_WINDOW, headers }) {
  const videoHeaders = headers || Array.from(vm.runInContext('STAGE6_VIDEO_HEADERS_', context));
  const sheets = {
    Creators: new MemorySheet('Creators', [['creator_name', 'channel_id', 'uploads_playlist_id', 'enabled'], ['Creator A', 'channel-1', 'uploads-1', true]]),
    Videos: new MemorySheet('Videos', [videoHeaders, ...videoRows.map((values) => videoHeaders.map((h) => values[h] ?? ''))]),
  };
  context.stage6ReadTable_ = (name) => {
    const sheet = sheets[name];
    return { sheet, headers: sheet.rows[0].map(String), rows: sheet.rows.slice(1).filter((row) => row.some((v) => v !== '')) };
  };
  context.stage6ReadConfig_ = () => CONFIG;
  context.stage6CallYoutube_ = (callback) => callback();
  const calls = [];
  context.YouTube = {
    PlaylistItems: { list: () => ({ items: playlist.map(([id, publishedAt]) => ({ contentDetails: { videoId: id, videoPublishedAt: publishedAt }, snippet: { title: id } })) }) },
    Videos: { list: (part, params) => { calls.push({ part, ids: params.id.split(',') }); return { items: params.id.split(',').filter((id) => details[id]).map((id) => ({ id, ...details[id] })) }; } },
  };
  const result = context.stage6DiscoverUploads(DAY, CONFIG, now);
  const sheet = sheets.Videos;
  const h = sheet.rows[0];
  const rows = Object.fromEntries(sheet.rows.slice(1).map((row) => [row[h.indexOf('video_id')], Object.fromEntries(h.map((k, i) => [k, row[i]]))]));
  return { result: JSON.parse(JSON.stringify(result)), rows, calls, headers: h };
}

test('Discovery records live streams as LIVE_REJECTED and only starts WATCH for real uploads', () => {
  const { result, rows, calls } = runDiscovery({
    playlist: [['stream', '2026-09-28T16:55:00Z'], ['premiere', '2026-09-28T16:45:00Z'], ['upload', '2026-09-28T16:40:00Z']],
    details: { stream: upcomingLive, premiere: upcomingPremiere, upload: ordinary },
  });
  assert.equal(rows.stream.lifecycle_state, 'LIVE_REJECTED');
  assert.equal(rows.stream.broadcast_type, 'LIVE');
  assert.equal(rows.premiere.lifecycle_state, 'PREMIERE_PENDING');
  assert.equal(rows.upload.lifecycle_state, 'WATCH');
  assert.equal(rows.upload.broadcast_type, 'VIDEO');
  assert.deepEqual(result.video_ids, ['upload']);
  assert.equal(result.live_rejected, 1);
  assert.equal(result.premieres_pending, 1);
  assert.equal(calls.length, 1);
  assert.match(calls[0].part, /liveStreamingDetails/);
});

test('a rejected live stream is never re-evaluated, even after it ends', () => {
  const { rows, calls } = runDiscovery({
    videoRows: [{ video_id: 'stream', lifecycle_state: 'LIVE_REJECTED', broadcast_type: 'LIVE', published_at: '2026-09-28T16:55:00.000Z' }],
    playlist: [['stream', '2026-09-28T16:55:00Z']],
    details: { stream: ordinary },
  });
  assert.equal(rows.stream.lifecycle_state, 'LIVE_REJECTED');
  assert.equal(calls.length, 0);
});

test('a waiting Premiere moves to WATCH with its actual start as published_at', () => {
  const { result, rows } = runDiscovery({
    videoRows: [{ video_id: 'premiere', lifecycle_state: 'PREMIERE_PENDING', broadcast_type: 'PREMIERE', published_at: '2026-09-28T16:45:00.000Z' }],
    playlist: [['premiere', '2026-09-28T16:45:00Z']],
    details: { premiere: endedPremiere },
  });
  assert.equal(rows.premiere.lifecycle_state, 'WATCH');
  assert.equal(rows.premiere.published_at, '2026-09-28T17:31:00.000Z');
  assert.equal(result.premieres_started, 1);
});

test('the 04:10 final sweep closes Premieres that never started', () => {
  const { rows } = runDiscovery({
    now: AFTER_WINDOW,
    videoRows: [{ video_id: 'premiere', lifecycle_state: 'PREMIERE_PENDING', broadcast_type: 'PREMIERE', published_at: '2026-09-28T16:45:00.000Z' }],
    playlist: [['premiere', '2026-09-28T16:45:00Z']],
    details: { premiere: upcomingPremiere },
  });
  assert.equal(rows.premiere.lifecycle_state, 'PREMIERE_OUT_OF_WINDOW');
});

test('Discovery adds the broadcast_type column to an existing Videos sheet', () => {
  const oldHeaders = Array.from(vm.runInContext('STAGE6_VIDEO_HEADERS_', context)).filter((h) => h !== 'broadcast_type');
  const { rows, headers } = runDiscovery({ headers: oldHeaders, playlist: [['stream', '2026-09-28T16:55:00Z']], details: { stream: liveNow } });
  assert.equal(headers[headers.length - 1], 'broadcast_type');
  assert.equal(rows.stream.broadcast_type, 'LIVE');
});

test('content filter drops over-long uploads and, when opted in, podcast / Q&A / unboxing / finance titles', () => {
  const reason = (title, duration = 'PT5M', config = { content_title_filters: 'ALL' }) =>
    context.stage6ContentFilterReason_({ contentDetails: { duration } }, title, config);
  assert.equal(reason('Weekly tech podcast', 'PT5M', {}), '');
  assert.equal(reason('Weekly tech podcast', 'PT5M', { content_title_filters: 'review, podcast' }), 'PODCAST');
  assert.equal(reason('Why Acme stock is falling', 'PT5M', { content_title_filters: 'PODCAST' }), '');
  assert.equal(reason('We answer your questions about an iPod comeback'), 'QA');
  assert.equal(reason("Dots are cute lil guys | full episode"), 'PODCAST');
  assert.equal(reason('Unboxing: $190 Amazon Kindle'), 'REVIEW');
  assert.equal(reason('Why Acme stock is falling'), 'FINANCE');
  assert.equal(reason('Starship flight 14 full livestream'), 'LIVESTREAM');
  assert.equal(reason('Atlas goes hands on', 'PT21M'), 'TOO_LONG');
  assert.equal(reason('Atlas goes hands on', 'PT21M', { content_max_duration_minutes: 30 }), '');
  for (const title of ['Atlas goes hands on', 'SpaceX Starship Flight 14: Everything that Happened in 15 Minutes',
    'Acme CEO on why its robot will outsell its cars', 'Robot vs human: who folds laundry faster?']) {
    assert.equal(reason(title, 'PT15M'), '', title);
  }
});

test('Discovery stores filtered uploads as CONTENT_REJECTED with the reason and never WATCHes them', () => {
  const { result, rows } = runDiscovery({
    playlist: [['robot demo', '2026-09-28T16:55:00Z'], ['weekly podcast', '2026-09-28T16:45:00Z'], ['long demo', '2026-09-28T16:40:00Z']],
    details: { 'robot demo': ordinary, 'weekly podcast': ordinary, 'long demo': { ...ordinary, contentDetails: { duration: 'PT1H5M' } } },
  });
  assert.equal(rows['robot demo'].lifecycle_state, 'WATCH');
  assert.equal(rows['weekly podcast'].lifecycle_state, 'CONTENT_REJECTED');
  assert.equal(rows['weekly podcast'].hot_reason, 'CONTENT_FILTER:PODCAST');
  assert.equal(rows['long demo'].hot_reason, 'CONTENT_FILTER:TOO_LONG');
  assert.deepEqual([result.discovered, result.content_rejected], [1, 2]);
});
