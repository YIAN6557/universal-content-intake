const STAGE6_REQUIRED_HOT_CHECKPOINTS_ = [30, 60, 120];
const STAGE6_RETIRED_HOT_CHECKPOINTS_ = [180, 360];

function stage6ReadHotConfig_(rawConfig) {
  const config = rawConfig || stage6ReadConfig_();
  const checkpoints = stage6ParseSnapshotStages_(config.snapshot_stage_minutes);
  if (checkpoints.length !== STAGE6_REQUIRED_HOT_CHECKPOINTS_.length ||
      checkpoints.some(function (stage, index) { return stage !== STAGE6_REQUIRED_HOT_CHECKPOINTS_[index]; })) {
    throw new Error('Stage 6 HOT Config invalid: snapshot_stage_minutes must contain every required checkpoint exactly once.');
  }

  const checkpointRatios = {};
  STAGE6_REQUIRED_HOT_CHECKPOINTS_.forEach(function (checkpoint) {
    const key = 'cold_start_checkpoint_' + checkpoint + '_ratio';
    checkpointRatios[checkpoint] = stage6HotConfigNumber_(config, key, 0, false);
  });
  Object.keys(config).forEach(function (key) {
    if (!/^cold_start_checkpoint_/.test(key)) return;
    const match = key.match(/^cold_start_checkpoint_(\d+)_ratio$/);
    if (!match || (STAGE6_REQUIRED_HOT_CHECKPOINTS_.indexOf(Number(match[1])) < 0 &&
        STAGE6_RETIRED_HOT_CHECKPOINTS_.indexOf(Number(match[1])) < 0)) {
      throw new Error('Stage 6 HOT Config invalid: illegal checkpoint parameter ' + key + '.');
    }
  });

  const completeWatchThreshold = stage6HotConfigNumber_(config, 'warm_baseline_min_complete_watch', 1, true);
  const finalViewsHistoryWindow = stage6HotConfigNumber_(config, 'cold_start_final_views_history_window', 1, true);
  const coldLikeRateMultiplier = stage6HotConfigNumber_(config, 'cold_start_like_rate_multiplier', 0, false);
  const accelerationMinSnapshotIndex = stage6HotConfigNumber_(config, 'acceleration_min_snapshot_index', 2, true);
  const warmRelativeVelocityThreshold = stage6HotConfigNumber_(config, 'warm_baseline_relative_velocity_threshold', 0, false);
  const warmLikeRateMultiplier = stage6HotConfigNumber_(config, 'warm_baseline_like_rate_multiplier', 0, false);
  const normalAfterMinutes = stage6HotConfigNumber_(config, 'normal_after_minutes', 1, true);
  if (accelerationMinSnapshotIndex > checkpoints.length) {
    throw new Error('Stage 6 HOT Config invalid: acceleration_min_snapshot_index exceeds configured checkpoints.');
  }
  if (checkpoints.indexOf(normalAfterMinutes) < 0) {
    throw new Error('Stage 6 HOT Config invalid: normal_after_minutes must match a configured checkpoint.');
  }
  return {
    __stage6HotConfig: true,
    completeWatchThreshold: completeWatchThreshold,
    finalViewsHistoryWindow: finalViewsHistoryWindow,
    snapshotCheckpoints: checkpoints,
    checkpointRatios: checkpointRatios,
    coldLikeRateMultiplier: coldLikeRateMultiplier,
    accelerationMinSnapshotIndex: accelerationMinSnapshotIndex,
    warmRelativeVelocityThreshold: warmRelativeVelocityThreshold,
    warmLikeRateMultiplier: warmLikeRateMultiplier,
    normalAfterMinutes: normalAfterMinutes,
  };
}

function stage6HotConfigNumber_(config, key, minimum, integer) {
  const raw = config[key];
  if (raw === null || raw === undefined || typeof raw === 'boolean' || String(raw).trim() === '') {
    throw new Error('Stage 6 HOT Config invalid: missing ' + key + '.');
  }
  if (typeof raw !== 'number' && !/^-?(?:\d+\.?\d*|\.\d+)$/.test(String(raw).trim())) {
    throw new Error('Stage 6 HOT Config invalid: nonnumeric ' + key + '.');
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || value < minimum || (integer && !Number.isInteger(value))) {
    throw new Error('Stage 6 HOT Config invalid: out-of-range ' + key + '.');
  }
  return value;
}

