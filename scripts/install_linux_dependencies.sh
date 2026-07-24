#!/usr/bin/env bash
# 安装 SSD PressureTest 在 Linux 上运行所需的系统工具。
# 用法：bash scripts/install_linux_dependencies.sh
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 执行：sudo bash scripts/install_linux_dependencies.sh" >&2
  exit 1
fi

if command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip fio nvme-cli smartmontools
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip fio nvme-cli smartmontools
else
  echo "不支持的发行版：请手动安装 python3、fio、nvme-cli、smartmontools。" >&2
  exit 1
fi

echo "系统依赖安装完成。接下来可执行："
echo "  python3 -m venv .venv"
echo "  source .venv/bin/activate"
echo "  python -m pip install -r requirements.txt"
