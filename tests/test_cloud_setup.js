const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const fixedNow = Date.UTC(2026, 9, 5, 2, 0, 0);
const secret = 'test-only-setup-secret';

class MemorySheet {
  constructor(name, rows = []) { this.name = name; this.rows = rows.map((row) => row.slice()); this.formats = {}; }
  getName() { return this.name; }
  setName(name) { this.name = name; }
  setFrozenRows() {}
  getLastRow() {
    for (let i = this.rows.length - 1; i >= 0; i -= 1) {
      if ((this.rows[i] || []).some((value) => value !== '' && value !== null && value !== undefined)) return i + 1;
    }
    return 0;
  }
  getLastColumn() {
    return this.rows.reduce((last, row) => {
      for (let i = (row || []).length - 1; i >= 0; i -= 1) {
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
        values.forEach((valuesRow, r) => {
          sheet.rows[row + r - 1] ||= [];
          valuesRow.forEach((value, c) => { sheet.rows[row + r - 1][column + c - 1] = value; });
        });
      },
      setValue(value) { sheet.rows[row - 1] ||= []; sheet.rows[row - 1][column - 1] = value; },
      setNumberFormat(format) { sheet.formats[`${row},${column}`] = format; },
      getDisplayValue() {
        const value = sheet.rows[row - 1]?.[column - 1];
        return value instanceof Date ? `${value.getUTCHours()}:${String(value.getUTCMinutes()).padStart(2, '0')}:00` : String(value ?? '');
      },
    };
  }
  getDataRange() { return this.getRange(1, 1, Math.max(1, this.getLastRow()), Math.max(1, this.getLastColumn())); }
  appendRow(values) { this.rows[this.getLastRow()] = values.slice(); }
}

function makeRuntime() {
  const sheets = [new MemorySheet('工作表1')];
  const spreadsheet = {
    getId: () => 'sheet-id-1',
    getUrl: () => 'https://docs.google.com/spreadsheets/d/sheet-id-1/edit',
    getSpreadsheetTimeZone: () => 'Etc/UTC',
    getSheetByName: (name) => sheets.find((sheet) => sheet.name === name) || null,
    getSheets: () => sheets.slice(),
    insertSheet: (name) => { const sheet = new MemorySheet(name); sheets.push(sheet); return sheet; },
  };
  const properties = {};
  const triggers = [];
  const context = vm.createContext({
    console, Date, Math, Set, Map, Number, String, Array, Object, JSON, Error, RegExp, Intl,
    Utilities: {
      computeHmacSha256Signature: (data, key) => Array.from(crypto.createHmac('sha256', key).update(data).digest()),
      base64Encode: (bytes) => Buffer.from(bytes).toString('base64'),
      formatDate(date, timeZone, pattern) {
        const parts = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(date);
        const p = Object.fromEntries(parts.map((part) => [part.type, part.value]));
        if (pattern === 'HH:mm') return `${p.hour}:${p.minute}`;
        return pattern.includes('|') ? `${p.year}-${p.month}-${p.day}|${p.hour}:${p.minute}` : `${p.year}-${p.month}-${p.day}`;
      },
      getUuid: () => crypto.randomUUID(),
    },
    SpreadsheetApp: { getActiveSpreadsheet: () => spreadsheet, openById: () => spreadsheet },
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (key) => (Object.hasOwn(properties, key) ? properties[key] : null),
      setProperty: (key, value) => { properties[key] = String(value); },
    }) },
    ScriptApp: {
      getProjectTriggers: () => triggers.slice(),
      deleteTrigger: (trigger) => { triggers.splice(triggers.indexOf(trigger), 1); },
      newTrigger: (handler) => ({ timeBased: () => ({ everyMinutes: () => ({ create: () => { triggers.push({ getHandlerFunction: () => handler }); } }) }) }),
    },
    LockService: { getScriptLock: () => ({ tryLock: () => true, waitLock: () => {}, releaseLock: () => {} }) },
    ContentService: {
      MimeType: { JSON: 'application/json' },
      createTextOutput: (content) => ({ content, setMimeType() { return this; }, getContent() { return this.content; } }),
    },
    Logger: { log: () => {} },
  });
  for (const file of fs.readdirSync(scriptPath).filter((name) => name.endsWith('.gs')).sort()) {
    vm.runInContext(fs.readFileSync(path.join(scriptPath, file), 'utf8'), context, { filename: file });
  }
  context.stage7NowMillis_ = () => fixedNow;
  return { context, sheets, spreadsheet, properties, triggers };
}

