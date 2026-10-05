// Stage 7 Queue contract. Keep transition rules pure; queue.gs owns Sheets I/O.
const STAGE7_QUEUE_HEADERS_ = [
  'queue_id', 'video_id', 'url', 'status', 'claim_token', 'claimed_at',
  'lease_until', 'attempts', 'last_error', 'local_job_id', 'completed_at', 'result_path', 'claim_request_id',
  'selection_day', 'selection_rank', 'rank2_start_cutoff_at', 'core_started_at',
];
const STAGE7_CLAIM_REQUEST_HEADERS_ = ['claim_request_id', 'queue_id', 'created_at'];
const STAGE7_QUEUE_STATUSES_ = ['PENDING', 'CLAIMED', 'PROCESSING', 'PAUSED', 'FAILED', 'COMPLETED'];
const STAGE7_QUEUE_LOCK_WAIT_MS_ = 30000;

// Derived from src/core/errors.py: ERROR_SPECS. Stage 1 remains the source of
// truth; tests compare this Cloud mirror against that contract.
const STAGE7_CORE_ERROR_RECOVERABILITY_ = {
  UNKNOWN_CONTENT: 'FAILED',
  URL_INVALID: 'FAILED',
  AUTH_REQUIRED: 'PAUSED',
  SUBTITLE_FAILED: 'PAUSED',
  ASR_FAILED: 'PAUSED',
  TRANSLATION_UNSUPPORTED: 'FAILED',
  NETWORK_PAUSED: 'PAUSED',
  PROVIDER_FAILED: 'FAILED',
  PARTIAL_FAILURE: 'PAUSED',
  CLEANUP_FAILED: 'PAUSED',
  API_FAILED: 'PAUSED',
};

function stage7QueueError_(code, message) {
  const error = new Error(message || 'Queue operation failed.');
  error.queueErrorCode = code;
  return error;
}

function stage7FailureStatusForCoreError_(coreError) {
  const value = coreError && typeof coreError === 'object' ? coreError : {};
  const code = String(value.code || '');
  const status = Object.prototype.hasOwnProperty.call(STAGE7_CORE_ERROR_RECOVERABILITY_, code)
    ? STAGE7_CORE_ERROR_RECOVERABILITY_[code] : null;
  if (!status) throw stage7QueueError_('INVALID_STATE', 'Failure does not match the Stage 1 Core Error Contract.');
  if (Object.prototype.hasOwnProperty.call(value, 'recoverable') && Boolean(value.recoverable) !== (status === 'PAUSED')) {
    throw stage7QueueError_('INVALID_STATE', 'Core Error recoverability does not match its stable error code.');
  }
  if (Object.prototype.hasOwnProperty.call(value, 'terminal') && Boolean(value.terminal) !== (status === 'FAILED')) {
    throw stage7QueueError_('INVALID_STATE', 'Core Error terminal flag does not match its stable error code.');
  }
  return status;
}

