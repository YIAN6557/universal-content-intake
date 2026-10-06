// The spreadsheet is the bound container (created by `clasp create --type sheets`);
// uciSetup() also records its ID so web-app requests can open it explicitly.
const STAGE6_SPREADSHEET_ID_PROPERTY_ = 'UCI_SPREADSHEET_ID';

function stage6Spreadsheet_() {
  let id = '';
  try {
    id = PropertiesService.getScriptProperties().getProperty(STAGE6_SPREADSHEET_ID_PROPERTY_) || '';
  } catch (error) {
    id = '';
  }
  if (id) return SpreadsheetApp.openById(id);
  const active = SpreadsheetApp.getActiveSpreadsheet();
  if (active) return active;
  throw new Error('UCI_SPREADSHEET_ID is not set. Run uciSetup() once from the Apps Script editor.');
}

const STAGE6_VIDEO_HEADERS_ = [
  'video_id', 'channel_id', 'creator_name', 'title', 'video_url', 'published_at',
  'discovered_at', 'lifecycle_state', 'first_snapshot_at', 'latest_snapshot_at',
  'latest_snapshot_stage_minutes', 'actual_elapsed_seconds', 'view_count',
  'like_count', 'comment_count', 'views_per_hour', 'interval_growth',
  'interval_growth_per_hour', 'acceleration', 'like_rate', 'relative_velocity',
  'creator_scale_median_views', 'hot_phase', 'hot_at', 'hot_reason',
  'candidate_at', 'complete_watch', 'data_hot_at', 'hot_mode', 'hot_checkpoint',
  'description', 'semantic_status', 'semantic_input_hash', 'semantic_decision',
  'semantic_reasons', 'semantic_checked_at', 'semantic_summary', 'semantic_provider',
  'selected_at', 'selection_day', 'selection_rank', 'selection_hot_strength', 'queue_id', 'queued_at',
  'selection_result', 'selection_closed_at', 'selection_reason', 'broadcast_type',
];
const STAGE6_SNAPSHOT_HEADERS_ = [
  'video_id', 'channel_id', 'snapshot_stage_minutes', 'published_at', 'captured_at',
  'actual_elapsed_seconds', 'view_count', 'like_count', 'comment_count',
  'views_per_hour', 'interval_growth', 'interval_growth_per_hour', 'acceleration',
  'like_rate', 'relative_velocity', 'creator_scale_median_views',
  'warm_baseline_sample_count', 'source', 'creator_id', 'checkpoint_minutes',
  'interval_view_growth', 'velocity', 'baseline_final_views_median', 'historical_median_like_rate',
  'historical_same_checkpoint_median_views', 'historical_same_checkpoint_median_like_rate',
];
const STAGE6_CREATOR_BASELINE_HEADERS_ = [
  'creator_id', 'cold_baseline_status', 'cold_baseline_checked_at',
  'cold_baseline_sample_count', 'cold_baseline_valid_like_rate_count',
  'cold_baseline_final_views_median', 'cold_baseline_like_rate_median', 'cold_baseline_error',
];
const STAGE6_BASELINE_IDENTITY_HEADERS_ = ['creator_id'];
// Minutes after rank2_start_cutoff during which the scheduler still does full
// work (late snapshots, cutoff expiry). Outside the active window it only reads
// Config, which keeps a one-minute trigger within the Apps Script daily
// trigger-runtime quota.
const STAGE6_IDLE_GRACE_MINUTES_ = 30;
// Scheduler trigger cadence (Apps Script allows 1, 5, 10, 15 or 30). Ten minutes
// matches the Discovery slots; every schedule check is "at or after" its time,
// so a run up to ten minutes late still performs due work exactly once.
const STAGE6_SCHEDULER_CADENCE_MINUTES_ = 10;

