// First-run setup (see SETUP.md). After `clasp push`, run uciSetup() once from
// the Apps Script editor: it needs the owner's one-time authorization and is
// idempotent. Everything after that is driven by `bin/uci-setup` through the
// signed setup_* Web App actions below, so no further browser work is needed.

const UCI_CREATOR_HEADERS_ = [
  'creator_name', 'channel_id', 'channel_url', 'uploads_playlist_id', 'enabled', 'priority',
  'trusted_creator', 'notes', 'creator_id',
];
const UCI_BASELINE_HEADERS_ = [
  'creator_name', 'channel_id', 'creator_id', 'video_id', 'title', 'published_at',
  'view_count', 'like_count', 'comment_count', 'like_rate',
];
// Settings an installation may change through setup_config_set. Anything else
// (idempotency markers, monitor status, internal thresholds) stays code-owned.
const UCI_SETUP_EDITABLE_KEYS_ = {
  production_timezone: 'timezone',
  discovery_window_start: 'clock',
  discovery_window_end: 'clock',
  final_sweep_time: 'clock',
  daily_selection_time: 'clock',
  rank2_start_cutoff: 'clock',
  daily_selection_max: 'selection_max',
  daily_selection_enabled: 'boolean',
  cold_start_checkpoint_30_ratio: 'ratio',
  cold_start_checkpoint_60_ratio: 'ratio',
  cold_start_checkpoint_120_ratio: 'ratio',
  cold_start_like_rate_multiplier: 'ratio',
  warm_baseline_relative_velocity_threshold: 'ratio',
  warm_baseline_like_rate_multiplier: 'ratio',
  content_max_duration_minutes: 'minutes',
  content_title_filters: 'filters',
  semantic_judge_enabled: 'boolean',
  semantic_gemini_model: 'model',
  semantic_editorial_brief: 'text',
};
const UCI_CHANNEL_ID_PATTERN_ = /^UC[A-Za-z0-9_-]{22}$/;

function uciSetup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('Run uciSetup() from the Apps Script project bound to your spreadsheet (clasp create --type sheets).');
  const properties = PropertiesService.getScriptProperties();
  properties.setProperty(STAGE6_SPREADSHEET_ID_PROPERTY_, ss.getId());
  // A brand-new installation selects nothing until `bin/uci-setup verify` passes.
  const freshInstall = !ss.getSheetByName('Config');
  const creators = ss.getSheetByName('Creators') || ss.insertSheet('Creators');
  const baseline = ss.getSheetByName('Baseline') || ss.insertSheet('Baseline');
  stage6EnsureHeaders_(creators, UCI_CREATOR_HEADERS_);
  stage6EnsureHeaders_(baseline, UCI_BASELINE_HEADERS_);
  stage6SetupSheets();
  stage7SetupQueue_();
  stage6InstallMonitorTrigger();
  if (freshInstall) uciSetupWriteConfig_('daily_selection_enabled', false);
  const report = uciSetupInspect_();
  Logger.log('Universal Content Intake setup complete: ' + JSON.stringify({
    spreadsheet_id: report.spreadsheet_id,
    sheets: report.sheets,
    scheduler_triggers: report.scheduler_triggers,
    properties: report.properties,
  }));
  if (!report.properties.UCI_QUEUE_HMAC_SECRET) {
    Logger.log('Next: run `bin/uci-setup` on your Mac; it generates the shared secret and tells you where to paste it.');
  }
  return report;
}

