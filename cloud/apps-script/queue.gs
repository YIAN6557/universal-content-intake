// Stage 7 Queue Sheet adapter. Schema growth reuses Stage 6's append-only
// header migration helpers; all Queue reads and writes share ScriptLock.
const STAGE7_QUEUE_CONFIG_ROWS_ = [
  ['queue_lease_seconds', 900, 'Lease duration for an active Queue task, in seconds.'],
  ['hmac_timestamp_tolerance_seconds', 300, 'Maximum accepted request timestamp skew, in seconds.'],
];

// This append-only request ledger preserves superseded IDs so an old retry can
// never be mistaken for a new poll after its Queue row has a newer owner.

function stage7SetupQueue() {
  const result = stage7SetupQueue_();
  Logger.log('Stage 7 Queue setup complete; existing Queue rows were preserved.');
  return result;
}

function stage7SetupQueue_() {
  const ss = stage6Spreadsheet_();
  let config = ss.getSheetByName('Config');
  if (!config) config = ss.insertSheet('Config');
  stage6EnsureHeaders_(config, ['key', 'value', 'description']);
  let queue = ss.getSheetByName('Queue');
  if (!queue) queue = ss.insertSheet('Queue');
  let claimRequests = ss.getSheetByName('QueueClaimRequests');
  if (!claimRequests) claimRequests = ss.insertSheet('QueueClaimRequests');
  stage7EnsureQueueHeaders_(queue);
  stage6EnsureHeaders_(claimRequests, STAGE7_CLAIM_REQUEST_HEADERS_);
  stage7EnsureQueueConfig_();
  const headers = queue.getRange(1, 1, 1, queue.getLastColumn()).getValues()[0].map(String);
  const claimRequestHeaders = claimRequests.getRange(1, 1, 1, claimRequests.getLastColumn()).getValues()[0].map(String);
  return {
    queue_headers: headers,
    claim_request_headers: claimRequestHeaders,
    config_keys: STAGE7_QUEUE_CONFIG_ROWS_.map(function (row) { return row[0]; }),
  };
}

function stage7EnsureQueueHeaders_(sheet) {
  const lastColumn = sheet.getLastColumn();
  if (!lastColumn) {
    stage6EnsureHeaders_(sheet, STAGE7_QUEUE_HEADERS_);
    return;
  }
  const existing = sheet.getRange(1, 1, 1, lastColumn).getValues()[0].map(function (header) {
    return header === null || header === undefined ? '' : String(header);
  });
  const present = new Set(existing.filter(function (header) { return String(header).trim() !== ''; }));
  // Stage 6's shared migrator appends missing columns; put the current order
  // first so legacy Queue layouts stay valid on the next idempotent run.
  const migrationContract = existing.filter(function (header) { return String(header).trim() !== ''; })
    .concat(STAGE7_QUEUE_HEADERS_.filter(function (header) { return !present.has(header); }));
  stage6EnsureHeaders_(sheet, migrationContract);
}

function stage7EnsureQueueConfig_() {
  const table = stage6ReadTable_('Config');
  if (table.headers.indexOf('key') < 0 || table.headers.indexOf('value') < 0 || table.headers.indexOf('description') < 0) {
    throw stage7QueueError_('CONFIG_INVALID', 'Config Sheet headers are invalid.');
  }
  const existing = stage6ReadConfig_();
  const missing = STAGE7_QUEUE_CONFIG_ROWS_.filter(function (row) {
    return !Object.prototype.hasOwnProperty.call(existing, row[0]);
  });
  if (missing.length) table.sheet.getRange(table.sheet.getLastRow() + 1, 1, missing.length, 3).setValues(missing);
}

function stage7QueueHeaders_() {
  return STAGE7_QUEUE_HEADERS_.slice();
}

function stage7ReadQueueConfig_() {
  const config = stage6ReadConfig_();
  return {
    leaseSeconds: stage7ConfigInteger_(config, 'queue_lease_seconds', 1),
    timestampToleranceSeconds: stage7ConfigInteger_(config, 'hmac_timestamp_tolerance_seconds', 0),
  };
}