function signed(runtime, action, payload = {}) {
  runtime.properties.UCI_QUEUE_HMAC_SECRET = secret;
  const inner = JSON.stringify({ action, ...payload });
  const timestamp = String(Math.floor(fixedNow / 1000));
  const signature = crypto.createHmac('sha256', secret).update(`${timestamp}\n${inner}`).digest('base64');
  const response = runtime.context.doPost({ postData: { contents: JSON.stringify({ timestamp, payload: inner, signature }) } });
  return JSON.parse(response.getContent());
}

function config(runtime) {
  return JSON.parse(JSON.stringify(runtime.context.stage6ReadConfig_()));
}

test('uciSetup turns an empty bound spreadsheet into a working installation', () => {
  const runtime = makeRuntime();
  const report = runtime.context.uciSetup();
  assert.equal(runtime.properties.UCI_SPREADSHEET_ID, 'sheet-id-1');
  for (const name of ['Creators', 'Baseline', 'Videos', 'Snapshots', 'Config', 'Queue', 'QueueClaimRequests']) {
    assert.ok(runtime.spreadsheet.getSheetByName(name), name);
  }
  // The localized default tab became Videos instead of being left behind.
  assert.equal(runtime.spreadsheet.getSheetByName('工作表1'), null);
  const configSheet = runtime.spreadsheet.getSheetByName('Config');
  const rowOf = (key) => configSheet.rows.findIndex((row) => row && row[0] === key) + 1;
  assert.equal(configSheet.formats[`${rowOf('discovery_window_end')},2`], '@');
  assert.equal(report.scheduler_triggers, 1);
  assert.equal(report.properties.UCI_QUEUE_HMAC_SECRET, false);
  assert.deepEqual(Array.from(report.creators), []);
  const creatorHeaders = runtime.spreadsheet.getSheetByName('Creators').rows[0];
  assert.ok(creatorHeaders.includes('uploads_playlist_id') && creatorHeaders.includes('cold_baseline_status'));
  assert.equal(config(runtime).discovery_window_end, '08:00');
  assert.equal(config(runtime).daily_selection_enabled, false);
  // A clock that Sheets converted to a time value is rewritten as text.
  const sweepRow = configSheet.rows.findIndex((row) => row && row[0] === 'final_sweep_time');
  configSheet.rows[sweepRow][1] = new Date(Date.UTC(1899, 11, 30, 8, 10));
  runtime.context.uciSetup();
  assert.equal(config(runtime).final_sweep_time, '08:10');
  assert.equal(configSheet.formats[`${sweepRow + 1},2`], '@');
  // Idempotent: a second run keeps one trigger and adds no duplicate rows, and
  // does not undo a selection switch that verify already turned on.
  runtime.context.uciSetupWriteConfig_('daily_selection_enabled', true);
  runtime.context.uciSetup();
  assert.equal(config(runtime).daily_selection_enabled, true);
  assert.equal(runtime.triggers.length, 1);
  assert.equal(Object.keys(config(runtime)).length, new Set(Object.keys(config(runtime))).size);
});

