import os
import platform
import shutil
import subprocess


def command_version(command):
    executable = shutil.which(command)
    if not executable:
        return {'installed': False, 'path': None, 'version': None}
    try:
        output = subprocess.run([command, '--version'], capture_output=True, text=True, timeout=5, check=False)
        version = (output.stdout or output.stderr or '').splitlines()
        version = version[0].strip() if version else '已安装'
    except (OSError, subprocess.TimeoutExpired):
        version = '已安装，版本读取失败'
    return {'installed': True, 'path': executable, 'version': version}


def collect_environment_health(destructive_enabled, is_root):
    tools = {name: command_version(name) for name in ('fio', 'nvme', 'smartctl', 'lsblk')}
    checks = []
    checks.append({'name': 'Linux 内核', 'ok': platform.system() == 'Linux', 'detail': '{0} {1}'.format(platform.system(), platform.release())})
    checks.append({'name': 'root 权限', 'ok': is_root, 'detail': '真实裸盘测试必须以 root 运行'})
    checks.append({'name': 'fio 执行器', 'ok': tools['fio']['installed'], 'detail': tools['fio']['version'] or '未安装'})
    checks.append({'name': '破坏性模式开关', 'ok': destructive_enabled, 'detail': 'ENABLE_DESTRUCTIVE_FIO=1 才允许真实裸盘测试'})
    checks.append({'name': 'nvme-cli', 'ok': tools['nvme']['installed'], 'detail': tools['nvme']['version'] or '可选：用于扩展日志'})
    checks.append({'name': 'smartmontools', 'ok': tools['smartctl']['installed'], 'detail': tools['smartctl']['version'] or '可选：用于 SATA SMART'})
    return {'platform': platform.platform(), 'hostname': platform.node(), 'pid': os.getpid(), 'checks': checks, 'tools': tools}