const STAGE6_CONFIG_ROWS_ = [
  ['monitor_interval_minutes', 10, 'Canonical monitor cadence.'],
  ['production_timezone', 'Asia/Shanghai', 'IANA timezone for production intake, selection, and start-cutoff rules.'],
  ['discovery_window_start', '00:00', 'Daily Batch intake window start in production_timezone.'],
  ['discovery_window_end', '08:00', 'Daily Batch intake window end, exclusive, in production_timezone.'],
  ['discovery_cadence_minutes', 10, 'Discovery polling cadence during the configured intake window.'],
  ['final_sweep_time', '08:10', 'One final Discovery sweep after the intake window.'],
  ['snapshot_stage_minutes', '30,60,120', 'WATCH snapshot targets in minutes after published_at.'],
  ['daily_selection_time', '10:00', 'Daily Selection start time in production_timezone.'],
  ['content_max_duration_minutes', 20, 'Discovery rejects uploads longer than this many minutes.'],
  ['content_title_filters', '', 'Title rules Discovery applies (comma-separated: PODCAST, KEYNOTE, QA, LIVESTREAM, REVIEW, FINANCE, TUTORIAL, GAMING, NEWS_ROUNDUP, AD, or ALL); empty applies none.'],
  ['rank2_start_cutoff', '12:00', 'Latest allowed Rank 2 Core start time in production_timezone.'],
  ['processing_concurrency', 1, 'Production Worker concurrency; V1 is serial.'],
  ['last_discovery_slot', '', 'Internal idempotency marker for the last Discovery schedule slot.'],
  ['last_final_sweep_day', '', 'Internal idempotency marker for the final sweep.'],
  ['last_daily_selection_day', '', 'Internal idempotency marker for Daily Selection.'],
  ['warm_baseline_min_complete_watch', 10, 'Warm Baseline activates only after this many complete WATCH videos.'],
  ['monitor_started_at', '', 'Set once during setup; initial Discovery only accepts uploads newer than this timestamp.'],
  ['monitor_status', 'ACTIVE', 'API_FAILED pauses the monitor until explicit resume; PAUSED_BY_USER means automatic monitoring is turned off.'],
  ['last_api_error_class', '', 'Sanitized error class only; raw exception and credentials are never stored.'],
  ['last_api_error_at', '', 'UTC time the monitor was paused.'],
  ['last_discovery_at', '', 'UTC completion time of the latest Discovery pass.'],
  ['cold_start_final_views_history_window', 20, 'Number of latest Creator Baseline videos used for Cold Start medians.'],
  ['cold_start_checkpoint_30_ratio', 0.025, 'Required views are final views median multiplied by this T+30 ratio.'],
  ['cold_start_checkpoint_60_ratio', 0.05, 'Required views are final views median multiplied by this T+60 ratio.'],
  ['cold_start_checkpoint_120_ratio', 0.10, 'Required views are final views median multiplied by this T+120 ratio.'],
  ['cold_start_like_rate_multiplier', 0.80, 'Cold Like Rate gate multiplier against historical median Like Rate.'],
  ['acceleration_min_snapshot_index', 2, 'Acceleration gate is eligible from this one-based Snapshot index.'],
  ['warm_baseline_relative_velocity_threshold', 1.50, 'Warm Relative Velocity minimum.'],
  ['warm_baseline_like_rate_multiplier', 0.80, 'Warm Like Rate gate multiplier against same-checkpoint historical median.'],
  ['normal_after_minutes', 120, 'A failed HOT decision at this configured checkpoint ends WATCH as NORMAL.'],
];

function stage6SetupSheets() {
  const ss = stage6Spreadsheet_();
  const creators = ss.getSheetByName('Creators');
  const baseline = ss.getSheetByName('Baseline');
  if (!creators || !baseline) throw new Error('Stage 6 requires existing Creators and Baseline sheets.');

  let videos = ss.getSheetByName('Videos');
  if (!videos) {
    // The default tab is "Sheet1", or its translation (e.g. 工作表1) on non-English accounts.
    const sheets = typeof ss.getSheets === 'function' ? ss.getSheets() : [];
    const blank = ss.getSheetByName('Sheet1') || sheets.filter(function (sheet) {
      return ['Creators', 'Baseline', 'Videos', 'Snapshots', 'Config', 'Queue', 'QueueClaimRequests'].indexOf(sheet.getName()) < 0;
    })[0] || null;
    if (blank && blank.getLastRow() === 0 && blank.getLastColumn() === 0) {
      blank.setName('Videos');
      videos = blank;
    } else {
      videos = ss.insertSheet('Videos');
    }
  }
  const snapshots = ss.getSheetByName('Snapshots') || ss.insertSheet('Snapshots');
  const config = ss.getSheetByName('Config') || ss.insertSheet('Config');
  stage6EnsureHeaders_(videos, STAGE6_VIDEO_HEADERS_);
  stage6EnsureHeaders_(snapshots, STAGE6_SNAPSHOT_HEADERS_);
  stage6EnsureHeaders_(creators, STAGE6_CREATOR_BASELINE_HEADERS_);
  stage6EnsureHeaders_(baseline, STAGE6_BASELINE_IDENTITY_HEADERS_);
  stage6EnsureConfig_(config);
  const configMap = stage6ReadConfig_();
  if (!configMap.monitor_started_at) stage6SetConfig_('monitor_started_at', new Date().toISOString());
  Logger.log('Stage 6 sheet setup complete. No historical Baseline rows were copied into Videos or Snapshots.');
}

