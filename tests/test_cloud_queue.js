const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const repoRoot = path.join(__dirname, '..');
const scriptPath = path.join(repoRoot, 'cloud', 'apps-script');

class MemorySheet {
  constructor(name, rows = []) {
    this.name = name;
    this.rows = rows.map((row) => row.slice());
    this.writes = 0;
    this.lockCheck = null;
  }
  getName() { return this.name; }
  setName(name) { this.name = name; }
  setFrozenRows() {}
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
        sheet.lockCheck?.();
        return Array.from({ length: rowCount }, (_, r) =>
          Array.from({ length: columnCount }, (_, c) => sheet.rows[row + r - 1]?.[column + c - 1] ?? ''));
      },
      setValues(values) {
        sheet.lockCheck?.();
        sheet.writes += 1;
        values.forEach((valuesRow, r) => {
          const target = row + r - 1;
          sheet.rows[target] ||= [];
          valuesRow.forEach((value, c) => { sheet.rows[target][column + c - 1] = value; });
        });
      },
      setValue(value) {
        sheet.lockCheck?.();
        sheet.writes += 1;
        sheet.rows[row - 1] ||= [];
        sheet.rows[row - 1][column - 1] = value;
      },
    };
  }
  getDataRange() {
    return this.getRange(1, 1, Math.max(1, this.getLastRow()), Math.max(1, this.getLastColumn()));
  }
  appendRow(values) {
    this.lockCheck?.();
    this.writes += 1;
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

const fixedNow = Date.UTC(2026, 8, 25, 8, 0, 0);
const secretValue = 'test-only-random-secret-do-not-log';

function makeRuntime({ queueRows, claimRequestRows, configRows = [] } = {}) {
  const config = new MemorySheet('Config', [
    ['key', 'value', 'description'],
    ['monitor_status', 'ACTIVE', 'fixture'],
    ...configRows,
  ]);
  const sheets = [config];
  if (queueRows) sheets.push(new MemorySheet('Queue', queueRows));
  if (claimRequestRows) sheets.push(new MemorySheet('QueueClaimRequests', claimRequestRows));
  const spreadsheet = new MemorySpreadsheet(sheets);
  let held = false;
  let lockAcquisitions = 0;
  const properties = {};
  const logs = [];
  const context = vm.createContext({
    console, Date, Math, Set, Map, Number, String, Array, Object, JSON, Error, RegExp,
    Utilities: {
      getUuid: () => crypto.randomUUID(),
      computeHmacSha256Signature: (data, key) => Array.from(crypto.createHmac('sha256', key).update(data).digest()),
      base64Encode: (bytes) => Buffer.from(bytes).toString('base64'),
    },
    SpreadsheetApp: { getActiveSpreadsheet() { return this.openById(''); }, openById: () => spreadsheet },
    LockService: {
      getScriptLock: () => ({
        waitLock: () => {
          if (held) throw new Error('lock already held');
          held = true;
          lockAcquisitions += 1;
        },
        tryLock: () => {
          if (held) return false;
          held = true;
          lockAcquisitions += 1;
          return true;
        },
        releaseLock: () => { held = false; },
      }),
    },
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (key) => Object.hasOwn(properties, key) ? properties[key] : null,
      setProperty: (key, value) => { properties[key] = String(value); },
      deleteProperty: (key) => { delete properties[key]; },
    }) },
    ContentService: {
      MimeType: { JSON: 'application/json' },
      createTextOutput: (content) => ({
        content,
        mimeType: null,
        setMimeType(value) { this.mimeType = value; return this; },
        getContent() { return this.content; },
      }),
    },
    Logger: { log: (value) => logs.push(String(value)) },
  });
  for (const file of ['monitor.gs', 'queue_contract.gs', 'queue.gs', 'api.gs']) {
    const fullPath = path.join(scriptPath, file);
    if (fs.existsSync(fullPath)) vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
  }
  context.stage7NowMillis_ = () => fixedNow;
  context.SpreadsheetApp = { getActiveSpreadsheet() { return this.openById(''); }, openById: () => spreadsheet };
  context.stage7SetupQueue_();
  const queue = spreadsheet.getSheetByName('Queue');
  const claimRequests = spreadsheet.getSheetByName('QueueClaimRequests');
  return {
    context,
    spreadsheet,
    queue,
    claimRequests,
    config,
    properties,
    logs,
    secret: secretValue,
    lockAcquisitions: () => lockAcquisitions,
    enforceLock() {
      queue.lockCheck = () => { if (!held) throw new Error('Queue sheet operation outside script lock'); };
      claimRequests.lockCheck = () => { if (!held) throw new Error('Queue claim request ledger operation outside script lock'); };
      config.lockCheck = () => { if (!held) throw new Error('Config read outside script lock'); };
    },
  };
}

function scriptValue(context, expression) { return vm.runInContext(expression, context); }

function fieldRow(fields, order) { return order.map((name) => Object.hasOwn(fields, name) ? fields[name] : ''); }

function enqueue(runtime, videoId, url = `https://www.youtube.com/watch?v=${videoId}`) {
  return runtime.context.stage7Enqueue_({ video_id: videoId, url });
}

function readRows(runtime) {
  return runtime.queue.rows.slice(1).filter((row) => row.some((value) => value !== ''));
}

function recordFor(runtime, videoId) {
  return readRows(runtime).map((row) => Object.fromEntries(runtime.queue.rows[0].map((h, i) => [h, row[i] ?? ''])))
    .find((record) => record.video_id === videoId);
}