function stage7ConfigInteger_(config, key, minimum) {
  const raw = config[key];
  if (raw === null || raw === undefined || typeof raw === 'boolean' || String(raw).trim() === '') {
    throw stage7QueueError_('CONFIG_INVALID', 'Required Queue Config is missing: ' + key + '.');
  }
  if (typeof raw !== 'number' && !/^\d+$/.test(String(raw).trim())) {
    throw stage7QueueError_('CONFIG_INVALID', 'Queue Config must be an integer: ' + key + '.');
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum) {
    throw stage7QueueError_('CONFIG_INVALID', 'Queue Config is out of range: ' + key + '.');
  }
  return value;
}

function stage7WithScriptLock_(callback) {
  const lock = LockService.getScriptLock();
  let acquired = false;
  try {
    lock.waitLock(STAGE7_QUEUE_LOCK_WAIT_MS_);
    acquired = true;
    return callback();
  } catch (error) {
    if (error && error.queueErrorCode) throw error;
    throw stage7QueueError_('INVALID_STATE', 'Queue operation could not acquire or use the script lock.');
  } finally {
    if (acquired) lock.releaseLock();
  }
}

function stage7QueueTable_() {
  const sheet = stage6Spreadsheet_().getSheetByName('Queue');
  if (!sheet) throw stage7QueueError_('CONFIG_INVALID', 'Queue Sheet is not initialized.');
  const values = sheet.getDataRange().getValues();
  if (!values.length || !values[0].length) throw stage7QueueError_('CONFIG_INVALID', 'Queue Sheet is missing its header row.');
  const headers = values[0].map(function (header) { return header === null || header === undefined ? '' : String(header); });
  stage6ValidateUniqueHeaders_(headers, 'Queue');
  const required = STAGE7_QUEUE_HEADERS_;
  if (required.some(function (header) { return headers.indexOf(header) < 0; })) {
    throw stage7QueueError_('CONFIG_INVALID', 'Queue Sheet schema is incomplete; run stage7SetupQueue_.');
  }
  const rows = [];
  const byQueueId = new Map();
  const byVideoId = new Map();
  const byClaimRequestId = new Map();
  values.slice(1).forEach(function (row, offset) {
    if (!row.some(function (value) { return value !== '' && value !== null && value !== undefined; })) return;
    const record = { sheet_row: offset + 2, sheet_values: row.slice() };
    headers.forEach(function (header, index) { if (header) record[header] = row[index]; });
    const queueId = String(record.queue_id || '').trim();
    const videoId = String(record.video_id || '').trim();
    if (!queueId || !videoId || !String(record.url || '').trim()) {
      throw stage7QueueError_('INVALID_STATE', 'Queue row is missing queue_id, video_id, or url.');
    }
    if (STAGE7_QUEUE_STATUSES_.indexOf(String(record.status || '')) < 0) {
      throw stage7QueueError_('INVALID_STATE', 'Queue row has an unknown status.');
    }
    const attempts = record.attempts === '' || record.attempts === null || record.attempts === undefined ? 0 : Number(record.attempts);
    if (!Number.isInteger(attempts) || attempts < 0) throw stage7QueueError_('INVALID_STATE', 'Queue attempts value is invalid.');
    if (byQueueId.has(queueId)) throw stage7QueueError_('INVALID_STATE', 'Queue contains a duplicate queue_id.');
    if (byVideoId.has(videoId)) throw stage7QueueError_('INVALID_STATE', 'Queue contains duplicate video_id tasks.');
    const claimRequestId = String(record.claim_request_id || '').trim();
    if (claimRequestId && byClaimRequestId.has(claimRequestId)) {
      throw stage7QueueError_('INVALID_STATE', 'Queue contains a duplicate current claim_request_id.');
    }
    byQueueId.set(queueId, record);
    byVideoId.set(videoId, record);
    if (claimRequestId) byClaimRequestId.set(claimRequestId, record);
    rows.push(record);
  });
  return {
    sheet: sheet,
    headers: headers,
    rows: rows,
    byQueueId: byQueueId,
    byVideoId: byVideoId,
    byClaimRequestId: byClaimRequestId,
  };
}

