// Stage 9 deterministic Candidate selection. This file only decides which
// already-CANDIDATE video to pass to Stage 7's explicit enqueue contract.
const STAGE9_SELECTION_CONFIG_ROWS_ = [
  ['daily_selection_enabled', true, 'Runs the current Asia/Shanghai Daily Batch Selection.'],
  ['daily_selection_max', 2, 'Maximum number of new selections per calendar day; V1 permits values from zero through two.'],
];
const STAGE9_SELECTION_VIDEO_HEADERS_ = [
  'selected_at', 'selection_day', 'selection_rank', 'selection_hot_strength', 'queue_id', 'queued_at',
  'selection_result', 'selection_closed_at', 'selection_reason',
];

function stage9ReadSelectionConfig_(rawConfig) {
  const config = rawConfig || stage6ReadConfig_();
  const enabledValue = config.daily_selection_enabled;
  let enabled;
  if (enabledValue === true || String(enabledValue).trim().toLowerCase() === 'true') enabled = true;
  else if (enabledValue === false || String(enabledValue).trim().toLowerCase() === 'false') enabled = false;
  else throw new Error('Stage 9 selection Config invalid: daily_selection_enabled must be true or false.');
  if (!enabled) return { enabled: false, dailyMax: null, timezone: null };

  const dailyMax = stage9SelectionInteger_(config, 'daily_selection_max', 0, 2);
  const timezone = String(config.production_timezone || '').trim();
  if (!timezone) throw new Error('Stage 9 selection Config invalid: production_timezone is required.');
  return { enabled: enabled, dailyMax: dailyMax, timezone: timezone };
}

function stage9SelectionInteger_(config, key, minimum, maximum) {
  const raw = config[key];
  if (raw === null || raw === undefined || typeof raw === 'boolean' || String(raw).trim() === '' ||
      (typeof raw !== 'number' && !/^\d+$/.test(String(raw).trim()))) {
    throw new Error('Stage 9 selection Config invalid: ' + key + ' must be an integer.');
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error('Stage 9 selection Config invalid: ' + key + ' is out of range.');
  }
  return value;
}

function stage9ResolveSelectionTimeZone_(rawConfig) {
  const config = rawConfig || stage6ReadConfig_();
  const timezone = String(config.production_timezone || '').trim();
  if (!timezone) throw new Error('Stage 9 selection Config invalid: production_timezone is required.');
  return timezone;
}

function stage9ClockMinutes_(value, key) {
  try {
    return stage6ClockMinutes_(value, key);
  } catch (error) {
    throw new Error('Stage 9 selection Config invalid: ' + key + ' must be a valid HH:mm or Sheets time value.');
  }
}

function stage9IsDailyBatchVideo_(publishedAt, selectionDay, config) {
  return typeof stage6IsPublishedInDailyBatch_ === 'function' &&
    stage6IsPublishedInDailyBatch_(publishedAt, selectionDay, config);
}

function stage9LocalCutoffIso_(selectionDay, clock, timezone) {
  const match = String(selectionDay || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) throw new Error('Stage 9 selection day is invalid.');
  const minutes = stage9ClockMinutes_(clock, 'rank2_start_cutoff');
  const hour = Math.floor(minutes / 60);
  const minute = minutes % 60;
  let guess = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), hour, minute, 0);
  for (let index = 0; index < 3; index += 1) {
    const local = stage6ZonedDateParts_(new Date(guess), timezone);
    const localAsUtc = Date.UTC(Number(local.day.slice(0, 4)), Number(local.day.slice(5, 7)) - 1,
      Number(local.day.slice(8, 10)), local.hour, local.minute, 0);
    const desiredAsUtc = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), hour, minute, 0);
    guess -= localAsUtc - desiredAsUtc;
  }
  return new Date(guess).toISOString();
}

function stage9SelectionDay_(value, timeZone) {
  const millis = stage9DateMillis_(value);
  if (millis === null) return null;
  return Utilities.formatDate(new Date(millis), timeZone, 'yyyy-MM-dd');
}

function stage9DateMillis_(value) {
  if (value instanceof Date) {
    const millis = value.getTime();
    return Number.isFinite(millis) ? millis : null;
  }
  if (value === null || value === undefined || String(value).trim() === '') return null;
  const millis = new Date(value).getTime();
  return Number.isFinite(millis) ? millis : null;
}

