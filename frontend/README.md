# 前端

独立的 HTML、CSS 和 JavaScript 操作台，通过 HTTP JSON 连接后端，不导入 Python 业务代码，不访问数据库。无需 Node 构建或 npm 安装。

## 本地启动

需要 Python 3.11+。在本目录运行：

```powershell
python serve.py
```

或从项目根目录运行 `python frontend/serve.py`。浏览器打开 http://127.0.0.1:5173 。

默认连接 http://127.0.0.1:8765 。运行时可指定其他 API：

```powershell
python serve.py --port 5180 --api-base http://127.0.0.1:8766
```

后端需同时使用 `--port 8766 --frontend-origin http://127.0.0.1:5180`。后端来源配置必须与浏览器地址一致。API 地址是包含协议和端口的 origin，不含 /api 路径。

前端服务即使后端未启动也能显示页面；连接失败时页面会提示。后端恢复后刷新页面。关闭页面或前端服务不会暂停后台运行。

## 静态部署和配置

`public/` 包含全部可部署资源：

- `index.html`：页面结构。
- `style.css`：页面样式。
- `app.js`：视图、操作表单与 API 调用。
- `config.js`：默认 `window.LAB_CONFIG.apiBase`。

使用本地 `serve.py` 时，`/config.js` 由 `--api-base` 生成，覆盖静态配置；直接静态托管 `public/` 时修改配置文件即可。两种方式都无需重新构建。静态服务器只应发布 public 目录，后端需要允许实际前端 origin。当前后端仍仅提供本机模拟控制。

前端服务没有 API 代理，后端也不提供静态页面。两端可以独立修改、测试、启动和停止。

## 验证

在本目录运行：

```powershell
python -m unittest discover -s tests -p test_frontend.py -v
node --check public/app.js
```

完整浏览器验收需要额外提供 Playwright 和 Chrome，应用运行不依赖它们。两端服务启动且当前批次为空闲或终态后执行：

```powershell
node tests/browser-smoke.cjs
```

可用 `CONSOLE_URL` 指定前端地址，API 地址从运行时配置读取。脚本会新增两个模拟批次，验证创建、暂停、继续、取消、报警、人工恢复、完成与历史；截图写入项目根目录 `.runtime/browser-verification/`。Playwright 可通过 NODE_PATH 指向已安装模块。

协议说明见 [操作台与驱动接口](../docs/05-console-and-drivers.md)。

Windows 下前端端口采用独占监听；重复启动会提示端口冲突并退出，已有前端继续提供页面。停止原终端中的服务后即可重新启动。
