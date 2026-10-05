function stage6CaptureDueSnapshots() {
  const config = stage6ReadConfig_();
  const hotConfig = stage6ReadHotConfig_(config);
  const stages = hotConfig.snapshotCheckpoints;
  const now = new Date();
  const videos = stage6ReadTable_('Videos');
  const snapshots = stage6ReadTable_('Snapshots');
  const videoIndex = stage6HeaderIndex_(videos.headers, ['video_id', 'channel_id', 'published_at', 'lifecycle_state']);
  const snapshotIndex = stage6HeaderIndex_(snapshots.headers, ['video_id', 'snapshot_stage_minutes', 'captured_at']);
  const existingStages = new Map();
  snapshots.rows.forEach(function (row) {
    const key = String(row[snapshotIndex.video_id]);
    if (!existingStages.has(key)) existingStages.set(key, []);
    existingStages.get(key).push(Number(row[snapshotIndex.snapshot_stage_minutes]));
  });

  const due = [];
  const allMissedRows = [];
  videos.rows.forEach(function (row, index) {
    const state = String(row[videoIndex.lifecycle_state]);
    // HOT/CANDIDATE ends the observation lifecycle; do not schedule later
    // snapshots after promotion.
    if (state !== 'WATCH') return;
    const videoId = String(row[videoIndex.video_id]);
    const plan = stage6SnapshotPlan_(stages, existingStages.get(videoId) || [], row[videoIndex.published_at], now.toISOString(), STAGE6_SNAPSHOT_LATE_TOLERANCE_MINUTES_);
    if (plan.stage !== null) due.push({ rowIndex: index + 2, row: row, videoId: videoId, nextStage: plan.stage });
    else if (plan.all_missed) allMissedRows.push(index + 2);
  });
  // Every remaining checkpoint passed its tolerance (e.g. after an outage):
  // end the observation instead of judging HOT on a snapshot taken hours late.
  const hotReasonIndex = videos.headers.indexOf('hot_reason');
  allMissedRows.forEach(function (sheetRow) {
    videos.sheet.getRange(sheetRow, videoIndex.lifecycle_state + 1).setValue('SNAPSHOT_MISSED');
    if (hotReasonIndex >= 0) videos.sheet.getRange(sheetRow, hotReasonIndex + 1).setValue('SNAPSHOT_MISSED');
  });
  if (!due.length) return 0;

  const stageRecords = [];
  const videoTailUpdates = new Map();
  const coldBaselineCache = new Map();
  stage6ChunkIds_(due.map(function (item) { return item.videoId; }), 50).forEach(function (ids) {
    const params = { id: ids.join(','), maxResults: ids.length };
    const response = stage6CallYoutube_(function () { return YouTube.Videos.list('snippet,statistics', params); });
    const returned = new Map(((response && response.items) || []).map(function (video) { return [String(video.id), video]; }));
    const capturedAt = new Date().toISOString();
    ids.forEach(function (id) {
      const dueItem = due.find(function (item) { return item.videoId === id; });
      const video = returned.get(id);
      if (!video || !video.statistics) return;
      const stats = video.statistics;
      const viewCount = stage6NumberOrNull_(stats.viewCount);
      const likeCount = stage6NumberOrNull_(stats.likeCount);
      const commentCount = stage6NumberOrNull_(stats.commentCount);
      if (viewCount === null) return;
      const previous = stage6LatestSnapshot_(snapshots.headers, snapshots.rows, id);
      const metrics = stage6BuildSnapshotMetrics_({
        viewCount: viewCount,
        likeCount: likeCount,
        publishedAt: dueItem.row[videoIndex.published_at],
        capturedAt: capturedAt,
        previous: previous,
      });
      const channelId = String(dueItem.row[videoIndex.channel_id]);
      const noBaseline = { sample_count: 0, final_views_median: null, historical_median_like_rate: null };
      const snapshotsAfterCurrent = snapshots.rows.concat(stage6SnapshotAsRow_(snapshots.headers, {
        video_id: id, channel_id: channelId, snapshot_stage_minutes: dueItem.nextStage,
        published_at: dueItem.row[videoIndex.published_at], captured_at: capturedAt, metrics: metrics,
        coldBaseline: noBaseline, warmBaseline: noBaseline, relativeVelocity: null, warmCount: 0,
        commentCount: commentCount,
      }));
      const completeCount = stage6CompleteWatchSampleCount_(channelId, videos.headers, videos.rows, snapshots.headers, snapshotsAfterCurrent, stages, id);
      const phase = stage6HotPhase_(completeCount, hotConfig.completeWatchThreshold);
      let coldBaseline = noBaseline;
      let warmBaseline = noBaseline;
      let relativeVelocity = null;
      let warmCount = 0;
      if (phase === 'COLD_START') {
        const baselineCacheKey = channelId + '\u0000' + id;
        if (!coldBaselineCache.has(baselineCacheKey)) {
          coldBaselineCache.set(baselineCacheKey, stage6CreatorBaseline_(channelId, hotConfig.finalViewsHistoryWindow, id));
        }
        coldBaseline = coldBaselineCache.get(baselineCacheKey);
      }
      if (phase === 'WARM_BASELINE') {
        warmBaseline = stage6CompletePeerStats_(id, channelId, dueItem.nextStage, videos.headers, videos.rows, snapshots.headers, snapshotsAfterCurrent, stages);
        warmCount = completeCount;
        if (warmBaseline.median_views !== null && warmBaseline.median_views > 0 && metrics.view_count !== null) {
          relativeVelocity = metrics.view_count / warmBaseline.median_views;
        }
      }
      const record = {
        video_id: id, channel_id: channelId, snapshot_stage_minutes: dueItem.nextStage,
        published_at: dueItem.row[videoIndex.published_at], captured_at: capturedAt,
        metrics: metrics, coldBaseline: coldBaseline, warmBaseline: warmBaseline, relativeVelocity: relativeVelocity,
        warmCount: warmCount,
        commentCount: commentCount,
      };
      stageRecords.push(stage6SnapshotAsRow_(snapshots.headers, record));
      videoTailUpdates.set(dueItem.rowIndex, stage6UpdatedVideoTail_(videos.headers, dueItem.row, record, stages, snapshotsAfterCurrent));
    });
  });

  if (stageRecords.length) {
    snapshots.sheet.getRange(snapshots.sheet.getLastRow() + 1, 1, stageRecords.length, snapshots.headers.length).setValues(stageRecords);
  }
  videoTailUpdates.forEach(function (tail, rowIndex) {
    const firstTailColumn = videos.headers.indexOf('first_snapshot_at') + 1;
    videos.sheet.getRange(rowIndex, firstTailColumn, 1, tail.length).setValues([tail]);
  });
  return stageRecords.length;
}

