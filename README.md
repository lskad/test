# Agent 查错月份的故障定位与补救实验

实验比较两个原因：Agent 把月份传错给工具（F2），或工具在执行数据库请求时把月份转错（F3）。F0 是无故障对照。程序生成同症状的配对轨迹，比较三个日志视图，并独立尝试四个位置的单步干预。

## 开始

使用 Python 3.10 或更高版本。代码只依赖 Python 标准库。随项目交付的正式本地结果可直接打开 [实验汇总](runs/course/results/summary.md) 和 [补救增益图](runs/course/results/rescue_heatmap.svg)。

```powershell
Set-Location -LiteralPath 'G:\python\python ob\数据挖掘'
python -m unittest discover -s tests -v
python experiment.py local --split dev --run runs/my-dev
python experiment.py local --split test --seed 20261120 --run runs/my-test
python experiment.py summary --run runs/my-test
```

`local` 每次创建一个新的运行目录。相同种子、代码和分组可重建同一套观测与实验条件。重算已有运行的报表用 `summary`；换种子或配置时给新运行取另一个目录名。

## 实验步骤与数据

1. **生成与处理数据。** A、B、C 三站各取 2025 年 2–11 月的 10 个目标月，每个任务使用独立的数据快照，含目标月及前月。每个站的 5、11 月用来调试。测试集中有 18 个数字不同、6 个两月总数及坏旗数恰好相同的任务。SQLite 查询与从原始观测计算的真值分开。固定的答辩示例是 A 站 9 月任务：目标月 100 条、12 条坏旗，该快照的前月 80 条、2 条坏旗。
2. **构造与验证轨迹。** 各任务运行 F0/F2/F3 三种条件。S1 提取任务，S2 形成工具实参，S3 执行真实 SQLite 查询，S4 汇总并引用回执。对每对故障检查上游一致、注入处首次出现差异、错月传播、关闭注入后恢复。
3. **盲判源头。** V0 只显示请求和报告，V1 加入任务状态和实际数据库查询，V2 只比 V1 多一个 `out_month` 字段。测试集产生 216 份打乱后的输入。规则基线可在本地运行；模型盲判单独调用。故障来源、任务编号、视图名和查询回执编号隔离在评分数据中。标签 `UNKNOWN` 表示证据不足，`OK` 表示判断无故障。
4. **找单点补救。** 从每条失败轨迹保存的 S1–S4 检查点分别执行原动作与替代动作。替代动作依次为重读任务、校正出参、校正数据库请求、核对回执并在错月时停止。只替换一个位置；后续仍会触发原先配置的后段故障。主实验一共 384 次本地续跑。停止能避免误报，在“交付正确统计”的验收中计为未完成。
5. **比较验收与汇总。** 数字验收只比较总数和坏旗数。证据验收还检查所引回执、实际站点、月份和与观测明细一致的计数。报表列出各组的成功次数、分母、误放率、补救增益与按基础任务配对的定位变化。

## 真实模型

默认服务为 DeepSeek 官方 `https://api.deepseek.com`，模型标识为 `deepseek-flash`。截至 2026-11-20，该标识指向 DeepSeek-V4.1-Flash-1017。实验固定非思考模式，盲判请求使用 JSON 输出，执行 Agent 使用函数工具调用。每次请求记录模型标识，响应日志也保留服务端返回的名称。官方别名以后可能更新。[DeepSeek 模型表](https://api-docs.deepseek.com/quick_start/pricing/)

本机首次设置或轮换 DeepSeek 密钥时运行 `python setup_deepseek.py`，按隐藏输入提示输入密钥。它由当前 Windows 用户通过 DPAPI 加密，项目目录中只保存代码和实验记录。也可通过 `DEEPSEEK_API_KEY` 为本次运行指定密钥。已有配置可直接运行以下命令；`runs/deepseek` 是单独的数据与结果目录，保留此前的本地基线。

```powershell
python experiment.py check --run runs/deepseek --max-calls 4
python experiment.py judge --run runs/deepseek --limit 3 --max-calls 6
python experiment.py judge --run runs/deepseek --max-calls 240
python experiment.py worker --run runs/deepseek --max-calls 300
```

`check` 在一个干净任务上验证模型查询 SQLite 后能否依据本轮回执提交正确数字，结果单独写入 `online/connectivity.jsonl`。正式盲判和执行 Agent 的统计各自独立。首次在其他机器使用时，可用 `python experiment.py local --split test --run runs/deepseek` 创建运行目录。

### 其他兼容端点

两个在线阶段需要支持 Chat Completions 兼容接口的服务。`worker` 还需要模型支持函数工具调用。先在本机 PowerShell 会话设置服务地址、真实模型名和凭据。服务地址填写到版本前缀，例如本地服务的 `http://127.0.0.1:8000/v1`。远程服务使用其公开的 HTTPS 地址。下面的命令从隐藏输入读取密钥，避免将密钥写入脚本或命令历史。

```powershell
$env:AGENT_BASE_URL = Read-Host '服务地址（含版本前缀）'
$env:AGENT_MODEL = Read-Host '模型名'
$env:AGENT_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'API 密钥' -AsSecureString)).Password
python experiment.py judge --run runs/course --limit 3 --max-calls 6
python experiment.py judge --run runs/course --max-calls 240
python experiment.py worker --run runs/course --max-calls 300
python experiment.py summary --run runs/course
```

- `judge` 为每份盲测输入创建独立会话，只把该视图的可见字段发给模型。成功输出和异常均落盘；同一配置可续跑。先用 `--limit` 小批量检查。对 216 份输入正式评分时省略该选项。
- `worker` 使用预选的 4 个测试任务，先收集合格的首个查询，再在同一前缀分出 F0/F2/F3。首次查询只注入一次，允许模型至多重查一次。仅对满足入选条件的失败轨迹在 S2、S3 做原动作/替代动作续跑，每组 3 次。模型一开始就自行恢复的任务也有记录，实际续跑次数可能少于 96。
- `--max-calls` 限制本次 HTTP 尝试次数，包括重试。一次续跑可能多次调用模型。按服务实际定价和调用记录核算费用。未配置模型时只运行 `local` 即可，此时报告中的模型样本为空。更换模型或提示词请另建运行目录，保持每一批实验条件一致。

## 查阅结果

| 路径（相对单次运行目录） | 用途 |
|---|---|
| `data/` | 原始观测、SQLite 查询库、任务清单、数据版本与校验信息 |
| `run.json` | 分组、种子、Python 版本及源文件摘要 |
| `results/traces.jsonl`、`checkpoints.jsonl` | 主轨迹、运行回执与四步重放起点 |
| `results/fault_truth.jsonl`、`construction_checks.jsonl` | 注入真值和配对构造核对记录 |
| `results/blind.jsonl`、`blind_key.jsonl`、`rules.jsonl` | 打乱的盲测输入、离线对照键、规则诊断结果 |
| `results/replays.jsonl`、`rescue_sets.csv` | 原动作/补救动作的逐次结果及各轨迹可补救位置 |
| `results/summary.md`、`localization.csv`、`rescue_matrix.csv`、`acceptance.csv` | 可复核的定位、补救和验收统计 |
| `online/` | 在线阶段的调用、盲判和真实执行记录，实际启动该阶段后创建 |

主实验的结论适用于合成单故障、列出的四种干预和相应权限。真实模型结果需独立报告。运行日志属于实验数据；课程要求的完整人机协作聊天记录另按时间顺序保存为 Markdown。
