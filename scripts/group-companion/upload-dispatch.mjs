import { spawn } from 'node:child_process';
import { execFile } from 'node:child_process';

export const OPENCLAW = '/Users/xianer/.local/bin/openclaw';
const PYTHON = '/Users/xianer/.hermes/hermes-agent/venv/bin/python';
const REVIEW = '/Users/xianer/.openclaw/workspace/scripts/production-parallel/review_cards.py';
const TASK = '/Users/xianer/.openclaw/workspace/scripts/production-parallel/upload_task.py';
const DETERMINISTIC = '/Users/xianer/.openclaw/workspace/scripts/production-parallel/deterministic_upload.py';
const SEND_RECEIPT = '/Users/xianer/.openclaw/workspace/scripts/group-companion/send_upload_receipt.py';
// A worker that has not returned within this window is considered lost. The
// durable receipt is then returned to the retry queue instead of remaining in
// dispatching forever. The upload itself remains idempotent by source+confirm.
export const UPLOAD_TIMEOUT_MS = 600000;
export const DETERMINISTIC_TIMEOUT_MS = 120000;

/**
 * Keep the useful part of a failed deterministic upload in the durable
 * receipt.  Child-process errors otherwise collapse to `code=1`, which makes
 * a ledger-template failure look like a transient worker crash to operators
 * and to the employee status view.
 */
export function summarizeUploadFailure(code, signal, output = {}) {
  const combined = [output.stderr, output.sendStderr, output.stdout, output.sendError]
    .filter(Boolean).map(value => String(value)).join('\n');
  const ledger = combined.match(/(?:远程台账缺少[^\r\n]+|台账[^\r\n]*(?:个人累计|月度汇总|每日汇总)[^\r\n]*|个人累计区[^\r\n]*)/);
  const detail = ledger?.[0]
    || combined.split(/\r?\n/).map(line => line.trim()).filter(line => line &&
      /(?:Error|Exception|错误|失败|缺少|退出)/i.test(line)).at(-1)
    || combined.split(/\r?\n/).map(line => line.trim()).filter(Boolean).at(-1)
    || '未返回具体错误';
  const exit = code == null ? `signal=${signal || 'unknown'}` : `code=${code}${signal ? ` signal=${signal}` : ''}`;
  return `上传任务失败（${exit}）：${detail}\n${combined}`;
}

export function once(callback) {
  let called = false;
  return (...args) => {
    if (called) return false;
    called = true;
    callback?.(...args);
    return true;
  };
}

