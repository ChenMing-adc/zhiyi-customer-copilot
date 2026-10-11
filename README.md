# 知意客服副驾

面向客服的本地演示工作台：业务 Excel 导入、会话检索与回放、情绪和意图分析、带证据的回复建议、人工演示发送，以及主管跟进看板。后端使用 FastAPI / SQLite，前端使用原生 HTML、CSS、JavaScript。

## 安装与启动

需要 Python 3.11+。在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

已有 `.env` 时不要覆盖。填写 `AI_PROVIDER`、`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`；OpenAI-compatible 服务使用 `AI_PROVIDER=openai`。温度与输出上限由 `.env` 控制。密钥只在本地保存。

将官方业务 Excel 放在本地，通过进程环境变量指定路径（这些路径变量不从 `.env` 加载）：

```powershell
$env:ZHIYI_SOURCE_PATH = 'C:\your-data\赛题 1：数据共情者-业务数据.xlsx'
.\scripts\run-copilot.ps1 -Python .\.venv\Scripts\python.exe -Port 8088
```

打开 <http://127.0.0.1:8088>。未指定数据路径时，默认读取仓库上级 `赛事方数据/赛题 1：数据共情者-业务数据.xlsx`。缺少数据时工作台仍可启动，但无业务会话。数据库自动生成在 `data/zhiyi.sqlite3`，也可通过进程环境变量 `ZHIYI_DB_PATH` 指定。

macOS / Linux 安装同一份依赖后可运行：

```bash
export ZHIYI_SOURCE_PATH='/your-data/赛题 1：数据共情者-业务数据.xlsx'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8088
```

当前阿里云 `qwen3.8-flash` 客服请求关闭混合思考以减少延迟；不修改 `.env`，其他模型不附加该参数。Ollama 连接代码仍保留，模型由使用者自行部署，仓库不携带权重。

## 代码目录

```text
app/
  main.py            服务入口
  copilot/           导入、上下文、分析、模型传输、API、数据存储
  core/config.py     模型配置
  schemas/dtos.py     模型消息结构
  services/ai.py      可复用模型客户端
  static/copilot/    客服工作台
scripts/             启动与显式运行的模型联调脚本
tests/               无数据启动检查与业务集成测试
```

旧校园页面、心理咨询流程、知识库、微调模型目录、MySQL / Redis 配置已移除。当前运行不需要 Docker、MySQL 或 Redis。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

启动检查不依赖业务数据，也不调用模型。业务集成测试需要官方原始 Excel；通过 `ZHIYI_SOURCE_PATH` 指定，缺少时明确跳过。普通测试使用模拟客户端，不调用外部模型。

`scripts/probe-copilot-model.py` 显式调用当前模型做无业务数据的连通性检查；`scripts/smoke-copilot-model.py` 显式将 S00053 的脱敏业务上下文发送到 `.env` 指定的模型进行联调。两者可能产生调用费用，均不会自动执行。

## 协作与边界

每人本地维护自己的 `.env`、数据文件和数据库。上述文件以及开发汇报、截图、方案文档均不入库。功能修改建议使用个人分支，通过 Pull Request 合并。

这是本地演示系统：身份选择器不等于生产登录，发送只写入演示库。正式企业鉴权、审批、千牛接入和正式评测尚未交付；当前启动方式绑定本机地址。
