# 独立评测工具与本地页面

## 可视化页面

运行 `./scripts/run-evaluation.ps1 -Port 8090`，打开 http://127.0.0.1:8090 。也可在已安装现有项目依赖的环境运行 `python -m uvicorn evaluation.web:app --host 127.0.0.1 --port 8090`。

页面可切换 CSDS / 官方业务数据、版本、开发/验证/测试分组；按会话 ID 收纳截点，支持搜索会话/消息；选会话后仅显示所属截点。数据没有统一用户 ID，因此不会擅自合并不同会话。截点卡片旁及详情顶部均可直接打开标注，保存后保留当前选择。点击“导入预测结果”选择下文规定格式的 JSON，页面会计算探索性指标并展示每条记录的预测、原始输出和回复草稿。预测记录可附带 `raw_output`、`draft` 或 `reply` 字段。空标准答案不会被当成错误或零分，计分样本数和待标注数单独显示。

页面支持单人标注与两套数据的真实调用。“查看评分示例”仍为人工构造，与真实运行分开。所有复核和运行结果只写入被 Git 忽略的 `data/evaluation/`，不读写产品数据库。CLI 数据准备/评分仍不调用模型。CSDS CLI 仅需标准库；页面与官方适配器使用现有项目依赖。

### 单人标注与团队同步

每条样本由一个人填写并保存即可成为 `confirmed` 标准答案，不需要第二人确认或第三人仲裁。表单记录作者、标签、风险确认、消息依据、理由与修改历史；旧页面提交会因版本冲突被拒绝。姓名是来源记录，不是账户认证。旧版 adjudicated 记录继续兼容。

三人可以按会话分工，避免同一会话的截点被重复修改。当前是本地页面，不会自动跨电脑同步：

1. 首次通过团队已有的私有共享渠道，将相同的 `data/evaluation/<数据版本>/` 文件夹复制到各自电脑。固定输入、分组和 manifest 保持一致；不要发到公开 GitHub。
2. 每人完成自己负责的样本后，点击“导出标注包”。标注包包含标签、作者、依据和历史，不包含完整聊天输入，但理由可能含业务信息，仍按内部数据传递。
3. 收到队友的包，在相同数据版本点击“导入标注包”。先预览新增/更新/保留/冲突数量，再保存。未知样本、标签异常或版本不匹配会被拒绝。
4. 同一标注历史的后续修改可合并；两边分别修改同一条时，明确选择保留本地或采用导入版本。采用导入时保留旧本地记录。重复导入不重复增加标注；预览后本地变化会要求重新预览。
5. 已有模型运行使用冻结答案，合并标注后点击“按最新复核重算”，无需再次调用模型。

GitHub 同步代码即可：同一仓库的协作者使用分支、pull/push，不需要不断 fork。数据、标注包、模型输出和 .env 被 Git 忽略。需要实时同步时，应部署带身份与访问控制的共用评测服务；当前仍只绑定 localhost，并未公开到网络。

### 真实模型运行与 Prompt 调优

CSDS 与官方数据均已获用户授权使用现有阿里云配置运行，不再限制 S00053。选择数据版本与开发/验证/测试分组，点击“配置真实基线”。在可搜索列表中勾选整段会话或具体截点，也可一键选择当前会话、全选搜索结果或清空；已选数量实时展示，不需要手输 ID。搜索不会清除已选内容。页面显示实际条数、前 10 条外发输入，并提供全部输入下载，修改范围后须更新预览。预览指纹绑定具体输入和模型目的地，启动时再次校验。开放入口不会自动触发全量调用。

调用继续复用现有 .env 和客户端，不改动产品 Prompt 或数据库。任务串行、同一进程限一个任务、单样本超时 60 秒；无自动重试，首次失败停止余下调用。停止按钮会等待当前请求完成。服务重启后不自动续跑。数据自动脱敏有局限，外发预览不能替代人名、地址等信息核查。

每次运行冻结样本 ID、标准答案、Prompt 原文/哈希/版本、模型参数及口径版本。缺失/失败输出保留在计划样本分母中，缺失标准答案不冒充零分。页面统计“已确认”时包括单人确认；没有人工确认的原始 CSDS 标签仍标记探索性。标注修改或导入后重算生成新的运行记录，保留原记录。

用户确认的官方口径：价保资格/补差申请归订单服务；仅缺失图片时情绪延续上一条可见买家消息；以退货施压也算投诉升级候选。两套标签不直接映射。

本工具测试独立分类与草稿 Prompt，不是线上多节点 Agent 全链路。草稿未经产品输出校验，不能直接发客户。风险仅两类候选，不含优先级。建议开发集调优、验证集检查、测试集最终验收；测试分组虽然开放，使用其结果调优后不能再称为未见盲测。

在仓库根目录运行，数据文件由使用者本地提供：

