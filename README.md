# 远程会议摘要的复核负荷与纠正闭环

本地离线应用：导入合成会议资料，区分“有人提议 / 会议决定 / 负责人确认 / 执行结果”，
让质量人员按分层抽样分配有限审阅时间，让负责人裁定事项与冲突，并把修改后的事实
经交接通知传到仍在执行的人。仅使用 Python 3.14 标准库与本地 SQLite，不接入任何
真实会议系统或外部账号。

## 业务规则（应用的不可绕过约束）

1. **提议不是承诺**：`suggestion` 提案只建立 `draft_proposal` 草稿；`decision` 提案
   进入 `pending_confirmation`。只有项目 `owner` 能确认成 `confirmed`，复核员标签不代行。
2. **关联不自动合并**：会议合并、说话人更正、摘要重跑不自动产生或合并事项；
   同主题跨会议只登记歧义，由人工 `items-link` / `items-merge` 裁定，工作记录随归并保留。
3. **新摘要冲突分流**：
   - 未领取事项 → `claim_blocked`，负责人裁定前禁止领取、不删除任务；
   - 执行中事项 → `in_progress_impact`，维持原承诺与已花费工时，向执行人发交接通知，
     执行人显式确认前通知一直留在队列，报表更新不会让它消失；
   - 新增建议不会替换原承诺；带工时事项的撤销请求会被拒绝，只能维持并补说明。
4. **质量复核**：概率样本保存入选概率，定向争议样本单列且不可外推；双人冲突在第三人
   裁定前不计确定标签；`unknown` 不计正确；错误率只用已裁定的概率样本做分层估计并给
   置信区间；样本不支撑时明确“不建议减少复核”。
5. **标签不越权**：`missing` 只建待确认提案；`extra`/`duplicate`/`owner` 进入负责人
   裁定队列，撤销、归并、改归属均由 owner 决定。
6. **并发安全**：状态机 CHECK + 条件 UPDATE（乐观锁 `row_version`）+ 唯一索引保证
   并发领取/标注不会产生重复工作，后到标签不能覆盖先前人工决定。

## 目录结构

```
meetingq/            应用包（db/importer/items/conflicts/reviews/sampling/reports/workflow/cli）
tests/               unittest 测试（合成资料，每个用例独立临时工作目录）
inputs/              只读来源资料（契约见 INPUT_CONTRACT.md，应用从不写回）
workdir/             默认本地工作目录（SQLite 状态库、报告），已在 .gitignore 忽略
```

## 运行方式

隔离模式（推荐，忽略环境变量与当前目录，防止不可信输入目录中的同名模块被导入）：

```bash
# 全量测试
python -I tests/run_tests.py

# 端到端演练（自动建库、导入并走完整闭环，输出中文过程）
python -I . --workdir ./workdir --at 2026-11-23T08:00:00Z demo --input inputs
```

普通模式（仓库既有入口，同样跑全部测试）：

```bash
python -m unittest discover -s tests -v
```

> 说明：`python -I -m unittest discover` 在隔离模式下不会把仓库根加入模块搜索路径，
> 因此隔离模式请使用 `tests/run_tests.py`。

## 命令行作业指南

全局参数（放在子命令前）：`--workdir` 工作目录、`--as` 操作人、`--at` 固定 UTC 时间、
`--json` 机器可读输出。

### 1. 持续导入（幂等，可重复执行）

```bash
python -I . --workdir ./workdir import --input inputs
```

重复投递同一来源主键不产生新业务活动；同主键内容变化登记 `import_issues`
（`content_conflict`）并保留原值；孤儿引用整批回滚；manifest 哈希不符直接拒绝。

### 2. 事项落实（负责人 / 执行人）

```bash
python -I . --workdir ./workdir --as U1 items-create --proposal A1     # decision→待确认
python -I . --workdir ./workdir --as U1 items-confirm --item ITM-xxx   # 负责人确认（含依赖链路）
python -I . --workdir ./workdir --as U3 items-claim --item ITM-xxx     # 执行人领取
python -I . --workdir ./workdir --as U3 items-progress --item ITM-xxx --hours 2.5 --note 初稿
python -I . --workdir ./workdir --as U3 items-complete --item ITM-xxx --note 交付完成
python -I . --workdir ./workdir items-list --project P1
python -I . --workdir ./workdir execution-feedback                     # 工时与进展反馈
python -I . --workdir ./workdir items-detail --item ITM-xxx            # 全链路回查
```