function stage6InstallMonitorTrigger() {
  stage6SetupSheets();
  const all = ScriptApp.getProjectTriggers();
  const scheduler = all.filter(function (trigger) { return trigger.getHandlerFunction() === 'stage6RunScheduler'; });
  all.filter(function (trigger) {
    return trigger.getHandlerFunction() === 'stage6RunMonitor' ||
      (trigger.getHandlerFunction() === 'stage6RunScheduler' && scheduler.indexOf(trigger) > 0);
  }).forEach(function (trigger) { ScriptApp.deleteTrigger(trigger); });
  // Apps Script cannot read an existing trigger's interval, so replace it to
  // guarantee the configured cadence.
  scheduler.forEach(function (trigger) { ScriptApp.deleteTrigger(trigger); });
  ScriptApp.newTrigger('stage6RunScheduler').timeBased().everyMinutes(STAGE6_SCHEDULER_CADENCE_MINUTES_).create();
  Logger.log('Stage 6 scheduler trigger count=1; cadence=' + STAGE6_SCHEDULER_CADENCE_MINUTES_ + ' minutes.');
}

const STAGE6_PAUSED_BY_USER_ = 'PAUSED_BY_USER';

function stage6ResumeMonitor() {
  stage6SetConfig_('monitor_status', 'ACTIVE');
  stage6SetConfig_('consecutive_api_failures', 0);
  stage6SetConfig_('last_api_error_class', '');
  stage6SetConfig_('last_api_error_at', '');
  Logger.log('Stage 6 monitor resumed by operator.');
}

function stage6RunMonitor() {
  // Keep the historical handler callable, but make it obey the current schedule.
  return stage6RunScheduler();
}

function stage6RunScheduler() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    Logger.log('Stage 6 monitor skipped because another execution holds the script lock.');
    return { status: 'SKIPPED_ALREADY_RUNNING' };
  }
  try {
    const config = stage6ReadConfig_();
    if (config.monitor_status === 'API_FAILED') {
      Logger.log('Stage 6 monitor is paused: API_FAILED.');
      return { status: 'API_FAILED', paused: true };
    }
    if (config.monitor_status === STAGE6_PAUSED_BY_USER_) {
      // The owner turned automatic monitoring off (bin/uci settings); nothing runs until they turn it back on.
      return { status: STAGE6_PAUSED_BY_USER_, paused: true };
    }
    try {
      const now = new Date();
      const schedule = stage6ScheduleDecision_(now, config);
      if (!schedule.active_window && !schedule.discovery_slot && !schedule.final_sweep_due && !schedule.selection_due) {
        return { status: 'IDLE', day: schedule.day, timezone: schedule.timezone };
      }
      let discovery = { discovered: 0, skipped: true };
      if (schedule.discovery_slot) {
        discovery = stage6DiscoverUploads(schedule.day, config);
        stage6SetConfig_('last_discovery_slot', schedule.discovery_slot);
        stage6SetConfig_('last_discovery_at', new Date().toISOString());
      } else if (schedule.final_sweep_due) {
        discovery = stage6DiscoverUploads(schedule.day, config);
        stage6SetConfig_('last_final_sweep_day', schedule.day);
        stage6SetConfig_('last_discovery_at', new Date().toISOString());
      }
      const baselineBootstrap = stage6EnsureCreatorBaselines_(config, false);
      const snapshotCount = stage6CaptureDueSnapshots();
      const hot = stage6RunHotEngine();
      const candidateRefresh = stage6RefreshCandidatePool_();
      const expiredRank2 = typeof stage7ExpirePendingRank2_ === 'function' && schedule.rank2_cutoff_reached
        ? stage7ExpirePendingRank2_(true, now.getTime()) : 0;
      let selection = { enabled: false, skipped: true };
      if (schedule.selection_due) {
        selection = stage9RunDailySelection_(true, now);
        if (selection && selection.enabled === true) {
          stage6SetConfig_('last_daily_selection_day', schedule.day);
        }
      }
      const result = {
        status: 'ACTIVE',
        day: schedule.day,
        timezone: schedule.timezone,
        baseline_bootstrap: baselineBootstrap,
        discovery: discovery,
        snapshots: snapshotCount,
        hot: hot,
        candidate_refresh: candidateRefresh,
        expired_rank2: expiredRank2,
        selection: selection,
      };
      if (Number(config.consecutive_api_failures) > 0) stage6SetConfig_('consecutive_api_failures', 0);
      Logger.log(JSON.stringify(result));
      return result;
    } catch (error) {
      if (!error || error.stage6ApiFailure !== true) {
        Logger.log('Stage 6 internal failure; not classified as API_FAILED.');
        throw error;
      }
      const errorClass = error.stage6ApiFailureClass || 'API_OR_APPS_SCRIPT_ERROR';
      Logger.log('YouTube API failure; class=' + errorClass + '; detail=' + (error.stage6ApiFailureDetail || ''));
      const decision = stage6ApiFailureDecision_(errorClass, config.consecutive_api_failures);
      stage6SetConfig_('consecutive_api_failures', decision.failures);
      stage6SetConfig_('last_api_error_class', errorClass);
      stage6SetConfig_('last_api_error_at', new Date().toISOString());
      if (!decision.pause) {
        // Retry on the next 10-minute run; schedule markers were not advanced.
        Logger.log('API failure ' + decision.failures + '/' + STAGE6_API_FAILURE_PAUSE_AFTER_ + '; monitor stays ACTIVE.');
        return { status: 'API_RETRY', paused: false, error_class: errorClass, failures: decision.failures };
      }
      stage6SetConfig_('monitor_status', 'API_FAILED');
      Logger.log('API_FAILED; monitor paused; class=' + errorClass);
      return { status: 'API_FAILED', paused: true, error_class: errorClass };
    }
  } catch (error) {
    throw error;
  } finally {
    lock.releaseLock();
  }
}