test('setup actions are signed: inspect, upsert creators and change settings', () => {
  const runtime = makeRuntime();
  runtime.context.uciSetup();
  const inspected = signed(runtime, 'setup_inspect');
  assert.equal(inspected.ok, true);
  assert.equal(inspected.data.properties.UCI_QUEUE_HMAC_SECRET, true);

  const upsert = signed(runtime, 'setup_creators_upsert', { creators: [
    { creator_name: 'Example Robotics', channel_id: 'UCaaaaaaaaaaaaaaaaaaaaaa', trusted_creator: true },
    { creator_name: 'Example Space', channel_id: 'UCbbbbbbbbbbbbbbbbbbbbbb', enabled: false },
  ] });
  assert.equal(upsert.ok, true, JSON.stringify(upsert));
  assert.deepEqual(upsert.data.added, ['UCaaaaaaaaaaaaaaaaaaaaaa', 'UCbbbbbbbbbbbbbbbbbbbbbb']);
  const creators = runtime.spreadsheet.getSheetByName('Creators');
  const h = creators.rows[0];
  const first = creators.rows[1];
  assert.equal(first[h.indexOf('uploads_playlist_id')], 'UUaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(first[h.indexOf('enabled')], true);
  assert.equal(first[h.indexOf('trusted_creator')], true);
  const again = signed(runtime, 'setup_creators_upsert', { creators: [{ creator_name: 'Example Robotics Lab', channel_id: 'UCaaaaaaaaaaaaaaaaaaaaaa', enabled: false }] });
  assert.deepEqual(again.data.updated, ['UCaaaaaaaaaaaaaaaaaaaaaa']);
  assert.equal(creators.rows.filter((row) => row && row[h.indexOf('channel_id')] === 'UCaaaaaaaaaaaaaaaaaaaaaa').length, 1);
  assert.equal(creators.rows[1][h.indexOf('creator_name')], 'Example Robotics Lab');

  const set = signed(runtime, 'setup_config_set', { values: {
    production_timezone: 'Europe/Berlin', discovery_window_start: '06:00', discovery_window_end: '12:00',
    final_sweep_time: '12:10', daily_selection_time: '14:00', rank2_start_cutoff: '16:00',
    daily_selection_max: 1, semantic_editorial_brief: 'Accept rocket launches only.', content_title_filters: 'podcast, ad',
  } });
  assert.equal(set.ok, true, JSON.stringify(set));
  const values = config(runtime);
  assert.equal(values.production_timezone, 'Europe/Berlin');
  assert.equal(values.daily_selection_time, '14:00');
  assert.equal(values.daily_selection_max, 1);
  assert.equal(values.semantic_editorial_brief, 'Accept rocket launches only.');
  assert.equal(values.content_title_filters, 'PODCAST,AD');
});

test('setup rejects unsafe values with a reason and leaves Config unchanged', () => {
  const runtime = makeRuntime();
  runtime.context.uciSetup();
  const before = config(runtime);
  const cases = [
    [{ monitor_status: 'ACTIVE' }, /cannot be changed/],
    [{ daily_selection_time: '25:00' }, /HH:mm/],
    [{ production_timezone: 'Mars/Base' }, /timezone/],
    [{ daily_selection_max: 5 }, /0, 1 or 2/],
    [{ content_title_filters: 'PODCAST,BOGUS' }, /unknown rule BOGUS/],
    [{ discovery_window_end: '09:00', final_sweep_time: '09:10', daily_selection_time: '10:00' }, /120 minutes/],
    [{ final_sweep_time: '03:00' }, /final_sweep_time must be at or after/],
    [{ rank2_start_cutoff: '09:00' }, /rank2_start_cutoff must be after/],
  ];
  for (const [values, reason] of cases) {
    const response = signed(runtime, 'setup_config_set', { values });
    assert.equal(response.ok, false, JSON.stringify(values));
    assert.match(response.error.detail, reason);
  }
  const bad = signed(runtime, 'setup_creators_upsert', { creators: [{ creator_name: 'x', channel_id: '@handle' }] });
  assert.match(bad.error.detail, /Invalid channel_id/);
  assert.deepEqual(config(runtime), before);
  // Unsigned callers learn nothing.
  const unsigned = runtime.context.doPost({ postData: { contents: JSON.stringify({ timestamp: '1', payload: '{"action":"setup_inspect"}', signature: 'x' }) } });
  const parsed = JSON.parse(unsigned.getContent());
  assert.equal(parsed.error.code, 'AUTH_FAILED');
  assert.equal(parsed.error.detail, undefined);
});