function stage7ClaimRequestTable_() {
  const sheet = stage6Spreadsheet_().getSheetByName('QueueClaimRequests');
  if (!sheet) throw stage7QueueError_('CONFIG_INVALID', 'Queue claim request ledger is not initialized.');
  const values = sheet.getDataRange().getValues();
  if (!values.length || !values[0].length) throw stage7QueueError_('CONFIG_INVALID', 'Queue claim request ledger is missing its header row.');
  const headers = values[0].map(function (header) { return header === null || header === undefined ? '' : String(header); });
  stage6ValidateUniqueHeaders_(headers, 'QueueClaimRequests');
  if (STAGE7_CLAIM_REQUEST_HEADERS_.some(function (header) { return headers.indexOf(header) < 0; })) {
    throw stage7QueueError_('CONFIG_INVALID', 'Queue claim request ledger schema is incomplete; run stage7SetupQueue_.');
  }
  const byRequestId = new Map();
  values.slice(1).forEach(function (row, offset) {
    if (!row.some(function (value) { return value !== '' && value !== null && value !== undefined; })) return;
    const record = { sheet_row: offset + 2 };
    headers.forEach(function (header, index) { if (header) record[header] = row[index]; });
    const requestId = String(record.claim_request_id || '').trim();
    const queueId = String(record.queue_id || '').trim();
    if (!requestId || !queueId) throw stage7QueueError_('INVALID_STATE', 'Queue claim request ledger row is incomplete.');
    if (byRequestId.has(requestId)) throw stage7QueueError_('INVALID_STATE', 'Queue claim request ledger contains a duplicate request ID.');
    byRequestId.set(requestId, record);
  });
  return { sheet: sheet, headers: headers, byRequestId: byRequestId };
}

function stage7AppendClaimRequest_(ledger, requestId, queueId, createdAt) {
  if (ledger.byRequestId.has(requestId)) {
    const existing = ledger.byRequestId.get(requestId);
    if (String(existing.queue_id) !== String(queueId)) {
      throw stage7QueueError_('CLAIM_REQUEST_EXPIRED', 'Claim request ID is already bound to another task.');
    }
    return existing;
  }
  const record = {
    claim_request_id: String(requestId),
    queue_id: String(queueId),
    created_at: String(createdAt),
  };
  ledger.sheet.appendRow(ledger.headers.map(function (header) {
    return Object.prototype.hasOwnProperty.call(record, header) ? record[header] : '';
  }));
  record.sheet_row = ledger.sheet.getLastRow();
  ledger.byRequestId.set(String(requestId), record);
  return record;
}

function stage7QueueClaimResponse_(record) {
  return {
    queue_id: record.queue_id,
    status: record.status,
    claim_request_id: String(record.claim_request_id || ''),
    claim_token: record.claim_token,
    claimed_at: record.claimed_at,
    url: record.url,
    video_id: record.video_id,
    lease_until: record.lease_until,
    attempts: Number(record.attempts || 0),
    local_job_id: record.local_job_id || '',
    selection_day: record.selection_day || '',
    selection_rank: record.selection_rank === '' || record.selection_rank === null || record.selection_rank === undefined ? null : Number(record.selection_rank),
    rank2_start_cutoff_at: record.rank2_start_cutoff_at || '',
    core_started_at: record.core_started_at || '',
    monitor: stage7MonitorEvidence_(record.video_id),
  };
}