function stage6ScheduleDecision_(now, config) {
  const values = config || {};
  const timeZone = String(values.production_timezone || '').trim();
  if (!timeZone) throw new Error('Production schedule Config invalid: production_timezone is required.');
  const parts = stage6ZonedDateParts_(now, timeZone);
  const start = stage6ClockMinutes_(values.discovery_window_start, 'discovery_window_start');
  const end = stage6ClockMinutes_(values.discovery_window_end, 'discovery_window_end');
  const sweep = stage6ClockMinutes_(values.final_sweep_time, 'final_sweep_time');
  const selection = stage6ClockMinutes_(values.daily_selection_time, 'daily_selection_time');
  const rank2 = stage6ClockMinutes_(values.rank2_start_cutoff, 'rank2_start_cutoff');
  const cadence = Number(values.discovery_cadence_minutes);
  if (!Number.isInteger(cadence) || cadence < 1 || end <= start || sweep < end || selection <= sweep || rank2 <= selection) {
    throw new Error('Production schedule Config is invalid.');
  }
  const currentMinute = parts.hour * 60 + parts.minute;
  let discoverySlot = '';
  if (currentMinute >= start && currentMinute < end) {
    const slotMinute = start + Math.floor((currentMinute - start) / cadence) * cadence;
    const clock = String(Math.floor(slotMinute / 60)).padStart(2, '0') + ':' + String(slotMinute % 60).padStart(2, '0');
    const candidateSlot = parts.day + ' ' + clock;
    if (stage6NormalizeDateMarker_(values.last_discovery_slot, true) !== candidateSlot) discoverySlot = candidateSlot;
  }
  const lastSweepDay = stage6NormalizeDateMarker_(values.last_final_sweep_day, false);
  // Apps Script time triggers are best-effort: a run can land at 04:11 or be
  // skipped. Catch up on the first run at/after the sweep time (the day marker
  // keeps it single), but never after Selection has closed the day's batch.
  const finalSweepDue = currentMinute >= sweep && currentMinute < selection && lastSweepDay !== parts.day;
  const lastSelectionDay = stage6NormalizeDateMarker_(values.last_daily_selection_day, false);
  const selectionDue = currentMinute >= selection && lastSelectionDay !== parts.day;
  return {
    timezone: timeZone,
    day: parts.day,
    discovery_slot: discoverySlot || null,
    final_sweep_due: finalSweepDue,
    selection_due: selectionDue,
    rank2_cutoff_reached: currentMinute >= rank2,
    // Discovery, WATCH snapshots, HOT, Selection and the Rank 2 cutoff all fall
    // inside [discovery_window_start, rank2_start_cutoff + grace).
    active_window: currentMinute >= start && currentMinute < Math.min(24 * 60, rank2 + STAGE6_IDLE_GRACE_MINUTES_),
  };
}

