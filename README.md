# Linux 企业级 SSD 稳定性与耐久压力测试系统（MVP）

面向 Linux 服务器企业级 NVMe/SATA SSD 的本地 Web 系统，具备设备发现、SMART/NVMe 健康遥测、压力任务编排、异常判定与报告导出。

## 运行

安全演示模式（不会写入磁盘）：

```bash
sudo bash scripts/install_linux_dependencies.sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python3 app.py
```

访问 <http://127.0.0.1:8080>。

## 已实现功能

- Linux `lsblk` 自动发现非旋转 SSD；已挂载设备会标记为受保护
- 测试准入判断：自动拒绝机械盘、只读盘、已挂载分区盘，以及系统盘、启动盘和 swap 所在盘
- 通过 `nvme smart-log` 或 `smartctl` 采集温度与健康信息（工具可选）
- 72 小时老化、24 小时稳定性、突发恢复三种企业级压力策略
- 压力任务创建、进度、停止与 HTML 报告导出
- 演示模式下的遥测、温度阈值和尾延迟异常
- 可选真实 `fio` 随机混合裸盘压力测试

## 真实 fio 压测：严重安全警告

真实模式是**破坏性裸盘写入**，会覆盖被测盘数据。系统设置四重限制：仅 Linux、必须 root、必须安装 `fio`、必须显式设置环境变量；同时会拒绝所有已挂载设备，操作员还要在页面输入完全一致的 `ERASE /dev/设备名` 确认短语。

仅在专用测试机上安装工具并启动：

```bash
sudo apt install fio nvme-cli smartmontools   # RHEL/Rocky 请使用 dnf
sudo ENABLE_DESTRUCTIVE_FIO=1 python3 app.py
```

不要在系统盘、生产盘、RAID 成员盘或任何已挂载设备上使用真实模式。生产部署建议增加设备白名单、独立测试账号、网络访问控制和审计日志。

## SSD 测试准入规则

页面会为每块磁盘显示“可测试”或“不可测试”，并给出原因。只有同时满足以下条件的 SSD 才能创建任务：

- 识别为非旋转 SSD；
- 设备不是只读；
- 整块盘及任意子分区均没有挂载点；
- 不包含根分区 `/`、`/boot`、`/boot/efi` 或 `[SWAP]`。

这些规则由后端在创建任务时再次校验，不能仅靠浏览器界面绕过。