// Creator-monitor facts for the local info.md. Read-only
// and best-effort: a missing sheet, row or column never blocks a claim.
const STAGE7_MONITOR_EVIDENCE_FIELDS_ = [
  ['video_id', 'video_id'], ['title', 'title'], ['creator_name', 'creator_name'], ['channel_id', 'channel_id'],
  ['published_at', 'published_at'], ['first_seen', 'discovered_at'], ['hot_at', 'data_hot_at'],
  ['views_at_hot', 'view_count'], ['like_rate', 'like_rate'], ['velocity', 'views_per_hour'],
  ['hot_reason', 'hot_reason'], ['hot_mode', 'hot_mode'], ['hot_checkpoint', 'hot_checkpoint'],
  ['selection_hot_strength', 'selection_hot_strength'],
];

function stage7MonitorEvidence_(videoId) {
  const id = String(videoId || '').trim();
  if (!id || typeof stage6ReadTable_ !== 'function') return null;
  try {
    const videos = stage6ReadTable_('Videos');
    const idIndex = videos.headers.indexOf('video_id');
    if (idIndex < 0) return null;
    const row = videos.rows.find(function (candidate) { return String(candidate[idIndex]) === id; });
    if (!row) return null;
    const evidence = {};
    STAGE7_MONITOR_EVIDENCE_FIELDS_.forEach(function (pair) {
      const index = videos.headers.indexOf(pair[1]);
      if (index < 0) return;
      let value = row[index];
      if (value instanceof Date) value = value.toISOString();
      if (value === '' || value === null || value === undefined) return;
      if (typeof value === 'number' && !Number.isFinite(value)) return;
      evidence[pair[0]] = typeof value === 'number' ? value : String(value);
    });
    return Object.keys(evidence).length ? evidence : null;
  } catch (error) {
    return null;
  }
}

function stage7ResolveClaimRequest_(ledgerEntry, table, requestId, nowMs) {
  const record = table.byQueueId.get(String(ledgerEntry.queue_id));
  if (!record || String(record.claim_request_id || '') !== String(requestId) ||
      (String(record.status) !== 'CLAIMED' && String(record.status) !== 'PROCESSING') ||
      !String(record.claim_token || '').trim()) {
    throw stage7QueueError_('CLAIM_REQUEST_EXPIRED', 'Claim request no longer owns an active task.');
  }
  const leaseUntil = stage7QueueEpochMillis_(record.lease_until);
  if (leaseUntil === null || leaseUntil <= nowMs) {
    throw stage7QueueError_('CLAIM_REQUEST_EXPIRED', 'Claim request lease has expired. Start a new poll.');
  }
  return stage7QueueClaimResponse_(record);
}

function stage7PersistQueueFields_(table, record, fieldNames) {
  fieldNames.forEach(function (field) {
    const column = table.headers.indexOf(field);
    if (column < 0) throw stage7QueueError_('CONFIG_INVALID', 'Queue Sheet is missing field ' + field + '.');
    table.sheet.getRange(record.sheet_row, column + 1).setValue(record[field] === null || record[field] === undefined ? '' : record[field]);
  });
}

function stage7PersistQueueRecord_(table, record) {
  const row = (record.sheet_values || []).slice();
  table.headers.forEach(function (header, index) {
    if (!header) return;
    row[index] = record[header] === null || record[header] === undefined ? '' : record[header];
  });
  table.sheet.getRange(record.sheet_row, 1, 1, table.headers.length).setValues([row]);
}

function stage7FindQueueRecord_(table, queueId) {
  const id = String(queueId || '');
  if (!id) throw stage7QueueError_('INVALID_REQUEST', 'queue_id is required.');
  const record = table.byQueueId.get(id);
  if (!record) throw stage7QueueError_('QUEUE_NOT_FOUND', 'Queue item was not found.');
  return record;
}

function stage7NewClaimToken_() {
  // Two UUID v4 values provide a 256-bit unguessable token using the GAS runtime RNG.
  return String(Utilities.getUuid()).replace(/-/g, '') + String(Utilities.getUuid()).replace(/-/g, '');
}