function stage9RequireHeaders_(table, required, tableName) {
  const missing = required.filter(function (header) { return table.headers.indexOf(header) < 0; });
  if (missing.length) throw new Error('Stage 9 ' + tableName + ' schema is incomplete; missing ' + missing.join(', ') + '.');
}

function stage9RowObject_(headers, row) {
  const value = {};
  (headers || []).forEach(function (header, index) {
    value[String(header)] = row && row[index] !== undefined ? row[index] : '';
  });
  return value;
}

function stage9CandidateHotEvidence_(video, snapshots, snapshotHeaders, hotConfig) {
  const checkpoint = stage6HotFiniteNumber_(video.hot_checkpoint);
  if (checkpoint === null || hotConfig.snapshotCheckpoints.indexOf(checkpoint) < 0) {
    return { eligible: false, reason: 'INVALID_HOT_CHECKPOINT' };
  }
  const mode = String(video.hot_mode || '').trim().toLowerCase();
  if (mode !== 'cold' && mode !== 'warm') return { eligible: false, reason: 'INVALID_HOT_MODE' };

  const videoIndex = snapshotHeaders.indexOf('video_id');
  const checkpointIndex = snapshotHeaders.indexOf('checkpoint_minutes');
  const matches = snapshots.filter(function (row) {
    return String(row[videoIndex]) === String(video.video_id) &&
      stage6HotFiniteNumber_(row[checkpointIndex]) === checkpoint;
  });
  if (matches.length !== 1) return { eligible: false, reason: matches.length ? 'DUPLICATE_HOT_SNAPSHOT' : 'HOT_SNAPSHOT_NOT_FOUND' };

  const row = matches[0];
  if (mode === 'cold') {
    const views = stage6HotFiniteNumber_(row[snapshotHeaders.indexOf('view_count')]);
    const baseline = stage6HotFiniteNumber_(row[snapshotHeaders.indexOf('baseline_final_views_median')]);
    const ratio = hotConfig.checkpointRatios[checkpoint];
    const requiredViews = baseline === null ? null : baseline * ratio;
    if (views === null || views < 0 || baseline === null || baseline < 0 || !Number.isFinite(requiredViews) || requiredViews <= 0) {
      return { eligible: false, reason: 'INVALID_COLD_HOT_EVIDENCE' };
    }
    const hotStrength = views / requiredViews;
    return Number.isFinite(hotStrength) && hotStrength >= 0
      ? { eligible: true, mode: mode, checkpoint: checkpoint, hotStrength: hotStrength }
      : { eligible: false, reason: 'INVALID_COLD_HOT_EVIDENCE' };
  }

  const relativeVelocity = stage6HotFiniteNumber_(row[snapshotHeaders.indexOf('relative_velocity')]);
  const threshold = hotConfig.warmRelativeVelocityThreshold;
  if (relativeVelocity === null || relativeVelocity < 0 || !Number.isFinite(threshold) || threshold <= 0) {
    return { eligible: false, reason: 'INVALID_WARM_HOT_EVIDENCE' };
  }
  const hotStrength = relativeVelocity / threshold;
  return Number.isFinite(hotStrength) && hotStrength >= 0
    ? { eligible: true, mode: mode, checkpoint: checkpoint, hotStrength: hotStrength }
    : { eligible: false, reason: 'INVALID_WARM_HOT_EVIDENCE' };
}

function stage9CompareCandidates_(left, right) {
  if (left.hotStrength !== right.hotStrength) return right.hotStrength - left.hotStrength;
  if (left.checkpoint !== right.checkpoint) return left.checkpoint - right.checkpoint;
  if (left.candidateAtMillis !== right.candidateAtMillis) return left.candidateAtMillis - right.candidateAtMillis;
  if (left.videoId < right.videoId) return -1;
  if (left.videoId > right.videoId) return 1;
  return 0;
}

function stage9SelectedTodayCount_(videos, selectedAtIndex, selectionDay, timeZone) {
  return videos.rows.reduce(function (count, row) {
    return count + (stage9SelectionDay_(row[selectedAtIndex], timeZone) === selectionDay ? 1 : 0);
  }, 0);
}

function stage9WriteSelectionMetadata_(videos, video, values) {
  const sheetRow = video.sheetRow;
  Object.keys(values).forEach(function (header) {
    const index = videos.headers.indexOf(header);
    if (index < 0) throw new Error('Stage 9 Videos schema is missing Selection field ' + header + '.');
    videos.sheet.getRange(sheetRow, index + 1).setValue(values[header] === null || values[header] === undefined ? '' : values[header]);
  });
}

