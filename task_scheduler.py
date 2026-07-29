TERMINAL_STATUSES = {'已完成', '已停止', '已中断', '失败'}
ACTIVE_STATUSES = {'运行中', '停止中'}
QUEUE_STATUS = '排队中'


def priority_value(task):
    value = task.get('priority', '普通')
    levels = {'紧急': 300, '高': 200, '普通': 100, '低': 0}
    return levels.get(value, levels['普通'])


def task_sort_key(task):
    return (-priority_value(task), task.get('queue_sequence', 0), task.get('created_at', ''), task.get('id', ''))


def active_tasks(tasks):
    return [task for task in tasks if task.get('status') in ACTIVE_STATUSES]


def queued_tasks(tasks):
    candidates = [task for task in tasks if task.get('status') == QUEUE_STATUS]
    return sorted(candidates, key=task_sort_key)


def next_runnable_task(tasks):
    if active_tasks(tasks):
        return None
    candidates = queued_tasks(tasks)
    return candidates[0] if candidates else None


def queue_position(tasks, task_id):
    for index, task in enumerate(queued_tasks(tasks), 1):
        if task.get('id') == task_id:
            return index
    return None


def summarize_queue(tasks):
    queued = queued_tasks(tasks)
    by_priority = {'紧急': 0, '高': 0, '普通': 0, '低': 0}
    for task in queued:
        priority = task.get('priority', '普通')
        by_priority[priority] = by_priority.get(priority, 0) + 1
    return {'active_count': len(active_tasks(tasks)), 'queued_count': len(queued), 'by_priority': by_priority, 'next_task_id': queued[0].get('id') if queued else None}


def can_change_priority(task):
    return task.get('status') == QUEUE_STATUS


def normalize_priority(value):
    allowed = {'紧急', '高', '普通', '低'}
    return value if value in allowed else '普通'


def batch_status(tasks):
    statuses = [task.get('status') for task in tasks]
    if not statuses:
        return '空批次'
    if any(status in ACTIVE_STATUSES or status == QUEUE_STATUS for status in statuses):
        return '执行中'
    if any(status == '失败' for status in statuses):
        return '已完成（含失败）'
    if any(status in {'已停止', '已中断'} for status in statuses):
        return '已完成（含中断）'
    return '已完成'
