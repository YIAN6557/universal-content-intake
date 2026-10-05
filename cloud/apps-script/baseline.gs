// Historical public-video evidence for deterministic Cold Start baselines.
// These rows are stored only in Baseline and never enter Videos, WATCH,
// Snapshots, Candidate, Selection, or Queue.

function stage6BackfillEnabledCreatorBaselines() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) throw new Error('Stage 6 baseline backfill is already running.');
  try {
    const config = stage6ReadConfig_();
    try {
      const result = stage6EnsureCreatorBaselines_(config, true);
      Logger.log('Stage 6 baseline backfill: ' + JSON.stringify(result));
      return result;
    } catch (error) {
      if (error && error.stage6ApiFailure === true) {
        stage6SetConfig_('monitor_status', 'API_FAILED');
        stage6SetConfig_('last_api_error_class', error.stage6ApiFailureClass || 'API_OR_APPS_SCRIPT_ERROR');
        stage6SetConfig_('last_api_error_at', new Date().toISOString());
      }
      throw error;
    }
  } finally {
    lock.releaseLock();
  }
}

function stage6EnsureCreatorBaselines_(rawConfig, forceRefresh) {
  const config = rawConfig || stage6ReadConfig_();
  const hotConfig = stage6ReadHotConfig_(config);
  const creators = stage6ReadTable_('Creators');
  const creatorIndex = stage6HeaderIndex_(creators.headers, [
    'creator_name', 'channel_id', 'uploads_playlist_id', 'enabled', 'creator_id',
    'cold_baseline_status', 'cold_baseline_checked_at',
  ]);
  const baseline = stage6ReadTable_('Baseline');
  const videos = stage6ReadTable_('Videos');
  const videoIndex = stage6HeaderIndex_(videos.headers, ['video_id']);
  const excludedVideoIds = new Set(videos.rows.map(function (row) { return String(row[videoIndex.video_id] || ''); }));
  const day = stage6ZonedDateParts_(new Date(), String(config.production_timezone)).day;
  const results = [];

  creators.rows.forEach(function (creatorRow, offset) {
    if (!stage6BaselineIsEnabled_(creatorRow[creatorIndex.enabled])) return;
    const creatorId = String(creatorRow[creatorIndex.channel_id] || '').trim();
    const playlistId = String(creatorRow[creatorIndex.uploads_playlist_id] || '').trim();
    if (!creatorId || !playlistId) return;

    const status = String(creatorRow[creatorIndex.cold_baseline_status] || '').trim();
    const checkedAt = creatorRow[creatorIndex.cold_baseline_checked_at];
    const checkedDay = checkedAt ? stage6ZonedDateParts_(checkedAt, String(config.production_timezone)).day : '';
    if (!forceRefresh && status === 'AVAILABLE') {
      results.push({ creator_name: String(creatorRow[creatorIndex.creator_name]), creator_id: creatorId, status: 'SKIPPED_AVAILABLE' });
      return;
    }
    if (!forceRefresh && status === 'BASELINE_UNAVAILABLE' && checkedDay === day) {
      results.push({ creator_name: String(creatorRow[creatorIndex.creator_name]), creator_id: creatorId, status: 'SKIPPED_UNAVAILABLE_TODAY' });
      return;
    }

    const sample = stage6FetchCreatorBaselineSample_(creatorRow, creators.headers, baseline.headers, excludedVideoIds, hotConfig.finalViewsHistoryWindow);
    stage6PersistCreatorBaselineRows_(baseline, creatorRow, creators.headers, sample.rows);
    const summary = stage6SummarizeCreatorBaseline_(sample.rows, baseline.headers, hotConfig.finalViewsHistoryWindow);
    const creatorSheetRow = offset + 2;
    stage6WriteCreatorBaselineSummary_(creators.sheet, creators.headers, creatorSheetRow, creatorId, summary);
    results.push({
      creator_name: String(creatorRow[creatorIndex.creator_name]),
      creator_id: creatorId,
      sample_count: summary.sample_count,
      valid_like_rate_sample_count: summary.valid_like_rate_sample_count,
      baseline_final_views_median: summary.final_views_median,
      baseline_like_rate_median: summary.like_rate_median,
      status: summary.status,
      unavailable_reason: summary.unavailable_reason,
    });
  });

  const result = {
    configured_history_window: hotConfig.finalViewsHistoryWindow,
    processed: results.filter(function (row) { return row.status !== 'SKIPPED_AVAILABLE' && row.status !== 'SKIPPED_UNAVAILABLE_TODAY'; }).length,
    available: results.filter(function (row) { return row.status === 'AVAILABLE'; }).length,
    unavailable: results.filter(function (row) { return row.status === 'BASELINE_UNAVAILABLE'; }).length,
    skipped: results.filter(function (row) { return row.status.indexOf('SKIPPED_') === 0; }).length,
  };
  if (forceRefresh || result.processed > 0) result.creators = results;
  result.hydrated_snapshot_rows = result.processed > 0 ? stage6HydrateColdBaselineSnapshots_() : 0;
  return result;
}

