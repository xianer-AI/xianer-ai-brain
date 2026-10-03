"""Employee-safe wording driven only by the reconciled batch state."""


def render_batch_status(status):
    state = status.get('state')
    date = status.get('production_date')
    prefix = f'{date} 这批' if date else '这批'
    if state == 'completed':
        return f'{prefix}已完成，系统已确认并完成 GitHub 回读。'
    if state == 'awaiting_confirmation':
        return f'{prefix}正在等待核对，请核对后回复“准确”。'
    if state == 'confirmed':
        return f'{prefix}已收到确认，系统正在自动同步。'
    if state == 'upload_failed':
        # The confirmation remains valid.  Do not ask the employee to
        # re-report or confirm again; the durable error is for administrators.
        return f'{prefix}{status.get("reason") or "已确认，上传失败，管理员处理中"}。'
    if state == 'needs_reconciliation':
        return f'{prefix}状态需要先核实，系统不会重复发送或重复上传。'
    if state == 'not_found':
        return '暂时找不到这批报数记录，请重新提供原始报数。'
    return '这批报数已收到，系统正在整理。'