function stage9QueueTaskForVideo_(queueTable, videoId) {
  return queueTable.byVideoId.get(String(videoId)) || null;
}

function stage9MarkVideoEliminatedByQueue_(videoId, closedAt) {
  const videos = stage6ReadTable_('Videos');
  const idIndex = videos.headers.indexOf('video_id');
  const resultIndex = videos.headers.indexOf('selection_result');
  const closedIndex = videos.headers.indexOf('selection_closed_at');
  const reasonIndex = videos.headers.indexOf('selection_reason');
  if ([idIndex, resultIndex, closedIndex].some(function (index) { return index < 0; })) return false;
  let found = false;
  videos.rows.forEach(function (row, offset) {
    if (String(row[idIndex]) !== String(videoId)) return;
    found = true;
    const sheetRow = offset + 2;
    videos.sheet.getRange(sheetRow, resultIndex + 1).setValue('ELIMINATED');
    videos.sheet.getRange(sheetRow, closedIndex + 1).setValue(closedAt);
    if (reasonIndex >= 0) videos.sheet.getRange(sheetRow, reasonIndex + 1).setValue('RANK2_START_CUTOFF');
  });
  return found;
}

function stage9RunDailySelection_(scriptLockAlreadyHeld, nowOverride) {
  const rawConfig = stage6ReadConfig_();
  const config = stage9ReadSelectionConfig_(rawConfig);
  if (!config.enabled) return { enabled: false, selected: 0, enqueued: 0, recovered: 0, selection_day: null };

  const timeZone = stage9ResolveSelectionTimeZone_(rawConfig);
  const now = nowOverride && typeof nowOverride.getTime === 'function'
    ? new Date(nowOverride.getTime()) : new Date();
  const nowMillis = now.getTime();
  const nowIso = now.toISOString();
  const parts = stage6ZonedDateParts_(now, timeZone);
  const selectionDay = parts.day;
  const currentMinute = parts.hour * 60 + parts.minute;
  if (currentMinute < stage9ClockMinutes_(rawConfig.daily_selection_time, 'daily_selection_time')) {
    return { enabled: true, skipped: 'BEFORE_SELECTION_TIME', selected: 0, enqueued: 0, selection_day: selectionDay };
  }
  const videos = stage6ReadTable_('Videos');
  const snapshots = stage6ReadTable_('Snapshots');
  const queue = stage7QueueTable_();
  stage9RequireHeaders_(videos, [
    'video_id', 'video_url', 'published_at', 'lifecycle_state', 'candidate_at', 'hot_mode', 'hot_checkpoint',
  ].concat(STAGE9_SELECTION_VIDEO_HEADERS_), 'Videos');
  stage9RequireHeaders_(snapshots, [
    'video_id', 'checkpoint_minutes', 'view_count', 'baseline_final_views_median', 'relative_velocity',
  ], 'Snapshots');

  const hotConfig = stage6ReadHotConfig_(rawConfig);
  const semanticEnabled = typeof stage8SemanticIsEnabled_ === 'function'
    ? stage8SemanticIsEnabled_(rawConfig.semantic_judge_enabled)
    : rawConfig.semantic_judge_enabled === true || String(rawConfig.semantic_judge_enabled).toLowerCase() === 'true';
  if (semanticEnabled) stage9RequireHeaders_(videos, ['semantic_decision'], 'Videos');
  const videoIndex = {};
  videos.headers.forEach(function (header, index) { videoIndex[header] = index; });
  const selectedAtIndex = videoIndex.selected_at;
  const idsInVideos = new Map();
  videos.rows.forEach(function (row) {
    const id = String(row[videoIndex.video_id] || '').trim();
    if (id) idsInVideos.set(id, (idsInVideos.get(id) || 0) + 1);
  });

  const selectedTodayAtStart = stage9SelectedTodayCount_(videos, selectedAtIndex, selectionDay, timeZone);
  const skipped = [];
  const eligible = [];
  let invalidHotEvidence = 0;
  let outsideBatch = 0;
  let semanticRejected = 0;
  let invalidPayload = 0;
  let duplicateVideoRows = 0;
  let eliminated = 0;
  let selected = 0;
  let enqueued = 0;
  let recovered = 0;
  let alreadySelected = 0;
  let selectedToday = selectedTodayAtStart;

  function recordSkip(videoId, reason) {
    skipped.push({ video_id: String(videoId || ''), reason: String(reason) });
  }
  function eliminate(video, reason, rank, strength) {
    stage9WriteSelectionMetadata_(videos, video, {
      selection_day: selectionDay,
      selection_rank: rank || '',
      selection_hot_strength: strength === undefined ? '' : strength,
      selection_result: 'ELIMINATED',
      selection_closed_at: nowIso,
      selection_reason: reason,
    });
    eliminated += 1;
    recordSkip(video.video_id, reason);
  }

  videos.rows.forEach(function (row, offset) {
    const video = stage9RowObject_(videos.headers, row);
    if (String(video.lifecycle_state || '') !== 'CANDIDATE') return;
    const result = String(video.selection_result || '').trim().toUpperCase();
    if (result === 'SELECTED' || result === 'ELIMINATED' || String(video.selection_closed_at || '').trim()) return;
    video.sheetRow = offset + 2;
    const videoId = String(video.video_id || '').trim();
    if (!videoId) { invalidPayload += 1; eliminate(video, 'INVALID_VIDEO_ID'); return; }
    if (idsInVideos.get(videoId) !== 1) { duplicateVideoRows += 1; eliminate(video, 'DUPLICATE_VIDEO_ROW'); return; }
    const existingTask = stage9QueueTaskForVideo_(queue, videoId);
    if (video.queue_id || video.selected_at || existingTask) {
      if (!existingTask) {
        alreadySelected += 1;
        eliminate(video, 'PERSISTED_SELECTION_WITHOUT_QUEUE');
        return;
      }
      const effectiveAt = video.selected_at || nowIso;
      const recordedDay = stage9SelectionDay_(effectiveAt, timeZone) || String(video.selection_day || selectionDay);
      const rankValue = stage9SelectionIntegerValue_(video.selection_rank) ||
        stage9SelectionIntegerValue_(existingTask.selection_rank) ||
        (video.selected_at ? stage9SelectedRank_(videos, selectedAtIndex, effectiveAt, videoId, timeZone) : selectedToday + 1);
      let recoveredTask = existingTask;
      if (String(existingTask.status) === 'PENDING' && !String(existingTask.selection_day || '').trim()) {
        const bridge = stage7Enqueue_({
          video_id: videoId,
          url: String(video.video_url).trim(),
          selection_day: recordedDay,
          selection_rank: rankValue,
          rank2_start_cutoff_at: stage9LocalCutoffIso_(recordedDay, rawConfig.rank2_start_cutoff, timeZone),
        }, scriptLockAlreadyHeld === true);
        recoveredTask = bridge && bridge.task;
      }
      stage9WriteSelectionMetadata_(videos, video, {
        selected_at: effectiveAt,
        selection_day: recordedDay,
        selection_rank: rankValue,
        selection_hot_strength: video.selection_hot_strength || '',
        queue_id: recoveredTask.queue_id,
        queued_at: video.queued_at || effectiveAt,
        selection_result: 'SELECTED',
        selection_closed_at: video.selection_closed_at || effectiveAt,
        selection_reason: video.selection_reason || 'QUEUE_IDEMPOTENCY_RECOVERY',
      });
      if (!video.selected_at && recordedDay === selectionDay) selectedToday += 1;
      alreadySelected += 1;
      if (!video.selected_at) recovered += 1;
      queue.byVideoId.set(videoId, recoveredTask);
      return;
    }
    if (!stage9IsDailyBatchVideo_(video.published_at, selectionDay, rawConfig)) {
      outsideBatch += 1;
      eliminate(video, 'OUTSIDE_DAILY_BATCH');
      return;
    }
    try {
      stage7ValidateEnqueuePayload_({ video_id: videoId, url: video.video_url });
    } catch (error) {
      invalidPayload += 1;
      eliminate(video, error && error.queueErrorCode ? error.queueErrorCode : 'INVALID_QUEUE_PAYLOAD');
      return;
    }
    const candidateAtMillis = stage9DateMillis_(video.candidate_at);
    if (candidateAtMillis === null || candidateAtMillis > nowMillis) {
      outsideBatch += 1;
      eliminate(video, candidateAtMillis === null ? 'INVALID_CANDIDATE_AT' : 'FUTURE_CANDIDATE');
      return;
    }
    if (semanticEnabled && String(video.semantic_decision || '').trim().toUpperCase() === 'REJECT') {
      semanticRejected += 1;
      eliminate(video, 'SEMANTIC_REJECT');
      return;
    }
    const evidence = stage9CandidateHotEvidence_(video, snapshots.rows, snapshots.headers, hotConfig);
    if (!evidence.eligible) {
      invalidHotEvidence += 1;
      eliminate(video, evidence.reason);
      return;
    }
    eligible.push({
      videoId: videoId,
      url: String(video.video_url).trim(),
      sheetRow: video.sheetRow,
      hotStrength: evidence.hotStrength,
      checkpoint: evidence.checkpoint,
      candidateAtMillis: candidateAtMillis,
      video: video,
      existingTask: existingTask,
    });
  });

  eligible.sort(stage9CompareCandidates_);
  const cutoffAt = stage9LocalCutoffIso_(selectionDay, rawConfig.rank2_start_cutoff, timeZone);
  eligible.forEach(function (candidate, index) {
    const video = candidate.video;
    const existingTask = candidate.existingTask;
    if (selectedToday >= config.dailyMax) {
      eliminate(video, 'DAILY_LIMIT', index + 1, candidate.hotStrength);
      return;
    }
    const rank = selectedToday + 1;
    const selectionAt = nowIso;
    let task = existingTask;
    let wasCreated = false;
    if (!task) {
      const result = stage7Enqueue_({
        video_id: candidate.videoId,
        url: candidate.url,
        selection_day: selectionDay,
        selection_rank: rank,
        rank2_start_cutoff_at: cutoffAt,
      }, scriptLockAlreadyHeld === true);
      task = result && result.task;
      wasCreated = result && result.created === true;
      if (!task || !task.queue_id || String(task.video_id) !== candidate.videoId ||
          ['PENDING', 'CLAIMED', 'PROCESSING', 'PAUSED', 'FAILED', 'COMPLETED'].indexOf(String(task.status)) < 0) {
        throw new Error('Stage 7 explicit enqueue returned an invalid task for Selection.');
      }
    }
    stage9WriteSelectionMetadata_(videos, video, {
      selected_at: selectionAt,
      selection_day: selectionDay,
      selection_rank: rank,
      selection_hot_strength: candidate.hotStrength,
      queue_id: task.queue_id,
      queued_at: selectionAt,
      selection_result: 'SELECTED',
      selection_closed_at: selectionAt,
      selection_reason: 'DAILY_TOP_CANDIDATE',
    });
    selectedToday += 1;
    selected += 1;
    if (wasCreated) enqueued += 1;
    else if (existingTask) recovered += 1;
    queue.byVideoId.set(candidate.videoId, task);
  });

  return {
    enabled: config.enabled,
    selection_day: selectionDay,
    timezone: timeZone,
    candidate_count: eligible.length,
    selected: selected,
    enqueued: enqueued,
    recovered: recovered,
    already_selected: alreadySelected,
    eliminated: eliminated,
    remaining_daily_slots: Math.max(0, config.dailyMax - selectedToday),
    skipped_invalid_hot_evidence: invalidHotEvidence,
    skipped_outside_batch: outsideBatch,
    skipped_semantic_reject: semanticRejected,
    skipped_invalid_payload: invalidPayload,
    skipped_duplicate_video_rows: duplicateVideoRows,
    skipped: skipped,
  };
}

function stage9SelectionIntegerValue_(value) {
  if (value === null || value === undefined || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 1 ? number : null;
}

function stage9SelectionNumber_(value) {
  if (value === null || value === undefined || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function stage9SelectedRank_(videos, selectedAtIndex, selectedAt, videoId, timeZone) {
  const targetMillis = stage9DateMillis_(selectedAt);
  const targetDay = stage9SelectionDay_(selectedAt, timeZone);
  const idsIndex = videos.headers.indexOf('video_id');
  const earlierOrEqual = videos.rows.filter(function (row) {
    if (stage9SelectionDay_(row[selectedAtIndex], timeZone) !== targetDay) return false;
    const millis = stage9DateMillis_(row[selectedAtIndex]);
    if (millis === null) return false;
    return millis < targetMillis || (millis === targetMillis && String(row[idsIndex]) <= String(videoId));
  });
  return Math.max(1, earlierOrEqual.length);
}