function stage6ParseSnapshotStages_(value) {
  if (value === null || value === undefined || String(value).trim() === '') {
    throw new Error('Stage 6 HOT Config invalid: snapshot_stage_minutes is required.');
  }
  const parts = String(value).split(',').map(function (part) { return String(part).trim(); });
  if (parts.some(function (part) { return !/^\d+$/.test(part); })) {
    throw new Error('Stage 6 HOT Config invalid: snapshot_stage_minutes contains a non-integer checkpoint.');
  }
  const stages = parts.map(Number);
  if (stages.some(function (stage) { return stage <= 0; }) || new Set(stages).size !== stages.length) {
    throw new Error('Stage 6 HOT Config invalid: checkpoints must be positive and unique.');
  }
  return stages.sort(function (a, b) { return a - b; });
}

// A snapshot is valid for a checkpoint only if it is captured no later than
// checkpoint + tolerance. Twice the 10-minute trigger cadence absorbs normal
// scheduling delay; anything later (an outage) would compare hours of views
// against a T+30 threshold, so that checkpoint is skipped instead.
const STAGE6_SNAPSHOT_LATE_TOLERANCE_MINUTES_ = 20;

// Returns the checkpoint to capture now ({stage}) and whether every remaining
// checkpoint is already past its tolerance ({all_missed}). Missed checkpoints
// are skipped, never back-filled.
function stage6SnapshotPlan_(stages, existingStages, publishedAt, capturedAt, toleranceMinutes) {
  const existing = new Set((existingStages || []).map(Number));
  const publishMs = stage6DateMillisOrNull_(publishedAt);
  const nowMs = stage6DateMillisOrNull_(capturedAt);
  if (publishMs === null || nowMs === null) return { stage: null, all_missed: false, missed: [] };
  const toleranceMs = Number(toleranceMinutes) * 60000;
  const missed = [];
  for (let i = 0; i < stages.length; i += 1) {
    const stage = Number(stages[i]);
    if (existing.has(stage)) continue;
    const targetMs = publishMs + stage * 60000;
    if (nowMs < targetMs) return { stage: null, all_missed: false, missed: missed };
    if (nowMs <= targetMs + toleranceMs) return { stage: stage, all_missed: false, missed: missed };
    missed.push(stage);
  }
  return { stage: null, all_missed: missed.length > 0, missed: missed };
}