function uciSetupInspect_() {
  const ss = stage6Spreadsheet_();
  const properties = PropertiesService.getScriptProperties();
  const sheets = ['Creators', 'Baseline', 'Videos', 'Snapshots', 'Config', 'Queue', 'QueueClaimRequests'];
  const present = {};
  sheets.forEach(function (name) { present[name] = !!ss.getSheetByName(name); });
  const config = present.Config ? stage6ReadConfig_() : {};
  const settings = {};
  Object.keys(UCI_SETUP_EDITABLE_KEYS_).concat(['monitor_status', 'discovery_cadence_minutes']).forEach(function (key) {
    settings[key] = Object.prototype.hasOwnProperty.call(config, key) ? stage7StatusValue_(config[key]) : null;
  });
  let creators = [];
  if (present.Creators) {
    const table = stage6ReadTable_('Creators');
    creators = table.rows.map(function (row) {
      const value = {};
      ['creator_name', 'channel_id', 'enabled', 'trusted_creator', 'notes', 'cold_baseline_status',
        'cold_baseline_final_views_median'].forEach(function (header) {
        const index = table.headers.indexOf(header);
        value[header] = index >= 0 ? stage7StatusValue_(row[index]) : null;
      });
      return value;
    });
  }
  const triggers = ScriptApp.getProjectTriggers().filter(function (trigger) {
    return trigger.getHandlerFunction() === 'stage6RunScheduler';
  }).length;
  return {
    spreadsheet_id: properties.getProperty(STAGE6_SPREADSHEET_ID_PROPERTY_) || '',
    spreadsheet_url: ss.getUrl ? ss.getUrl() : '',
    sheets: present,
    settings: settings,
    creators: creators,
    scheduler_triggers: triggers,
    properties: {
      UCI_QUEUE_HMAC_SECRET: !!properties.getProperty(STAGE7_QUEUE_HMAC_PROPERTY_),
      UCI_GEMINI_API_KEY: !!properties.getProperty(STAGE8_GEMINI_API_KEY_PROPERTY_),
    },
  };
}

function uciSetupConfigSet_(request) {
  const values = request && request.values;
  if (!values || typeof values !== 'object' || Array.isArray(values) || !Object.keys(values).length) {
    throw stage7QueueError_('INVALID_REQUEST', 'values must be a non-empty object.');
  }
  const normalized = {};
  Object.keys(values).forEach(function (key) {
    const kind = UCI_SETUP_EDITABLE_KEYS_[key];
    if (!kind) throw stage7QueueError_('INVALID_REQUEST', 'Setting ' + key + ' cannot be changed through setup.');
    normalized[key] = uciSetupNormalizeValue_(key, kind, values[key]);
  });
  // Validate the resulting schedule (all times within one production day) before writing.
  const merged = Object.assign({}, stage6ReadConfig_(), normalized);
  uciSetupValidateSchedule_(merged);
  try {
    stage6ScheduleDecision_(new Date(), merged);
  } catch (error) {
    throw stage7QueueError_('CONFIG_INVALID', String(error && error.message || error));
  }
  Object.keys(normalized).forEach(function (key) { uciSetupWriteConfig_(key, normalized[key]); });
  return { updated: Object.keys(normalized).sort(), settings: uciSetupInspect_().settings };
}

function uciSetupValidateSchedule_(config) {
  const minutes = {};
  ['discovery_window_start', 'discovery_window_end', 'final_sweep_time', 'daily_selection_time', 'rank2_start_cutoff']
    .forEach(function (key) { minutes[key] = stage6ClockMinutes_(config[key], key); });
  const rules = [
    [minutes.discovery_window_end > minutes.discovery_window_start, 'discovery_window_end must be after discovery_window_start (same day).'],
    [minutes.final_sweep_time >= minutes.discovery_window_end, 'final_sweep_time must be at or after discovery_window_end.'],
    [minutes.daily_selection_time >= minutes.discovery_window_end + 120,
      'daily_selection_time must be at least 120 minutes after discovery_window_end so the T+120 checkpoint exists.'],
    [minutes.daily_selection_time > minutes.final_sweep_time, 'daily_selection_time must be after final_sweep_time.'],
    [minutes.rank2_start_cutoff > minutes.daily_selection_time, 'rank2_start_cutoff must be after daily_selection_time.'],
  ];
  rules.forEach(function (rule) { if (!rule[0]) throw stage7QueueError_('CONFIG_INVALID', rule[1]); });
}