function stage7NewQueueId_(table) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const candidate = String(Utilities.getUuid());
    if (!table.byQueueId.has(candidate)) return candidate;
  }
  throw stage7QueueError_('INVALID_STATE', 'Could not generate a unique Queue ID.');
}

function stage7ValidateEnqueuePayload_(source) {
  const value = source || {};
  const videoId = String(value.video_id || '').trim();
  const url = String(value.url || '').trim();
  if (!videoId || videoId.length > 256 || !/^https?:\/\/[^\s]+$/i.test(url) || /[\u0000-\u001f\u007f]/.test(videoId)) {
    throw stage7QueueError_('INVALID_REQUEST', 'Queue enqueue requires a valid video_id and absolute HTTP(S) URL.');
  }
  const selectionDay = String(value.selection_day || '').trim();
  const rawRank = value.selection_rank;
  const rank = rawRank === null || rawRank === undefined || String(rawRank).trim() === '' ? null : Number(rawRank);
  const rank2Cutoff = String(value.rank2_start_cutoff_at || '').trim();
  const hasSelectionMetadata = Boolean(selectionDay || rank !== null || rank2Cutoff);
  if (hasSelectionMetadata && (!/^\d{4}-\d{2}-\d{2}$/.test(selectionDay) ||
      !Number.isInteger(rank) || rank < 1 || rank > 2 || (rank === 2 && stage7QueueEpochMillis_(rank2Cutoff) === null))) {
    throw stage7QueueError_('INVALID_REQUEST', 'Selection Queue metadata is invalid.');
  }
  return {
    video_id: videoId,
    url: url,
    selection_day: hasSelectionMetadata ? selectionDay : '',
    selection_rank: hasSelectionMetadata ? rank : '',
    rank2_start_cutoff_at: hasSelectionMetadata && rank === 2 ? rank2Cutoff : '',
  };
}

function stage7Enqueue_(source, scriptLockAlreadyHeld) {
  const payload = stage7ValidateEnqueuePayload_(source);
  const enqueue = function () {
    const videoId = payload.video_id;
    const url = payload.url;
    const table = stage7QueueTable_();
    const duplicate = table.byVideoId.get(videoId);
    if (duplicate) {
      if (payload.selection_day && String(duplicate.status) === 'PENDING' && !String(duplicate.selection_day || '').trim()) {
        const next = Object.assign({}, duplicate, {
          selection_day: payload.selection_day,
          selection_rank: payload.selection_rank,
          rank2_start_cutoff_at: payload.rank2_start_cutoff_at,
        });
        stage7PersistQueueFields_(table, next, ['selection_day', 'selection_rank', 'rank2_start_cutoff_at']);
        Object.assign(duplicate, next);
      }
      return { created: false, task: stage7QueuePublicRecord_(duplicate) };
    }
    const record = {
      queue_id: stage7NewQueueId_(table),
      video_id: videoId,
      url: url,
      status: 'PENDING',
      claim_token: '',
      claimed_at: '',
      lease_until: '',
      attempts: 0,
      last_error: '',
      local_job_id: '',
      completed_at: '',
      result_path: '',
      claim_request_id: '',
      selection_day: payload.selection_day,
      selection_rank: payload.selection_rank,
      rank2_start_cutoff_at: payload.rank2_start_cutoff_at,
      core_started_at: '',
    };
    const row = table.headers.map(function (header) {
      return Object.prototype.hasOwnProperty.call(record, header) ? record[header] : '';
    });
    table.sheet.appendRow(row);
    record.sheet_row = table.sheet.getLastRow();
    return { created: true, task: stage7QueuePublicRecord_(record) };
  };
  // Stage 9 invokes this only inside stage6RunMonitor_'s existing ScriptLock;
  // all other callers retain the original lock-protected public helper path.
  return scriptLockAlreadyHeld === true ? enqueue() : stage7WithScriptLock_(enqueue);
}

function stage7NowMillis_() {
  return new Date().getTime();
}

