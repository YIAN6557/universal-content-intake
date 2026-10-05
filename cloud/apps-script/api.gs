// Thin authenticated Apps Script Web App boundary for Queue operations.
// Request envelope: {timestamp: epoch-seconds, payload: exact JSON string,
// signature: base64(HMAC-SHA256(secret, timestamp + "\n" + payload))}.
const STAGE7_QUEUE_HMAC_PROPERTY_ = 'UCI_QUEUE_HMAC_SECRET';
const STAGE7_QUEUE_API_ERROR_MESSAGES_ = {
  AUTH_FAILED: 'Request authentication failed.',
  INVALID_REQUEST: 'Request payload is invalid.',
  QUEUE_NOT_FOUND: 'Queue item was not found.',
  QUEUE_EMPTY: 'No Queue item is currently available.',
  INVALID_STATE: 'Queue item is in an invalid state for this operation.',
  CLAIM_TOKEN_INVALID: 'Claim token is invalid.',
  CLAIM_REQUEST_EXPIRED: 'Claim request no longer owns an active task. Start a new poll.',
  LEASE_EXPIRED: 'Queue lease has expired.',
  CONFIG_INVALID: 'Queue configuration is invalid.',
};

function doPost(event) {
  let response;
  let signatureVerified = false;
  try {
    const rawBody = event && event.postData ? event.postData.contents : '';
    const request = stage7AuthenticateQueueApiRequest_(rawBody, stage7NowMillis_(), function () { signatureVerified = true; });
    const data = stage7DispatchQueueApi_(request);
    response = { ok: true, data: data === undefined ? null : data };
  } catch (error) {
    const requestedCode = error && error.queueErrorCode;
    const code = Object.prototype.hasOwnProperty.call(STAGE7_QUEUE_API_ERROR_MESSAGES_, requestedCode)
      ? requestedCode : 'INVALID_STATE';
    response = { ok: false, error: { code: code, message: STAGE7_QUEUE_API_ERROR_MESSAGES_[code] } };
    // An unexpected platform exception (for example a lost Sheets permission)
    // is otherwise indistinguishable from a real INVALID_STATE. Signed callers
    // get its message; unsigned callers never see internal details.
    // Signed setup calls also get the validation reason for rejected values.
    if (signatureVerified && (!requestedCode || code === 'INVALID_REQUEST' || code === 'CONFIG_INVALID')) {
      response.error.detail = stage7ErrorDetail_(error);
    }
    Logger.log('Stage 7 Queue API rejected request; code=' + code + (response.error.detail ? '; detail=' + response.error.detail : '') + '.');
  }
  return ContentService.createTextOutput(JSON.stringify(response)).setMimeType(ContentService.MimeType.JSON);
}

function stage7ErrorDetail_(error) {
  const text = String((error && error.message) || error || '').replace(/\s+/g, ' ').trim();
  return text.slice(0, 300) || 'unknown error';
}

function stage7AuthenticateQueueApiRequest_(rawBody, nowMillis, onSignatureVerified) {
  let envelope;
  try {
    envelope = JSON.parse(String(rawBody || ''));
  } catch (error) {
    throw stage7QueueError_('AUTH_FAILED', 'Request authentication failed.');
  }
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
    throw stage7QueueError_('AUTH_FAILED', 'Request authentication failed.');
  }
  const timestamp = String(envelope.timestamp || '');
  const payloadText = typeof envelope.payload === 'string' ? envelope.payload : '';
  const suppliedSignature = typeof envelope.signature === 'string' ? envelope.signature : '';
  if (!/^\d+$/.test(timestamp) || !payloadText || !suppliedSignature) {
    throw stage7QueueError_('AUTH_FAILED', 'Request authentication failed.');
  }

  const secret = PropertiesService.getScriptProperties().getProperty(STAGE7_QUEUE_HMAC_PROPERTY_);
  if (!secret) throw stage7QueueError_('AUTH_FAILED', 'Request authentication failed.');
  const timestampSeconds = Number(timestamp);
  if (!Number.isSafeInteger(timestampSeconds) || !Number.isFinite(nowMillis)) {
    throw stage7QueueError_('AUTH_FAILED', 'Request authentication failed.');
  }
  // Verify the signature before touching the Sheet so that a Sheet failure
  // can be reported to an authenticated caller (see doPost).
  const expectedBytes = Utilities.computeHmacSha256Signature(timestamp + '\n' + payloadText, String(secret));
  const expectedSignature = Utilities.base64Encode(expectedBytes);
  if (!stage7ConstantTimeStringEquals_(expectedSignature, suppliedSignature)) {
    throw stage7QueueError_('AUTH_FAILED', 'Request authentication failed.');
  }
  if (typeof onSignatureVerified === 'function') onSignatureVerified();
  const config = stage7ReadQueueConfig_();
  const nowSeconds = Math.floor(nowMillis / 1000);
  if (Math.abs(nowSeconds - timestampSeconds) > config.timestampToleranceSeconds) {
    throw stage7QueueError_('AUTH_FAILED', 'Request authentication failed.');
  }

  let request;
  try {
    request = JSON.parse(payloadText);
  } catch (error) {
    throw stage7QueueError_('INVALID_REQUEST', 'Request payload is invalid.');
  }
  if (!request || typeof request !== 'object' || Array.isArray(request) || typeof request.action !== 'string') {
    throw stage7QueueError_('INVALID_REQUEST', 'Request payload is invalid.');
  }
  return request;
}