function stage6FetchCreatorBaselineSample_(creatorRow, creatorHeaders, baselineHeaders, excludedVideoIds, historyWindow) {
  const creatorName = String(creatorRow[creatorHeaders.indexOf('creator_name')] || '');
  const creatorId = String(creatorRow[creatorHeaders.indexOf('channel_id')] || '');
  const playlistId = String(creatorRow[creatorHeaders.indexOf('uploads_playlist_id')] || '');
  const byVideoId = new Map();
  const visited = new Set();
  let pageToken = '';

  do {
    const params = { playlistId: playlistId, maxResults: 50 };
    if (pageToken) params.pageToken = pageToken;
    const playlist = stage6CallYoutube_(function () {
      return YouTube.PlaylistItems.list('snippet,contentDetails', params);
    });
    const candidates = [];
    ((playlist && playlist.items) || []).forEach(function (item) {
      const details = item.contentDetails || {};
      const snippet = item.snippet || {};
      const videoId = String(details.videoId || '');
      const publishedAt = String(details.videoPublishedAt || snippet.publishedAt || '');
      if (!videoId || !publishedAt || visited.has(videoId) || excludedVideoIds.has(videoId)) return;
      if (stage6DateMillisOrNull_(publishedAt) === null) return;
      visited.add(videoId);
      candidates.push({ video_id: videoId, published_at: new Date(publishedAt).toISOString(), title: String(snippet.title || '') });
    });

    if (candidates.length) {
      const response = stage6CallYoutube_(function () {
        return YouTube.Videos.list('snippet,statistics,status', {
          id: candidates.map(function (item) { return item.video_id; }).join(','),
          maxResults: candidates.length,
        });
      });
      const byId = new Map(((response && response.items) || []).map(function (video) { return [String(video.id), video]; }));
      candidates.forEach(function (candidate) {
        if (byVideoId.size >= historyWindow) return;
        const video = byId.get(candidate.video_id);
        if (!video || !video.statistics || !video.status || String(video.status.privacyStatus) !== 'public') return;
        const viewCount = stage6BaselineFiniteNumber_(video.statistics.viewCount);
        if (viewCount === null || viewCount < 0) return;
        const likeCount = stage6BaselineFiniteNumber_(video.statistics.likeCount);
        const commentCount = stage6BaselineFiniteNumber_(video.statistics.commentCount);
        const publishedAt = video.snippet && video.snippet.publishedAt
          ? new Date(video.snippet.publishedAt).toISOString() : candidate.published_at;
        const likeRate = viewCount > 0 && likeCount !== null ? likeCount / viewCount : null;
        const values = {
          creator_name: creatorName,
          channel_id: creatorId,
          creator_id: creatorId,
          video_id: candidate.video_id,
          title: String((video.snippet && video.snippet.title) || candidate.title),
          published_at: publishedAt,
          view_count: viewCount,
          like_count: likeCount === null ? '' : likeCount,
          comment_count: commentCount === null ? '' : commentCount,
          like_rate: likeRate === null || !Number.isFinite(likeRate) ? '' : likeRate,
        };
        byVideoId.set(candidate.video_id, baselineHeaders.map(function (header) {
          return Object.prototype.hasOwnProperty.call(values, header) ? values[header] : '';
        }));
      });
    }
    pageToken = playlist && playlist.nextPageToken ? String(playlist.nextPageToken) : '';
  } while (byVideoId.size < historyWindow && pageToken);

  const rows = Array.from(byVideoId.values()).sort(function (left, right) {
    return stage6DateMillisOrNull_(right[baselineHeaders.indexOf('published_at')]) -
      stage6DateMillisOrNull_(left[baselineHeaders.indexOf('published_at')]);
  }).slice(0, historyWindow);
  return { rows: rows };
}