function signRequest(runtime, action, payload = {}, timestamp = Math.floor(fixedNow / 1000)) {
  const inner = JSON.stringify({ action, ...payload });
  const ts = String(timestamp);
  const signature = crypto.createHmac('sha256', runtime.secret).update(`${ts}\n${inner}`).digest('base64');
  return { timestamp: ts, payload: inner, signature };
}

function post(runtime, request) {
  const response = runtime.context.doPost({ postData: { contents: JSON.stringify(request) } });
  return JSON.parse(response.getContent());
}

function signedPost(runtime, action, payload = {}, timestamp) {
  runtime.properties.UCI_QUEUE_HMAC_SECRET = runtime.secret;
  const requestPayload = action === 'claim' && !payload.claim_request_id
    ? { ...payload, claim_request_id: crypto.randomUUID() } : payload;
  return post(runtime, signRequest(runtime, action, requestPayload, timestamp));
}

function claim(runtime, requestId = crypto.randomUUID(), now = fixedNow) { return runtime.context.stage7ClaimNext_(requestId, now); }
function heartbeat(runtime, current, now = fixedNow) {
  return runtime.context.stage7Heartbeat_({ queue_id: current.queue_id, claim_token: current.claim_token }, now);
}
function startProcessing(runtime) {
  const current = claim(runtime);
  runtime.context.stage7Heartbeat_({ queue_id: current.queue_id, claim_token: current.claim_token }, fixedNow);
  return current;
}

test('empty Queue setup adds the formal fields/config and is idempotent', () => {
  const runtime = makeRuntime();
  const headers = Array.from(scriptValue(runtime.context, 'STAGE7_QUEUE_HEADERS_'));
  assert.deepEqual(runtime.queue.rows[0], headers);
  assert.equal(runtime.context.stage7ReadQueueConfig_().leaseSeconds, 900);
  assert.equal(runtime.context.stage7ReadQueueConfig_().timestampToleranceSeconds, 300);
  const before = runtime.spreadsheet.sheets.map((sheet) => [sheet.name, sheet.snapshot()]);
  const writesBeforeRepeat = runtime.spreadsheet.sheets.map((sheet) => [sheet.name, sheet.writes]);
  runtime.context.stage7SetupQueue_();
  assert.deepEqual(runtime.spreadsheet.sheets.map((sheet) => [sheet.name, sheet.snapshot()]), before);
  assert.deepEqual(runtime.spreadsheet.sheets.map((sheet) => [sheet.name, sheet.writes]), writesBeforeRepeat);
});

test('legacy and partial Queue schemas append only missing fields and preserve existing rows', () => {
  const required = [
    'queue_id', 'video_id', 'url', 'status', 'claim_token', 'claimed_at', 'lease_until',
    'attempts', 'last_error', 'local_job_id', 'completed_at', 'result_path', 'claim_request_id',
    'selection_day', 'selection_rank', 'rank2_start_cutoff_at', 'core_started_at',
  ];
  const legacy = ['video_id', 'url', 'status', 'extra_legacy'];
  const existingRow = ['v-legacy', 'https://example.invalid/v-legacy', 'PENDING', 'preserve'];
  const runtime = makeRuntime({ queueRows: [legacy, existingRow] });
  assert.deepEqual(runtime.queue.rows[0].slice(0, legacy.length), legacy);
  assert.deepEqual(runtime.queue.rows[1].slice(0, legacy.length), existingRow);
  assert.deepEqual(runtime.queue.rows[0].slice(legacy.length), required.filter((h) => !legacy.includes(h)));
  assert.deepEqual(runtime.context.stage7SetupQueue_().queue_headers, runtime.queue.rows[0]);
  const afterFirst = runtime.queue.snapshot();
  const writesBeforeSecondSetup = runtime.queue.writes;
  runtime.context.stage7SetupQueue_();
  assert.deepEqual(runtime.queue.snapshot(), afterFirst);
  assert.equal(runtime.queue.writes, writesBeforeSecondSetup);
});

test('claim_request_id already present in a partial schema is retained in place without duplication', () => {
  const legacy = ['queue_id', 'claim_request_id', 'video_id', 'url', 'status'];
  const existingRow = ['q-1', 'request-legacy-000001', 'v-1', 'https://example.invalid/v-1', 'PENDING'];
  const runtime = makeRuntime({ queueRows: [legacy, existingRow] });
  assert.deepEqual(runtime.queue.rows[0].slice(0, legacy.length), legacy);
  assert.deepEqual(runtime.queue.rows[1].slice(0, legacy.length), existingRow);
  assert.equal(runtime.queue.rows[0].filter((field) => field === 'claim_request_id').length, 1);
  const afterMigration = runtime.queue.snapshot();
  runtime.context.stage7SetupQueue_();
  assert.deepEqual(runtime.queue.snapshot(), afterMigration);
});

test('claim request ledger migration appends headers without rewriting existing rows and remains idempotent', () => {
  const legacyHeader = ['request_key', 'queue_key'];
  const legacyRow = ['legacy-request-id', 'legacy-queue-id'];
  const runtime = makeRuntime({ claimRequestRows: [legacyHeader, legacyRow] });
  assert.deepEqual(runtime.claimRequests.rows[0].slice(0, 2), legacyHeader);
  assert.deepEqual(runtime.claimRequests.rows[1], legacyRow);
  assert.deepEqual(runtime.claimRequests.rows[0].slice(2), ['claim_request_id', 'queue_id', 'created_at']);
  const afterFirst = runtime.claimRequests.snapshot();
  const writes = runtime.claimRequests.writes;
  runtime.context.stage7SetupQueue_();
  assert.deepEqual(runtime.claimRequests.snapshot(), afterFirst);
  assert.equal(runtime.claimRequests.writes, writes);
});