function stage7ConstantTimeStringEquals_(left, right) {
  const a = String(left || '');
  const b = String(right || '');
  let difference = a.length ^ b.length;
  const limit = Math.max(a.length, b.length);
  for (let index = 0; index < limit; index += 1) {
    difference |= (index < a.length ? a.charCodeAt(index) : 0) ^ (index < b.length ? b.charCodeAt(index) : 0);
  }
  return difference === 0;
}

function stage7DispatchQueueApi_(request) {
  const action = String(request.action || '');
  switch (action) {
    case 'claim':
      return stage7ClaimNext_(request.claim_request_id, stage7NowMillis_());
    case 'heartbeat':
      return stage7Heartbeat_(request, stage7NowMillis_());
    case 'core_started':
      return stage7MarkCoreStarted_(request, stage7NowMillis_());
    // The Canonical API exposes claim / heartbeat / complete / fail. The first
    // valid heartbeat performs CLAIMED → PROCESSING; no separate start route.
    case 'complete':
      return stage7Complete_(request, stage7NowMillis_());
    case 'fail':
      return stage7Fail_(request, stage7NowMillis_());
    case 'status':
      return stage7StatusReport_(request, stage7NowMillis_());
    // First-run setup (setup.gs), driven by bin/uci-setup on the owner's Mac.
    case 'setup_inspect':
      return uciSetupInspect_();
    case 'setup_config_set':
      return uciSetupConfigSet_(request);
    case 'setup_creators_upsert':
      return uciSetupCreatorsUpsert_(request);
    default:
      throw stage7QueueError_('INVALID_REQUEST', 'Unsupported Queue API action.');
  }
}

// Read-only health and batch report for local monitoring, so checking the
// system never depends on a browser Google session. Each read is isolated:
// a failing step is reported in `checks` with its message instead of failing
// the whole request.
const STAGE7_STATUS_CONFIG_KEYS_ = [
  'production_timezone', 'monitor_status', 'daily_selection_enabled', 'last_discovery_slot', 'last_discovery_at',
  'last_final_sweep_day', 'last_daily_selection_day', 'last_api_error_class', 'last_api_error_at',
  'discovery_window_start', 'discovery_window_end', 'final_sweep_time', 'daily_selection_time', 'rank2_start_cutoff',
  'cold_start_checkpoint_30_ratio', 'cold_start_checkpoint_60_ratio', 'cold_start_checkpoint_120_ratio',
  'semantic_judge_enabled',
];
const STAGE7_STATUS_VIDEO_FIELDS_ = [
  'video_id', 'creator_name', 'title', 'published_at', 'discovered_at', 'lifecycle_state', 'broadcast_type',
  'hot_reason', 'hot_mode', 'hot_checkpoint', 'data_hot_at', 'selection_result', 'selection_rank',
  'selection_reason', 'queue_id',
];
const STAGE7_STATUS_SNAPSHOT_FIELDS_ = [
  'snapshot_stage_minutes', 'captured_at', 'view_count', 'like_rate', 'baseline_final_views_median',
  'historical_median_like_rate',
];

