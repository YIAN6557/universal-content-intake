const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '..', 'cloud', 'apps-script');

function load(tables) {
  const context = vm.createContext({ console, Date, Math, Number, String, Array, Object, JSON, Error, Map, Set });
  for (const file of ['queue_contract.gs', 'queue.gs']) {
    const fullPath = path.join(scriptPath, file);
    vm.runInContext(fs.readFileSync(fullPath, 'utf8'), context, { filename: fullPath });
  }
  context.stage6ReadTable_ = (name) => {
    if (!tables[name]) throw new Error('Missing sheet ' + name);
    return tables[name];
  };
  return context;
}

const headers = ['video_id', 'title', 'creator_name', 'discovered_at', 'data_hot_at', 'view_count',
  'like_rate', 'views_per_hour', 'hot_reason', 'hot_mode', 'hot_checkpoint', 'semantic_summary'];

test('claim response carries HOT evidence from the Videos row for info.md', () => {
  const context = load({ Videos: { headers, rows: [
    ['other', 'Other', 'X', '', '', 1, 0.1, 1, '', '', '', ''],
    ['vid-1', 'Title', 'Creator', new Date('2026-09-28T00:10:00Z'), new Date('2026-09-28T01:10:00Z'),
      54321, 0.042, 18000, 'cold_views_like_rate', 'cold', 60, 'ignored'],
  ] } });
  const response = context.stage7QueueClaimResponse_({ queue_id: 'q', video_id: 'vid-1', attempts: 1 });
  assert.deepEqual(JSON.parse(JSON.stringify(response.monitor)), {
    video_id: 'vid-1', title: 'Title', creator_name: 'Creator',
    first_seen: '2026-09-28T00:10:00.000Z', hot_at: '2026-09-28T01:10:00.000Z',
    views_at_hot: 54321, like_rate: 0.042, velocity: 18000,
    hot_reason: 'cold_views_like_rate', hot_mode: 'cold', hot_checkpoint: 60,
  });
});

test('missing Videos sheet or row never blocks a claim response', () => {
  assert.equal(load({}).stage7QueueClaimResponse_({ queue_id: 'q', video_id: 'vid-1' }).monitor, null);
  const context = load({ Videos: { headers, rows: [] } });
  assert.equal(context.stage7QueueClaimResponse_({ queue_id: 'q', video_id: 'vid-1' }).monitor, null);
});