function stage6HotFiniteNumber_(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  if (typeof value !== 'number' && !/^-?(?:\d+\.?\d*|\.\d+)$/.test(String(value).trim())) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function stage6HotAtLeast_(value, threshold) {
  if (value === null || threshold === null || !Number.isFinite(value) || !Number.isFinite(threshold)) return false;
  const tolerance = Number.EPSILON * Math.max(1, Math.abs(value), Math.abs(threshold)) * 4;
  return value >= threshold || Math.abs(value - threshold) <= tolerance;
}

function stage6HotPhase_(completeWatchCount, threshold) {
  const required = stage6HotFiniteNumber_(threshold);
  if (required === null || required < 1) throw new Error('Stage 6 HOT Config invalid: complete WATCH threshold is not configured.');
  const complete = stage6HotFiniteNumber_(completeWatchCount);
  if (complete === null || complete < 0 || !Number.isInteger(complete)) {
    throw new Error('Stage 6 HOT input invalid: complete WATCH count must be a nonnegative integer.');
  }
  return complete >= required ? 'WARM_BASELINE' : 'COLD_START';
}

function stage6EvaluateHot_(input, config) {
  const rules = config && config.__stage6HotConfig === true ? config : stage6ReadHotConfig_(config);
  const checkpoint = stage6HotFiniteNumber_(input && input.checkpoint_minutes);
  const checkpointIndex = checkpoint === null ? -1 : rules.snapshotCheckpoints.indexOf(checkpoint);
  if (checkpointIndex < 0) throw new Error('Stage 6 HOT Config invalid: decision checkpoint is not configured.');

  const count = stage6HotFiniteNumber_(input.complete_watch_count);
  const mode = stage6HotPhase_(count, rules.completeWatchThreshold) === 'COLD_START' ? 'cold' : 'warm';
  const currentViews = stage6HotFiniteNumber_(input.current_view_count);
  const currentLikeRate = stage6HotFiniteNumber_(input.current_like_rate);
  const acceleration = stage6HotFiniteNumber_(input.acceleration);
  const snapshotIndex = checkpointIndex + 1;
  const accelerationPass = snapshotIndex >= rules.accelerationMinSnapshotIndex && acceleration !== null && acceleration > 0;
  let requiredViews = null;
  let relativeVelocity = null;
  let viewsPass = null;
  let relativeVelocityPass = null;
  let likeRatePass = false;
  let primaryPass = false;
  let unavailableReason = null;

  if (mode === 'cold') {
    const baselineViews = stage6HotFiniteNumber_(input.baseline_final_views_median);
    const baselineLikeRate = stage6HotFiniteNumber_(input.historical_median_like_rate);
    if (baselineViews !== null && baselineViews >= 0) {
      const candidateRequiredViews = baselineViews * rules.checkpointRatios[checkpoint];
      if (Number.isFinite(candidateRequiredViews)) {
        requiredViews = candidateRequiredViews;
        viewsPass = stage6HotAtLeast_(currentViews, requiredViews);
        primaryPass = viewsPass;
        if (currentViews === null) unavailableReason = 'cold_current_views_unavailable';
      } else {
        viewsPass = false;
        unavailableReason = 'cold_required_views_unavailable';
      }
    } else {
      viewsPass = false;
      unavailableReason = 'cold_baseline_unavailable';
    }
    likeRatePass = currentLikeRate !== null && baselineLikeRate !== null && baselineLikeRate >= 0 &&
      stage6HotAtLeast_(currentLikeRate, baselineLikeRate * rules.coldLikeRateMultiplier);
  } else {
    const sameCheckpointViews = stage6HotFiniteNumber_(input.historical_same_checkpoint_median_views);
    const sameCheckpointLikeRate = stage6HotFiniteNumber_(input.historical_same_checkpoint_median_like_rate);
    if (sameCheckpointViews !== null && sameCheckpointViews > 0 && currentViews !== null) {
      const candidateRelativeVelocity = currentViews / sameCheckpointViews;
      if (Number.isFinite(candidateRelativeVelocity)) {
        relativeVelocity = candidateRelativeVelocity;
        relativeVelocityPass = stage6HotAtLeast_(relativeVelocity, rules.warmRelativeVelocityThreshold);
        primaryPass = relativeVelocityPass;
      } else {
        relativeVelocityPass = false;
        unavailableReason = 'warm_relative_velocity_unavailable';
      }
    } else {
      relativeVelocityPass = false;
      unavailableReason = sameCheckpointViews === null || sameCheckpointViews <= 0
        ? 'warm_checkpoint_baseline_unavailable'
        : 'warm_current_views_unavailable';
    }
    likeRatePass = currentLikeRate !== null && sameCheckpointLikeRate !== null && sameCheckpointLikeRate >= 0 &&
      stage6HotAtLeast_(currentLikeRate, sameCheckpointLikeRate * rules.warmLikeRateMultiplier);
  }

  const matched = primaryPass && (likeRatePass || accelerationPass);
  let reason;
  if (matched) {
    const prefix = mode === 'cold' ? 'cold_views' : 'warm_relative_velocity';
    reason = prefix + (likeRatePass ? '_like_rate' : '') + (accelerationPass ? '_acceleration' : '');
  } else if (unavailableReason) {
    reason = unavailableReason;
  } else if (!primaryPass) {
    reason = mode === 'cold' ? (currentViews === null ? 'cold_current_views_unavailable' : 'cold_views_gate_failed')
      : 'warm_relative_velocity_gate_failed';
  } else {
    reason = mode === 'cold' ? 'cold_secondary_gate_failed' : 'warm_secondary_gate_failed';
  }
  const targetState = matched ? 'DATA_HOT'
    : (unavailableReason === 'cold_baseline_unavailable' && checkpoint >= rules.normalAfterMinutes
      ? 'BASELINE_UNAVAILABLE'
      : (unavailableReason ? 'WATCH' : (checkpoint >= rules.normalAfterMinutes ? 'NORMAL' : 'WATCH')));
  const resultReason = targetState === 'BASELINE_UNAVAILABLE' ? 'BASELINE_UNAVAILABLE' : reason;
  return {
    matched: matched,
    mode: mode,
    checkpoint: checkpoint,
    required_views: requiredViews,
    relative_velocity: relativeVelocity,
    views_pass: viewsPass,
    relative_velocity_pass: relativeVelocityPass,
    like_rate_pass: likeRatePass,
    acceleration_pass: accelerationPass,
    reason: resultReason,
    target_state: targetState,
    snapshot_index: snapshotIndex,
  };
}

function stage6RunHotEngine() {
  const rawConfig = stage6ReadConfig_();
  const config = stage6ReadHotConfig_(rawConfig);
  const videos = stage6ReadTable_('Videos');
  const snapshots = stage6ReadTable_('Snapshots');
  const index = stage6HeaderIndex_(videos.headers, [
    'video_id', 'channel_id', 'lifecycle_state', 'complete_watch', 'data_hot_at', 'hot_mode', 'hot_checkpoint', 'hot_reason',
  ]);
  const snapshotIndex = stage6HeaderIndex_(snapshots.headers, [
    'video_id', 'checkpoint_minutes', 'captured_at', 'view_count', 'like_rate', 'acceleration',
    'baseline_final_views_median', 'historical_median_like_rate',
    'historical_same_checkpoint_median_views', 'historical_same_checkpoint_median_like_rate',
  ]);
  const completeIdsByCreator = {};
  videos.rows.forEach(function (row) {
    if (row[index.complete_watch] === true || String(row[index.complete_watch]).toLowerCase() === 'true') {
      const creatorId = String(row[index.channel_id]);
      if (!completeIdsByCreator[creatorId]) completeIdsByCreator[creatorId] = new Set();
      completeIdsByCreator[creatorId].add(String(row[index.video_id]));
    }
  });
  let hotCount = 0;
  let normalCount = 0;
  let baselineUnavailableCount = 0;
  let checkedCount = 0;
  videos.rows.forEach(function (row, offset) {
    if (String(row[index.lifecycle_state]) !== 'WATCH') return;
    const latest = stage6LatestSnapshotRecord_(snapshots.headers, snapshots.rows, String(row[index.video_id]));
    if (!latest) return;
    const checkpoint = stage6HotFiniteNumber_(latest[snapshotIndex.checkpoint_minutes]);
    const decision = stage6EvaluateHot_({
      video_id: String(row[index.video_id]),
      creator_id: String(row[index.channel_id]),
      complete_watch_count: stage6CompleteWatchCountExcludingVideo_(
        completeIdsByCreator, String(row[index.channel_id]), String(row[index.video_id])),
      checkpoint_minutes: checkpoint,
      current_view_count: latest[snapshotIndex.view_count],
      current_like_rate: latest[snapshotIndex.like_rate],
      acceleration: latest[snapshotIndex.acceleration],
      baseline_final_views_median: latest[snapshotIndex.baseline_final_views_median],
      historical_median_like_rate: latest[snapshotIndex.historical_median_like_rate],
      historical_same_checkpoint_median_views: latest[snapshotIndex.historical_same_checkpoint_median_views],
      historical_same_checkpoint_median_like_rate: latest[snapshotIndex.historical_same_checkpoint_median_like_rate],
    }, config);
    checkedCount += 1;
    const nextState = stage6HotLifecycleTransition_(String(row[index.lifecycle_state]), decision);
    const sheetRow = offset + 2;
    if (nextState === 'DATA_HOT') {
      const now = new Date().toISOString();
      videos.sheet.getRange(sheetRow, index.lifecycle_state + 1).setValue(nextState);
      videos.sheet.getRange(sheetRow, index.data_hot_at + 1).setValue(row[index.data_hot_at] || now);
      videos.sheet.getRange(sheetRow, index.hot_mode + 1).setValue(decision.mode);
      videos.sheet.getRange(sheetRow, index.hot_checkpoint + 1).setValue(decision.checkpoint);
      videos.sheet.getRange(sheetRow, index.hot_reason + 1).setValue(decision.reason);
      const legacyPhase = videos.headers.indexOf('hot_phase');
      const legacyHotAt = videos.headers.indexOf('hot_at');
      if (legacyPhase >= 0) videos.sheet.getRange(sheetRow, legacyPhase + 1).setValue(decision.mode === 'cold' ? 'COLD_START' : 'WARM_BASELINE');
      if (legacyHotAt >= 0) videos.sheet.getRange(sheetRow, legacyHotAt + 1).setValue(row[legacyHotAt] || now);
      hotCount += 1;
    } else if (nextState === 'NORMAL') {
      videos.sheet.getRange(sheetRow, index.lifecycle_state + 1).setValue(nextState);
      normalCount += 1;
    } else if (nextState === 'BASELINE_UNAVAILABLE') {
      videos.sheet.getRange(sheetRow, index.lifecycle_state + 1).setValue(nextState);
      videos.sheet.getRange(sheetRow, index.hot_mode + 1).setValue(decision.mode);
      videos.sheet.getRange(sheetRow, index.hot_checkpoint + 1).setValue(decision.checkpoint);
      videos.sheet.getRange(sheetRow, index.hot_reason + 1).setValue('BASELINE_UNAVAILABLE');
      baselineUnavailableCount += 1;
    }
  });
  return {
    evaluated_hot: hotCount,
    marked_normal: normalCount,
    marked_baseline_unavailable: baselineUnavailableCount,
    checked: checkedCount,
    warm_baseline_complete_samples: Math.max.apply(null, [0].concat(Object.keys(completeIdsByCreator).map(function (key) { return completeIdsByCreator[key].size; }))),
  };
}

function stage6CompleteWatchCountExcludingVideo_(completeIdsByCreator, creatorId, videoId) {
  const completeIds = completeIdsByCreator[String(creatorId)] || new Set();
  return Math.max(0, completeIds.size - (completeIds.has(String(videoId)) ? 1 : 0));
}

function stage6LatestSnapshotRecord_(headers, rows, videoId) {
  const videoIndex = headers.indexOf('video_id');
  const capturedIndex = headers.indexOf('captured_at');
  let latest = null;
  rows.forEach(function (row) {
    if (String(row[videoIndex]) !== String(videoId)) return;
    if (!latest || new Date(row[capturedIndex]).getTime() > new Date(latest[capturedIndex]).getTime()) latest = row;
  });
  return latest;
}

function stage6RefreshCandidatePool_() {
  const videos = stage6ReadTable_('Videos');
  const stateIndex = videos.headers.indexOf('lifecycle_state');
  const candidateIndex = videos.headers.indexOf('candidate_at');
  if (stateIndex < 0 || candidateIndex < 0) throw new Error('Videos sheet is missing candidate fields.');
  const semanticEnabled = typeof stage8SemanticIsEnabled_ === 'function' &&
    stage8SemanticIsEnabled_(stage6ReadConfig_().semantic_judge_enabled);
  const semanticContext = semanticEnabled ? {
    creators: stage6ReadTable_('Creators'),
    baseline: stage6ReadTable_('Baseline'),
  } : null;
  let promoted = 0;
  videos.rows.forEach(function (row, offset) {
    if (semanticEnabled && String(row[stateIndex]) === 'DATA_HOT' &&
        !stage8AllowsCandidate_(videos, row, semanticContext)) return;
    const nextState = stage6CandidateLifecycleTransition_(String(row[stateIndex]));
    if (nextState === String(row[stateIndex])) return;
    const sheetRow = offset + 2;
    videos.sheet.getRange(sheetRow, stateIndex + 1).setValue(nextState);
    if (!row[candidateIndex]) videos.sheet.getRange(sheetRow, candidateIndex + 1).setValue(new Date().toISOString());
    promoted += 1;
  });
  return promoted;
}

function stage6HotLifecycleTransition_(currentState, decision) {
  if (currentState !== 'WATCH' || !decision || typeof decision.target_state !== 'string') return currentState;
  if (decision.target_state === 'DATA_HOT' || decision.target_state === 'NORMAL' ||
      decision.target_state === 'BASELINE_UNAVAILABLE') return decision.target_state;
  return currentState;
}

function stage6CandidateLifecycleTransition_(currentState) {
  return currentState === 'DATA_HOT' ? 'CANDIDATE' : currentState;
}

function stage6Median_(values) {
  const sorted = (values || []).map(Number).filter(Number.isFinite).sort(function (a, b) { return a - b; });
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}
