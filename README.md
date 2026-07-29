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
- 预设策略参数可调整：测试时长（1–720 小时）、块大小、读比例、队列深度（1–1024）与温度阈值（35–90°C）
- 高级 fio 参数可调整：I/O 模式、并发作业数（1–32）、预热时间（0–3600 秒）、带宽上限（0–20000 MB/s）和 CRC32C 数据校验
- 压力任务创建、进度、停止与 HTML 报告导出
- 演示模式下的遥测、温度阈值和尾延迟异常
- 可选真实 `fio` 随机混合裸盘压力测试
- NVMe 诊断日志采集：全量/关键 telemetry，以及 `0xC0`、`0xCA` 扩展 SMART 日志，可从页面下载
- 全局串行任务队列：任意时刻仅运行一个 SSD 压测任务，其他任务按创建顺序自动排队
- 服务启动恢复：重启后原本运行、停止中或排队中的任务会标记为“已中断”，不会自动恢复破坏性 fio 写盘
- SQLite 结果归档：已完成、停止、失败和服务重启中断的任务会写入 `results.db`，保存原始任务快照、分析结果和 HTML 报告快照
- 任务记录支持状态/关键字筛选，并可导出全部任务摘要 CSV
- 运行环境自检：识别 Linux 内核、root 权限、fio、nvme-cli、smartmontools 与破坏性模式开关状态
- 遥测规则：温度、P99 延迟与吞吐衰减按统一规则生成可审计的告警事件
- 分阶段策略模型：新建任务会记录预热、主负载、恢复观察阶段以及 IOPS/限速/写入量估算
- 操作审计：任务创建、开始、停止、告警和完成事件同步写入 SQLite 审计表
- 时序遥测归档：温度、P99、吞吐与健康度在内存趋势缓存和 SQLite 中双重保存，支持按时间段、阶段、指标查询与 min/max 降采样
- 告警闭环：告警拥有稳定编号，确认状态与待发送通知队列持久化；自定义告警策略可绑定到新建任务
- 报告证据完整度：HTML 报告增加采样质量、阶段执行证据、资产快照基线和告警处置闭环，并在证据 ZIP 中提供结构化 JSON

## 测试结果数据库

系统使用 Python 标准库 SQLite，无需额外安装数据库服务。项目目录下的 `results.db` 会自动创建，建议在服务器上定期备份该文件。

- `test_results` 表：按任务编号保存设备信息、测试配置、状态、结论、评分、风险等级和时间信息；
- `result_json`：原始任务、遥测采样和事件记录快照；
- `analysis_json`：稳定性分析算法的结构化结果；
- `report_html`：任务结束时生成的 HTML 报告快照。

可使用以下接口读取归档结果：

```text
GET /api/results
GET /api/results/<任务编号>/report
GET /api/audit-events
GET /api/tasks/<任务编号>/telemetry?metric=temperature&max_points=240
GET /api/tasks/<任务编号>/report-evidence
GET /api/alerts
GET /api/notifications
```

其中 `telemetry` 接口返回按时间排序的标准化时序点、降采样结果及缺失间隔/完整性统计；`report-evidence` 返回报告使用的阶段、数据质量、资产历史和告警闭环依据。通知队列只记录待发送与处置状态，系统不会未经配置向外部地址发送消息。

## 真实 fio 压测：严重安全警告

真实模式是**破坏性裸盘写入**，会覆盖被测盘数据。系统设置四重限制：仅 Linux、必须 root、必须安装 `fio`、必须显式设置环境变量；同时会拒绝所有不满足 SSD 准入条件的设备。无需输入确认短语，但操作员必须勾选确认当前选择的是专用测试 SSD 且允许覆盖数据。

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
- 整块盘不存在任何分区（即使分区未挂载也不可测试）；
- 整块盘及任意子分区均没有挂载点；
- 不包含根分区 `/`、`/boot`、`/boot/efi` 或 `[SWAP]`。

这些规则由后端在创建任务时再次校验，不能仅靠浏览器界面绕过。

NVMe SMART 温度字段按规范通常以 Kelvin 返回，系统会自动转换为摄氏度显示。例如原始值 `306` 会显示为约 `32.9°C`。
