# 实验室自动化排程与执行系统

面向机械臂、移液站、封膜机、撕膜机、振荡器、冰箱、耗材堆栈、荧光定量计、离心机和扫码器的可扩展工作站。

目标：正常流程只需人工上料和收料；系统负责多设备分配、板位追踪、过程记录、报警和可核实的故障恢复。

## 当前进度

已实现 **可操作的本地模拟后台、运行控制和可扩展驱动接口**。型号尚未确定，当前全部使用模拟设备。

- 自由流程编排：添加、删除、复制、调整步骤，配置设备和板位，自动补齐机械臂转运。
- 模板版本保存、JSON 导入导出、隔离预演和预计甘特时间线；批次保存独立流程快照。
- 中文操作台：运行状态、10 类设备、板位、实际执行时间线、报警、日志、批次历史和 JSON 导出。
- 创建正常、回执丢失、结果未知三种模拟批次，自动推进所选模板。
- 暂停、继续、取消；未知动作保留资源锁，报警确认不会解除阻塞。
- 人工证据恢复：确认完成，或确认未执行后用新命令重试；保留原命令与审计。
- SQLite 持久化、旧数据库兼容迁移、服务重启后核实并暂停。
- 驱动协议、显式注册表、持久化命令路由和每台设备独立模拟账本。

## 目录结构

```text
backend/                 后端，独立运行
  run.py                 API 启动入口
  lab_orchestrator/      调度、控制、驱动和 HTTP API
  examples/              设备与流程配置
  tests/                 后端测试
frontend/                前端，独立运行
  serve.py               本地静态服务与 API 地址配置
  public/                HTML、CSS、JavaScript 和默认配置
  tests/                 前端服务测试和浏览器验收
.runtime/console/        已有运行数据，拆分后继续沿用
docs/                    架构与接口文档
```

## 启动前后端

需要 Python 3.11+，运行时无需安装第三方库。在项目根目录打开两个终端分别执行：

后端（8765，仅提供 JSON API）：

```powershell
python backend/run.py
```

前端（5173）：

```powershell
python frontend/serve.py
```

浏览器打开 http://127.0.0.1:5173 ，点击“新建模拟批次”。前端通过 HTTP 连接后端，不导入后端代码。关闭前端不会停止后台调度；停止后端后，使用同一数据目录重启会核实并暂停当前批次。

后端默认数据目录固定为项目根目录的 `.runtime/console/`，从根目录或 backend 目录启动均沿用原数据。历史页面显示摘要，当前批次可导出完整 JSON。默认每 0.3 秒推进 1 秒虚拟时间，前端每秒刷新。

自定义端口时，同时指定前端使用的 API 和后端允许的前端来源：

```powershell
# 终端一
python backend/run.py --port 8766 --frontend-origin http://127.0.0.1:5180
# 终端二
python frontend/serve.py --port 5180 --api-base http://127.0.0.1:8766
```

前端 `public/` 也可作为独立静态站点托管，修改 `config.js` 的 apiBase；后端必须允许对应前端来源。当前两端的本地服务均只监听 127.0.0.1。

详见 [后端说明](backend/README.md)、[前端说明](frontend/README.md) 和 [操作台与驱动接口](docs/05-console-and-drivers.md)。

## 自由组合流程

进入「流程编排」选择操作、调整顺序和参数，点击「预演流程」检查资源与板位，再保存模板或创建批次。原双板流程保留为内置模板。重复访问设备时可选择逐板执行。详见 [流程编排使用说明](docs/06-flexible-workflows.md)。

更新后需要重启前后端并刷新页面。模板修改只用于新批次，当前批次继续使用其创建时的快照。

## 重复启动提示

每个服务只需在一个终端启动，并保持该终端打开。出现“运行数据正由另一个后台占用”表示已有控制器使用相同数据目录；使用已有服务，或在原终端按 Ctrl+C 停止后再启动。无需删除数据库或 .lock 文件，锁随进程退出自动释放。

端口被占用时先停止对应服务，或按上面的示例同时修改前端地址和后端允许来源。独立后端实例必须同时使用不同的 `--data` 与 `--port`。

## 验证

从项目根目录执行：

```powershell
Push-Location backend
python -m unittest discover -s tests -v
Pop-Location
python -m unittest discover -s frontend/tests -p test_frontend.py -v
node --check frontend/public/app.js
node --check frontend/public/composer.js
```

验证包含后端行为测试、前端服务测试，以及真实跨端口浏览器完整操作。自由编排验收使用独立数据目录。双板示例包含 24 步；直接内核演示用时 49 秒虚拟时间。暂停和恢复可能改变总时长，不能据此推断真实设备性能。

## 直接调用

需要 Python 3.11+，无需安装第三方库。以下 Python 示例在 `backend/` 目录运行，或将该目录加入调用方的 Python 模块路径。

```python
from lab_orchestrator import run_demo

report = run_demo('.runtime/my-first-run')  # 必须是尚不存在的新目录
print(report['state'])
print(report['positions'])
```

也可使用便捷入口：

```powershell
cd backend
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
6. [模拟配置说明](backend/examples/README.md)
7. [操作台与驱动接口](docs/05-console-and-drivers.md)
8. [本轮实施计划](docs/superpowers/plans/2026-09-24-runtime-console.md)

## 当前边界

这是本地模拟系统，尚未接入真实硬件或生产部署。操作人名称为本地审计标签，尚无账号认证与角色权限。十类设备均可登记，操作库可组合其声明的能力。撕膜、离心和耗材出板目前仅为通用模拟动作；真实工艺状态、离心配平和耗材库存需单独实现。

当前每个执行数据库只容纳一个批次（可包含多块板），操作台将历次批次保存在独立目录。支持容量 1、能力匹配的可组合步骤、自动转运、跨板依赖和逐板/交错执行；同一块板上的动作需明确排序。条件分支、动态循环、耗材库存、工艺时间窗、多批次同时执行、自动缓冲路径规划和优化算法尚未实现。

架构文档描述长期目标；当前实现以本 README 和阶段验收为准。MADSci、EOS、PyLabRobot 仍是候选，未集成，未宣称兼容真实型号。