test('setup preserves manually configured lease and timestamp values', () => {
  const runtime = makeRuntime({ configRows: [
    ['queue_lease_seconds', 120, 'manual'],
    ['hmac_timestamp_tolerance_seconds', 45, 'manual'],
  ] });
  const config = runtime.context.stage7ReadQueueConfig_();
  assert.equal(config.leaseSeconds, 120);
  assert.equal(config.timestampToleranceSeconds, 45);
});

test('explicit valid task enqueue creates PENDING without Stage 6 or Selection status and prevents duplicate video rows', () => {
  const runtime = makeRuntime();
  const first = enqueue(runtime, 'video-1');
  assert.equal(first.created, true);
  assert.equal(first.task.video_id, 'video-1');
  assert.ok(first.task.queue_id);
  assert.notEqual(first.task.queue_id, '2');
  for (const status of ['PENDING', 'CLAIMED', 'PROCESSING', 'PAUSED', 'FAILED', 'COMPLETED']) {
    runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('status') + 1).setValue(status);
    const duplicate = enqueue(runtime, 'video-1');
    assert.equal(duplicate.created, false);
    assert.equal(duplicate.task.status, status);
    assert.equal(readRows(runtime).length, 1);
  }
});

test('enqueue validates only its task payload and never inspects Stage 6 status', () => {
  const runtime = makeRuntime();
  const source = { video_id: 'v-explicit', url: 'https://example.invalid/v-explicit' };
  Object.defineProperty(source, 'source_status', { get() { throw new Error('Queue must not inspect Stage 6 state'); } });
  const added = runtime.context.stage7Enqueue_(source);
  assert.equal(added.created, true);
  assert.equal(added.task.status, 'PENDING');
  assert.equal(readRows(runtime).length, 1);
});

test('claim returns null for empty Queue and claims only one PENDING task per request', () => {
  const runtime = makeRuntime();
  assert.equal(claim(runtime), null);
  enqueue(runtime, 'video-1');
  enqueue(runtime, 'video-2');
  const first = claim(runtime);
  assert.equal(first.video_id, 'video-1');
  assert.equal(first.status, 'CLAIMED');
  assert.equal(claim(runtime).video_id, 'video-2');
  assert.deepEqual(readRows(runtime).map((row) => row[runtime.queue.rows[0].indexOf('attempts')]), [1, 1]);
});

test('same claim_request_id retry returns the original task and leaves the next task PENDING', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  enqueue(runtime, 'video-2');
  const requestId = 'req-0000000000000001';
  const first = claim(runtime, requestId);
  const retry = claim(runtime, requestId);
  assert.equal(retry.queue_id, first.queue_id);
  assert.equal(retry.claim_token, first.claim_token);
  assert.equal(retry.attempts, first.attempts);
  assert.equal(recordFor(runtime, 'video-1').attempts, 1);
  assert.equal(recordFor(runtime, 'video-2').status, 'PENDING');
  assert.equal(runtime.claimRequests.getLastRow(), 2);
  assert.equal(runtime.claimRequests.rows[1][0], requestId);
  assert.equal(runtime.claimRequests.rows[1][1], first.queue_id);
  assert.equal(claim(runtime, 'req-0000000000000002').video_id, 'video-2');
});

test('claim rejects a malformed request ID instead of silently creating a different poll', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  assert.throws(() => claim(runtime, ''), (error) => error.queueErrorCode === 'INVALID_REQUEST');
  assert.equal(recordFor(runtime, 'video-1').status, 'PENDING');
  assert.equal(recordFor(runtime, 'video-1').attempts, 0);
});

test('same claim_request_id retry remains idempotent after the task entered PROCESSING', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const first = claim(runtime, 'req-processing-000001');
  heartbeat(runtime, first);
  const replay = claim(runtime, 'req-processing-000001');
  assert.equal(replay.queue_id, first.queue_id);
  assert.equal(replay.status, 'PROCESSING');
  assert.equal(replay.claim_token, first.claim_token);
  assert.equal(replay.attempts, 1);
});

test('expired and taken-over claim_request_id values are rejected without restoring old ownership', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const oldRequestId = 'req-0000000000000003';
  const oldClaim = claim(runtime, oldRequestId);
  const leaseColumn = runtime.queue.rows[0].indexOf('lease_until') + 1;
  runtime.queue.getRange(2, leaseColumn).setValue(new Date(fixedNow).toISOString());
  assert.throws(() => claim(runtime, oldRequestId), (error) => error.queueErrorCode === 'CLAIM_REQUEST_EXPIRED');
  const current = claim(runtime, 'req-0000000000000004', fixedNow + 1);
  assert.equal(current.attempts, 2);
  assert.notEqual(current.claim_token, oldClaim.claim_token);
  assert.throws(() => claim(runtime, oldRequestId, fixedNow + 2), (error) => error.queueErrorCode === 'CLAIM_REQUEST_EXPIRED');
  assert.equal(recordFor(runtime, 'video-1').claim_token, current.claim_token);
  assert.deepEqual(runtime.claimRequests.rows.slice(1).map((row) => row[0]), [oldRequestId, 'req-0000000000000004']);
});

test('claim writes unguessable token, timestamps, and increments attempts only on ownership', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const first = claim(runtime);
  assert.equal(first.attempts, 1);
  assert.equal(first.claim_token, recordFor(runtime, 'video-1').claim_token);
  assert.ok(first.claim_token.length >= 32);
  assert.equal(first.claimed_at, new Date(fixedNow).toISOString());
  assert.equal(first.lease_until, new Date(fixedNow + 900_000).toISOString());
  assert.equal(claim(runtime), null);
  assert.equal(recordFor(runtime, 'video-1').attempts, 1);
});

