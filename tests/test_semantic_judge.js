const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');
const context = vm.createContext({
  console, Date, Math, Set, Map, Number, String, Array, Object, JSON, Error,
  Utilities: {
    DigestAlgorithm: { SHA_256: 'SHA-256' },
    Charset: { UTF_8: 'UTF-8' },
    computeDigest: (_algorithm, text) => Array.from(crypto.createHash('sha256').update(String(text), 'utf8').digest())
      .map((byte) => byte > 127 ? byte - 256 : byte),
  },
});
for (const file of ['monitor.gs', 'discovery.gs', 'stats.gs', 'scoring.gs', 'semantic.gs', 'selection.gs']) {
  const fullPath = path.join(scriptPath, file);
  if (fs.existsSync(fullPath)) vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
}

function value(name) { return vm.runInContext(name, context); }

function validSemanticResult(overrides = {}) {
  return {
    decision: 'ACCEPT',
    reasons: ['TARGET_RELEVANT', 'CHINESE_AUDIENCE_VALUE'],
    summary_zh: '内容与目标方向相关，具有受众价值。',
    ...overrides,
  };
}

function semanticConfig(overrides = {}) {
  return {
    semantic_judge_enabled: true,
    semantic_provider: 'gemini',
    semantic_retry_count: 1,
    semantic_timeout_seconds: 17,
    semantic_gemini_model: 'test-model',
    ...overrides,
  };
}

function geminiEnvelope(text) {
  return {
    candidates: [{
      content: { role: 'model', parts: [{ text }] },
      finishReason: 'STOP',
      index: 0,
      safetyRatings: [],
    }],
    usageMetadata: { promptTokenCount: 1, candidatesTokenCount: 1, totalTokenCount: 2 },
    modelVersion: 'test-model',
    responseId: 'test-response',
  };
}

class MemorySheet {
  constructor(name, rows) { this.name = name; this.rows = rows.map((row) => row.slice()); }
  getName() { return this.name; }
  getLastRow() { return this.rows.reduce((last, row, i) => row.some((v) => v !== '' && v != null) ? i + 1 : last, 0); }
  getLastColumn() { return Math.max(0, ...this.rows.map((row) => row.reduce((last, value, i) => value !== '' && value != null ? i + 1 : last, 0))); }
  setFrozenRows() {}
  getRange(row, column, rowCount = 1, columnCount = 1) {
    const sheet = this;
    return {
      getValues() {
        return Array.from({ length: rowCount }, (_, r) => Array.from({ length: columnCount }, (_, c) => sheet.rows[row + r - 1]?.[column + c - 1] ?? ''));
      },
      setValues(values) {
        values.forEach((valuesRow, r) => valuesRow.forEach((value, c) => {
          sheet.rows[row + r - 1] ||= [];
          sheet.rows[row + r - 1][column + c - 1] = value;
        }));
      },
      setValue(value) {
        sheet.rows[row - 1] ||= [];
        sheet.rows[row - 1][column - 1] = value;
      },
    };
  }
}

function configRows(config) {
  return [['key', 'value', 'description'], ...Object.entries(config).map(([key, value]) => [key, value, 'test'])];
}

function hotVideoRow(videoHeaders, { videoId = 'v1', state = 'DATA_HOT', title = 'AI Agent workflow', description = 'A practical AI agent walkthrough.', trust = false } = {}) {
  const row = videoHeaders.map(() => '');
  const set = (key, value) => { const i = videoHeaders.indexOf(key); if (i >= 0) row[i] = value; };
  set('video_id', videoId);
  set('channel_id', 'creator-1');
  set('creator_name', 'Creator One');
  set('title', title);
  set('description', description);
  set('video_url', `https://www.youtube.com/watch?v=${videoId}`);
  set('published_at', '2026-09-25T10:00:00Z');
  set('lifecycle_state', state);
  set('hot_reason', 'cold_views_acceleration');
  set('hot_mode', 'cold');
  set('hot_checkpoint', 60);
  return row;
}