function stage7QueueEpochMillis_(value) {
  if (value instanceof Date) {
    const dateMs = value.getTime();
    return Number.isFinite(dateMs) ? dateMs : null;
  }
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (value === null || value === undefined || String(value).trim() === '') return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function stage7QueueLeaseUntil_(nowMs, leaseSeconds) {
  if (!Number.isFinite(nowMs) || !Number.isInteger(leaseSeconds) || leaseSeconds < 1) {
    throw stage7QueueError_('CONFIG_INVALID', 'Queue lease configuration is invalid.');
  }
  const deadline = nowMs + leaseSeconds * 1000;
  if (!Number.isFinite(deadline)) throw stage7QueueError_('CONFIG_INVALID', 'Queue lease configuration is invalid.');
  return new Date(deadline).toISOString();
}

function stage7QueueIsExpired_(record, nowMs) {
  const untilMs = stage7QueueEpochMillis_(record && record.lease_until);
  if (untilMs === null) throw stage7QueueError_('INVALID_STATE', 'Queue lease timestamp is invalid.');
  return untilMs <= nowMs;
}

function stage7QueueRequireIdentity_(record, queueId, claimToken) {
  if (!record) throw stage7QueueError_('QUEUE_NOT_FOUND', 'Queue item was not found.');
  if (!queueId || String(record.queue_id) !== String(queueId)) {
    throw stage7QueueError_('QUEUE_NOT_FOUND', 'Queue item was not found.');
  }
  if (!claimToken || String(record.claim_token || '') !== String(claimToken)) {
    throw stage7QueueError_('CLAIM_TOKEN_INVALID', 'Claim token is invalid.');
  }
}

function stage7QueueClaimTransition_(record, nowMs, leaseSeconds, claimToken, claimRequestId) {
  if (!record || !record.queue_id || !record.video_id) {
    throw stage7QueueError_('INVALID_STATE', 'Queue row is missing its stable identity.');
  }
  const status = String(record.status || '');
  if (STAGE7_QUEUE_STATUSES_.indexOf(status) < 0) {
    throw stage7QueueError_('INVALID_STATE', 'Queue row has an unknown status.');
  }
  let eligible = status === 'PENDING';
  if (status === 'CLAIMED' || status === 'PROCESSING') eligible = stage7QueueIsExpired_(record, nowMs);
  if (!eligible) return null;
  const currentAttempts = record.attempts === '' || record.attempts === null || record.attempts === undefined
    ? 0 : Number(record.attempts);
  if (!Number.isInteger(currentAttempts) || currentAttempts < 0) {
    throw stage7QueueError_('INVALID_STATE', 'Queue attempts value is invalid.');
  }
  if (!claimToken || String(claimToken).length < 32 || !claimRequestId) {
    throw stage7QueueError_('INVALID_STATE', 'A fresh random claim token is required.');
  }
  return Object.assign({}, record, {
    status: 'CLAIMED',
    claim_token: String(claimToken),
    claim_request_id: String(claimRequestId),
    claimed_at: new Date(nowMs).toISOString(),
    lease_until: stage7QueueLeaseUntil_(nowMs, leaseSeconds),
    attempts: currentAttempts + 1,
  });
}

function stage7QueueHeartbeatTransition_(record, request, nowMs, leaseSeconds) {
  stage7QueueRequireIdentity_(record, request && request.queue_id, request && request.claim_token);
  const status = String(record.status || '');
  if (status !== 'CLAIMED' && status !== 'PROCESSING') {
    throw stage7QueueError_('INVALID_STATE', 'Queue item state is not valid for heartbeat.');
  }
  if (stage7QueueIsExpired_(record, nowMs)) throw stage7QueueError_('LEASE_EXPIRED', 'Queue lease has expired.');
  return Object.assign({}, record, {
    status: 'PROCESSING',
    lease_until: stage7QueueLeaseUntil_(nowMs, leaseSeconds),
  });
}

function stage7QueueCompleteTransition_(record, request, nowMs) {
  const value = request || {};
  stage7QueueRequireIdentity_(record, value.queue_id, value.claim_token);
  if (!value.local_job_id || !value.result_path) {
    throw stage7QueueError_('INVALID_REQUEST', 'Complete requires local_job_id and result_path.');
  }
  if (String(record.status) === 'COMPLETED') {
    if (String(record.local_job_id) === String(value.local_job_id) &&
        String(record.result_path) === String(value.result_path)) return Object.assign({}, record);
    throw stage7QueueError_('INVALID_STATE', 'A completed Queue item is immutable.');
  }
  if (String(record.status) !== 'PROCESSING') {
    throw stage7QueueError_('INVALID_STATE', 'Queue item state must be PROCESSING before completion.');
  }
  if (stage7QueueIsExpired_(record, nowMs)) throw stage7QueueError_('LEASE_EXPIRED', 'Queue lease has expired.');
  return Object.assign({}, record, {
    status: 'COMPLETED',
    local_job_id: String(value.local_job_id),
    result_path: String(value.result_path),
    completed_at: new Date(nowMs).toISOString(),
  });
}

function stage7QueueErrorRecord_(coreError) {
  const value = coreError && typeof coreError === 'object' ? coreError : {};
  stage7FailureStatusForCoreError_(value);
  const code = String(value.code || '');
  const message = String(value.message || '').replace(/[\u0000-\u001f\u007f]/g, ' ').trim();
  if (!message) throw stage7QueueError_('INVALID_REQUEST', 'Failure requires a Core error message.');
  return JSON.stringify({ code: code, message: message.slice(0, 2000) });
}

function stage7QueueFailTransition_(record, request, nowMs) {
  const value = request || {};
  stage7QueueRequireIdentity_(record, value.queue_id, value.claim_token);
  if (!value.local_job_id) throw stage7QueueError_('INVALID_REQUEST', 'Failure requires local_job_id.');
  const targetStatus = stage7FailureStatusForCoreError_(value.error);
  const errorRecord = stage7QueueErrorRecord_(value.error);
  const status = String(record.status || '');
  if (status === 'PAUSED' || status === 'FAILED') {
    if (status === targetStatus && String(record.last_error || '') === errorRecord &&
        String(record.local_job_id || '') === String(value.local_job_id)) return Object.assign({}, record);
    throw stage7QueueError_('INVALID_STATE', 'Queue failure result is immutable.');
  }
  if (status !== 'PROCESSING') throw stage7QueueError_('INVALID_STATE', 'Queue item must be PROCESSING before failure.');
  if (stage7QueueIsExpired_(record, nowMs)) throw stage7QueueError_('LEASE_EXPIRED', 'Queue lease has expired.');
  return Object.assign({}, record, {
    status: targetStatus,
    last_error: errorRecord,
    local_job_id: String(value.local_job_id),
  });
}

function stage7QueueCoreStartedTransition_(record, request, nowMs) {
  const value = request || {};
  stage7QueueRequireIdentity_(record, value.queue_id, value.claim_token);
  if (String(record.status || '') !== 'PROCESSING') {
    throw stage7QueueError_('INVALID_STATE', 'Queue item must be PROCESSING before Core start authorization.');
  }
  if (stage7QueueIsExpired_(record, nowMs)) throw stage7QueueError_('LEASE_EXPIRED', 'Queue lease has expired.');
  const rank = record.selection_rank === '' || record.selection_rank === null || record.selection_rank === undefined
    ? null : Number(record.selection_rank);
  const cutoff = stage7QueueEpochMillis_(record.rank2_start_cutoff_at);
  if (rank === 2 && cutoff !== null && cutoff <= nowMs && !String(record.core_started_at || '').trim()) {
    return Object.assign({}, record, {
      status: 'FAILED',
      last_error: stage7Rank2CutoffError_(),
    });
  }
  if (rank === 2 && cutoff === null) {
    throw stage7QueueError_('CONFIG_INVALID', 'Rank 2 Queue item is missing rank2_start_cutoff_at.');
  }
  if (String(record.core_started_at || '').trim()) return Object.assign({}, record);
  return Object.assign({}, record, { core_started_at: new Date(nowMs).toISOString() });
}

function stage7Rank2CutoffError_() {
  return JSON.stringify({ code: 'RANK2_START_CUTOFF', message: 'Rank 2 did not start before the configured cutoff.' });
}