test('Rank 1 has no Core-start cutoff; Rank 2 must start before cutoff but may finish after it', () => {
  const runtime = makeRuntime();
  const queueId = 'queue-cutoff-test';
  const claimToken = 'claim-token-for-cutoff-test-000000000000';
  const leaseUntil = '2030-01-01T00:00:00.000Z';
  const cutoff = Date.parse('2026-09-25T00:00:00.000Z'); // 08:00 Asia/Shanghai
  const request = { queue_id: queueId, claim_token: claimToken };

  const rankOne = {
    queue_id: queueId, video_id: 'video-rank-one', status: 'PROCESSING', claim_token: claimToken,
    lease_until: leaseUntil, selection_rank: 1, rank2_start_cutoff_at: new Date(cutoff).toISOString(),
    core_started_at: '',
  };
  const rankOneStarted = runtime.context.stage7QueueCoreStartedTransition_(rankOne, request, cutoff + 60 * 60 * 1000);
  assert.equal(rankOneStarted.status, 'PROCESSING');
  assert.ok(rankOneStarted.core_started_at);

  const rankTwo = { ...rankOne, video_id: 'video-rank-two', selection_rank: 2 };
  const rankTwoStarted = runtime.context.stage7QueueCoreStartedTransition_(rankTwo, request, cutoff - 1000);
  assert.equal(rankTwoStarted.status, 'PROCESSING');
  assert.equal(rankTwoStarted.core_started_at, new Date(cutoff - 1000).toISOString());
  const stillOwnedAfterCutoff = runtime.context.stage7QueueCoreStartedTransition_(rankTwoStarted, request, cutoff + 60 * 60 * 1000);
  assert.equal(stillOwnedAfterCutoff.status, 'PROCESSING');
  assert.equal(stillOwnedAfterCutoff.core_started_at, rankTwoStarted.core_started_at);
  const completedAfterCutoff = runtime.context.stage7QueueCompleteTransition_(stillOwnedAfterCutoff, {
    ...request, local_job_id: 'job-rank-two', result_path: '/output/rank-two',
  }, cutoff + 60 * 60 * 1000);
  assert.equal(completedAfterCutoff.status, 'COMPLETED');

  const lateStart = runtime.context.stage7QueueCoreStartedTransition_(rankTwo, request, cutoff);
  assert.equal(lateStart.status, 'FAILED');
  assert.match(lateStart.last_error, /RANK2_START_CUTOFF/);
});

test('pending Rank 2 is terminalized at its start cutoff instead of remaining claimable', () => {
  const runtime = makeRuntime();
  const cutoff = new Date(fixedNow).toISOString();
  const inserted = runtime.context.stage7Enqueue_({
    video_id: 'video-rank-two-expired',
    url: 'https://www.youtube.com/watch?v=video-rank-two-expired',
    selection_day: '2026-09-25',
    selection_rank: 2,
    rank2_start_cutoff_at: cutoff,
  });
  assert.equal(inserted.task.status, 'PENDING');
  assert.equal(runtime.context.stage7ExpirePendingRank2_(false, fixedNow), 1);
  const record = recordFor(runtime, 'video-rank-two-expired');
  assert.equal(record.status, 'FAILED');
  assert.match(record.last_error, /RANK2_START_CUTOFF/);
  assert.equal(claim(runtime), null);
});

test('Rank 2 cannot be claimed until Rank 1 is completed or terminally failed', () => {
  const runtime = makeRuntime();
  const rankOne = runtime.context.stage7Enqueue_({
    video_id: 'video-serial-rank-one',
    url: 'https://www.youtube.com/watch?v=video-serial-rank-one',
    selection_day: '2026-09-25', selection_rank: 1,
  });
  const rankTwo = runtime.context.stage7Enqueue_({
    video_id: 'video-serial-rank-two',
    url: 'https://www.youtube.com/watch?v=video-serial-rank-two',
    selection_day: '2026-09-25', selection_rank: 2,
    rank2_start_cutoff_at: '2030-01-01T00:00:00.000Z',
  });
  assert.equal(rankOne.task.status, 'PENDING');
  assert.equal(rankTwo.task.status, 'PENDING');

  const first = claim(runtime, 'req-serial-rank1-00001');
  assert.equal(first.video_id, 'video-serial-rank-one');
  assert.equal(claim(runtime, 'req-serial-rank2-00001'), null);

  const rankOneStatusColumn = runtime.queue.rows[0].indexOf('status') + 1;
  const rankOneSheetRow = runtime.queue.rows.findIndex((row) => row[runtime.queue.rows[0].indexOf('video_id')] === 'video-serial-rank-one') + 1;
  runtime.queue.getRange(rankOneSheetRow, rankOneStatusColumn).setValue('PAUSED');
  assert.equal(claim(runtime, 'req-serial-rank2-00002'), null);

  runtime.queue.getRange(rankOneSheetRow, rankOneStatusColumn).setValue('COMPLETED');
  const second = claim(runtime, 'req-serial-rank2-00003');
  assert.equal(second.video_id, 'video-serial-rank-two');
  assert.equal(second.selection_rank, 2);
});

test('A and B cannot both claim the same task and every read/write is under LockService', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  runtime.enforceLock();
  const workerA = claim(runtime);
  const workerB = claim(runtime);
  assert.ok(workerA.claim_token);
  assert.equal(workerB, null);
  assert.equal(recordFor(runtime, 'video-1').attempts, 1);
  assert.equal(runtime.lockAcquisitions(), 3); // one enqueue plus the two serialized claims
});