function makeVideoTables(rows, { trusted = false, history = [] } = {}) {
  const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const videos = new MemorySheet('Videos', [headers, ...rows]);
  const creators = new MemorySheet('Creators', [
    ['channel_id', 'creator_name', 'trusted_creator'],
    ['creator-1', 'Creator One', trusted],
  ]);
  const baseline = new MemorySheet('Baseline', [
    ['channel_id', 'video_id', 'title', 'description', 'semantic_summary', 'published_at'],
    ...history.map((record) => ['creator-1', record.video_id, record.title, record.description || '', record.semantic_summary || '', record.published_at || '']),
  ]);
  const config = new MemorySheet('Config', configRows(semanticConfig()));
  const tables = { Videos: videos, Creators: creators, Baseline: baseline, Config: config };
  const originalReadTable = context.stage6ReadTable_;
  const originalReadConfig = context.stage6ReadConfig_;
  context.stage6ReadTable_ = (name) => {
    const sheet = tables[name];
    if (!sheet) throw new Error(`Unexpected table ${name}`);
    return { sheet, headers: sheet.rows[0], rows: sheet.rows.slice(1) };
  };
  context.stage6ReadConfig_ = () => Object.fromEntries(config.rows.slice(1).map(([key, val]) => [key, val]));
  return {
    headers, videos, tables,
    restore() { context.stage6ReadTable_ = originalReadTable; context.stage6ReadConfig_ = originalReadConfig; },
  };
}

test('semantic defaults are disabled and schema/config migration appends idempotently', () => {
  const videoHeaders = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const oldHeaders = videoHeaders.slice(0, -8);
  const row = oldHeaders.map((_, index) => `keep-${index}`);
  const videos = new MemorySheet('Videos', [oldHeaders, row]);
  const configRowsBefore = [['key', 'value', 'description'], ['semantic_judge_enabled', true, 'manual']];
  const config = new MemorySheet('Config', configRowsBefore);
  context.stage6EnsureHeaders_(videos, videoHeaders);
  const originalReadTable = context.stage6ReadTable_;
  context.stage6ReadTable_ = (name) => ({ sheet: config, headers: config.rows[0], rows: config.rows.slice(1) });
  context.stage6EnsureConfig_(config);
  const afterFirst = JSON.parse(JSON.stringify({ headers: videos.rows, config: config.rows }));
  context.stage6EnsureHeaders_(videos, videoHeaders);
  context.stage6EnsureConfig_(config);
  context.stage6ReadTable_ = originalReadTable;

  assert.equal(value('STAGE8_CONFIG_ROWS_').find((entry) => entry[0] === 'semantic_judge_enabled')[1], false);
  assert.equal(config.rows.find((entry) => entry[0] === 'semantic_judge_enabled')[1], true);
  assert.deepEqual(videos.rows[0], videoHeaders);
  assert.deepEqual(videos.rows[1].slice(0, oldHeaders.length), row);
  assert.deepEqual({ headers: videos.rows, config: config.rows }, afterFirst);
  assert.equal(new Set(videos.rows[0]).size, videos.rows[0].length);
  assert.equal(new Set(config.rows.slice(1).map((entry) => entry[0])).size, config.rows.length - 1);
});

test('semantic response validation accepts only the formal discrete contract', () => {
  const valid = context.stage8ValidateSemanticOutput_(validSemanticResult());
  assert.equal(valid.status, 'SUCCESS');
  assert.equal(valid.result.decision, 'ACCEPT');
  assert.deepEqual(Array.from(valid.result.reasons), ['TARGET_RELEVANT', 'CHINESE_AUDIENCE_VALUE']);

  for (const output of [
    'not json',
    { ...validSemanticResult(), decision: 'MAYBE' },
    { ...validSemanticResult(), reasons: ['MADE_UP_REASON'] },
    { decision: 'ACCEPT', reasons: ['TARGET_RELEVANT'] },
  ]) {
    assert.equal(context.stage8ValidateSemanticOutput_(output).status, 'INVALID_RESPONSE');
  }
});