export function buildUploadAgentArgs({ source, confirmation, group, task }) {
  if (!task || !task.startsWith('/') || /[\r\n'`$]/.test(task)) {
    throw new Error('上传任务缺少有效的机器生成任务文件');
  }
  const sessionKey = `agent:xiaowen-ceo:production-confirm:${source}:${confirmation}`;
  const command = `${PYTHON} ${TASK}`;
  const message = `员工本人已确认生产批次，程序已核验消息和卡片绑定。任务文件：${task}。先执行 ${command} inspect --task '${task}' 读取唯一的原报数、确认记录和核对卡；消息编号必须从任务读取，不手动重打、改写或推断。核对卡中标记“数量为0，请核实”的项目以本次“准确”为确认，按0双写入。读取最新GitHub内容和SHA，保留旧记录，生成完整候选后，仅执行 ${command} commit --task '${task}' --expected-sha <最新SHA> --file <完整候选文件> --review-note <核对说明>。此入口自动传递原始消息编号并运行既有commit_guard.py，提交后回读。不要另行使用service.py show或手填--source/--confirmation。只处理任务指定员工和批次。必须取得真实commit和回读一致才回复成功；失败保留待处理并如实说明。若任务已存在verified回执，直接核对回执返回结果，不重复提交。`;
  return [
    'agent', '--agent', 'xiaowen-ceo', '--session-key', sessionKey,
    '--message', message, '--deliver',
    '--reply-channel', 'feishu', '--reply-account', 'main',
    '--reply-to', `chat:${group}`, '--json',
  ];
}

export function prepareUploadTask({ source, confirmation, group }) {
  return new Promise((resolve, reject) => {
    execFile(PYTHON, [TASK, 'prepare', '--source', source, '--confirmation', confirmation, '--group', group],
      { timeout: 15000, maxBuffer: 262144 }, (err, out) => {
        if (err) return reject(err);
        try { resolve(JSON.parse(out)); } catch (error) { reject(error); }
      });
  });
}

export async function prepareReadyTask(params, prepare = prepareUploadTask, wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  // message:received may finish persisting after before_dispatch. Wait only
  // for that bounded handoff; invalid IDs/bindings fail without an agent run.
  for (const delay of [0, 500, 2000, 5000]) {
    if (delay) await wait(delay);
    try {
      const prepared = await prepare(params);
      if (prepared.source !== params.source || prepared.confirmation !== params.confirmation || prepared.group !== params.group) {
        throw new Error('机器任务与派发批次不一致');
      }
      return prepared;
    } catch (error) {
      const pending = /收件箱中未找到|尚未完成结构化核对/.test(String(error?.stderr || error?.message || error));
      if (!pending || delay === 5000) throw error;
    }
  }
}

/**
 * Start an upload worker with an explicit Feishu destination.
 * The caller owns durable receipt state; this function only reports whether
 * the process could be started, so a later retry remains safe after crashes.
 */
export async function startUploadAgent(params, { onError, onExit, prepareTask = prepareReadyTask, spawnWorker = spawn } = {}) {
  const prepared = await prepareTask(params);
  const streams = child => {
    let stdout = '';
    let stderr = '';
    const collect = (target, chunk) => {
      if (target.length < 262144) return target + String(chunk).slice(0, 262144 - target.length);
      return target;
    };
    child.stdout?.on('data', chunk => { stdout = collect(stdout, chunk); });
    child.stderr?.on('data', chunk => { stderr = collect(stderr, chunk); });
    return { get stdout() { return stdout; }, get stderr() { return stderr; }, collect };
  };
  let child = spawnWorker(PYTHON, [DETERMINISTIC, '--task', prepared.task,
    '--lease-key', `production-confirm:${params.source}:${params.confirmation}`], {
    detached: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let capture = streams(child);
  let timedOut = false;
  let timeout;
  // Lease updates are serialized so a fast deterministic probe cannot race a
  // fallback worker and leave the recovery queue watching the old PID.
  let leaseUpdate = Promise.resolve();
  const markLease = pid => {
    leaseUpdate = leaseUpdate.then(() => markUploadDispatchStarted({
      confirmation: params.confirmation, pid,
    })).catch(() => {});
    return leaseUpdate;
  };
  const armTimeout = (ms, onTimeout) => {
    timeout?.unref?.();
    timeout = setTimeout(onTimeout, ms);
    timeout.unref?.();
  };
  const currentOutput = extra => ({ stdout: capture.stdout, stderr: capture.stderr, ...extra });
  const startLegacy = () => {
    child = spawnWorker(OPENCLAW, buildUploadAgentArgs({ ...params, task: prepared.task }), {
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    // The deterministic probe exits with 75 before the long-lived fallback
    // worker starts. Refresh the durable lease in order so recovery never
    // treats the probe PID as the live worker and launches a duplicate.
    markLease(child.pid);
    capture = streams(child);
    const finishLegacy = once((code, signal, output) => onExit?.(code, signal, output));
    const reportLegacyError = once((error) => onError?.(error, currentOutput({ deterministicFallback: true })));
    child.once('error', (error) => reportLegacyError(error));
    armTimeout(UPLOAD_TIMEOUT_MS, () => {
      timedOut = true;
      const stderr = `${capture.stderr}\nupload worker timeout after ${UPLOAD_TIMEOUT_MS}ms`;
      child.kill('SIGTERM');
      finishLegacy(null, 'SIGTERM', { stdout: capture.stdout, stderr, timedOut });
    });
    child.once('exit', (code, signal) => {
      clearTimeout(timeout);
      finishLegacy(code, signal, currentOutput({ timedOut }));
    });
    child.unref();
    return child;
  };
  // A timeout kills the child and is normally followed by an exit event.  A
  // single settlement keeps the gate from enqueueing the same batch twice.
  let probeSettled = false;
  const finishProbe = callback => {
    if (probeSettled) return false;
    probeSettled = true;
    callback?.();
    return true;
  };
  const reportProbeError = once((error) => onError?.(error, currentOutput({ deterministic: true })));
  child.once('error', (error) => reportProbeError(error));
  armTimeout(DETERMINISTIC_TIMEOUT_MS, () => {
    finishProbe(() => {
      timedOut = true;
      const stderr = `${capture.stderr}\ndeterministic upload timeout after ${DETERMINISTIC_TIMEOUT_MS}ms`;
      child.kill('SIGTERM');
      onExit?.(null, 'SIGTERM', { stdout: capture.stdout, stderr, timedOut, deterministic: true });
    });
  });
  child.once('exit', (code, signal) => {
    clearTimeout(timeout);
    if (probeSettled) return;
    if (code === 75) {
      finishProbe(() => startLegacy());
      return;
    }
    if (code !== 0) {
      finishProbe(() => onExit?.(code, signal, currentOutput({ timedOut, deterministic: true })));
      return;
    }
    let receipt;
    try {
      receipt = JSON.parse(capture.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1));
    } catch {
      finishProbe(() => onExit?.(1, signal, currentOutput({ timedOut, deterministic: true, parseError: true })));
      return;
    }
    execFile(PYTHON, [SEND_RECEIPT, '--group', params.group, '--receipt-json', JSON.stringify(receipt)],
      { timeout: 15000, maxBuffer: 262144 }, (error, out) => {
        if (error) {
          finishProbe(() => onExit?.(1, signal, currentOutput({ timedOut, deterministic: true, receipt, sendError: String(error), sendStderr: error.stderr })));
          return;
        }
        try {
          const delivery = JSON.parse(out.trim().split(/\r?\n/).filter(Boolean).at(-1));
          finishProbe(() => onExit?.(0, signal, currentOutput({ timedOut, deterministic: true, receipt, replyMessageId: delivery.message_id })));
        } catch (error) {
          finishProbe(() => onExit?.(1, signal, currentOutput({ timedOut, deterministic: true, receipt, sendError: String(error) })));
        }
      });
  });
  markLease(child.pid);
  child.unref();
  return child.pid;
}

function reviewCommand(args) {
  return new Promise((resolve, reject) => {
    execFile(PYTHON, [REVIEW, ...args], { timeout: 15000, maxBuffer: 262144 }, (err, out) => {
      if (err) return reject(err);
      try { resolve(JSON.parse(out)); } catch (parseError) { reject(parseError); }
    });
  });
}

export function verifyUploadReceipt({ source, confirmation }) {
  return reviewCommand(['verify-dispatch', '--source', source, '--confirmation', confirmation]);
}

export function markUploadDispatchFailed({ confirmation, error }) {
  return reviewCommand(['mark-dispatch-failed', '--message-id', confirmation, '--error', String(error || '上传任务失败')]);
}

export function markUploadDispatchStarted({ confirmation, pid }) {
  return reviewCommand(['mark-dispatch-started', '--message-id', confirmation, '--pid', String(pid)]);
}

export function quarantineUploadReceipt({ confirmation, error }) {
  return reviewCommand(['quarantine-confirmation', '--message-id', confirmation, '--error', String(error || '确认回执缺少原始报数')]);
}