test('non-PENDING live tasks, PAUSED, FAILED, and COMPLETED are not normally claimed', () => {
  const runtime = makeRuntime();
  for (const [i, status] of ['CLAIMED', 'PROCESSING', 'PAUSED', 'FAILED', 'COMPLETED'].entries()) {
    runtime.queue.appendRow(fieldRow({ queue_id: `q-${i}`, video_id: `v-${i}`, url: 'https://example.invalid', status,
      claim_token: `token-${i}`, lease_until: new Date(fixedNow + 60_000).toISOString(), attempts: 1 }, runtime.queue.rows[0]));
  }
  assert.equal(claim(runtime), null);

  const corrupt = makeRuntime();
  corrupt.queue.appendRow(fieldRow({ queue_id: 'q-bad', video_id: 'v-bad', url: 'https://example.invalid', status: 'NOT_A_QUEUE_STATUS', attempts: 0 }, corrupt.queue.rows[0]));
  assert.throws(() => claim(corrupt), /unknown status/i);
});

test('expired CLAIMED and PROCESSING leases can be reclaimed with a new token and one attempt', () => {
  for (const status of ['CLAIMED', 'PROCESSING']) {
    const runtime = makeRuntime();
    enqueue(runtime, 'video-1');
    const old = claim(runtime);
    runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('status') + 1).setValue(status);
    runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('lease_until') + 1).setValue(new Date(fixedNow).toISOString());
    const reclaimed = claim(runtime);
    assert.equal(reclaimed.status, 'CLAIMED');
    assert.equal(reclaimed.attempts, 2);
    assert.notEqual(reclaimed.claim_token, old.claim_token);
    assert.throws(() => heartbeat(runtime, old), /token/i);
  }
});

test('lease configuration change affects next claim without code changes', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const header = runtime.config.rows[0];
  const row = runtime.config.rows.findIndex((value) => value[0] === 'queue_lease_seconds');
  runtime.config.getRange(row + 1, header.indexOf('value') + 1).setValue(60);
  assert.equal(claim(runtime).lease_until, new Date(fixedNow + 60_000).toISOString());
});

test('heartbeat validates token, state, and non-expired lease; does not change token or attempts', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const current = claim(runtime);
  const result = heartbeat(runtime, current);
  assert.equal(result.status, 'PROCESSING');
  assert.equal(result.lease_until, new Date(fixedNow + 900_000).toISOString());
  assert.equal(recordFor(runtime, 'video-1').claim_token, current.claim_token);
  assert.equal(recordFor(runtime, 'video-1').attempts, 1);
  assert.throws(() => runtime.context.stage7Heartbeat_({ queue_id: current.queue_id, claim_token: 'wrong' }, fixedNow), /token/i);
  assert.throws(() => runtime.context.stage7Heartbeat_({ queue_id: current.queue_id, claim_token: current.claim_token }, fixedNow + 900_000), /lease/i);
  runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('status') + 1).setValue('PAUSED');
  assert.throws(() => heartbeat(runtime, current), /state/i);
});

test('repeated heartbeat is safe and preserves failure/result fields', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const current = claim(runtime);
  runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('last_error') + 1).setValue('keep error');
  runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('completed_at') + 1).setValue('keep completion');
  runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('result_path') + 1).setValue('keep result');
  heartbeat(runtime, current);
  heartbeat(runtime, current, fixedNow + 30_000);
  const row = recordFor(runtime, 'video-1');
  assert.equal(row.last_error, 'keep error');
  assert.equal(row.completed_at, 'keep completion');
  assert.equal(row.result_path, 'keep result');
  assert.equal(row.attempts, 1);
});

test('claim-token and lease expiry use epoch time rather than lexical date comparison', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const current = claim(runtime);
  runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('lease_until') + 1).setValue('2026-09-25T08:00:00.000Z');
  assert.throws(() => heartbeat(runtime, current), /lease/i);
  assert.equal(claim(runtime).attempts, 2);
});

test('complete requires the active token and PROCESSING lease, then is idempotent for identical result', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const current = startProcessing(runtime);
  const request = { queue_id: current.queue_id, claim_token: current.claim_token, local_job_id: 'job-1', result_path: '/output/job-1' };
  const completed = runtime.context.stage7Complete_(request, fixedNow);
  assert.equal(completed.status, 'COMPLETED');
  assert.equal(completed.completed_at, new Date(fixedNow).toISOString());
  const duplicate = runtime.context.stage7Complete_(request, fixedNow + 2_000_000);
  assert.deepEqual(JSON.parse(JSON.stringify(duplicate)), JSON.parse(JSON.stringify(completed)));
  assert.equal(claim(runtime), null);
});

test('complete rejects wrong token, expired lease, changed result, and old token after reclaim', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  let current = startProcessing(runtime);
  assert.throws(() => runtime.context.stage7Complete_({ queue_id: current.queue_id, claim_token: 'bad', local_job_id: 'j', result_path: '/p' }, fixedNow), /token/i);
  assert.throws(() => runtime.context.stage7Complete_({ queue_id: current.queue_id, claim_token: current.claim_token, local_job_id: 'j', result_path: '/p' }, fixedNow + 900_000), /lease/i);
  runtime.queue.getRange(2, runtime.queue.rows[0].indexOf('lease_until') + 1).setValue(new Date(fixedNow).toISOString());
  const reclaimed = claim(runtime);
  assert.throws(() => runtime.context.stage7Complete_({ queue_id: current.queue_id, claim_token: current.claim_token, local_job_id: 'j', result_path: '/p' }, fixedNow), /token/i);
  current = reclaimed;
  assert.throws(() => runtime.context.stage7Complete_({ queue_id: current.queue_id, claim_token: current.claim_token, local_job_id: 'j', result_path: '/different' }, fixedNow), /state/i);
});

