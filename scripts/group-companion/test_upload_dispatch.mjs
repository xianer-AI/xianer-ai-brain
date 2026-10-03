import assert from 'node:assert/strict';
import test from 'node:test';
import { buildUploadAgentArgs, prepareReadyTask, once, UPLOAD_TIMEOUT_MS, summarizeUploadFailure } from './upload-dispatch.mjs';

test('upload workers have a finite timeout for recoverable dispatch state', () => {
  assert.equal(UPLOAD_TIMEOUT_MS, 600000);
});

test('worker settlement is idempotent when timeout is followed by exit', () => {
  const events = [];
  const finish = once(value => events.push(value));
  assert.equal(finish('timeout'), true);
  assert.equal(finish('exit'), false);
  assert.deepEqual(events, ['timeout']);
});

test('upload retry uses an explicit Feishu group target', () => {
  const args = buildUploadAgentArgs({
    source: 'om_source',
    confirmation: 'om_confirmation',
    group: 'oc_group',
    task: '/tmp/production-upload-task.json',
  });
  assert.deepEqual(args.slice(0, 6), [
    'agent', '--agent', 'xiaowen-ceo', '--session-key',
    'agent:xiaowen-ceo:production-confirm:om_source:om_confirmation',
    '--message',
  ]);
  assert.ok(args.includes('--deliver'));
  assert.deepEqual(args.slice(-7), [
    '--reply-channel', 'feishu', '--reply-account', 'main',
    '--reply-to', 'chat:oc_group', '--json',
  ]);
});

test('upload task identity is stable for idempotent retries', () => {
  const a = buildUploadAgentArgs({source: 'om_a', confirmation: 'om_c', group: 'oc_group', task: '/tmp/production-upload-task.json'});
  const b = buildUploadAgentArgs({source: 'om_a', confirmation: 'om_c', group: 'oc_group', task: '/tmp/production-upload-task.json'});
  assert.deepEqual(a, b);
});

test('deterministic stderr keeps the real ledger failure instead of code=1', () => {
  const error = summarizeUploadFailure(1, null, {
    stderr: 'Traceback ...\nRuntimeError: 远程台账缺少个人累计区\n',
  });
  assert.match(error, /远程台账缺少个人累计区/);
  assert.match(error, /code=1/);
});

test('preflight waits for inbox persistence but rejects a mismatched task', async () => {
  const params = {source: 'om_source', confirmation: 'om_confirmation', group: 'oc_group'};
  let attempts = 0;
  const waits = [];
  const prepared = await prepareReadyTask(params, async () => {
    attempts += 1;
    if (attempts < 3) {
      const error = new Error('上传任务校验失败：确认消息尚未完成结构化核对');
      error.stderr = error.message;
      throw error;
    }
    return {...params, task: '/tmp/production-upload-task.json'};
  }, async delay => waits.push(delay));
  assert.equal(prepared.task, '/tmp/production-upload-task.json');
  assert.equal(attempts, 3);
  assert.deepEqual(waits, [500, 2000]);

  await assert.rejects(
    prepareReadyTask(params, async () => ({...params, confirmation: 'om_other', task: '/tmp/task.json'}), async () => {}),
    /机器任务与派发批次不一致/,
  );
});
