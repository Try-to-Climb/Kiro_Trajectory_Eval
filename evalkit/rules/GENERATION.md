# 规则生成的两种方式

规则可以手写,也可以让 LLM 生成。目前有两条生成路线,产出的都是**意图式** `.checks.json`
(和 `agent-eval.intent.checks.json` 同一形状),都走同一个引擎评测。

| | 一次性生成 | 分步生成 |
|---|---|---|
| 脚本 | `rules/generate-rule.sh` | `rules/genrule/` |
| LLM 调用 | 1 次 | 10~16 轮(ACP 单会话) |
| 耗时 | ~3 分钟 | ~2.5 分钟(三个 agent 并行也是这个量级) |
| 输入 | AUTHORING.md 全文 + agent 配置 | agent 配置 + 提示词 + skill 描述 + subagent 名单 |
| 中间产物 | 无 | `*.policy_ir.json`,可人工审 |
| 多用途 agent | 一份文件 | 每个任务类型一份文件 |

## 用法

一次性生成:

```bash
cd evalkit
# 改 rules/generate-rule.sh 顶部的 AGENT_JSON 路径,然后
bash rules/generate-rule.sh
```

分步生成:

```bash
cd evalkit

# ① 只看任务类型划分(1 轮,~6 秒),确认切分合理再往下走
python3 rules/genrule/extract_tasks.py <agent.json | prompt.md>

# ② 完整抽取(任务类型 → 每阶段 policy → 主干),每个任务类型出一份 IR
python3 rules/genrule/generate.py <agent.json | prompt.md>

# ③ 编译成规则文件(纯代码,不调 LLM)
python3 rules/genrule/compile_ir.py rules/genrule/out/<name>.policy_ir.json \
    --out rules/genrule/out/<name>.checks.json

# ④ 自检 + 评测(走原有引擎)
python3 -m trajectory.runner rules/genrule/out/<name>.checks.json --compile
python3 -m trajectory.runner rules/genrule/out/<name>.checks.json --session <sid>
```

产物落在 `rules/genrule/out/`,不会覆盖 `rules/` 下的手写规则。细节见
[`genrule/README.md`](genrule/README.md)。

## 实测结论(以 agent-eval 为例)

同一个被测 agent,三份规则对比:

| | 手写 | 一次性生成 | 分步生成 |
|---|---|---|---|
| 条数 | 14 | 27 | 29 |
| required | 5 | 11 | 6 |
| 覆盖阶段 | Phase 1-4 | Phase 1-5 | **Phase 1-8 全覆盖** |
| pipeline 质量 | 好(末步落在派发) | 好(末步落在派发) | 差(四步全是 write) |
| 归纳出宽条目(`dispatches eval-*`) | 有 | 有 | 无 |
| `if_claims` 抓造假 | 无 | 3 条 | 无 |
| 派发细分到具体子 agent | 无 | 有 | 有 |

**分步生成的长处是全和广,不是准。** 它按阶段逐轮抽取,所以提示词后半段(报告、自省、
实盘验证、深度评测)不会漏;但表达完整性目前落后于一次性生成——`pipeline` 挑不出关键
环节、不会把多条同类规则归纳成一条宽的、没用上 `if_claims`。

根因是结构性的:一次性生成直接读 `AUTHORING.md` 全文,能照着 §4 的 pipeline 范例和 §2 的
完整意图表写;分步生成的三份提示词是对 AUTHORING.md 的**转述**,转述丢了这些内容。
补法是在 policy / backbone 那几轮把 AUTHORING.md 相关章节原文附上,而不是改写。

## 怎么选

- **想快速起一份可用规则** → 一次性生成。表达最完整,一次调用。
- **被测 agent 流程长、阶段多,担心后半段漏掉** → 分步生成。
- **被测 agent 有多种互斥用途** → 分步生成。它按任务类型分文件,避免"为 A 任务写的规则
  被拿去判 B 任务的运行"(实测 22 个 agent-eval session 里只有 5 个走编排流程,其余任务
  类型完全不同)。
- **要求最高精度** → 手写,或以生成结果为底稿人工收口。生成产物目前有两类需人工过一遍:
  过宽的 target(如 `reads *.json`、`runs mkdir*`),以及无法表达的精确禁令
  (区分 `python3 x.py` 执行与 `cat x.py` 查看,需要底层 `type` + `exclude` 的多工具正则)。

## 关于 severity

两条路线都只能从提示词措辞推断级别,而 `required` 的真实含义是"每次合法运行都必然做"
—— 这个事实提示词里没有。分步生成在提示词里加了 7 条 `required` 否决项(标了 optional、
需用户确认、条件性、只适用部分范围、辅助产物、依赖既有状态、不确定),把 agent-eval 的
required 从 29 条压到 6 条。

若手头有若干**已知合法**且**同一任务类型**的真实 run,可用命中率校准:

```bash
python3 rules/genrule/compile_ir.py <ir.json> --calibrate <sid1> <sid2> ... --out <checks.json>
```

命中率 1.0 且样本 ≥5 → required;部分命中 → recommended;零命中 → optional 并标 REVIEW;
禁令被合法 run 触犯 → 剔除并报告。留一交叉验证显示顺序类(`pipeline` / `before`)最不稳定,
故一律封顶 recommended。