test('recoverable Core error pauses and terminal Core error fails; repeated failure is idempotent', () => {
  for (const [code, expected] of [['NETWORK_PAUSED', 'PAUSED'], ['PROVIDER_FAILED', 'FAILED']]) {
    const runtime = makeRuntime();
    enqueue(runtime, 'video-1');
    const current = startProcessing(runtime);
    const request = { queue_id: current.queue_id, claim_token: current.claim_token, local_job_id: 'job-1', error: { code, message: `safe ${code}` } };
    const failed = runtime.context.stage7Fail_(request, fixedNow);
    assert.equal(failed.status, expected);
    assert.equal(failed.local_job_id, 'job-1');
    assert.match(failed.last_error, new RegExp(code));
    const duplicate = runtime.context.stage7Fail_(request, fixedNow + 2_000_000);
    assert.deepEqual(JSON.parse(JSON.stringify(duplicate)), JSON.parse(JSON.stringify(failed)));
    assert.equal(failed.attempts, 1);
  }
});

test('fail rejects forged recoverability, wrong token, and expired lease', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'video-1');
  const current = startProcessing(runtime);
  const base = { queue_id: current.queue_id, claim_token: current.claim_token, local_job_id: 'job-1' };
  assert.throws(() => runtime.context.stage7Fail_({ ...base, error: { code: 'MADE_UP', recoverable: true } }, fixedNow), /error/i);
  assert.throws(() => runtime.context.stage7Fail_({ ...base, error: {
    code: 'NETWORK_PAUSED', message: 'network down', recoverable: false,
  } }, fixedNow), /recoverability/i);
  assert.throws(() => runtime.context.stage7Fail_({ ...base, claim_token: 'bad', error: { code: 'NETWORK_PAUSED' } }, fixedNow), /token/i);
  assert.throws(() => runtime.context.stage7Fail_({ ...base, error: { code: 'NETWORK_PAUSED', message: 'network down' } }, fixedNow + 900_000), /lease/i);
});

test('Queue error recoverability map stays aligned with Stage 1 Core Error Contract', () => {
  const cloud = scriptValue(makeRuntime().context, 'STAGE7_CORE_ERROR_RECOVERABILITY_');
  const expected = JSON.parse(execFileSync('python3', ['-c', [
    'import json',
    'from src.core.errors import ErrorCode, ERROR_SPECS',
    'from src.queue.contract import QueueStatus',
    'print(json.dumps({code.value: ("PAUSED" if ERROR_SPECS[code].recoverable else "FAILED") for code in ErrorCode}))',
  ].join('; ')], { cwd: repoRoot, encoding: 'utf8' }));
  assert.deepEqual(JSON.parse(JSON.stringify(cloud)), expected);
});

test('Python Core Queue DTOs and Apps Script expose one matching field/status/error contract', () => {
  const pythonContract = JSON.parse(execFileSync('python3', ['-c', [
    'import json',
    'from src.queue.contract import QUEUE_FIELDS, QueueStatus, QueueApiErrorCode',
    'print(json.dumps({"fields": QUEUE_FIELDS, "statuses": [item.value for item in QueueStatus], "errors": [item.value for item in QueueApiErrorCode]}))',
  ].join('; ')], { cwd: repoRoot, encoding: 'utf8' }));
  const cloudContract = scriptValue(makeRuntime().context,
    '({fields: STAGE7_QUEUE_HEADERS_, statuses: STAGE7_QUEUE_STATUSES_, errors: Object.keys(STAGE7_QUEUE_API_ERROR_MESSAGES_)})');
  assert.deepEqual(JSON.parse(JSON.stringify(cloudContract.fields)), pythonContract.fields);
  assert.deepEqual(JSON.parse(JSON.stringify(cloudContract.statuses)), pythonContract.statuses);
  assert.deepEqual(cloudContract.errors.slice().sort(), pythonContract.errors.slice().sort());
});

test('HMAC API accepts a valid signature and returns the uniform JSON envelope', () => {
  const runtime = makeRuntime();
  const response = signedPost(runtime, 'claim');
  assert.deepEqual(response, { ok: true, data: null });
});

test('Python and Apps Script sign the same raw payload bytes with an identical HMAC fixture', () => {
  const fixturePath = path.join(__dirname, 'fixtures', 'queue_hmac_payload.json');
  const payload = fs.readFileSync(fixturePath, 'utf8').replace(/\r?\n$/, '');
  const timestamp = '1790323200';
  const secret = 'test-only-queue-client-secret';
  const nodeSignature = crypto.createHmac('sha256', secret).update(`${timestamp}\n${payload}`, 'utf8').digest('base64');
  const pythonSignature = execFileSync('python3', ['-c', [
    'import sys',
    'from src.queue.client import sign_payload',
    'print(sign_payload(sys.argv[1], sys.argv[2], sys.argv[3]))',
  ].join('; '), secret, timestamp, payload], { cwd: repoRoot, encoding: 'utf8' }).trim();
  assert.equal(pythonSignature, nodeSignature);

  const runtime = makeRuntime();
  runtime.properties.UCI_QUEUE_HMAC_SECRET = secret;
  const request = runtime.context.stage7AuthenticateQueueApiRequest_(
    JSON.stringify({ timestamp, payload, signature: pythonSignature }), fixedNow,
  );
  assert.deepEqual(JSON.parse(JSON.stringify(request)), JSON.parse(payload));
});