function stage6NextDueStage_(stages, existingStages, publishedAt, capturedAt) {
  const existing = new Set((existingStages || []).map(Number));
  const publishMs = stage6DateMillisOrNull_(publishedAt);
  const nowMs = stage6DateMillisOrNull_(capturedAt);
  if (publishMs === null || nowMs === null) return null;
  for (let i = 0; i < stages.length; i += 1) {
    const stage = Number(stages[i]);
    if (!existing.has(stage)) return nowMs >= publishMs + stage * 60000 ? stage : null;
  }
  return null;
}

function stage6BuildSnapshotMetrics_(input) {
  const publishedMs = stage6DateMillisOrNull_(input.publishedAt);
  const capturedMs = stage6DateMillisOrNull_(input.capturedAt);
  const elapsedSeconds = publishedMs !== null && capturedMs !== null ? Math.floor((capturedMs - publishedMs) / 1000) : null;
  const actualElapsedSeconds = elapsedSeconds;
  const viewCount = stage6NumberOrNull_(input.viewCount);
  const likeCount = stage6NumberOrNull_(input.likeCount);
  const previous = input.previous || null;
  let intervalGrowth = null;
  let intervalVelocity = null;
  let acceleration = null;
  if (previous && viewCount !== null && stage6NumberOrNull_(previous.view_count) !== null) {
    intervalGrowth = viewCount - stage6NumberOrNull_(previous.view_count);
    const previousElapsedSeconds = stage6NumberOrNull_(previous.actual_elapsed_seconds);
    const intervalSeconds = actualElapsedSeconds !== null && previousElapsedSeconds !== null
      ? actualElapsedSeconds - previousElapsedSeconds
      : null;
    if (intervalSeconds !== null && intervalSeconds > 0) {
      const candidateVelocity = intervalGrowth / intervalSeconds * 3600;
      intervalVelocity = Number.isFinite(candidateVelocity) ? candidateVelocity : null;
      const previousVelocity = stage6NumberOrNull_(previous.velocity);
      if (intervalVelocity !== null && previousVelocity !== null) {
        const candidateAcceleration = intervalVelocity - previousVelocity;
        acceleration = Number.isFinite(candidateAcceleration) ? candidateAcceleration : null;
      }
    }
  }
  const cumulativeCandidate = viewCount !== null && elapsedSeconds !== null && elapsedSeconds > 0 ? viewCount / elapsedSeconds * 3600 : null;
  const cumulativeVelocity = cumulativeCandidate !== null && Number.isFinite(cumulativeCandidate) ? cumulativeCandidate : null;
  const velocity = previous ? intervalVelocity : cumulativeVelocity;
  const likeRateCandidate = viewCount !== null && viewCount > 0 && likeCount !== null ? likeCount / viewCount : null;
  const likeRate = likeRateCandidate !== null && Number.isFinite(likeRateCandidate) ? likeRateCandidate : null;
  return {
    actual_elapsed_seconds: actualElapsedSeconds,
    views_per_hour: cumulativeVelocity,
    interval_growth: intervalGrowth,
    interval_growth_per_hour: intervalVelocity,
    interval_view_growth: intervalGrowth,
    velocity: velocity,
    acceleration: acceleration,
    like_rate: likeRate,
    view_count: viewCount,
    like_count: likeCount,
  };
}