function uciSetupNormalizeValue_(key, kind, value) {
  const fail = function (why) { throw stage7QueueError_('INVALID_REQUEST', key + ': ' + why); };
  if (kind === 'clock') {
    const text = String(value === null || value === undefined ? '' : value).trim();
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(text)) fail('use HH:mm');
    return text;
  }
  if (kind === 'timezone') {
    const text = String(value || '').trim();
    try {
      if (!text) throw new Error('empty');
      Utilities.formatDate(new Date(), text, 'HH:mm');
    } catch (error) {
      fail('unknown IANA timezone');
    }
    return text;
  }
  if (kind === 'boolean') {
    if (value === true || value === false) return value;
    fail('use true or false');
  }
  const number = Number(value);
  if (kind === 'selection_max') {
    if (!Number.isInteger(number) || number < 0 || number > 2) fail('use 0, 1 or 2');
    return number;
  }
  if (kind === 'ratio') {
    if (!Number.isFinite(number) || number <= 0 || number > 100) fail('use a positive number');
    return number;
  }
  if (kind === 'minutes') {
    if (!Number.isInteger(number) || number < 1 || number > 600) fail('use whole minutes between 1 and 600');
    return number;
  }
  if (kind === 'model') {
    const text = String(value || '').trim();
    if (text && !/^[A-Za-z0-9._-]+$/.test(text)) fail('invalid model name');
    return text;
  }
  if (kind === 'filters') {
    const names = String(value === null || value === undefined ? '' : value).toUpperCase().split(/[\s,]+/).filter(Boolean);
    const known = STAGE6_CONTENT_TITLE_RULES_.map(function (rule) { return rule[0]; }).concat(['ALL']);
    const unknown = names.filter(function (name) { return known.indexOf(name) < 0; });
    if (unknown.length) fail('unknown rule ' + unknown.join(', ') + '; use ' + known.join(', '));
    return names.join(',');
  }
  if (kind === 'text') {
    const text = String(value === null || value === undefined ? '' : value).trim();
    if (text.length > 20000) fail('keep it under 20000 characters');
    return text;
  }
  fail('unsupported');
}

function uciSetupWriteConfig_(key, value) {
  const table = stage6ReadTable_('Config');
  const keyIndex = table.headers.indexOf('key');
  const valueIndex = table.headers.indexOf('value');
  const rowOffset = table.rows.findIndex(function (row) { return String(row[keyIndex]) === key; });
  const textual = typeof value === 'string';
  if (rowOffset < 0) {
    table.sheet.appendRow([key, textual ? "'" + value : value, 'Set by uci-setup.']);
    return;
  }
  const range = table.sheet.getRange(rowOffset + 2, valueIndex + 1);
  // Keep clocks and names as text so Sheets does not turn "08:00" into a time value.
  if (textual && typeof range.setNumberFormat === 'function') range.setNumberFormat('@');
  range.setValue(value);
}

function uciSetupCreatorsUpsert_(request) {
  const creators = request && request.creators;
  if (!Array.isArray(creators) || !creators.length || creators.length > 200) {
    throw stage7QueueError_('INVALID_REQUEST', 'creators must be a non-empty list (at most 200).');
  }
  const table = stage6ReadTable_('Creators');
  const index = stage6HeaderIndex_(table.headers, UCI_CREATOR_HEADERS_);
  const added = [];
  const updated = [];
  creators.forEach(function (item) {
    const channelId = String(item && item.channel_id || '').trim();
    const name = String(item && item.creator_name || '').trim();
    if (!UCI_CHANNEL_ID_PATTERN_.test(channelId)) throw stage7QueueError_('INVALID_REQUEST', 'Invalid channel_id: ' + channelId);
    if (!name) throw stage7QueueError_('INVALID_REQUEST', 'creator_name is required for ' + channelId);
    const enabled = item.enabled === undefined ? true : item.enabled === true;
    const trusted = item.trusted_creator === true;
    const notes = String(item.notes || '').slice(0, 500);
    const offset = table.rows.findIndex(function (row) { return String(row[index.channel_id]) === channelId; });
    if (offset >= 0) {
      const sheetRow = offset + 2;
      table.sheet.getRange(sheetRow, index.creator_name + 1).setValue(name);
      table.sheet.getRange(sheetRow, index.enabled + 1).setValue(enabled);
      table.sheet.getRange(sheetRow, index.trusted_creator + 1).setValue(trusted);
      if (item.notes !== undefined) table.sheet.getRange(sheetRow, index.notes + 1).setValue(notes);
      updated.push(channelId);
      return;
    }
    const values = {
      creator_name: name,
      channel_id: channelId,
      channel_url: 'https://www.youtube.com/channel/' + channelId,
      uploads_playlist_id: 'UU' + channelId.slice(2),
      enabled: enabled,
      priority: 1,
      trusted_creator: trusted,
      notes: notes,
      creator_id: channelId,
    };
    const row = table.headers.map(function (header) { return Object.prototype.hasOwnProperty.call(values, header) ? values[header] : ''; });
    table.sheet.appendRow(row);
    table.rows.push(row);
    added.push(channelId);
  });
  return { added: added, updated: updated, creators: uciSetupInspect_().creators };
}
