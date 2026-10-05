function stage6DiscoverUploads(batchDay, scheduleConfig, now) {
  const creators = stage6ReadTable_('Creators');
  let videos = stage6ReadTable_('Videos');
  if (videos.headers.indexOf('broadcast_type') < 0) {
    stage6EnsureHeaders_(videos.sheet, STAGE6_VIDEO_HEADERS_);
    videos = stage6ReadTable_('Videos');
  }
  const videoIdColumn = videos.headers.indexOf('video_id');
  const lifecycleColumn = videos.headers.indexOf('lifecycle_state');
  if (videoIdColumn < 0 || lifecycleColumn < 0) throw new Error('Videos sheet is missing required columns.');
  const existingIds = new Set(videos.rows.map(function (row) { return String(row[videoIdColumn]); }));
  const config = stage6ReadConfig_();
  const productionConfig = scheduleConfig || config;
  const day = String(batchDay || stage6ZonedDateParts_(new Date(), productionConfig.production_timezone).day);
  const newRecords = [];
  const seenThisRun = new Set(existingIds);
  const channelIndex = creators.headers.indexOf('channel_id');
  const nameIndex = creators.headers.indexOf('creator_name');
  const uploadsIndex = creators.headers.indexOf('uploads_playlist_id');
  const enabledIndex = creators.headers.indexOf('enabled');
  if ([channelIndex, nameIndex, uploadsIndex].some(function (index) { return index < 0; })) {
    throw new Error('Creators sheet is missing a required stable identity column.');
  }

  creators.rows.forEach(function (creatorRow) {
    if (enabledIndex >= 0 && creatorRow[enabledIndex] !== true && String(creatorRow[enabledIndex]).toLowerCase() !== 'true') return;
    const playlistId = String(creatorRow[uploadsIndex] || '').trim();
    const channelId = String(creatorRow[channelIndex] || '').trim();
    if (!playlistId || !channelId) return;
    let pageToken = '';
    let stop = false;
    while (!stop) {
      const params = { playlistId: playlistId, maxResults: 50 };
      if (pageToken) params.pageToken = pageToken;
      const response = stage6CallYoutube_(function () {
        return YouTube.PlaylistItems.list('snippet,contentDetails', params);
      });
      const items = (response && response.items) || [];
      for (let i = 0; i < items.length; i += 1) {
        const item = items[i];
        const details = item.contentDetails || {};
        const snippet = item.snippet || {};
        const videoId = String(details.videoId || '');
        if (!videoId) continue;
        if (seenThisRun.has(videoId)) {
          stop = true;
          break;
        }
        const publishedAt = details.videoPublishedAt || '';
        if (!publishedAt) continue;
        const publishedParts = stage6ZonedDateParts_(publishedAt, productionConfig.production_timezone);
        const startMinute = stage6ClockMinutes_(productionConfig.discovery_window_start, 'discovery_window_start');
        if (publishedParts.day < day || (publishedParts.day === day && publishedParts.hour * 60 + publishedParts.minute < startMinute)) {
          stop = true;
          break;
        }
        if (!stage6IsPublishedInDailyBatch_(publishedAt, day, productionConfig)) continue;
        const record = {
          video_id: videoId,
          channel_id: channelId,
          creator_name: String(creatorRow[nameIndex] || ''),
          title: String(snippet.title || ''),
          description: String(snippet.description || ''),
          published_at: new Date(publishedAt).toISOString(),
          lifecycle_state: 'WATCH',
        };
        newRecords.push(record);
        seenThisRun.add(videoId);
      }
      pageToken = response && response.nextPageToken ? response.nextPageToken : '';
      if (!pageToken || stop) break;
    }
  });

  // Live streams and Premieres only become visible as such through
  // videos.list; re-check waiting Premieres on every Discovery run as well.
  const pendingRows = [];
  videos.rows.forEach(function (row, index) {
    if (String(row[lifecycleColumn]) === 'PREMIERE_PENDING') pendingRows.push({ sheetRow: index + 2, videoId: String(row[videoIdColumn]) });
  });
  const detailIds = newRecords.map(function (record) { return record.video_id; })
    .concat(pendingRows.map(function (pending) { return pending.videoId; }));
  const details = new Map();
  stage6ChunkIds_(detailIds, 50).forEach(function (ids) {
    const response = stage6CallYoutube_(function () {
      return YouTube.Videos.list('snippet,contentDetails,liveStreamingDetails', { id: ids.join(','), maxResults: ids.length });
    });
    ((response && response.items) || []).forEach(function (video) { details.set(String(video.id), video); });
  });
  const nowIso = (now ? new Date(now) : new Date()).toISOString();

  const newRows = newRecords.map(function (record) {
    const decision = stage6ClassifyBroadcast_(details.get(record.video_id), day, productionConfig, nowIso, false);
    record.lifecycle_state = decision.lifecycle_state;
    record.broadcast_type = decision.broadcast_type;
    if (decision.published_at) record.published_at = decision.published_at;
    if (decision.lifecycle_state === 'WATCH' || decision.lifecycle_state === 'PREMIERE_PENDING') {
      const rejected = stage6ContentFilterReason_(details.get(record.video_id), record.title, config);
      if (rejected) {
        record.lifecycle_state = 'CONTENT_REJECTED';
        record.hot_reason = 'CONTENT_FILTER:' + rejected;
      }
    }
    return stage6BuildVideoRow_(videos.headers, record);
  });
  if (newRows.length) {
    videos.sheet.getRange(videos.sheet.getLastRow() + 1, 1, newRows.length, videos.headers.length).setValues(newRows);
  }

  const publishedColumn = videos.headers.indexOf('published_at');
  const titleColumn = videos.headers.indexOf('title');
  const reasonColumn = videos.headers.indexOf('hot_reason');
  let premieresStarted = 0;
  pendingRows.forEach(function (pending) {
    const decision = stage6ClassifyBroadcast_(details.get(pending.videoId), day, productionConfig, nowIso, true);
    if (decision.lifecycle_state === 'PREMIERE_PENDING') return;
    if (decision.published_at && publishedColumn >= 0) {
      videos.sheet.getRange(pending.sheetRow, publishedColumn + 1).setValue(decision.published_at);
    }
    const title = titleColumn >= 0 ? String(videos.rows[pending.sheetRow - 2][titleColumn] || '') : '';
    const rejected = decision.lifecycle_state === 'WATCH'
      ? stage6ContentFilterReason_(details.get(pending.videoId), title, config) : '';
    videos.sheet.getRange(pending.sheetRow, lifecycleColumn + 1).setValue(rejected ? 'CONTENT_REJECTED' : decision.lifecycle_state);
    if (rejected && reasonColumn >= 0) videos.sheet.getRange(pending.sheetRow, reasonColumn + 1).setValue('CONTENT_FILTER:' + rejected);
    if (decision.lifecycle_state === 'WATCH' && !rejected) premieresStarted += 1;
  });

  const watchIds = newRows.filter(function (row) { return String(row[lifecycleColumn]) === 'WATCH'; })
    .map(function (row) { return String(row[videoIdColumn]); });
  return {
    discovered: watchIds.length,
    video_ids: watchIds,
    live_rejected: newRows.filter(function (row) { return String(row[lifecycleColumn]) === 'LIVE_REJECTED'; }).length,
    content_rejected: newRows.filter(function (row) { return String(row[lifecycleColumn]) === 'CONTENT_REJECTED'; }).length,
    premieres_pending: newRows.filter(function (row) { return String(row[lifecycleColumn]) === 'PREMIERE_PENDING'; }).length,
    premieres_started: premieresStarted,
  };
}