function stage6LatestSnapshot_(headers, rows, videoId) {
  const index = stage6HeaderIndex_(headers, ['video_id', 'captured_at']);
  let latest = null;
  rows.forEach(function (row) {
    if (String(row[index.video_id]) !== String(videoId)) return;
    if (!latest || new Date(row[index.captured_at]).getTime() > new Date(latest[index.captured_at]).getTime()) latest = row;
  });
  if (!latest) return null;
  const velocityIndex = headers.indexOf('velocity');
  const legacyVelocity = stage6NumberOrNull_(latest[headers.indexOf('interval_growth_per_hour')]) !== null
    ? latest[headers.indexOf('interval_growth_per_hour')]
    : latest[headers.indexOf('views_per_hour')];
  return {
    view_count: latest[headers.indexOf('view_count')],
    captured_at: latest[index.captured_at],
    actual_elapsed_seconds: latest[headers.indexOf('actual_elapsed_seconds')],
    velocity: velocityIndex >= 0 ? stage6NumberOrNull_(latest[velocityIndex]) : legacyVelocity,
  };
}

function stage6CreatorBaseline_(channelId, historyWindow, currentVideoId) {
  const baseline = stage6ReadTable_('Baseline');
  const ix = stage6HeaderIndex_(baseline.headers, ['channel_id', 'video_id', 'view_count', 'like_rate', 'published_at']);
  const publishedIndex = baseline.headers.indexOf('published_at');
  const videoIdIndex = baseline.headers.indexOf('video_id');
  const rows = baseline.rows.filter(function (row) {
    return String(row[ix.channel_id]) === String(channelId) &&
      (currentVideoId === null || currentVideoId === undefined || videoIdIndex < 0 || String(row[videoIdIndex]) !== String(currentVideoId)) &&
      stage6DateMillisOrNull_(row[publishedIndex]) !== null;
  });
  rows.sort(function (a, b) {
    return new Date(b[publishedIndex]).getTime() - new Date(a[publishedIndex]).getTime();
  });
  const latest = rows.slice(0, historyWindow);
  const completeViews = latest.map(function (row) { return stage6NumberOrNull_(row[ix.view_count]); })
    .filter(function (value) { return value !== null; });
  const validLikeRates = latest.map(function (row) { return stage6NumberOrNull_(row[ix.like_rate]); })
    .filter(function (value) { return value !== null; });
  const historyComplete = completeViews.length >= historyWindow;
  return {
    selected_history_count: latest.length,
    sample_count: completeViews.length,
    valid_like_rate_sample_count: validLikeRates.length,
    history_complete: historyComplete,
    final_views_median: completeViews.length ? stage6Median_(completeViews) : null,
    historical_median_like_rate: validLikeRates.length ? stage6Median_(validLikeRates) : null,
  };
}

function stage6CompleteWatchSampleCount_(channelId, videoHeaders, videoRows, snapshotHeaders, snapshotRows, stages, excludedVideoId) {
  const vi = stage6HeaderIndex_(videoHeaders, ['video_id', 'channel_id']);
  const si = stage6HeaderIndex_(snapshotHeaders, ['video_id', 'channel_id', 'snapshot_stage_minutes', 'view_count']);
  const complete = new Set();
  const stageSets = new Map();
  snapshotRows.forEach(function (row) {
    const id = String(row[si.video_id]);
    if (String(row[si.channel_id]) !== String(channelId) || stage6NumberOrNull_(row[si.view_count]) === null) return;
    if (!stageSets.has(id)) stageSets.set(id, new Set());
    stageSets.get(id).add(Number(row[si.snapshot_stage_minutes]));
  });
  videoRows.forEach(function (row) {
    if (String(row[vi.channel_id]) !== String(channelId)) return;
    const id = String(row[vi.video_id]);
    if (excludedVideoId !== null && excludedVideoId !== undefined && id === String(excludedVideoId)) return;
    const seen = stageSets.get(id) || new Set();
    if (stages.every(function (stage) { return seen.has(Number(stage)); })) complete.add(id);
  });
  return complete.size;
}

