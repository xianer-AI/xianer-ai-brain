import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

// Load the real gate with all process-launch boundaries replaced by throwing
// stubs. This stays portable in CI and cannot touch a local OpenClaw runtime.
const source = (await readFile(new URL('./openclaw-gate.mjs', import.meta.url), 'utf8'))
  .replace(/^import \{ execFile \} from 'node:child_process';$/m,
    "const execFile=()=>{throw new Error('process launch forbidden in display test')};")
  .replace(/^import \{ ([^}]+) \} from 'file:[^']+\/upload-dispatch\.mjs';$/m,
    (_, names) => names.split(',').map(name =>
      `const ${name.trim()}=()=>{throw new Error('upload forbidden in display test')};`).join('\n'));
assert.ok(!source.includes("from 'file:"), 'the test must not import the live runtime');
const { cardAction, looksLikeReport } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const group = 'oc_1f8587b1bcde12a0d1bb6053ab2b748a';
const ctx = { channelId: 'feishu', accountId: 'main', conversationId: group };
const event = (action = 'modify') => ({
  content: `生产核对 ${action} abcdef123456`,
  messageId: 'card-action-display-fixture', senderId: 'ou_isolated',
});

test('a correction uses only the authenticated display note and keeps callback binding', async () => {
  for (const note of ['数量口径：**下机翻袜产量**（已翻好数量）', '数量口径：**烤边产量**']) {
    const incoming = event();
    const before = structuredClone(incoming);
    let calls = 0;
    const result = await cardAction(incoming, ctx, async payload => {
      calls += 1;
      assert.deepEqual(payload, {
        text: incoming.content, id: incoming.messageId, sender: incoming.senderId, group,
      });
      return { action: 'modify', source: 'om_original', token: 'abcdef123456', quantity_note: note };
    });
    assert.equal(calls, 1);
    assert.deepEqual(incoming, before);
    assert.match(result.text, /这批先不上传/);
    assert.ok(result.text.endsWith(`\n${note}`));
    assert.ok(!(result.text.includes('下机') && result.text.includes('烤边')));
  }
});

test('unknown identity, authentication failure and unrelated callbacks never gain a guessed label', async () => {
  const generic = await cardAction(event(), ctx, async () => ({ action: 'modify' }));
  assert.ok(!generic.text.includes('数量口径'));
  const rejected = await cardAction(event(), ctx, async () => { throw new Error('wrong employee'); });
  assert.ok(!rejected.text.includes('数量口径'));
  assert.match(rejected.text, /旧核对按钮已停用/);
  const shouldNotRun = async () => { throw new Error('must not authenticate this event'); };
  assert.equal(await cardAction(event(), { ...ctx, accountId: 'other' }, shouldNotRun), undefined);
  assert.equal(await cardAction({ ...event(), content: '错误' }, ctx, shouldNotRun), undefined);
});

test('confirmation and deferral retain their original behavior even if a note is present', async () => {
  assert.equal(await cardAction(event('confirm'), ctx,
    async () => ({ action: 'confirm', quantity_note: 'must not display' })), undefined);
  assert.deepEqual(await cardAction(event('defer'), ctx,
    async () => ({ action: 'defer', quantity_note: 'must not display' })), {
    handled: true, text: '收到，谢谢说明！这批已暂缓上传，需要时再重新核对。',
  });
});

test('explicit quantity lookups cannot be acknowledged as reports, while real reports remain reports', () => {
  for (const content of [
    '查一下下机翻袜产量，冰冰袜1000双是不是昨天的？',
    '查询烤边产量：棉堆堆袜5000双有没有入账？',
    '小文，麻烦帮我查看下机翻袜产量，冰冰袜1000双的记录',
  ]) assert.equal(looksLikeReport(content), false, content);
  const six = '棉堆堆袜100 冰冰袜200 小腿袜30 过膝袜0 女船袜0 男船袜0';
  for (const content of [six, `补报 2026-10-06 工序：下机 ${six}`,
    `更正生产日期：2027-01-02 工序：烤边 ${six}`,
    `请帮我看下本次报数：工序：下机 ${six}`,
  ]) assert.equal(looksLikeReport(content), true, content);
});