// Decides how a newly discovered upload enters the Monitor.
// - Ordinary upload: WATCH.
// - Live stream (upcoming or live with no fixed duration, or an already ended
//   broadcast that was never seen as a Premiere): LIVE_REJECTED, permanently.
//   An ended broadcast cannot be told apart from an ended Premiere, so it is
//   treated as live; Discovery runs every 10 minutes, so Premieres are
//   normally seen while still upcoming.
// - Premiere (upcoming or playing with a fixed duration): PREMIERE_PENDING
//   until it starts, then WATCH with published_at = actual start time so the
//   T+30/60/120 checkpoints measure real audience time. A Premiere that has not
//   started inside the Discovery window, or starts outside it, is
//   PREMIERE_OUT_OF_WINDOW.
// A video missing from videos.list (removed or private) stays WATCH; the
// snapshot path already handles unavailable videos.
function stage6ClassifyBroadcast_(video, batchDay, config, nowIso, knownPremiere) {
  if (!video) return { lifecycle_state: 'WATCH', broadcast_type: 'UNKNOWN' };
  const snippet = video.snippet || {};
  const live = video.liveStreamingDetails || null;
  const content = String(snippet.liveBroadcastContent || 'none');
  if (!live && content === 'none') return { lifecycle_state: 'WATCH', broadcast_type: 'VIDEO' };
  const seconds = stage6IsoDurationSeconds_((video.contentDetails || {}).duration);
  const hasFixedDuration = seconds !== null && seconds > 0;
  const isPremiere = knownPremiere || ((content === 'upcoming' || content === 'live') && hasFixedDuration);
  if (!isPremiere) return { lifecycle_state: 'LIVE_REJECTED', broadcast_type: 'LIVE' };

  const startedAt = live && live.actualStartTime ? new Date(live.actualStartTime).toISOString() : '';
  if (startedAt && content !== 'upcoming') {
    return stage6IsPublishedInDailyBatch_(startedAt, batchDay, config)
      ? { lifecycle_state: 'WATCH', broadcast_type: 'PREMIERE', published_at: startedAt }
      : { lifecycle_state: 'PREMIERE_OUT_OF_WINDOW', broadcast_type: 'PREMIERE' };
  }
  return stage6IsPublishedInDailyBatch_(nowIso, batchDay, config)
    ? { lifecycle_state: 'PREMIERE_PENDING', broadcast_type: 'PREMIERE' }
    : { lifecycle_state: 'PREMIERE_OUT_OF_WINDOW', broadcast_type: 'PREMIERE' };
}