function stage6CompletePeerStats_(videoId, channelId, stage, videoHeaders, videoRows, snapshotHeaders, snapshotRows, stages) {
  const vi = stage6HeaderIndex_(videoHeaders, ['video_id', 'channel_id']);
  const si = stage6HeaderIndex_(snapshotHeaders, ['video_id', 'channel_id', 'snapshot_stage_minutes', 'view_count', 'like_rate']);
  const completeIds = new Set();
  const stageSets = new Map();
  snapshotRows.forEach(function (row) {
    if (String(row[si.channel_id]) !== String(channelId) || stage6NumberOrNull_(row[si.view_count]) === null) return;
    const id = String(row[si.video_id]);
    if (!stageSets.has(id)) stageSets.set(id, new Set());
    stageSets.get(id).add(Number(row[si.snapshot_stage_minutes]));
  });
  videoRows.forEach(function (row) {
    const id = String(row[vi.video_id]);
    if (id === String(videoId) || String(row[vi.channel_id]) !== String(channelId)) return;
    const seen = stageSets.get(id) || new Set();
    if (stages.every(function (value) { return seen.has(Number(value)); })) completeIds.add(id);
  });
  const peers = snapshotRows.filter(function (row) {
    return completeIds.has(String(row[si.video_id])) &&
      String(row[si.channel_id]) === String(channelId) && Number(row[si.snapshot_stage_minutes]) === Number(stage);
  });
  const views = peers.map(function (row) { return stage6NumberOrNull_(row[si.view_count]); }).filter(function (value) { return value !== null; });
  const likeRates = peers.map(function (row) { return stage6NumberOrNull_(row[si.like_rate]); })
    .filter(function (value) { return value !== null; });
  return {
    sample_count: peers.length,
    median_views: stage6Median_(views),
    median_like_rate: likeRates.length ? stage6Median_(likeRates) : null,
  };
}

function stage6SnapshotAsRow_(headers, record) {
  const metrics = record.metrics;
  const values = {
    video_id: record.video_id,
    channel_id: record.channel_id,
    snapshot_stage_minutes: record.snapshot_stage_minutes,
    published_at: record.published_at,
    captured_at: record.captured_at,
    actual_elapsed_seconds: metrics.actual_elapsed_seconds === null ? '' : metrics.actual_elapsed_seconds,
    view_count: metrics.view_count,
    like_count: metrics.like_count === null ? '' : metrics.like_count,
    comment_count: record.commentCount === null || record.commentCount === undefined ? '' : record.commentCount,
    views_per_hour: metrics.views_per_hour === null ? '' : metrics.views_per_hour,
    interval_growth: metrics.interval_growth === null ? '' : metrics.interval_growth,
    interval_growth_per_hour: metrics.interval_growth_per_hour === null ? '' : metrics.interval_growth_per_hour,
    acceleration: metrics.acceleration === null ? '' : metrics.acceleration,
    like_rate: metrics.like_rate === null ? '' : metrics.like_rate,
    relative_velocity: record.relativeVelocity === null ? '' : record.relativeVelocity,
    creator_scale_median_views: record.coldBaseline.final_views_median === null ? '' : record.coldBaseline.final_views_median,
    warm_baseline_sample_count: record.warmCount,
    source: 'YouTube Data API videos.list (batched IDs)',
    creator_id: record.channel_id,
    checkpoint_minutes: record.snapshot_stage_minutes,
    interval_view_growth: metrics.interval_view_growth === null ? '' : metrics.interval_view_growth,
    velocity: metrics.velocity === null ? '' : metrics.velocity,
    baseline_final_views_median: record.coldBaseline.final_views_median === null ? '' : record.coldBaseline.final_views_median,
    historical_median_like_rate: record.coldBaseline.historical_median_like_rate === null ? '' : record.coldBaseline.historical_median_like_rate,
    historical_same_checkpoint_median_views: record.warmBaseline.median_views === null || record.warmBaseline.median_views === undefined ? '' : record.warmBaseline.median_views,
    historical_same_checkpoint_median_like_rate: record.warmBaseline.median_like_rate === null || record.warmBaseline.median_like_rate === undefined ? '' : record.warmBaseline.median_like_rate,
  };
  return headers.map(function (header) { return Object.prototype.hasOwnProperty.call(values, header) ? values[header] : ''; });
}