function stage6PersistCreatorBaselineRows_(baseline, creatorRow, creatorHeaders, sampleRows) {
  const channelId = String(creatorRow[creatorHeaders.indexOf('channel_id')] || '');
  const videoIndex = baseline.headers.indexOf('video_id');
  const channelIndex = baseline.headers.indexOf('channel_id');
  const lastRow = baseline.sheet.getLastRow();
  const existingRows = baseline.rows;
  const appended = [];
  sampleRows.forEach(function (sampleRow) {
    const id = String(sampleRow[videoIndex]);
    const existingIndex = existingRows.findIndex(function (row) {
      return String(row[channelIndex]) === channelId && String(row[videoIndex]) === id;
    });
    if (existingIndex >= 0) {
      const merged = existingRows[existingIndex].slice();
      while (merged.length < baseline.headers.length) merged.push('');
      ['creator_name', 'channel_id', 'creator_id', 'video_id', 'title', 'published_at',
        'view_count', 'like_count', 'comment_count', 'like_rate'].forEach(function (header) {
        const index = baseline.headers.indexOf(header);
        if (index >= 0) merged[index] = sampleRow[index];
      });
      baseline.sheet.getRange(existingIndex + 2, 1, 1, baseline.headers.length).setValues([merged]);
      existingRows[existingIndex] = merged;
    } else {
      appended.push(sampleRow);
    }
  });
  if (appended.length) {
    baseline.sheet.getRange(lastRow + 1, 1, appended.length, baseline.headers.length).setValues(appended);
    Array.prototype.push.apply(baseline.rows, appended);
  }
}

function stage6SummarizeCreatorBaseline_(rows, headers, historyWindow) {
  // Row values are already filtered to public videos with a valid view_count.
  const viewIndex = headers.indexOf('view_count');
  const likeRateIndex = headers.indexOf('like_rate');
  if (viewIndex < 0 || likeRateIndex < 0) throw new Error('Baseline sheet is missing view_count or like_rate.');
  const views = rows.map(function (row) { return Number(row[viewIndex]); }).filter(Number.isFinite);
  const likeRates = rows.map(function (row) {
    const value = row[likeRateIndex];
    return value === '' || value === null || value === undefined ? null : Number(value);
  }).filter(function (value) { return value !== null && Number.isFinite(value); });
  const viewMedian = stage6Median_(views);
  const likeMedian = likeRates.length ? stage6Median_(likeRates) : null;
  return {
    sample_count: views.length,
    valid_like_rate_sample_count: likeRates.length,
    final_views_median: viewMedian,
    like_rate_median: likeMedian,
    status: viewMedian === null ? 'BASELINE_UNAVAILABLE' : 'AVAILABLE',
    unavailable_reason: viewMedian === null ? 'NO_PUBLIC_HISTORY_WITH_VALID_VIEWS' : '',
    history_window: historyWindow,
  };
}