test('Gemini prompt is semantic-only and structured output is bounded by the response schema', () => {
  const input = {
    video_id: 'v1', creator_id: 'creator-1', creator_name: 'Creator One',
    title: 'Agent workflow', description: 'Build an AI Agent workflow.',
    published_at: '2026-09-25T10:00:00Z', url: 'https://example.test/v1',
    trusted_creator: false, hot_reason: 'cold_views_acceleration', hot_mode: 'cold', hot_checkpoint: 60,
    history: [{ video_id: 'old-1', title: 'Earlier agent tutorial', description: '', semantic_summary: 'Agent basics.' }],
  };
  const config = semanticConfig();
  const request = context.stage8BuildGeminiRequest_(input, config);
  const body = request.body;
  assert.match(body.systemInstruction.parts[0].text, /data HOT/i);
  assert.match(body.systemInstruction.parts[0].text, /never.*score.*rank/i);
  assert.deepEqual(JSON.parse(JSON.stringify(body.generationConfig.responseJsonSchema.properties.decision.enum)), ['ACCEPT', 'REJECT']);
  assert.ok(!JSON.stringify(body).includes('semantic_score'));
  assert.ok(!JSON.stringify(body).includes('view_count'));
  assert.equal(request.timeoutSeconds, 17);
});

test('Gemini adapter reads the Script Property and validates the real GenerateContent envelope', () => {
  const apiKey = 'unit-test-only-secret-value';
  const expected = validSemanticResult();
  const calls = [];
  context.PropertiesService = { getScriptProperties: () => ({ getProperty: (key) => key === 'UCI_GEMINI_API_KEY' ? apiKey : null }) };
  context.UrlFetchApp = { fetch: (url, options) => {
    calls.push({ url, options });
    return { getResponseCode: () => 200, getContentText: () => JSON.stringify(geminiEnvelope(JSON.stringify(expected))) };
  } };

  const result = context.stage8GeminiAdapter_({ title: 'Agent workflow' }, semanticConfig());
  assert.equal(result.status, 'SUCCESS');
  assert.deepEqual(JSON.parse(JSON.stringify(result.result)), expected);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /models\/test-model:generateContent$/);
  assert.equal(calls[0].options.headers['x-goog-api-key'], apiKey);
  assert.equal(calls[0].options.timeoutSeconds, 17);
  assert.equal(calls[0].options.muteHttpExceptions, true);
});

test('missing Gemini credential or model returns unavailable without network access', () => {
  let fetchCount = 0;
  context.PropertiesService = { getScriptProperties: () => ({ getProperty: () => '' }) };
  context.UrlFetchApp = { fetch: () => { fetchCount += 1; throw new Error('must not fetch'); } };
  assert.equal(context.stage8GeminiAdapter_({}, semanticConfig()).status, 'UNAVAILABLE');
  context.PropertiesService = { getScriptProperties: () => ({ getProperty: () => 'not-read' }) };
  assert.equal(context.stage8GeminiAdapter_({}, semanticConfig({ semantic_gemini_model: '' })).status, 'UNAVAILABLE');
  assert.equal(fetchCount, 0);
});

test('semantic judge retries only transient provider failures and then returns unavailable', () => {
  let attempts = 0;
  const adapter = () => {
    attempts += 1;
    return { status: 'UNAVAILABLE', retryable: true, reason: 'TEMPORARY_SERVICE' };
  };
  const result = context.stage8Judge_({ video_id: 'v1' }, semanticConfig({ semantic_retry_count: 2 }), adapter);
  assert.equal(attempts, 3);
  assert.equal(result.status, 'UNAVAILABLE');
  assert.equal(result.reason, 'TEMPORARY_SERVICE');

  attempts = 0;
  const invalid = context.stage8Judge_({ video_id: 'v1' }, semanticConfig(), () => {
    attempts += 1;
    return { status: 'INVALID_RESPONSE', reason: 'SCHEMA_INVALID' };
  });
  assert.equal(attempts, 1);
  assert.equal(invalid.status, 'INVALID_RESPONSE');
});

test('Gemini HTTP 5xx retries with the configured timeout; quota response is not retried', () => {
  const apiKey = 'unit-test-only-secret-value';
  let calls = 0;
  context.PropertiesService = { getScriptProperties: () => ({ getProperty: () => apiKey }) };
  context.UrlFetchApp = { fetch: () => {
    calls += 1;
    if (calls === 1) return { getResponseCode: () => 503, getContentText: () => '{"error":"temporary"}' };
    return { getResponseCode: () => 200, getContentText: () => JSON.stringify(geminiEnvelope(JSON.stringify(validSemanticResult()))) };
  } };
  const retried = context.stage8Judge_({ video_id: 'v1' }, semanticConfig());
  assert.equal(retried.status, 'SUCCESS');
  assert.equal(calls, 2);

  calls = 0;
  context.UrlFetchApp = { fetch: () => { calls += 1; return { getResponseCode: () => 429, getContentText: () => '{}' }; } };
  const quota = context.stage8Judge_({ video_id: 'v1' }, semanticConfig());
  assert.equal(quota.status, 'UNAVAILABLE');
  assert.equal(quota.reason, 'QUOTA_UNAVAILABLE');
  assert.equal(calls, 1);
});