function stage6NormalizeDateMarker_(value, includeTime) {
  if (value === null || value === undefined || value === '') return '';
  const exact = String(value).trim();
  if (!includeTime && /^\d{4}-\d{2}-\d{2}$/.test(exact)) return exact;
  if (includeTime) {
    const match = exact.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})$/);
    if (match) return match[1] + ' ' + match[2];
  }

  let date = null;
  if (value instanceof Date) {
    date = value;
  } else if (typeof value === 'number' && Number.isFinite(value)) {
    // Google Sheets date/time serials use 1899-12-30 as day zero.
    date = new Date(Math.round((value - 25569) * 86400000));
  } else {
    const parsed = new Date(exact);
    if (Number.isFinite(parsed.getTime())) date = parsed;
  }
  if (!date || !Number.isFinite(date.getTime())) return '';
  // Legacy Config date markers are Sheets serials. Their date and clock are
  // already encoded as UTC wall-clock fields for this production sheet, so
  // do not depend on getSpreadsheetTimeZone(), which may return an empty
  // string in the Apps Script runtime even when Sheets metadata has a zone.
  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, '0');
  const day = String(date.getUTCDate()).padStart(2, '0');
  const datePart = year + '-' + month + '-' + day;
  if (!includeTime) return datePart;
  const hour = String(date.getUTCHours()).padStart(2, '0');
  const minute = String(date.getUTCMinutes()).padStart(2, '0');
  return datePart + ' ' + hour + ':' + minute;
}

function stage6ZonedDateParts_(value, timeZone) {
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error('Production schedule time is invalid.');
  const formatted = Utilities.formatDate(date, timeZone, 'yyyy-MM-dd|HH:mm');
  const match = String(formatted).match(/^(\d{4}-\d{2}-\d{2})\|(\d{2}):(\d{2})$/);
  if (!match) throw new Error('Production timezone could not format the current time.');
  return { day: match[1], hour: Number(match[2]), minute: Number(match[3]) };
}

function stage6ClockMinutes_(value, key) {
  let clockValue = value;
  if (value instanceof Date) {
    const spreadsheet = stage6Spreadsheet_();
    const spreadsheetTimeZone = String(spreadsheet.getSpreadsheetTimeZone() || '').trim();
    if (!spreadsheetTimeZone) {
      throw new Error('Production schedule Config invalid: spreadsheet timezone is required to read ' + key + '.');
    }
    clockValue = Utilities.formatDate(value, spreadsheetTimeZone, 'HH:mm');
  } else if (typeof value === 'number') {
    if (!Number.isFinite(value) || value < 0 || value >= 1) {
      throw new Error('Production schedule Config invalid: ' + key + ' must be a time of day.');
    }
    const exactMinutes = value * 24 * 60;
    const roundedMinutes = Math.round(exactMinutes);
    if (Math.abs(exactMinutes - roundedMinutes) > 0.001) {
      throw new Error('Production schedule Config invalid: ' + key + ' must resolve to a whole minute.');
    }
    return roundedMinutes;
  }
  const match = String(clockValue === null || clockValue === undefined ? '' : clockValue).trim().match(/^(\d{2}):(\d{2})$/);
  if (!match || Number(match[1]) > 23 || Number(match[2]) > 59) {
    throw new Error('Production schedule Config invalid: ' + key + ' must be HH:mm or a Sheets time value.');
  }
  return Number(match[1]) * 60 + Number(match[2]);
}

function stage6IsPublishedInDailyBatch_(publishedAt, batchDay, config) {
  const value = config || {};
  const timeZone = String(value.production_timezone || '').trim();
  if (!timeZone || !batchDay) return false;
  const parts = stage6ZonedDateParts_(publishedAt, timeZone);
  const minute = parts.hour * 60 + parts.minute;
  const start = stage6ClockMinutes_(value.discovery_window_start, 'discovery_window_start');
  const end = stage6ClockMinutes_(value.discovery_window_end, 'discovery_window_end');
  return parts.day === String(batchDay) && minute >= start && minute < end;
}

