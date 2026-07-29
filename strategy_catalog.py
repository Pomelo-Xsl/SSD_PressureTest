from copy import deepcopy


REQUIRED_FIELDS = {'id', 'name', 'duration', 'block_size', 'read_ratio', 'queue_depth', 'threshold_temp'}


def validate_strategy(strategy, block_sizes):
    missing = REQUIRED_FIELDS - set(strategy)
    if missing:
        raise ValueError('策略缺少字段：' + '、'.join(sorted(missing)))
    if not str(strategy['name']).strip():
        raise ValueError('策略名称不能为空')
    if int(strategy['duration']) < 1 or int(strategy['duration']) > 720:
        raise ValueError('策略时长必须在 1 到 720 小时之间')
    if strategy['block_size'] not in block_sizes:
        raise ValueError('策略块大小不受支持')
    if int(strategy['read_ratio']) < 0 or int(strategy['read_ratio']) > 100:
        raise ValueError('策略读比例必须在 0 到 100 之间')
    if int(strategy['queue_depth']) < 1 or int(strategy['queue_depth']) > 1024:
        raise ValueError('策略队列深度必须在 1 到 1024 之间')
    if int(strategy['threshold_temp']) < 35 or int(strategy['threshold_temp']) > 90:
        raise ValueError('策略温度阈值必须在 35 到 90 摄氏度之间')


def normalize_strategy(strategy, block_sizes):
    item = deepcopy(strategy)
    item['name'] = str(item['name']).strip()[:80]
    item['description'] = str(item.get('description') or '').strip()[:240]
    item['duration'] = int(item['duration'])
    item['read_ratio'] = int(item['read_ratio'])
    item['queue_depth'] = int(item['queue_depth'])
    item['threshold_temp'] = int(item['threshold_temp'])
    item['enabled'] = bool(item.get('enabled', True))
    item['version'] = int(item.get('version', 1))
    validate_strategy(item, block_sizes)
    return item


def enabled_strategies(plans):
    return [plan for plan in plans if plan.get('enabled', True)]


def strategy_snapshot(plan):
    return {'id': plan['id'], 'name': plan['name'], 'version': plan.get('version', 1), 'duration': plan['duration'], 'block_size': plan['block_size'], 'read_ratio': plan['read_ratio'], 'queue_depth': plan['queue_depth'], 'threshold_temp': plan['threshold_temp'], 'description': plan.get('description', '')}