人工关联与重复裁定（无标题相似度自动合并）：

```bash
python -I . --workdir ./workdir --as U2 items-link --item ITM-keep --proposal A8 --note 人工核对
python -I . --workdir ./workdir --as U2 items-merge --keep ITM-keep --duplicate ITM-dup --note 裁定重复
```

### 3. 冲突与交接

```bash
python -I . --workdir ./workdir conflicts-detect          # 扫描待人工裁定关系
python -I . --workdir ./workdir conflicts-list --project P2
python -I . --workdir ./workdir --as U2 conflicts-decide --conflict CFL-xxx \
    --action uphold --note "维持原承诺，新建议不替换"      # uphold/revoke/merge/dismiss
python -I . --workdir ./workdir handoff-queue --executor U4
python -I . --workdir ./workdir --as U4 handoff-ack --notification NTF-xxx --note 已知悉
```

### 4. 质量复核（抽样、标注、裁定、落账）

```bash
python -I . --workdir ./workdir --as Q1 review-plan --seed 20261123 --target A1
python -I . --workdir ./workdir --as Q1 review-import-labels --round RND-xxx   # 承载 reviews.json
python -I . --workdir ./workdir review-tasks --round RND-xxx
python -I . --workdir ./workdir --as Q1 review-label --task TSK-xxx --label extra
python -I . --workdir ./workdir --as U1 review-adjudicate --task TSK-xxx --label extra --note 裁定理由
python -I . --workdir ./workdir review-consensus --round RND-xxx
python -I . --workdir ./workdir --as Q1 review-apply --task TSK-xxx            # 落业务侧，不越权
python -I . --workdir ./workdir --as Q1 review-close --round RND-xxx
python -I . --workdir ./workdir lead-queue --project P1                         # 负责人待办
```

标签语义：`accepted` 正常、`extra` 多报承诺、`missing` 漏决定、`duplicate` 重复事项、
`owner` 归属错误、`unknown` 无法判断。

### 5. 报告与互相回查

```bash
python -I . --workdir ./workdir report --round RND-xxx --out reports
python -I . --workdir ./workdir report                       # 累计估计（同提案取最新裁定）
python -I . --workdir ./workdir trace-item --item ITM-xxx    # 事项→会议/标签/冲突/通知
python -I . --workdir ./workdir trace-review --task TSK-xxx  # 复核→提案/事项反向回查
python -I . --workdir ./workdir dashboard                    # 负责人工作台计数
python -I . --workdir ./workdir version-diff --meeting M0    # 摘要版本间事实变化
```

报告字段 `estimates` 按 `any_error/extra/missing/duplicate/owner` 分别给错误负担、
比率、标准误与置信区间；`targeted_excluded_from_estimation=true` 表明定向样本未参与
外推；`reduction_advice.advice=hold` 时附带具体原因（样本量、精度、未决冲突/通知）。

## 测试覆盖（42 个用例，全部使用合成资料）

- 导入幂等、manifest 防篡改、内容冲突登记不覆盖、孤儿整批回滚；
- 建议/决定/确认/领取/进展/完成状态机与项目角色权限；
- 人工关联、归并保留工时、终态留痕、乐观锁与并发领取唯一成功；
- 未领取阻断、执行中影响通知、带工时拒绝撤销、通知持久化与重复确认拒绝；
- 分层抽样容量与普通样本下限、种子可重复、入选概率留存、定向溢出拒绝；
- 双人冲突裁定、回避规则、unknown 不计正确、标签不越权、并发标注不重复；
- 仅概率样本的错误率/置信区间估计、小样本不降配建议、双向回查、工作目录隔离。

## 离线与资料安全

- 输入目录只读：状态与报告只写入 `--workdir`，不同工作目录互不污染。
- 读取不可信资料的进程使用 `python -I`，避免输入目录中的同名 `.py` 被导入。
- 容器内复核（可选）：`docker build -t local-meetingq . && docker run --rm --network none local-meetingq`。
