# 后端

Python 3.11+ 标准库实现的模拟调度服务，不依赖前端目录。

## 启动

在本目录运行：

```powershell
python run.py
```

也可从项目根目录运行 `python backend/run.py`。默认 API 是 http://127.0.0.1:8765/api/v1/state ，后端不再返回页面。

默认数据库仍为项目根目录 `.runtime/console/`，与拆分前一致，不受启动时工作目录影响。显式 `--data` 相对路径则相对于当前终端目录。

```powershell
python run.py --port 8766 --frontend-origin http://127.0.0.1:5180
```

`--frontend-origin` 可重复指定，提供时替换默认的 127.0.0.1:5173 和 localhost:5173。页面来源、协议和端口须精确匹配；不要添加路径或末尾斜杠。服务仍仅绑定 127.0.0.1。

## 代码

- `lab_orchestrator/engine.py`：调度与命令核实。
- `lab_orchestrator/controls.py`：暂停、继续、取消和人工恢复。
- `lab_orchestrator/drivers.py`：协议、注册表和设备路由。
- `lab_orchestrator/service.py`：单写入线程与批次管理。
- `lab_orchestrator/server.py`：JSON API、来源校验和 CORS。
- `examples/`：10 类模拟设备与双板流程。
- `tests/`：39 项后端行为测试。

## 测试和直接调用

在本目录运行：

```powershell
python -m unittest discover -s tests -v
python -m lab_orchestrator.demo
```

外部 Python 调用需将本目录加入模块路径，再 `from lab_orchestrator import Engine, Simulator, run_demo`。当前全部使用模拟驱动。

接口与恢复证据说明见 [操作台与驱动接口](../docs/05-console-and-drivers.md)。

重复启动时会明确提示数据目录或端口冲突。已有后台退出后再启动；无需删除锁文件。启动因端口冲突失败时会释放已取得的数据锁。