test('signed claim API replays one claim_request_id without consuming the next Queue task', () => {
  const runtime = makeRuntime();
  enqueue(runtime, 'api-video-1');
  enqueue(runtime, 'api-video-2');
  const claimRequestId = 'req-api-000000000001';
  const first = signedPost(runtime, 'claim', { claim_request_id: claimRequestId });
  const replay = signedPost(runtime, 'claim', { claim_request_id: claimRequestId });
  assert.equal(first.data.queue_id, replay.data.queue_id);
  assert.equal(first.data.claim_token, replay.data.claim_token);
  assert.equal(recordFor(runtime, 'api-video-1').attempts, 1);
  assert.equal(recordFor(runtime, 'api-video-2').status, 'PENDING');
});

test('signed Apps Script API executes claim, heartbeat-to-start, complete, and recoverable fail', () => {
  const completeRuntime = makeRuntime();
  enqueue(completeRuntime, 'api-video-1');
  const claimed = signedPost(completeRuntime, 'claim');
  assert.equal(claimed.ok, true);
  assert.equal(claimed.data.status, 'CLAIMED');
  const started = signedPost(completeRuntime, 'heartbeat', {
    queue_id: claimed.data.queue_id, claim_token: claimed.data.claim_token,
  });
  assert.equal(started.data.status, 'PROCESSING');
  const completed = signedPost(completeRuntime, 'complete', {
    queue_id: claimed.data.queue_id,
    claim_token: claimed.data.claim_token,
    local_job_id: 'job-api-1',
    result_path: '/output/job-api-1',
  });
  assert.equal(completed.data.status, 'COMPLETED');
  assert.equal(completed.data.result_path, '/output/job-api-1');

  const failRuntime = makeRuntime();
  enqueue(failRuntime, 'api-video-2');
  const failClaim = signedPost(failRuntime, 'claim');
  signedPost(failRuntime, 'heartbeat', {
    queue_id: failClaim.data.queue_id, claim_token: failClaim.data.claim_token,
  });
  const paused = signedPost(failRuntime, 'fail', {
    queue_id: failClaim.data.queue_id,
    claim_token: failClaim.data.claim_token,
    local_job_id: 'job-api-2',
    error: { code: 'NETWORK_PAUSED', message: 'network interruption' },
  });
  assert.equal(paused.data.status, 'PAUSED');
});

test('HMAC API rejects missing secret, signature, timestamp, invalid signature, tampered body, and stale timestamp', () => {
  const runtime = makeRuntime();
  const payload = JSON.stringify({ action: 'claim' });
  const timestamp = String(Math.floor(fixedNow / 1000));
  const good = signRequest(runtime, 'claim');
  const withoutSecret = post(runtime, good);
  assert.equal(withoutSecret.error.code, 'AUTH_FAILED');
  runtime.properties.UCI_QUEUE_HMAC_SECRET = runtime.secret;
  assert.equal(post(runtime, { payload, signature: good.signature }).error.code, 'AUTH_FAILED');
  assert.equal(post(runtime, { timestamp, payload }).error.code, 'AUTH_FAILED');
  assert.equal(post(runtime, { ...good, signature: 'wrong' }).error.code, 'AUTH_FAILED');
  assert.equal(post(runtime, { ...good, payload: JSON.stringify({ action: 'heartbeat', queue_id: 'q' }) }).error.code, 'AUTH_FAILED');
  assert.equal(post(runtime, signRequest(runtime, 'claim', {}, 1)).error.code, 'AUTH_FAILED');
  assert.ok(runtime.logs.every((line) => !line.includes(runtime.secret)));

  const toleranceRuntime = makeRuntime();
  toleranceRuntime.properties.UCI_QUEUE_HMAC_SECRET = toleranceRuntime.secret;
  const oneSecondAhead = Math.floor(fixedNow / 1000) + 1;
  const toleranceClaim = { claim_request_id: 'req-tolerance-00001' };
  assert.equal(post(toleranceRuntime, signRequest(toleranceRuntime, 'claim', toleranceClaim, oneSecondAhead)).ok, true);
  const toleranceRow = toleranceRuntime.config.rows.findIndex((row) => row[0] === 'hmac_timestamp_tolerance_seconds');
  toleranceRuntime.config.getRange(toleranceRow + 1, 2).setValue(0);
  assert.equal(post(toleranceRuntime, signRequest(toleranceRuntime, 'claim', toleranceClaim, oneSecondAhead)).error.code, 'AUTH_FAILED');
});

test('API maps empty queue to success/null and hides internal exception details', () => {
  const runtime = makeRuntime();
  assert.deepEqual(signedPost(runtime, 'claim'), { ok: true, data: null });
  runtime.properties.UCI_QUEUE_HMAC_SECRET = runtime.secret;
  const badAction = signedPost(runtime, 'not-an-action');
  assert.equal(badAction.ok, false);
  assert.equal(typeof badAction.error.code, 'string');
  assert.equal(badAction.stack, undefined);
});

test('API returns stable not-found, invalid-state, and config errors without stack details', () => {
  const runtime = makeRuntime();
  const missing = signedPost(runtime, 'heartbeat', { queue_id: 'missing', claim_token: 'token' });
  assert.equal(missing.error.code, 'QUEUE_NOT_FOUND');
  enqueue(runtime, 'video-1');
  const current = claim(runtime);
  const wrongState = signedPost(runtime, 'complete', {
    queue_id: current.queue_id, claim_token: current.claim_token, local_job_id: 'job-1', result_path: '/out',
  });
  assert.equal(wrongState.error.code, 'INVALID_STATE');
  const leaseRow = runtime.config.rows.findIndex((row) => row[0] === 'queue_lease_seconds');
  runtime.config.rows.splice(leaseRow, 1);
  const invalidConfig = signedPost(runtime, 'claim');
  assert.equal(invalidConfig.error.code, 'CONFIG_INVALID');
  assert.equal(invalidConfig.stack, undefined);
});

