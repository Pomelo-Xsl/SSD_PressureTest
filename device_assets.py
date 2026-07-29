from hashlib import sha256


def asset_id(device):
    identity = '|'.join([str(device.get('serial') or ''), str(device.get('path') or ''), str(device.get('name') or '')])
    return sha256(identity.encode('utf-8')).hexdigest()[:16]


def classify_device_risk(device):
    reasons = device.get('test_reasons') or []
    text = '；'.join(reasons)
    if device.get('testable'):
        return {'level': '低', 'score': 0, 'action': '可作为专用测试盘候选，启动前仍需操作员确认。'}
    if any(keyword in text for keyword in ('系统盘', '启动盘', '交换分区', '挂载', '分区')):
        return {'level': '高', 'score': 90, 'action': '禁止裸盘测试；请先确认该盘没有系统、业务或分区依赖。'}
    if any(keyword in text for keyword in ('机械盘', '只读', '非 SSD')):
        return {'level': '高', 'score': 80, 'action': '设备类型或写入状态不满足测试要求。'}
    return {'level': '中', 'score': 50, 'action': '请复核设备信息、控制器状态和测试准入原因。'}


def enrich_device(device):
    item = dict(device)
    item['asset_id'] = asset_id(item)
    item['risk'] = classify_device_risk(item)
    item['asset_label'] = '{0} · {1}'.format(item.get('name', '未知 SSD'), item.get('serial', '未读取序列号'))
    return item


def summarize_inventory(devices):
    summary = {'total': len(devices), 'testable': 0, 'high_risk': 0, 'medium_risk': 0, 'low_risk': 0, 'by_interface': {}}
    for device in devices:
        if device.get('testable'):
            summary['testable'] += 1
        level = device.get('risk', {}).get('level', '中')
        if level == '高':
            summary['high_risk'] += 1
        elif level == '低':
            summary['low_risk'] += 1
        else:
            summary['medium_risk'] += 1
        interface = device.get('interface', '未知')
        summary['by_interface'][interface] = summary['by_interface'].get(interface, 0) + 1
    return summary