function stage6UpdatedVideoTail_(headers, row, record, stages, snapshotsAfterCurrent) {
  const first = headers.indexOf('first_snapshot_at');
  const tail = row.slice(first);
  const set = function (name, value) { const i = headers.indexOf(name) - first; if (i >= 0) tail[i] = value; };
  const metrics = record.metrics;
  set('first_snapshot_at', row[headers.indexOf('first_snapshot_at')] || record.captured_at);
  set('latest_snapshot_at', record.captured_at);
  set('latest_snapshot_stage_minutes', record.snapshot_stage_minutes);
  set('actual_elapsed_seconds', metrics.actual_elapsed_seconds === null ? '' : metrics.actual_elapsed_seconds);
  set('view_count', metrics.view_count);
  set('like_count', metrics.like_count === null ? '' : metrics.like_count);
  set('comment_count', record.commentCount === null || record.commentCount === undefined ? '' : record.commentCount);
  set('views_per_hour', metrics.views_per_hour === null ? '' : metrics.views_per_hour);
  set('interval_growth', metrics.interval_growth === null ? '' : metrics.interval_growth);
  set('interval_growth_per_hour', metrics.interval_growth_per_hour === null ? '' : metrics.interval_growth_per_hour);
  set('acceleration', metrics.acceleration === null ? '' : metrics.acceleration);
  set('like_rate', metrics.like_rate === null ? '' : metrics.like_rate);
  set('relative_velocity', record.relativeVelocity === null ? '' : record.relativeVelocity);
  const coldMedian = record.coldBaseline && record.coldBaseline.final_views_median;
  set('creator_scale_median_views', coldMedian === null || coldMedian === undefined ? '' : coldMedian);
  const observedStages = snapshotsAfterCurrent.filter(function (snapshotRow) {
    return String(snapshotRow[headers.indexOf('video_id')]) === String(record.video_id);
  });
  const snapshotStageIndex = STAGE6_SNAPSHOT_HEADERS_.indexOf('snapshot_stage_minutes');
  const hasStages = new Set(observedStages.map(function (snapshotRow) { return Number(snapshotRow[snapshotStageIndex]); }));
  set('complete_watch', stages.every(function (stage) { return hasStages.has(Number(stage)); }));
  return tail;
}

function stage6NumberOrNull_(value) {
  if (value === null || value === undefined || typeof value === 'boolean' ||
      (typeof value === 'string' && (value.trim() === '' || !/^-?(?:\d+\.?\d*|\.\d+)$/.test(value.trim())))) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function stage6DateMillisOrNull_(value) {
  if (value === null || value === undefined || (typeof value === 'string' && value.trim() === '')) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isFinite(milliseconds) ? milliseconds : null;
}

function stage6HeaderIndex_(headers, names) {
  const result = {};
  names.forEach(function (name) {
    const index = headers.indexOf(name);
    if (index < 0) throw new Error('Missing required column ' + name + '.');
    result[name] = index;
  });
  return result;
}

function stage6CallYoutube_(callback) {
  try {
    return callback();
  } catch (error) {
    const wrapped = new Error('YouTube Data API call failed.');
    wrapped.stage6ApiFailure = true;
    wrapped.stage6ApiFailureClass = stage6ClassifyApiFailure_(error);
    // YouTube error text carries no credentials; keep it for the execution log.
    wrapped.stage6ApiFailureDetail = String(error && (error.message || error) || '').slice(0, 300);
    throw wrapped;
  }
}