function stage7ValidateClaimRequestId_(requestId) {
  const value = typeof requestId === 'string' ? requestId.trim() : '';
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/.test(value)) {
    throw stage7QueueError_('INVALID_REQUEST', 'claim_request_id must be a unique identifier between 16 and 128 characters.');
  }
  return value;
}

function stage7ClaimNext_(claimRequestId, nowMs) {
  const requestId = stage7ValidateClaimRequestId_(claimRequestId);
  const at = nowMs === undefined ? stage7NowMillis_() : Number(nowMs);
  return stage7WithScriptLock_(function () {
    let table = stage7QueueTable_();
    if (stage7ExpirePendingRank2_(true, at, table) > 0) table = stage7QueueTable_();
    const ledger = stage7ClaimRequestTable_();
    const existingRequest = ledger.byRequestId.get(requestId);
    if (existingRequest) return stage7ResolveClaimRequest_(existingRequest, table, requestId, at);
    const partiallyPersisted = table.byClaimRequestId.get(requestId);
    if (partiallyPersisted) {
      const response = stage7ResolveClaimRequest_({ queue_id: partiallyPersisted.queue_id }, table, requestId, at);
      stage7AppendClaimRequest_(ledger, requestId, partiallyPersisted.queue_id, partiallyPersisted.claimed_at);
      return response;
    }
    const config = stage7ReadQueueConfig_();
    for (let index = 0; index < table.rows.length; index += 1) {
      const current = table.rows[index];
      if (stage7QueueRankTwoBlocked_(table, current)) continue;
      const claimed = stage7QueueClaimTransition_(current, at, config.leaseSeconds, stage7NewClaimToken_(), requestId);
      if (!claimed) continue;
      const priorRequestId = String(current.claim_request_id || '').trim();
      if (priorRequestId && !ledger.byRequestId.has(priorRequestId)) {
        stage7AppendClaimRequest_(ledger, priorRequestId, current.queue_id, current.claimed_at || '');
      }
      // One range write makes the owner/token/request ID visible together.
      stage7PersistQueueRecord_(table, claimed);
      stage7AppendClaimRequest_(ledger, requestId, claimed.queue_id, claimed.claimed_at);
      return stage7QueueClaimResponse_(claimed);
    }
    return null;
  });
}

function stage7QueueRankTwoBlocked_(table, record) {
  if (Number(record && record.selection_rank) !== 2) return false;
  const day = String(record.selection_day || '').trim();
  if (!day) return true;
  const rankOne = table.rows.filter(function (candidate) {
    return String(candidate.selection_day || '').trim() === day && Number(candidate.selection_rank) === 1;
  });
  if (rankOne.length !== 1) return true;
  const status = String(rankOne[0].status || '');
  return status !== 'COMPLETED' && status !== 'FAILED';
}

function stage7ExpirePendingRank2_(scriptLockAlreadyHeld, nowMs, suppliedTable) {
  const expire = function () {
    const table = suppliedTable || stage7QueueTable_();
    let count = 0;
    table.rows.forEach(function (record) {
      if (String(record.status) !== 'PENDING' || Number(record.selection_rank) !== 2) return;
      const cutoff = stage7QueueEpochMillis_(record.rank2_start_cutoff_at);
      if (cutoff === null || cutoff > nowMs || String(record.core_started_at || '').trim()) return;
      const next = Object.assign({}, record, { status: 'FAILED', last_error: stage7Rank2CutoffError_() });
      stage7PersistQueueFields_(table, next, ['status', 'last_error']);
      if (typeof stage9MarkVideoEliminatedByQueue_ === 'function') {
        stage9MarkVideoEliminatedByQueue_(record.video_id, new Date(nowMs).toISOString());
      }
      count += 1;
    });
    return count;
  };
  return scriptLockAlreadyHeld === true ? expire() : stage7WithScriptLock_(expire);
}

