# 实验室自动化排程与执行系统

面向机械臂、移液站、封膜机、撕膜机、振荡器、冰箱、耗材堆栈、荧光定量计、离心机和扫码器的可扩展工作站。

目标：正常流程只需人工上料和收料；系统负责多设备分配、板位追踪、过程记录、报警和可核实的故障恢复。

## 当前进度

已实现 **可操作的本地模拟后台、运行控制和可扩展驱动接口**。型号尚未确定，当前全部使用模拟设备。

- 中文操作台：运行状态、10 类设备、板位、实际执行时间线、报警、日志、批次历史和 JSON 导出。
- 创建正常、回执丢失、结果未知三种模拟批次，自动推进双板流程。
- 暂停、继续、取消；未知动作保留资源锁，报警确认不会解除阻塞。
- 人工证据恢复：确认完成，或确认未执行后用新命令重试；保留原命令与审计。
- SQLite 持久化、旧数据库兼容迁移、服务重启后核实并暂停。
- 驱动协议、显式注册表、持久化命令路由和每台设备独立模拟账本。

## 启动操作台

需要 Python 3.11+，运行时无需安装第三方库。在项目目录执行：

```powershell
python -m lab_orchestrator.server --port 8765 --data .runtime/console
```

浏览器打开 http://127.0.0.1:8765 ，点击“新建模拟批次”。默认每 0.3 秒推进 1 秒虚拟时间；页面每秒刷新。按 Ctrl+C 停止服务，下次使用同一数据目录启动即可恢复。

数据保存在 `.runtime/console/`：批次目录、控制器数据库、设备路由和模拟设备账本。历史页面显示摘要；当前批次可导出完整 JSON。系统同一时间运行一个批次，结束后可创建下一批次。

验证覆盖 33 项自动化行为测试和浏览器完整操作流程。双板示例包含 24 个步骤；直接内核演示用时 49 秒虚拟时间。暂停、恢复及启动时机可能改变操作台的虚拟总时长，不能据此推断真实设备性能。

详细说明见 [操作台、HTTP API 与驱动接口](docs/05-console-and-drivers.md)。

## 直接调用

需要 Python 3.11+，无需安装第三方库。从当前项目目录运行，或将本项目目录加入调用方的 Python 模块路径。

```python
from lab_orchestrator import run_demo

report = run_demo('.runtime/my-first-run')  # 必须是尚不存在的新目录
print(report['state'])
print(report['positions'])
```

也可使用便捷入口：

```powershell
python -m lab_orchestrator.demo
python -m unittest discover -s tests -v
```

输出目录含 `runtime.sqlite`（控制器状态）、`devices.sqlite`（模拟设备事实）、`report.json` 和 `timeline.html`。直接用浏览器打开 HTML 查看时间线。

故障模拟：

```python
from lab_orchestrator import run_demo

# 查询原动作后继续，不会再次移液。
report = run_demo('.runtime/lost-ack-example', {'plate_01.pipette': 'lost_ack'})

# 停留在 blocked，保持资源和板位不确定状态。
report = run_demo('.runtime/unknown-example', {'plate_01.move_to_scan': 'unknown'})
```

使用核心函数控制推进和重启核实：

```python
import json
from pathlib import Path
from lab_orchestrator import Engine, Simulator

cell = json.loads(Path('examples/workcell.json').read_text(encoding='utf-8'))
flow = json.loads(Path('examples/workflow.json').read_text(encoding='utf-8'))
simulator = Simulator('.runtime/manual/devices.sqlite')
engine = Engine(cell, '.runtime/manual/runtime.sqlite', simulator)
try:
    engine.submit(flow, key='batch-001')  # 同键同内容返回原批次
    engine.recover()                     # 查询未终结命令，不盲目重发
    result = engine.run_until_complete()
finally:
    engine.close()
    simulator.close()
```

## 文档导航

1. [历史需求与决策来源](docs/01-context.md)
2. [总体架构和模块关系](docs/superpowers/specs/2026-09-24-lab-orchestration-design.md)
3. [设备与业务接口设计](docs/02-contracts.md)
4. [数据、状态机与恢复设计](docs/03-state-and-recovery.md)
5. [当前验收与后续阶段](docs/04-delivery.md)
6. [模拟配置说明](examples/README.md)
7. [操作台与驱动接口](docs/05-console-and-drivers.md)
8. [本轮实施计划](docs/superpowers/plans/2026-09-24-runtime-console.md)

## 当前边界

这是本地模拟系统，尚未接入真实硬件或生产部署。操作人名称为本地审计标签，尚无账号认证与角色权限。十类设备均可登记，但示例仅执行机械臂、扫码、移液、封膜、振荡、检测及冰箱确认；离心配平、撕膜、主动堆栈需单独开发。

当前每个执行数据库只容纳一个批次（可包含多块板），操作台将历次批次保存在独立目录。只支持容量 1、固定设备路线和单板线性流程。完整 DAG、耗材库存、工艺时间窗、多批次同时执行、自动缓冲路径规划和优化算法尚未实现。

架构文档描述长期目标；当前实现以本 README 和阶段验收为准。MADSci、EOS、PyLabRobot 仍是候选，未集成，未宣称兼容真实型号。