// Content filter. Discovery drops uploads that can never fit before they are
// observed: longer than content_max_duration_minutes (Config, default 20), or a
// title matching one of the rules named in content_title_filters (Config,
// comma-separated names from STAGE6_CONTENT_TITLE_RULES_, or ALL; empty applies
// none, so each installation opts into the rules that match its direction).
// Rejected uploads become CONTENT_REJECTED (inert downstream, like
// LIVE_REJECTED) with hot_reason CONTENT_FILTER:<REASON>. Topic and style
// judgement beyond these rules belongs to the optional Stage 8 Semantic Judge.
const STAGE6_CONTENT_MAX_MINUTES_DEFAULT_ = 20;
const STAGE6_CONTENT_TITLE_RULES_ = [
  ['PODCAST', /\b(podcast|full episode|full interview|full conversation|full talk)\b/i],
  ['KEYNOTE', /\b(keynote|webinar|earnings call|investor day|shareholder meeting|press conference)\b/i],
  ['QA', /(\bq\s?&\s?a\b|\bama\b|answer(ing)? your questions)/i],
  ['LIVESTREAM', /\b(live ?stream|livestream|live replay|replay of)\b/i],
  ['REVIEW', /\b(unboxing|unboxed|review|buyer'?s guide|specs)\b/i],
  ['FINANCE', /\b(stocks?|earnings|crypto|bitcoin|ethereum|invest|investing|investors?)\b/i],
  ['TUTORIAL', /\b(tutorial|how to code|coding|programming|pc build|build a pc)\b/i],
  ['GAMING', /\b(gameplay|gaming|let'?s play)\b/i],
  ['NEWS_ROUNDUP', /\b(news roundup|this week in|weekly recap|tech news)\b/i],
  ['AD', /(#ad\b|\bsponsored\b|\bpaid partnership\b)/i],
];

function stage6ContentFilterReason_(video, title, config) {
  const raw = Number((config || {}).content_max_duration_minutes);
  const maxMinutes = Number.isFinite(raw) && raw > 0 ? raw : STAGE6_CONTENT_MAX_MINUTES_DEFAULT_;
  const seconds = video ? stage6IsoDurationSeconds_((video.contentDetails || {}).duration) : null;
  if (seconds !== null && seconds > maxMinutes * 60) return 'TOO_LONG';
  const active = stage6ContentTitleFilters_((config || {}).content_title_filters);
  const text = String(title || (video && video.snippet && video.snippet.title) || '');
  for (let i = 0; i < STAGE6_CONTENT_TITLE_RULES_.length; i += 1) {
    const rule = STAGE6_CONTENT_TITLE_RULES_[i];
    if (active.has(rule[0]) && rule[1].test(text)) return rule[0];
  }
  return '';
}

function stage6ContentTitleFilters_(value) {
  const names = String(value === null || value === undefined ? '' : value).toUpperCase().split(/[\s,]+/).filter(Boolean);
  if (names.indexOf('ALL') >= 0) return new Set(STAGE6_CONTENT_TITLE_RULES_.map(function (rule) { return rule[0]; }));
  return new Set(names);
}

function stage6IsoDurationSeconds_(value) {
  const match = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/.exec(String(value || ''));
  if (!match) return null;
  return Number(match[1] || 0) * 86400 + Number(match[2] || 0) * 3600 + Number(match[3] || 0) * 60 + Number(match[4] || 0);
}

function stage6BuildVideoRow_(headers, record) {
  const values = {
    video_id: record.video_id,
    channel_id: record.channel_id,
    creator_name: record.creator_name,
    title: record.title,
    description: record.description || '',
    video_url: 'https://www.youtube.com/watch?v=' + encodeURIComponent(record.video_id),
    published_at: record.published_at,
    discovered_at: new Date().toISOString(),
    lifecycle_state: record.lifecycle_state,
    broadcast_type: record.broadcast_type || '',
    hot_reason: record.hot_reason || '',
    complete_watch: false,
  };
  return headers.map(function (header) { return Object.prototype.hasOwnProperty.call(values, header) ? values[header] : ''; });
}

function stage6SelectNewUploads_(items, knownIds, batchDay, config) {
  const existing = knownIds instanceof Set ? knownIds : new Set(knownIds || []);
  const seen = new Set(existing);
  const scheduleConfig = config || {};
  const result = [];
  (items || []).forEach(function (item) {
    const videoId = String(item.videoId || item.video_id || '');
    const publishedAt = item.publishedAt || item.published_at || '';
    if (!videoId || seen.has(videoId) || !publishedAt) return;
    if (!stage6IsPublishedInDailyBatch_(publishedAt, batchDay, scheduleConfig)) return;
    result.push({ video_id: videoId, title: item.title || '', published_at: new Date(publishedAt).toISOString(), lifecycle_state: 'WATCH' });
    seen.add(videoId);
  });
  return result;
}