function stage7MarkCoreStarted_(request, nowMs) {
  const value = request || {};
  const at = nowMs === undefined ? stage7NowMillis_() : Number(nowMs);
  return stage7WithScriptLock_(function () {
    const table = stage7QueueTable_();
    const current = stage7FindQueueRecord_(table, value.queue_id);
    const next = stage7QueueCoreStartedTransition_(current, value, at);
    if (String(next.status) === 'FAILED') {
      stage7PersistQueueFields_(table, next, ['status', 'last_error']);
      if (typeof stage9MarkVideoEliminatedByQueue_ === 'function') {
        stage9MarkVideoEliminatedByQueue_(next.video_id, new Date(at).toISOString());
      }
      return { queue_id: next.queue_id, status: 'FAILED', last_error: next.last_error };
    }
    if (!String(current.core_started_at || '').trim()) stage7PersistQueueFields_(table, next, ['core_started_at']);
    return { queue_id: next.queue_id, status: next.status, core_started_at: next.core_started_at };
  });
}

function stage7Heartbeat_(request, nowMs) {
  const value = request || {};
  const at = nowMs === undefined ? stage7NowMillis_() : Number(nowMs);
  return stage7WithScriptLock_(function () {
    const config = stage7ReadQueueConfig_();
    const table = stage7QueueTable_();
    const current = stage7FindQueueRecord_(table, value.queue_id);
    const next = stage7QueueHeartbeatTransition_(current, value, at, config.leaseSeconds);
    stage7PersistQueueFields_(table, next, ['status', 'lease_until']);
    return { queue_id: next.queue_id, status: next.status, lease_until: next.lease_until };
  });
}

function stage7Complete_(request, nowMs) {
  const value = request || {};
  const at = nowMs === undefined ? stage7NowMillis_() : Number(nowMs);
  return stage7WithScriptLock_(function () {
    const table = stage7QueueTable_();
    const current = stage7FindQueueRecord_(table, value.queue_id);
    const next = stage7QueueCompleteTransition_(current, value, at);
    if (String(current.status) !== 'COMPLETED') {
      stage7PersistQueueFields_(table, next, ['status', 'local_job_id', 'result_path', 'completed_at']);
    }
    return { queue_id: next.queue_id, status: next.status, local_job_id: next.local_job_id, result_path: next.result_path, completed_at: next.completed_at };
  });
}

function stage7Fail_(request, nowMs) {
  const value = request || {};
  const at = nowMs === undefined ? stage7NowMillis_() : Number(nowMs);
  return stage7WithScriptLock_(function () {
    const table = stage7QueueTable_();
    const current = stage7FindQueueRecord_(table, value.queue_id);
    const next = stage7QueueFailTransition_(current, value, at);
    if (String(current.status) !== 'PAUSED' && String(current.status) !== 'FAILED') {
      stage7PersistQueueFields_(table, next, ['status', 'last_error', 'local_job_id']);
    }
    return {
      queue_id: next.queue_id,
      status: next.status,
      local_job_id: next.local_job_id,
      last_error: next.last_error,
      attempts: Number(next.attempts || 0),
    };
  });
}

function stage7QueuePublicRecord_(record) {
  return {
    queue_id: record.queue_id,
    video_id: record.video_id,
    url: record.url,
    status: record.status,
    claim_token: record.claim_token || '',
    claimed_at: record.claimed_at || '',
    lease_until: record.lease_until || '',
    attempts: Number(record.attempts || 0),
    last_error: record.last_error || '',
    local_job_id: record.local_job_id || '',
    completed_at: record.completed_at || '',
    result_path: record.result_path || '',
    claim_request_id: record.claim_request_id || '',
    selection_day: record.selection_day || '',
    selection_rank: record.selection_rank === '' || record.selection_rank === null || record.selection_rank === undefined ? null : Number(record.selection_rank),
    rank2_start_cutoff_at: record.rank2_start_cutoff_at || '',
    core_started_at: record.core_started_at || '',
  };
}