function stage6WriteCreatorBaselineSummary_(sheet, headers, rowIndex, creatorId, summary) {
  const values = {
    creator_id: creatorId,
    cold_baseline_status: summary.status,
    cold_baseline_checked_at: new Date().toISOString(),
    cold_baseline_sample_count: summary.sample_count,
    cold_baseline_valid_like_rate_count: summary.valid_like_rate_sample_count,
    cold_baseline_final_views_median: summary.final_views_median === null ? '' : summary.final_views_median,
    cold_baseline_like_rate_median: summary.like_rate_median === null ? '' : summary.like_rate_median,
    cold_baseline_error: summary.unavailable_reason || '',
  };
  Object.keys(values).forEach(function (key) {
    const column = headers.indexOf(key);
    if (column < 0) throw new Error('Creators sheet is missing baseline metadata column ' + key + '.');
    const cell = sheet.getRange(rowIndex, column + 1);
    if (key === 'cold_baseline_checked_at' && typeof cell.setNumberFormat === 'function') cell.setNumberFormat('@');
    cell.setValue(values[key]);
  });
}

function stage6HydrateColdBaselineSnapshots_() {
  const config = stage6ReadConfig_();
  const hotConfig = stage6ReadHotConfig_(config);
  const videos = stage6ReadTable_('Videos');
  const snapshots = stage6ReadTable_('Snapshots');
  const vi = stage6HeaderIndex_(videos.headers, ['video_id', 'channel_id', 'lifecycle_state']);
  const si = stage6HeaderIndex_(snapshots.headers, [
    'video_id', 'channel_id', 'baseline_final_views_median', 'historical_median_like_rate', 'creator_scale_median_views',
  ]);
  let updated = 0;
  videos.rows.forEach(function (video) {
    if (String(video[vi.lifecycle_state]) !== 'WATCH') return;
    const creatorId = String(video[vi.channel_id] || '');
    const videoId = String(video[vi.video_id] || '');
    const completeCount = stage6CurrentCompleteWatchCount_(creatorId, videos, snapshots, videoId, hotConfig.snapshotCheckpoints);
    if (completeCount >= hotConfig.completeWatchThreshold) return;
    const baseline = stage6CreatorBaseline_(creatorId, hotConfig.finalViewsHistoryWindow, videoId);
    if (baseline.final_views_median === null) return;
    snapshots.rows.forEach(function (snapshot, offset) {
      if (String(snapshot[si.video_id]) !== videoId) return;
      snapshots.sheet.getRange(offset + 2, si.baseline_final_views_median + 1).setValue(baseline.final_views_median);
      snapshots.sheet.getRange(offset + 2, si.creator_scale_median_views + 1).setValue(baseline.final_views_median);
      snapshots.sheet.getRange(offset + 2, si.historical_median_like_rate + 1)
        .setValue(baseline.historical_median_like_rate === null ? '' : baseline.historical_median_like_rate);
      updated += 1;
    });
  });
  return updated;
}

function stage6CurrentCompleteWatchCount_(creatorId, videos, snapshots, excludedVideoId, stages) {
  const vi = stage6HeaderIndex_(videos.headers, ['video_id', 'channel_id', 'complete_watch']);
  const si = stage6HeaderIndex_(snapshots.headers, ['video_id', 'channel_id', 'checkpoint_minutes', 'view_count']);
  const completeIds = new Set();
  videos.rows.forEach(function (video) {
    const id = String(video[vi.video_id] || '');
    if (id === excludedVideoId || String(video[vi.channel_id]) !== creatorId) return;
    const seen = new Set();
    snapshots.rows.forEach(function (snapshot) {
      if (String(snapshot[si.video_id]) === id && String(snapshot[si.channel_id]) === creatorId &&
          stage6BaselineFiniteNumber_(snapshot[si.view_count]) !== null) seen.add(Number(snapshot[si.checkpoint_minutes]));
    });
    if (stages.every(function (stage) { return seen.has(Number(stage)); })) completeIds.add(id);
  });
  return completeIds.size;
}

function stage6BaselineIsEnabled_(value) {
  return value === true || String(value).toLowerCase() === 'true';
}

function stage6BaselineFiniteNumber_(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