function statusRuntime() {
  const runtime = makeRuntime({ configRows: [
    ['production_timezone', 'Asia/Shanghai', ''], ['discovery_window_start', '00:00', ''], ['discovery_window_end', '04:00', ''],
    ['last_discovery_slot', '2026-09-30 03:50', ''], ['last_daily_selection_day', new Date('2026-09-30T00:00:00Z'), ''],
  ] });
  const videoHeaders = ['video_id', 'creator_name', 'title', 'published_at', 'lifecycle_state', 'broadcast_type', 'hot_reason', 'selection_day'];
  runtime.spreadsheet.sheets.push(new MemorySheet('Videos', [
    videoHeaders,
    ['in-batch', 'Example Tech', 'Protests', '2026-09-29T17:21:39.000Z', 'NORMAL', 'VIDEO', '', ''],
    ['live-one', 'NSF', 'Launch', '2026-09-29T18:00:00.000Z', 'LIVE_REJECTED', 'LIVE', '', ''],
    ['other-day', 'Example Reviews', 'Old', '2026-09-27T18:59:02.000Z', 'NORMAL', 'VIDEO', '', ''],
  ]));
  runtime.spreadsheet.sheets.push(new MemorySheet('Snapshots', [
    ['video_id', 'snapshot_stage_minutes', 'captured_at', 'view_count', 'like_rate', 'baseline_final_views_median', 'historical_median_like_rate'],
    ['in-batch', 30, '2026-09-29T17:55:00.000Z', 423, 0.026, 33750, 0.0123],
    ['other-day', 30, '2026-09-27T19:30:00.000Z', 276, 0.04, 45112, 0.009],
  ]));
  runtime.context.Utilities.formatDate = (date, timezone, pattern) => {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(date);
    const p = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    if (pattern === 'HH:mm') return `${p.hour}:${p.minute}`;
    return pattern.includes('|') ? `${p.year}-${p.month}-${p.day}|${p.hour}:${p.minute}` : `${p.year}-${p.month}-${p.day}`;
  };
  runtime.properties.UCI_QUEUE_HMAC_SECRET = runtime.secret;
  return runtime;
}

test('signed status reports Config markers, the batch videos with snapshots, and the Queue without side effects', () => {
  const runtime = statusRuntime();
  enqueue(runtime, 'active-video');
  const before = runtime.queue.snapshot();
  const response = post(runtime, signRequest(runtime, 'status', { day: '2026-09-30' }));
  assert.equal(response.ok, true, JSON.stringify(response));
  const report = response.data;
  assert.equal(report.healthy, true);
  assert.equal(report.day, '2026-09-30');
  assert.equal(report.config.last_discovery_slot, '2026-09-30 03:50');
  // Date cells read back exactly as the Scheduler compares them.
  assert.equal(report.config.last_daily_selection_day, '2026-09-30');
  assert.deepEqual(report.videos.map((video) => video.video_id), ['in-batch', 'live-one']);
  assert.equal(report.videos[1].broadcast_type, 'LIVE');
  assert.deepEqual(report.videos[0].snapshots.map((snap) => snap.view_count), [423]);
  assert.deepEqual(report.queue.map((task) => [task.video_id, task.status]), [['active-video', 'PENDING']]);
  assert.deepEqual(runtime.queue.snapshot(), before);
});

test('status isolates a failing read and names it instead of failing the whole report', () => {
  const runtime = statusRuntime();
  runtime.spreadsheet.sheets = runtime.spreadsheet.sheets.filter((sheet) => sheet.name !== 'Snapshots');
  const report = post(runtime, signRequest(runtime, 'status', { day: '2026-09-30' })).data;
  assert.equal(report.healthy, false);
  const failed = report.checks.filter((check) => !check.ok);
  assert.deepEqual(failed.map((check) => check.name), ['snapshots']);
  assert.match(failed[0].error, /Snapshots/);
  assert.equal(report.videos.length, 2);
  assert.equal(post(runtime, signRequest(runtime, 'status', { day: '30/09/2026' })).error.code, 'INVALID_REQUEST');
});

test('a lost Sheets permission is reported to signed callers only, never to unsigned ones', () => {
  const runtime = makeRuntime();
  runtime.properties.UCI_QUEUE_HMAC_SECRET = runtime.secret;
  runtime.context.SpreadsheetApp = { getActiveSpreadsheet() { return this.openById(''); }, openById: () => {
    throw new Error('You do not have permission to call SpreadsheetApp.openById. Required permissions: https://www.googleapis.com/auth/spreadsheets');
  } };
  const signed = post(runtime, signRequest(runtime, 'claim', { claim_request_id: 'req-permission-0001' }));
  assert.equal(signed.error.code, 'INVALID_STATE');
  assert.match(signed.error.detail, /permission to call SpreadsheetApp\.openById/);
  const forged = post(runtime, { ...signRequest(runtime, 'claim', { claim_request_id: 'req-permission-0001' }), signature: 'forged' });
  assert.equal(forged.error.code, 'AUTH_FAILED');
  assert.equal(forged.error.detail, undefined);
  const report = post(runtime, signRequest(runtime, 'status')).error;
  assert.match(report.detail, /permission/);
  assert.ok(runtime.logs.every((line) => !line.includes(runtime.secret)));
});
