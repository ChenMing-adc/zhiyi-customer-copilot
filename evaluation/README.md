# 离线评测工具（第一阶段）

只做数据准备、固定分组和离线评分，没有模型调用入口。产品不导入此目录，也不需要安装评测依赖。CSDS 工具仅使用 Python 标准库；官方数据准备复用产品导入器和回放逻辑，需要现有运行依赖，但只创建并销毁临时数据库，不读取或写入 `data/zhiyi.sqlite3`。

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

在 annotations 中填写 gold，完成双人复核及仲裁后将 review_status 改为 `adjudicated`，reviewers 填两个不同标注者 ID，annotation_version 填本轮版本。工具仅核验这些记录字段，不能替代实际人工复核。

原始 CSDS 标签和产品标签分开评测，不直接映射 positive→缓和。不同来源、不同模式分别出报告，不合并分数。输入目前只自动遮蔽手机号和邮箱，姓名、地址和账号等仍需人工脱敏；本阶段没有外发操作。

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