function stage6EnsureHeaders_(sheet, headers) {
  const required = headers.map(function (header) { return String(header); });
  const sheetName = stage6SheetName_(sheet);
  stage6ValidateUniqueHeaders_(required, sheetName);
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow === 0 && lastColumn === 0) {
    sheet.getRange(1, 1, 1, required.length).setValues([required]);
    sheet.setFrozenRows(1);
    return;
  }
  if (lastColumn === 0) {
    throw new Error('Missing header row in sheet ' + sheetName + '.');
  }
  const existing = sheet.getRange(1, 1, 1, lastColumn).getValues()[0].map(function (header) {
    return header === null || header === undefined ? '' : String(header);
  });
  stage6ValidateUniqueHeaders_(existing, sheetName);

  const requiredIndex = new Map();
  required.forEach(function (header, index) { requiredIndex.set(header, index); });
  let previousIndex = -1;
  existing.forEach(function (header) {
    if (!requiredIndex.has(header)) return;
    const index = requiredIndex.get(header);
    if (index <= previousIndex) throw new Error('Unexpected header order in sheet ' + sheetName + '.');
    previousIndex = index;
  });

  const existingSet = new Set(existing.filter(function (header) { return header !== ''; }));
  const missing = required.filter(function (header) { return !existingSet.has(header); });
  if (missing.length) {
    // Append after the sheet's physical last column so existing data in unnamed columns is never overwritten.
    sheet.getRange(1, lastColumn + 1, 1, missing.length).setValues([missing]);
  }
}

function stage6SheetName_(sheet) {
  return sheet && typeof sheet.getName === 'function' ? sheet.getName() : 'unknown';
}

function stage6ValidateUniqueHeaders_(headers, sheetName) {
  const seen = new Set();
  headers.forEach(function (value) {
    const header = String(value || '').trim();
    if (!header) return;
    if (seen.has(header)) throw new Error('Duplicate header "' + header + '" in sheet ' + sheetName + '.');
    seen.add(header);
  });
}

function stage6EnsureConfig_(sheet) {
  if (sheet.getLastRow() === 0 && sheet.getLastColumn() === 0) {
    sheet.getRange(1, 1, 1, 3).setValues([['key', 'value', 'description']]);
    sheet.setFrozenRows(1);
  } else {
    if (sheet.getLastColumn() < 3) throw new Error('Unexpected header schema in sheet Config.');
    const headerValues = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    if (headerValues.slice(0, 3).join('|') !== 'key|value|description') {
      throw new Error('Unexpected header schema in sheet Config.');
    }
    stage6ValidateUniqueHeaders_(headerValues, 'Config');
  }
  const existing = stage6ReadConfig_();
  const missing = stage6ConfigDefaults_().filter(function (row) {
    return !Object.prototype.hasOwnProperty.call(existing, row[0]);
  });
  if (missing.length) {
    const firstRow = sheet.getLastRow() + 1;
    // Keep clock defaults such as "08:00" as text; Sheets would otherwise store
    // a time value whose reading depends on the spreadsheet timezone.
    missing.forEach(function (row, offset) {
      if (/^\d{2}:\d{2}$/.test(String(row[1]))) {
        const cell = sheet.getRange(firstRow + offset, 2);
        if (typeof cell.setNumberFormat === 'function') cell.setNumberFormat('@');
      }
    });
    sheet.getRange(firstRow, 1, missing.length, 3).setValues(missing);
  }
  stage6MigrateLegacyScheduleConfig_(sheet);
}

function stage6MigrateLegacyScheduleConfig_(sheet) {
  const table = stage6ReadTable_('Config');
  const keyIndex = table.headers.indexOf('key');
  const valueIndex = table.headers.indexOf('value');
  const migrations = [
    { key: 'snapshot_stage_minutes', legacy: '30,60,120,180,360', current: '30,60,120' },
    { key: 'normal_after_minutes', legacy: '360', current: 120 },
  ];
  migrations.forEach(function (migration) {
    const rowIndex = table.rows.findIndex(function (row) {
      return String(row[keyIndex] || '').trim() === migration.key;
    });
    if (rowIndex < 0) return;
    const current = String(table.rows[rowIndex][valueIndex] === null || table.rows[rowIndex][valueIndex] === undefined
      ? '' : table.rows[rowIndex][valueIndex]).trim();
    if (current !== migration.legacy) return;
    sheet.getRange(rowIndex + 2, valueIndex + 1).setValue(migration.current);
  });
}