test('Stage 6 deterministic DATA_HOT to CANDIDATE flow remains unchanged when judge is disabled', () => {
  const setup = makeVideoTables([hotVideoRow(Array.from(value('STAGE6_VIDEO_HEADERS_')))], { trusted: false });
  let calls = 0;
  const originalAdapter = context.stage8GeminiAdapter_;
  context.stage8GeminiAdapter_ = () => { calls += 1; throw new Error('disabled judge called'); };
  setup.tables.Config.rows.find((row) => row[0] === 'semantic_judge_enabled')[1] = false;
  try {
    assert.equal(context.stage6RefreshCandidatePool_(), 1);
    assert.equal(setup.videos.rows[1][setup.headers.indexOf('lifecycle_state')], 'CANDIDATE');
    assert.equal(calls, 0);
  } finally {
    context.stage8GeminiAdapter_ = originalAdapter;
    setup.restore();
  }
});

test('ordinary WATCH videos are never sent to Semantic Judge', () => {
  const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const setup = makeVideoTables([hotVideoRow(headers, { state: 'WATCH' })]);
  let calls = 0;
  const originalAdapter = context.stage8GeminiAdapter_;
  context.stage8GeminiAdapter_ = () => { calls += 1; return { status: 'SUCCESS', result: validSemanticResult() }; };
  try {
    assert.equal(context.stage6RefreshCandidatePool_(), 0);
    assert.equal(setup.videos.rows[1][headers.indexOf('lifecycle_state')], 'WATCH');
    assert.equal(calls, 0);
  } finally {
    context.stage8GeminiAdapter_ = originalAdapter;
    setup.restore();
  }
});

test('Discovery persists video descriptions for later semantic input when available', () => {
  const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const row = context.stage6BuildVideoRow_(headers, {
    video_id: 'discovered-video', creator_name: 'Creator One', title: 'AI workflow',
    description: 'A description supplied by the YouTube playlist snippet.',
    published_at: '2026-09-25T10:00:00Z', lifecycle_state: 'WATCH',
  });
  assert.equal(row[headers.indexOf('description')], 'A description supplied by the YouTube playlist snippet.');
});

test('DATA_HOT ACCEPT proceeds to CANDIDATE and stores the verified metadata result', () => {
  const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const setup = makeVideoTables([hotVideoRow(headers)], { history: [{ video_id: 'old', title: 'Previous AI agent video' }] });
  let calls = 0;
  const originalAdapter = context.stage8GeminiAdapter_;
  let observedInput;
  context.stage8GeminiAdapter_ = (input) => { calls += 1; observedInput = input; return { status: 'SUCCESS', result: validSemanticResult() }; };
  try {
    assert.equal(context.stage6RefreshCandidatePool_(), 1);
    const row = setup.videos.rows[1];
    assert.equal(row[headers.indexOf('lifecycle_state')], 'CANDIDATE');
    assert.equal(row[headers.indexOf('semantic_status')], 'SUCCESS');
    assert.equal(row[headers.indexOf('semantic_decision')], 'ACCEPT');
    assert.equal(row[headers.indexOf('semantic_provider')], 'gemini');
    assert.ok(row[headers.indexOf('semantic_checked_at')]);
    assert.ok(row[headers.indexOf('semantic_input_hash')]);
    assert.equal(calls, 1);
    assert.equal(observedInput.video_id, 'v1');
    assert.equal(observedInput.creator_id, 'creator-1');
    assert.equal(observedInput.creator_name, 'Creator One');
    assert.equal(observedInput.description, 'A practical AI agent walkthrough.');
    assert.equal(observedInput.trusted_creator, false);
    assert.equal(observedInput.history[0].title, 'Previous AI agent video');
    assert.equal(Object.hasOwn(observedInput, 'view_count'), false);
  } finally {
    context.stage8GeminiAdapter_ = originalAdapter;
    setup.restore();
  }
});