function stage7StatusValue_(value) {
  if (value instanceof Date) return isNaN(value.getTime()) ? '' : value.toISOString();
  return value === null || value === undefined ? '' : value;
}

function stage7StatusReport_(request, nowMs) {
  const checks = [];
  const step = function (name, callback) {
    try {
      const value = callback();
      checks.push({ name: name, ok: true });
      return value;
    } catch (error) {
      checks.push({ name: name, ok: false, error: stage7ErrorDetail_(error) });
      return null;
    }
  };
  const report = { server_time: new Date(nowMs).toISOString(), checks: checks };
  const config = step('config', function () { return stage6ReadConfig_(); });
  const requestedDay = String((request && request.day) || '');
  if (requestedDay && !/^\d{4}-\d{2}-\d{2}$/.test(requestedDay)) {
    throw stage7QueueError_('INVALID_REQUEST', 'Status day must be YYYY-MM-DD.');
  }
  if (config) {
    report.config = {};
    STAGE7_STATUS_CONFIG_KEYS_.forEach(function (key) { report.config[key] = stage7StatusValue_(config[key]); });
    // Sheets turns day/slot markers into Date cells; report them the way the
    // Scheduler compares them (production-timezone day strings).
    [['last_discovery_slot', true], ['last_final_sweep_day', false], ['last_daily_selection_day', false]].forEach(function (marker) {
      try { report.config[marker[0]] = stage6NormalizeDateMarker_(config[marker[0]], marker[1]); } catch (error) { /* keep raw value */ }
    });
    report.day = requestedDay || step('day', function () {
      return stage6ZonedDateParts_(new Date(nowMs), config.production_timezone).day;
    }) || '';
  } else {
    report.day = requestedDay;
  }

  const videos = step('videos', function () { return stage6ReadTable_('Videos'); });
  const snapshots = step('snapshots', function () { return stage6ReadTable_('Snapshots'); });
  if (videos && config && report.day) {
    const col = function (table, name) { return table.headers.indexOf(name); };
    const snapshotsById = {};
    if (snapshots) {
      snapshots.rows.forEach(function (row) {
        const id = String(row[col(snapshots, 'video_id')]);
        (snapshotsById[id] = snapshotsById[id] || []).push(STAGE7_STATUS_SNAPSHOT_FIELDS_.reduce(function (out, field) {
          const index = col(snapshots, field);
          out[field] = index >= 0 ? stage7StatusValue_(row[index]) : '';
          return out;
        }, {}));
      });
    }
    report.videos = videos.rows.filter(function (row) {
      const published = row[col(videos, 'published_at')];
      const selectionDay = String(stage7StatusValue_(row[col(videos, 'selection_day')]) || '');
      return (published && stage6IsPublishedInDailyBatch_(published, report.day, config)) || selectionDay.indexOf(report.day) === 0;
    }).map(function (row) {
      const out = {};
      STAGE7_STATUS_VIDEO_FIELDS_.forEach(function (field) {
        const index = col(videos, field);
        out[field] = index >= 0 ? stage7StatusValue_(row[index]) : '';
      });
      out.snapshots = snapshotsById[String(out.video_id)] || [];
      return out;
    });
  }

  const queue = step('queue', function () { return stage7QueueTable_(); });
  if (queue) {
    const active = ['PENDING', 'CLAIMED', 'PROCESSING', 'PAUSED'];
    report.queue = queue.rows.filter(function (record) {
      return active.indexOf(String(record.status)) >= 0 || String(stage7StatusValue_(record.selection_day) || '').indexOf(report.day) === 0;
    }).map(function (record) {
      let errorCode = '';
      try { errorCode = record.last_error ? String(JSON.parse(String(record.last_error)).code || '') : ''; } catch (error) { errorCode = 'unparsed'; }
      return {
        queue_id: String(record.queue_id), video_id: String(record.video_id), status: String(record.status),
        attempts: Number(record.attempts || 0), selection_rank: stage7StatusValue_(record.selection_rank),
        local_job_id: String(record.local_job_id || ''), completed_at: stage7StatusValue_(record.completed_at),
        last_error_code: errorCode,
      };
    });
    report.queue_total = queue.rows.length;
  }
  report.healthy = checks.every(function (check) { return check.ok; });
  return report;
}