function stage6ConfigDefaults_() {
  const stage6Rows = STAGE6_CONFIG_ROWS_.map(function (row) { return row.slice(); });
  const stage8Rows = typeof STAGE8_CONFIG_ROWS_ === 'undefined' ? [] : STAGE8_CONFIG_ROWS_.map(function (row) { return row.slice(); });
  const stage9Rows = typeof STAGE9_SELECTION_CONFIG_ROWS_ === 'undefined'
    ? [] : STAGE9_SELECTION_CONFIG_ROWS_.map(function (row) { return row.slice(); });
  return stage6Rows.concat(stage8Rows, stage9Rows);
}

function stage6ReadTable_(name) {
  const sheet = stage6Spreadsheet_().getSheetByName(name);
  if (!sheet) throw new Error('Missing required sheet ' + name + '.');
  const values = sheet.getDataRange().getValues();
  if (!values.length) return { sheet: sheet, headers: [], rows: [] };
  return { sheet: sheet, headers: values[0].map(String), rows: values.slice(1).filter(function (row) { return row.some(function (value) { return value !== ''; }); }) };
}

function stage6ReadConfig_() {
  const table = stage6ReadTable_('Config');
  const keyIndex = table.headers.indexOf('key');
  const valueIndex = table.headers.indexOf('value');
  const result = {};
  if (keyIndex < 0 || valueIndex < 0) return result;
  table.rows.forEach(function (row) {
    const key = String(row[keyIndex] === null || row[keyIndex] === undefined ? '' : row[keyIndex]).trim();
    if (!key) return;
    if (Object.prototype.hasOwnProperty.call(result, key)) throw new Error('Duplicate Config key "' + key + '".');
    result[key] = row[valueIndex];
  });
  return result;
}

function stage6SetConfig_(key, value) {
  const table = stage6ReadTable_('Config');
  const keyIndex = table.headers.indexOf('key');
  const valueIndex = table.headers.indexOf('value');
  const sheet = table.sheet;
  for (let i = 0; i < table.rows.length; i += 1) {
    if (String(table.rows[i][keyIndex]) === key) {
      const range = sheet.getRange(i + 2, valueIndex + 1);
      if (['last_discovery_slot', 'last_final_sweep_day', 'last_daily_selection_day'].indexOf(key) >= 0 &&
          typeof range.setNumberFormat === 'function') range.setNumberFormat('@');
      range.setValue(value);
      return;
    }
  }
  sheet.appendRow([key, value, 'Stage 6 runtime value.']);
}

function stage6ClassifyApiFailure_(error) {
  const text = String(error && (error.message || error) || '').toLowerCase();
  if (/quota|rate.?limit|daily.?limit|too many requests/.test(text)) return 'QUOTA_OR_RATE_LIMIT';
  if (/permission|forbidden|unauthorized|access.?denied|\b401\b|\b403\b/.test(text)) return 'PERMISSION_OR_AUTH';
  if (/timeout|timed out|network|temporar|unavailable|backend|server error|internal error|try again|empty response|\b50[0-4]\b/.test(text)) return 'TRANSIENT_API_OR_SERVICE';
  return 'API_OR_APPS_SCRIPT_ERROR';
}

// Incident 2026-10-05: one unclassified YouTube error at 00:25 paused the
// monitor for the rest of the night. Quota and permission failures still pause
// at once (retrying cannot help); any other failure is retried on the next
// 10-minute run and only pauses after this many consecutive failing runs.
const STAGE6_API_FAILURE_PAUSE_AFTER_ = 3;

function stage6ApiFailureDecision_(errorClass, previousFailures) {
  const previous = Number(previousFailures);
  const failures = (Number.isFinite(previous) && previous > 0 ? Math.floor(previous) : 0) + 1;
  const immediate = errorClass === 'QUOTA_OR_RATE_LIMIT' || errorClass === 'PERMISSION_OR_AUTH';
  return { failures: failures, pause: immediate || failures >= STAGE6_API_FAILURE_PAUSE_AFTER_ };
}

function stage6ChunkIds_(ids, size) {
  const chunks = [];
  for (let i = 0; i < ids.length; i += size) chunks.push(ids.slice(i, i + size));
  return chunks;
}