test('DATA_HOT REJECT stays DATA_HOT and the unchanged successful input is cached', () => {
  const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const setup = makeVideoTables([hotVideoRow(headers)]);
  let calls = 0;
  const originalAdapter = context.stage8GeminiAdapter_;
  context.stage8GeminiAdapter_ = () => {
    calls += 1;
    return { status: 'SUCCESS', result: validSemanticResult({ decision: 'REJECT', reasons: ['PURE_ADVERTISEMENT'], summary_zh: '纯广告内容。' }) };
  };
  try {
    assert.equal(context.stage6RefreshCandidatePool_(), 0);
    assert.equal(setup.videos.rows[1][headers.indexOf('lifecycle_state')], 'DATA_HOT');
    assert.equal(context.stage6RefreshCandidatePool_(), 0);
    assert.equal(calls, 1);
    assert.equal(setup.videos.rows[1][headers.indexOf('semantic_decision')], 'REJECT');
  } finally {
    context.stage8GeminiAdapter_ = originalAdapter;
    setup.restore();
  }
});

test('changing semantic input invalidates the cached result and reevaluates', () => {
  const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const setup = makeVideoTables([hotVideoRow(headers)]);
  let calls = 0;
  const originalAdapter = context.stage8GeminiAdapter_;
  context.stage8GeminiAdapter_ = () => {
    calls += 1;
    return { status: 'SUCCESS', result: validSemanticResult({ decision: 'REJECT', reasons: ['DUPLICATE_CONTENT'], summary_zh: '和历史内容重复。' }) };
  };
  try {
    context.stage6RefreshCandidatePool_();
    setup.videos.rows[1][headers.indexOf('description')] = 'Updated description changes the semantic input.';
    context.stage6RefreshCandidatePool_();
    assert.equal(calls, 2);
  } finally {
    context.stage8GeminiAdapter_ = originalAdapter;
    setup.restore();
  }
});

test('trusted creator can proceed on unavailable or invalid response; non-trusted remains DATA_HOT', () => {
  for (const trusted of [true, false]) {
    const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
    const setup = makeVideoTables([hotVideoRow(headers)], { trusted });
    const originalAdapter = context.stage8GeminiAdapter_;
    context.stage8GeminiAdapter_ = () => ({ status: 'UNAVAILABLE', reason: 'NOT_CONFIGURED' });
    try {
      assert.equal(context.stage6RefreshCandidatePool_(), trusted ? 1 : 0);
      const row = setup.videos.rows[1];
      assert.equal(row[headers.indexOf('lifecycle_state')], trusted ? 'CANDIDATE' : 'DATA_HOT');
      assert.equal(row[headers.indexOf('semantic_status')], 'UNAVAILABLE');
      assert.equal(row[headers.indexOf('semantic_decision')], '');
    } finally {
      context.stage8GeminiAdapter_ = originalAdapter;
      setup.restore();
    }
  }
});

test('invalid model responses do not crash Candidate refresh or get cached as decisions', () => {
  const headers = Array.from(value('STAGE6_VIDEO_HEADERS_'));
  const setup = makeVideoTables([hotVideoRow(headers)]);
  let calls = 0;
  const originalAdapter = context.stage8GeminiAdapter_;
  context.stage8GeminiAdapter_ = () => { calls += 1; return { status: 'INVALID_RESPONSE', reason: 'SCHEMA_INVALID' }; };
  try {
    assert.equal(context.stage6RefreshCandidatePool_(), 0);
    assert.equal(context.stage6RefreshCandidatePool_(), 0);
    assert.equal(calls, 2);
    const row = setup.videos.rows[1];
    assert.equal(row[headers.indexOf('lifecycle_state')], 'DATA_HOT');
    assert.equal(row[headers.indexOf('semantic_status')], 'INVALID_RESPONSE');
    assert.equal(row[headers.indexOf('semantic_decision')], '');
  } finally {
    context.stage8GeminiAdapter_ = originalAdapter;
    setup.restore();
  }
});