```powershell
python scripts/evaluate.py audit --source ../情绪数据集/CSDS美妆标注.json --out data/evaluation/audit-v1
python scripts/evaluate.py prepare --source ../情绪数据集/CSDS美妆标注.json --out data/evaluation/csds-v1
python scripts/evaluate.py prepare-official --source '../赛事方数据/赛题 1：数据共情者-业务数据.xlsx' --out data/evaluation/official-v1
python scripts/evaluate.py demo --out data/evaluation/example-v1
```

输出目录必须是 `data/evaluation/` 内的新子目录。重复运行不能覆盖已有结果，需换版本目录名。CSDS 的数据契约校验包括字段、角色、枚举和序号；这是专用适配器，不是通用 JSON Schema 引擎。

## 数据包和复核

- `samples.jsonl`：买家消息逐轮截点，最近 28 条消息（`--window` 可调整），显式白名单输入，不含真值、未来消息、场景标签。官方包为逐轮回放；并非全量复盘评测。
- `annotations.jsonl`：独立标准答案。默认 `unreviewed`，官方标签为 `null`，不能把未标注风险视为无风险 `[]`。
- `split.json`：会话分组和固定分配；近似完整会话自动同组，也支持 `--groups groups.json` 指定关联 ID 数组，例如 `[["A","B"],["C","D"]]`。按约 60/20/20 分配后，在不拆组、不改组数的前提下交换整组，改善出现在至少三个独立组中的标签覆盖；不保证各类均衡。
- `coverage.json`：各分组类别覆盖和缺失标签。必须人工检查；投诉等极少类别不能通过拆散同一会话来补足。跨来源模板相似性需人工检查；自动近似检测不能证明没有泄漏。
- `manifest.json`：来源哈希、输入/分组哈希、标签集合、窗口和 dataset_id。固定输入或分组被改动时拒绝评分；需要重新准备新版本。
- `audit.json`：CSDS 分布和待复核规则冲突，不自动改标签。

已看过的 CSDS 样例和官方 S00053 默认放开发集；额外已用于调优的会话通过 `--dev-ids ID1 ID2` 声明。准备后的分组是固定候选，需要人工确认覆盖和同源模板后再作为正式盲测划分。

在 annotations 中填写 gold，单人确认后将 review_status 改为 `confirmed`，reviewers 填一位标注者 ID，annotation_version 填本轮版本。工具仅核验这些记录字段，不能替代实际人工复核。

原始 CSDS 标签和产品标签分开评测，不直接映射 positive→缓和。不同来源、不同模式分别出报告，不合并分数。输入目前只自动遮蔽手机号和邮箱，姓名、地址和账号等仍需人工脱敏；两套数据均已获模型调用授权；点击运行时按选择范围发送。

## 预测格式与评分

预测 JSON（dataset_id 从数据包 manifest 复制；此处仅展示格式）：

```json
{
  "dataset_id": "copy-from-manifest",
  "split": "dev",
  "run_id": "manual-format-example",
  "prompt_version": "not-run",
  "predictions": [
    {"sample_id": "csds:ID:2", "status": "ok", "emotion": "neutral", "intent": "refund", "elapsed_ms": 120, "total_tokens": 80},
    {"sample_id": "csds:ID:3", "status": "error"}
  ]
}
```

只有所选 split 的样本可进入同一预测文件。重复 ID、未知样本或数据版本不匹配直接拒绝；缺失、异常和非法标签作为失败计入分母。只有 `status=ok` 的合法字段才有效。官方风险字段为类型列表，例如 `"risk": ["投诉升级"]`，明确无风险为 `[]`。

```powershell
python scripts/evaluate.py score --bundle data/evaluation/csds-v1 --predictions data/evaluation/predictions-v1.json --split dev --out data/evaluation/report-v1
```

标准答案尚未完成复核时默认拒绝评分。调试可显式加 `--allow-unreviewed`，报告会标记 exploratory_unreviewed，不能作为验收结果。`--annotations` 可指定另存的完整复核文件，样本 ID 必须与数据包一致。

报告包含固定标签 Macro-F1、受支持标签 Macro-F1、逐类 Precision/Recall/F1/支持数、混淆矩阵、全样本准确率、有效输出准确率、覆盖率、错误列表和提供了测量值时的耗时/Token 统计。未出现且未预测的类别 F1 为 N/A，在固定标签 Macro-F1 中按零计；因此必须一起看类别支持数，不能比较标签覆盖不同的数据包来宣称提升。

风险评分仅针对两类候选的多标签 P/R/F1，不代表已实现 PRD 全部风险或高优先级判定。趋势、证据语义支持、会话聚类置信区间和跨运行统计比较尚未实现。

demo 的预测是人工构造的正确/错误/缺失示例，不调用模型，报告明确标记 `synthetic_example_NOT_model_accuracy`。

## 测试

```powershell
python -m unittest discover -s tests -p test_evaluation.py -v
python -m unittest discover -s tests -v
```

真实数据、标注、分组、预测、报告均位于已忽略的 `data/` 中。此 README 仅是工具必要使用说明；不包含业务样本或标准答案。后续真实模型运行器、Prompt 版本和自动对比另行实现，本轮没有修改线上 Prompt 或 `.env`。
