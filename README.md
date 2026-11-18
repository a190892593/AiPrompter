# AI 面试助手 · 实时提词器

真实面试/仿真面试时，把**面试官和自己**的语音实时转成文字，判断面试官是否在提问，
并给出可以照着念的参考回答（极简 / 标准 / 深度三版可切）。整场结束自动评分复盘。

全程本机运行，页面由 Python 直接托管。**音频默认不出本机**：`ASR_PROVIDER=local` 时识别完全在本机完成；选 `volc` / `cloud` / `deepseek` 时音频会发到对应的云服务。发给大模型的是文本（转写 + 问题 + 岗位/简历背景），不是音频。

---

## 快速开始

用 [uv](https://docs.astral.sh/uv/) 管理环境，依赖固定在 `requirements.txt`。

**环境要求：Windows 10/11 + Python 3.12 及以上。** `numpy==2.5.5` 要求 `>=3.12`，`fastapi` / `onnxruntime` / `miniaudio` / `websockets` 要求 `>=3.11`，用 3.10 或 3.11 会在装依赖时直接失败。

### 1. 安装 uv（只需一次）

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 建环境并装依赖

```powershell
uv venv --python 3.12        # 没有 3.12 时 uv 会自己下
uv pip install -r requirements.txt
```

国内网络慢可以先切镜像：

```powershell
$env:UV_INDEX_URL="https://repo.huaweicloud.com/repository/pypi/simple"
```

### 3. 配置密钥

```powershell
copy .env.example .env
```

**没有 `LLM_API_KEY` 也能启动**：服务照常起来、界面和存储都正常，只是意图识别退回本地规则、话术用模板兜底（`/api/health` 里 `llm_live=false`、`model=rules`）。要真正出提词和评分，必须填一把能调模型的 Key。

语音识别 `ASR_PROVIDER` 共 6 个取值，默认 `auto`：

| 取值 | 说明 | 需要什么 |
|---|---|---|
| `auto`（默认） | 按 `local → volc → cloud → mock` 顺序挑第一个可用的 | 见下面各行 |
| `local` | 本地离线识别（sherpa-onnx paraformer） | `models/` 下的语音模型，约 226MB |
| `volc` | 火山豆包流式识别 2.0 | `VOLC_SPEECH_KEY` + `VOLC_APP_ID` |
| `cloud` | 任意 OpenAI 兼容的 `/audio/transcriptions` | `CLOUD_ASR_KEY` |
| `deepseek` | DeepSeek 官方 ASR（[`POST /v1/tools/asr`](https://api-docs.deepseek.com/zh-cn/guides/asr/)），默认复用 `LLM_API_KEY`，按音频时长计费（0.39 元/小时，限时五折） | 一把 DeepSeek Key |
| `mock` | 演示脚本，不需要麦克风，但**不会识别真实语音** | 无 |

> ⚠️ 默认的 `auto` 在「既没下载本地模型、也没配火山 Key」时会静默落到 `mock`：界面一切正常，但你说话不会上屏。第一次跑请显式写 `ASR_PROVIDER=local` 或 `volc`，别靠 `auto` 猜。

DeepSeek ASR 是**整段上传**（非流式）：按静音切句，一句说完再把整段 WAV（16k 单声道，≤120 秒）发走，所以是"说完一句、1 秒左右整句上屏"，不逐字出字。想用单独的 Key 可以加 `DEEPSEEK_ASR_KEY`，想走代理可以加 `DEEPSEEK_ASR_BASE_URL`（默认跟随 `LLM_BASE_URL`）。

### 4. 下载本地模型（可选）

用到本地识别或题库语义召回时才需要：

```powershell
uv run python tools/download_models.py --semantic          # 语音 + 语义，都下
uv run python tools/download_models.py                     # 只下语音识别模型
uv run python tools/download_models.py --only-semantic     # 只下语义召回模型
```

- 语音识别模型：约 226MB（sherpa-onnx paraformer 中英双语）→ `models/sherpa-onnx-streaming-paraformer-bilingual-zh-en`
- 语义召回模型：约 23MB（bge-small-zh 量化版）→ `models/bge-small-zh-v1.5`

### 5. 前端（仓库已带构建产物，可跳过）

只有改了 `frontend/src/` 才需要重新构建。这一步需要 Node 18+/20+：

```powershell
corepack enable pnpm        # Node 自带 corepack，一条命令就有 pnpm
cd frontend
pnpm install
pnpm build                  # 产物写到 frontend/build
```

### 6. 启动

```powershell
uv run python -m app
```

也可以用它启动（进程异常退出会自动重启，面试更稳）：

```powershell
powershell -ExecutionPolicy Bypass -File run.ps1
```

启动后不会自动打开浏览器，日志在 `data/logs/app.log`。

需要自动开页就加 `--open-browser`（记得用 `uv run`，系统 Python 里没有装依赖）：

```powershell
uv run python -m app --open-browser
```

- **控制台窗口**：从终端启动会保留窗口、`Ctrl+C` 停止；用双击 `run.ps1` / 快捷方式启动时，如果控制台里只剩 python 进程（说明是后台拉起的）才会自动隐藏。想强制留着看日志就加 `--console`。
- **用 `run.ps1` 时**：进程退出会被 3 秒后自动拉起，这是设计如此；要真正停下得结束启动器（`powershell` 进程），只杀 python 会被重新拉起。
- 服务地址 <http://127.0.0.1:8765> 。
- 页面只是显示器，关掉页面不影响采集；只有点「结束并评分」才会结束本场。

### 依赖清单

`requirements.txt` 里只有直接依赖：

| 包 | 用途 |
|---|---|
| fastapi / uvicorn[standard] / websockets | Web 服务与实时推送 |
| httpx | 调用大模型接口 |
| python-dotenv | 读取 `.env` |
| numpy | 音频重采样与向量运算 |
| PyAudioWPatch | WASAPI 环回采集（仅 Windows） |
| sherpa-onnx / onnxruntime | 本地离线语音识别 |
| tokenizers / jieba | 题库语义召回与中文分词 |
| edge-tts / miniaudio | 仿真 TTS 在线回退（SAPI 不可用时）与音频播放 |
| pypdf / python-docx | 简历 PDF / Word 解析成 Markdown |

---

## 核心机制（追问 / 反问 / 意图识别）

### 多轮追问（追问链）

- 意图识别输出 `followup` 字段：以「那…呢 / 那如果 / 刚才你说 / 除了…还有 / 为什么 / 具体多少」等承接词开头的问句默认判为追问（前提是前面已经有面试官提问 + 候选人回答，且不是新话题）
- 追问被打上「追问」徽章；生成话术时会把「上一问 + 上一版话术要点」一起给模型，回答先承接上文再展开
- 追问去重阈值放松到 0.96（普通问题 0.85），避免「那如果量再涨十倍呢」被误判成重复而漏答

### 追问/反问链（话题树）

- 每张卡片记录 `root_id / parent_id / depth`：Q → 追问 → 再追问 → 反问，形成一棵话题树
- 生成话术时把**整条链**（第1问、第2问追加、已答要点）一起给模型，而不只是上一问
- 卡片显示「追问 · L2」「反问 · L3」徽章 + 「↳ 承接：上一问」行，一眼看出在第几层
- 面试详情的评分报告里会给出话题链统计：话题数、最深追问层数、追问次数、反问次数
- **话题链评分**：先按话题链打分（root_question / depth / score / comment / weakest_layer），再按单题打分；
  链越深越虚分越低，并指出这条链里最虚的一轮（weakest_layer）

### 反问/质疑（challenge）

- 意图识别输出 `challenge` 字段：面试官用「你确定吗 / 真的吗 / 凭什么 / 有数据吗 / 这能行吗 / 怎么证明 / 为什么不用 X / 那岂不是…」等方式反问质疑，判为需要回答的问题
- 卡片显示「反问」徽章；话术会收到【提问方式】提示：先正面回应质疑（数据/线上结果/边界取舍），再展开细节
- 反问与追问可同时为 true（如「那这个方案真的扛得住吗」），话术提示会合并成“先承接上一问，再回应质疑”
- 模型漏标时用质疑词表兜底（CHALLENGE_MARKERS），不依赖模型稳定输出

### 意图识别 → 提词答案生成：完整调用栈与实测耗时

#### 调用栈（代码级）

```
① ASR 定稿（VAD 静音满 900ms，或识别引擎先给出端点——两者任一命中）
   app/state.py: Session._run_channel → emit_final(channel="interviewer", text)
     ├─ correct_transcript()                     本地术语清洗（定义在 app/asr/correct.py）  ~0ms
     ├─ record_final() + 广播 final              落盘/上屏
     └─ app/pipeline.py: feed_final()            写上下文
          └─ _schedule_judge()                   取消上一次未完成判题，重新排

② 自适应去抖（app/pipeline.py: _judge_after_debounce）
   · 完整句（。？！? 结尾，或 ≥12 字且不以“那个/我是说/如果/然后”结尾）→ 120ms
   · 明显没说完（半句/口头禅结尾）                             → 600ms

③ 判题本地准备（app/pipeline.py: _judge）                       <1ms
   ├─ 判题缓存：同一句话 45s 内重复 → 直接复用结论（0 次模型调用）
   ├─ 确定性快速通道：明确旁白/引子/短附和 → 直接判定（0 次模型调用）
   ├─ memory.similar()：命中人工纠错经验 → 直接返回（0 次模型调用）
   └─ prompts.render("judge.user")：1725 token（前缀缓存命中 1536）

④ 意图识别模型（app/judge.py: classify → app/llm.py: chat_json）
   llm.chat_json(temperature=0.1, max_tokens=220, thinking=False)
     ├─ TTFT（首 token）                        450~680ms
     ├─ 解码 85~92 token                        400~600ms（5~7ms/token）
     └─ 返回 {type, question, focus, spans, followup, challenge,
              corrected, search_queries, confidence, reason}
   并行：_start_prefetch() → 题库检索 + 记忆加载（467ms，被完全隐藏）
   注意：意图识别默认 thinking=False，由独立开关 `JUDGE_THINKING` 控制，不受答案的「深度思考」总开关影响

⑤ 判题后处理（<1ms，除标注外都是异步派发，不阻塞出卡）
   ├─ corrected 回写气泡/上下文（广播 final_update）             同步
   ├─ 出卡广播 question（含追问/反问链 root/parent/depth）       同步
   ├─ 启动 _produce_answer（标准版流式 + 极简/深度并行）         异步
   ├─ _post_verify（二次复核）  0.85~0.97s                      异步
   └─ _post_rewrite（问题重写） 0.8~1.2s，仅空泛问题触发        异步

⑥ 答案生成（app/pipeline.py: _produce_answer，出卡后立即开始）
   ├─ 取预热好的 references/memory（没有则 asyncio.gather 并行检索）
   ├─ judge.answer_messages：岗位 JD + 简历 + 记忆 + 问题 + 考察点
   │                         + 追问链 + 提问方式（质疑/追问）
   ├─ 标准版：llm.chat_stream_events → answer_delta 逐字推送
   ├─ 极简/深度：judge.compose_answer 并行（深度档在 `LLM_THINKING` 打开时 thinking=True）
   ├─ _polish：占位符兜底改写（`_produce_answer` 内定义的闭包）
   └─ answer_done → answer_variants（三版齐全）
```

#### 实测耗时（本机 Windows + DeepSeek-flash，2026-11）

| 阶段 | 实测 | 说明 |
|---|---|---|
| VAD 静音判定 | 900ms | `VAD_SILENCE_MS`，可调 |
| 自适应去抖 | 120ms / 600ms | 完整句 120ms，未完成句 600ms |
| 本地预处理 | <1ms | 术语规则表 0.38ms、经验库 0.14ms、prompt 拼装 0.04ms |
| 判题 prompt | 1725 token | 前缀缓存命中 1536，仅 ~190 需 prefill |
| 判题 TTFT | 450~680ms | 前缀缓存 + HTTP 预热后 |
| 判题解码 | 400~600ms | 输出 85~92 token |
| **判题总耗时** | **0.9~1.3s** | 出卡时间主要由它决定 |
| 参考检索预热 | 467ms（隐藏） | 与判题并行，不算进出卡时间 |
| **定稿 → 出卡** | **约 1.3s（完整句）** | 120ms 去抖 + ~1.2s 判题 |
| 答案首字 | 出卡后 +0.55~0.6s | 标准版开始流式 |
| 标准版完成 | 出卡后 +1.3s | 极简 +0.6~0.8s |
| 深度版完成 | 出卡后 +5.4s | `LLM_THINKING` 打开时 thinking=True（本机已开），只影响深度 tab |
| 三版齐全 | 出卡后 +5.4~6.7s | 等最慢的深度版；标准版早已可见 |
| 二次复核（异步） | 0.85~0.97s | 不阻塞出卡 |
| 问题重写（异步） | 0.8~1.2s | 仅空泛问题触发，先出卡再更新 |

#### 一次完整请求的事件时间线（实测）

```
定稿后 +128ms    judge_start（去抖 120ms 结束）
+1334ms          出卡 question + 开始生成答案
+1924ms          答案首字（标准版）
+2637ms          标准版完成
+6688ms          三版齐全（深度档 thinking 完成）
```

从“说完最后一个字”到出卡约 **2.2s**（900ms VAD + 120ms 去抖 + ~1.2s 判题）。
判题条上显示的 `1.2~1.9s` 是第 ③④⑤ 步的合计，不含 VAD 和去抖。

#### 相关参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `VAD_SILENCE_MS` | 900 | 静音多久算一句话说完 |
| 自适应去抖 | 120 / 600ms | 代码常量，完整句/未完成句（`JUDGE_DEBOUNCE_MS` 已不参与实际去抖，只出现在「意图识别开始」的日志里） |
| `JUDGE_TIMEOUT` | 20s | 判题超时，超时走规则兜底 |
| `JUDGE_CONFIDENCE` | 0.6 | 置信度门槛 |
| `INTENT_VERIFY_BELOW` | 0.85 | 低于该置信度触发异步复核 |
| 判题 max_tokens | 220 | 输出 JSON 上限（`JUDGE_THINKING=1` 时放宽到 900） |
| 判题 thinking | 关 | 二次复核 / 问题重写**硬关闭**；主判由 `JUDGE_THINKING` 决定，默认关 |
| 答案 thinking | 仅深度档 | 极简/标准不思考；深度档受 `LLM_THINKING` 总开关控制（**默认关**，本机 `.env` 里开了 1） |

### 意图识别提速（第二轮，精度不变）

- **术语纠正并入意图识别（一次调用完成）**：判题输出新增 `corrected`（纠正后的整句），流水线用它回写气泡与上下文；不再有独立的术语纠错模型调用（省 ~0.6s，也少一个竞态）
- **确定性快速通道**：明确的旁白/引子/短附和直接判定，不调模型（省 ~1s）
- **同句判题缓存**：同一句话 45 秒内重复出现（ASR 抖动/面试官重复）直接复用结论
- **提示词静态前置**：规则/输出/示例放最前，便于上下文缓存命中；输出 max_tokens 400→220
- 实测：单次意图识别 prefill token 2147→1667（-22%），分类耗时通常 0.8~1.2s

### 历史优化记录（部分已回退）

- 已保留：三版并发生成、术语纠正并入意图识别、判题前缀缓存、同句判题缓存、确定性快速通道
- 已回退：投机预判（半句复用）默认关闭（`SPECULATIVE_INTENT=0`），优先保证精度
- 注：二次校验当前默认门槛 0.85（`INTENT_VERIFY_BELOW`），不是 0.75

### 意图识别实时统计（提词器顶部）

提词器顶部常驻一条统计条，展开可看明细：
- 意图识别次数、出卡数、跳过数
- 复核次数 / 撤卡数 / 修正数（异步复核）
- 追问次数、反问次数、最深追问层数
- 平均意图识别耗时、最长耗时
- 人工纠错次数（点「意图识别错了？」计入）
- 展开后还有跳过分类：旁白/离场、非提问、低置信、重复。注意「内联二次校验拦截」这一格默认恒为 0——正常链路走的是下面的**异步撤卡**，撤卡单独计在「撤卡数」里。

### 意图识别异步复核（卡片先出 + 撤卡）

1. 意图识别出卡后立即广播卡片并开始生成话术（不阻塞）
2. 后台异步复核，**满足任一条件就触发**：置信度 < `INTENT_VERIFY_BELOW`（默认 0.85）、问题由碎片合并（`spans ≥ 2`）、判定为追问（`followup`）、判定为反问（`challenge`）
3. 复核通过：无感知；复核修正：更新卡片问题并重新生成话术；复核不通过：卡片打「已撤销」并取消话术
4. 复核内容：是否真的是要回答的问题、是否忠实原话、最后一句是不是旁白（避免重复出卡）
5. 相关事件：`verify_result` / `question_updated` / `question_rejected`
6. 开关：设置页「意图识别」分组，或 `.env` 的 `INTENT_VERIFY=0` 关闭；`INTENT_VERIFY_TIMEOUT=8` 控制超时

### 检索改写（search_queries）

意图识别在一次模型调用里顺带输出 1~3 条「检索改写」，不额外增加一次 LLM 调用：

```json
{"question": "如何衡量检索质量？", "search_queries": ["召回率 评测", "RAG 召回 指标"]}
```

- `question` 是自包含的问题，用于出卡和答案生成
- `search_queries` 只保留「术语 + 对象」的关键词短语，专门用于题库/资料检索
- 检索时用 `question + search_queries` 多路召回，按「多路共识分」合并去重（被越多路命中、名次越靠前越优先）
- 检索改写在卡片里落盘（`questions[].search_queries`），方便回放排查

实测（12 条口语化问法 × 3 轮平均，`tools/eval_search_queries.py`）：

| 路线 | Recall@1 | Recall@3 | MRR |
|---|---|---|---|
| A. 原话直接检索 | 66.7% | 83.3% | 0.750 |
| B. 意图识别还原后的 question | 66.7% | 83.3% | 0.727 |
| C. question + search_queries 多路 | 66.7% | **88.9%** | **0.759** |

结论：多路检索主要提升 Recall@3（+5.6 个百分点）和 MRR；Recall@1 不变，因为 Top1 仍由规范化问题决定。代价是判题输出多 ~20-40 token，无额外模型调用。

## 意图识别评测（`uv run python tools/eval_judge.py`）

| 指标 | 说明 |
|---|---|
| 是否需要回答准确率 | 第一指标：该不该开口 |
| 细分类型准确率 | question/instruction/statement/chitchat/aside/incomplete |
| 问题字面覆盖率 | 归纳问题与原预期问题的双字匹配 |
| 问题语义相似度 | 本地 bge 向量余弦，衡量"是不是同一个问题" |
| 旁白准确率 | 接电话/网络噪音等 aside 识别 |

## 页面地址（每个菜单都有独立 URL，刷新不会跳回首页）

| 页面 | 地址 |
|---|---|
| 面试列表 | `/` |
| 实时面试 | `/interviews/<id>` |
| 面试详情 | `/detail/<id>` |
| 提示词模板 / 面试题库 / 岗位管理 / 简历管理 / 设置 / 余额 / 调用日志 | `/prompts` `/bank` `/positions` `/resumes` `/settings` `/balance` `/logs` |

## 主要接口（给改代码的人）

前端只通过两类通道和后台说话：普通 REST + 一条 WebSocket `WS /ws/events`（`question` / `answer_delta` / `answer_variants` / `verify_result` / `question_updated` / `question_rejected` / `final` 等事件都从它推）。下面是改动代码时会碰到的接口。

| 分组 | 接口 |
|---|---|
| 健康与设备 | `GET /api/health`、`GET /api/engines`、`GET /api/devices`、`POST /api/devices/monitor`、`POST /api/devices/monitor/stop` |
| 会话 | `POST /api/session/start` / `stop` / `finish` / `resume`、`GET /api/session/state`、`GET /api/session/latest`、`POST /api/session/devices`、`POST /api/session/engine` |
| 仿真 | `POST /api/session/sim`（开关 + 是否发声 + 题目来源，一个接口全包） |
| 测试注入 | `POST /api/session/inject`（注入一句面试官提问，走真实定稿链路）、`POST /api/session/inject_audio` |
| 面试记录 | `GET/POST /api/interviews`、`GET/DELETE /api/interviews/{id}`、`POST /api/interviews/{id}/stop`、`POST /api/interviews/{id}/resume`、`GET /api/interviews/running`、`POST /api/interviews/batch-delete` |
| 答案 | `POST /api/answer/generate`（手动提问）、`POST /api/answer/suggest` |
| 配置与内容 | `GET/PUT /api/settings`、`GET /api/prompts`、`PUT /api/prompts/{key}`、`POST /api/prompts/{key}/reset`、`/api/bank`、`/api/positions`、`/api/resumes`、`POST /api/resumes/parse`、`POST /api/tts/check` |
| 观测 | `GET /api/llm/calls`、`GET /api/llm/calls/{id}`、`GET /api/llm/stats`、`GET /api/stats`、`GET /api/balance`、`GET /api/profile`、`POST /api/feedback`（「意图识别错了？」就是打这里） |

## 界面（左侧菜单，可折叠）

左侧栏共 6 项：

| 菜单 | 作用 |
|---|---|
| 面试列表（默认页） | 历史面试列表 + 顶部唯一一个「🎙 开始面试」按钮，弹窗里选岗位和简历后进入实时面试（仿真不是入口，是面试开始后的开关） |
| 提示词模板 | 8 条 LLM 提示词在线可改，改完立即生效 |
| 面试题库 | 题目 + 参考答案，按分类（自由填写的岗位名，默认「通用」）归类 |
| 岗位管理 | 岗位名 / JD / 技能要求，新建面试时选择 |
| 简历管理 | 多份个人简历，支持上传 PDF / Word 自动转 Markdown，新建面试时选择，内容作为回答背景 |
| 调用日志 | 每次 LLM 调用的完整出入参、耗时、token |

另外两个不在左侧栏：**设置**（模型、识别、采集、意图识别参数）和**余额**（DeepSeek 余额 + 调用量统计）在顶部导航；**面试详情**从列表行的「详情」进入，是完整对话流 + 三版提词 + 评分报告（不是播放器）。

### 详情页布局

- 结束状态后面直接跟「结束原因：xxx」，不再单独占一条提示条
- 意图识别统计 + 意图识别记录挪到右栏「提词器」列表上方，和问题卡片放在一起看
- 头部元信息分两行：第一行时间/时长/对话数/问题数，第二行状态/结束原因/识别引擎/模型/费用，窄屏自动换行不挤成半截
- 对话流和提词器两栏撑满页面高度，各自内部滚动，不再在页面底部留大块空白
- **结束面试后不再显示「本场面试已结束 / 本场面试评分」页面**，直接跳到这场面试的详情页（实时页和面试列表里结束都会跳）
- 详情页评分报告默认隐藏：右上角「返回面试列表」下方有「📊 评分报告」按钮，点击弹窗查看，Esc 或点遮罩关闭
- 每张问题卡片下方可展开「模型调用（意图识别 + 三版答案）」，与实时提词器一致：模型、耗时、token、成功状态，点行可看完整入参/出参

### 面试ID

新建面试的 ID 格式：**年月日时分 + 4 位随机数**，例如 `202611181542-6859`。

- 时间前缀让列表按分钟排序，同一分钟内也不会重复（生成时查重重试）
- 面试列表每一行显示 `ID xxxx`，详情页标题下也显示
- 历史数据（旧 12 位十六进制 ID）保持原样，不影响继续面试和回放

### 公共顶部导航

所有页面共用一条顶部导航，右侧三个入口：

- 「💰 余额」：DeepSeek 余额 + 调用量统计
- 「🌙 夜间模式 / ☀️ 白天模式」：切换主题，记忆在 localStorage
- 「⚙ 设置」：模型、识别、采集、意图识别参数，改完立即生效

左侧栏因此只保留业务菜单（面试列表 / 提示词模板 / 面试题库 / 岗位管理 / 简历管理 / 调用日志）。

### 一场面试的一生

1. 进入「面试列表」→ 点「🎙 开始面试」，在弹窗里选岗位和个人简历（都必填），点「开始」进入实时面试。**仿真不是入口**，是进入面试后打开的「仿真输入」开关。
2. 开始瞬间**建一条记录并开始计时**，刷新页面、关掉浏览器都不会停表（时间存在服务端）。
3. 在面试页点「结束并评分」才正常结束，自动生成评分报告并**直接进入本场面试详情页**（不再停留在评分页）。
4. **没手动停、又超过 2 小时**的，服务端巡检（每 30 秒）会把它标记成「系统自动中断」，结束时间卡在开始后 2 小时整。
5. 没结束的面试随时可以重新进入：提词器页顶部会提示「有一场面试还没结束」，点「继续这场面试」就回到原来的对话流和提词卡片。
6. 「面试列表」里一场一条，带状态、用时、总分，点开按时间轴回放。

---

## 数据流

```
腾讯会议/飞书 扬声器 ──WASAPI 环回──┐
                                    ├─VAD 静音 900ms─▶ 流式 ASR 定稿 ─▶ 对话流
麦克风 ─────────────────────────────┘
                                        │
                     自适应去抖 120ms（完整句）/ 600ms（未完句）
                                        ▼
              意图识别（deepseek-flash，thinking=False，约 1.2s）
              ├─ 同时并行：题库参考检索 + 记忆加载（467ms，被隐藏）
              └─ 输出：类型 / 真正的提问 / 追问 / 反问 / 纠正后整句
                                        │
                          出卡（定稿后约 1.3s）
                                        │
                       答案生成（deepseek-flash，流式）
                     ├─ 标准版：首字 +0.55s，约 1.3s 完成（不思考）
                     ├─ 极简：约 +0.6~0.8s（不思考）
                     └─ 深度：后台并行，约 +5.4s（仅此档会开思考，需 LLM_THINKING=1）
                                        │
              异步复核（0.85~0.97s）：通过/修正/撤卡，不阻塞出卡
              异步重写（0.8~1.2s）：问题空泛时先出卡再更新
                                        │
                          结束 ─▶ 评分报告（含话题链评分）
```

上下文分三层拼装：

1. **固定层**：岗位 JD + 技能要求 + 跨场历史薄弱点
2. **滑动窗口**：意图识别最近 20 句、话术最近 16 句（带时间戳和停顿标记）、评分用全量
3. **检索层**：题库参考答案（语义 + BM25 混合召回；预热取 top 2，正式检索/多路合并最多 3 条）

同一句被识别引擎重发时（火山按它自己的句子边界回传累积文本，和本地 VAD 切句不同步），
按 `TRANSCRIPT_DEDUP_SECONDS`（默认 30 秒）去重：重复的丢弃，更完整的原地覆盖，不会出现「双份对话」。

---

## 响应速度与思考模式

一次提问的时间线（本机 + DeepSeek 实测）：

| 阶段 | 实测时间 |
|---|---|
| 说话结束 → ASR 定稿 | 900ms（VAD 静音判定） |
| 定稿 → 出卡 | 约 1.3s（去抖 120ms + 判题 ~1.2s） |
| 出卡 → 标准版首字 | +0.55~0.6s |
| 出卡 → 标准版完成 | +1.3s（不思考） |
| 出卡 → 极简版完成 | +0.6~0.8s（不思考） |
| 出卡 → 深度版完成 | +5.4s（thinking=True，只影响深度 tab） |
| 二次复核 | +0.85~0.97s（异步，不阻塞出卡） |

**全部是流式输出**：正文逐字推送（`answer_delta`），思考过程也逐字推送（`answer_reasoning`），
不是等整段生成完才显示。

已做的提速：

- **自适应去抖**：固定 900ms → 完整句 120ms / 未完成句 600ms
- **参考检索并行预热**：467ms 的题库检索+记忆加载与判题并行，出卡时间不再包含它
- **三版并行生成**：标准版流式的同时，极简/深度在后台并行跑
- **思考分档**：只有深度档 thinking=True，标准/极简首字约 0.55s
- **启动预热**：预热判题提示词前缀（命中 1536/1725 token）+ HTTP 连接
- **意图识别模型用 flash**：盲测准确率与 chat 相同，但快约 2 秒

### 三版答案：表格布局 + 可拖拽列宽

极简/标准/深度三列改为共享边框的表格布局，列边界可拖拽调整宽度：

- 默认列宽 1:2:3（极简 1/6、标准 2/6、深度 3/6）
- 拖动两列之间的分隔条即可调宽，单列最小 8%
- 拖完自动记住（localStorage 键 `answer-col-ratios`），换场面试/刷新页面保持
- 窄屏（<1100px）自动改为上下堆叠
- 实时提词器与历史详情页共用同一个组件 `AnswerColumns`

### 三版列头显示生成模式

答案卡片的极简/标准/深度三列，列头会显示该版实际使用的生成模式：

- `联网搜索`：标准档自动联网
- `深度思考`：深度档开思考（未联网时）
- `深度思考+联网搜索`：深度档思考 + 联网
- 极简档不思考、不联网，不显示标签

模式字段随 `answer_done` / `answer_variants` 事件下发并落盘（`questions[].modes`），回放/详情页同样显示。

### 深度思考与联网搜索（DeepSeek 原生能力）

两个开关都直接调用 DeepSeek API 的原生能力，不在本地自己实现搜索或推理：

| 开关 | API 参数 | 效果 |
|---|---|---|
| 深度思考 | `thinking: {"type": "enabled"}` | 模型先输出思考过程（`reasoning_content`），再给正文 |
| 联网搜索 | `tools: [deepseek_web_search]` | 服务端自动搜索，回答带引用（`annotations`） |
| 两者同开 | 思考 + 搜索 | 深度思考搜索：多步推理中自主搜索、打开网页 |

- 搜索、抓取、上下文拼接全部由 DeepSeek 服务端完成，客户端只负责展示
- 回答里的引用会解析成「来源」chip，点开是原网页
- **自动联网（新）**：自动答案的**标准档和深度档默认开启联网**，极简档不联网；深度档是「深度思考 + 联网搜索」同开（是否真的思考取决于 `LLM_THINKING`，联网不受它影响）
- 手动提问仍由输入框的「深度思考 / 智能搜索」chip 控制，不受自动策略影响
- 开关：`.env` 的 `ANSWER_WEB_SEARCH=0` 可整体关闭自动联网
- 意图识别默认关闭思考（二次复核、问题重写是硬关闭）；答案极简/标准不思考，只有深度档会开思考（受 `LLM_THINKING` 总开关控制，默认关）
- 模型由 `.env` 的 `LLM_MODEL` 决定（代码默认 `deepseek-chat`，`.env.example` 配的是 `deepseek-flash`）；**代码不会因为开思考就自动切到 `deepseek-reasoner`**

### 意图识别要不要开思考？（实测结论：不开）

同一套 48 个意图识别用例，开/关思考两次评测：

| 指标 | 思考=关（默认） | 思考=开 |
|---|---|---|
| 是否需要回答 | 100% | 100% |
| 细分类型 | 97.9% | 97.9% |
| 追问 / 反问识别 | 100% / 100% | 100% / 100% |
| 问题语义相似度 | 90.8% | 90.6% |
| 误报 / 漏报 | 0 / 0 | 0 / 0 |
| 单次判题耗时 | **0.86~1.34s** | **1.46~3.14s** |
| 单次输出 token | ~90 | 160~295（含 reasoning） |

结论：**意图识别开思考准确率没有提升，耗时翻倍**。结构化短输出更适合直接出答案。

**哪些调用建议开思考：**

| 调用 | 建议 | 原因 |
|---|---|---|
| 意图识别主判 | 关 | 输出是短 JSON，思考无收益且慢 |
| 二次复核 | 关（已关） | 只需判断是/否 |
| 问题重写 | 关（已关） | 短句改写，思考收益小 |
| 答案 · 极简档 | 关 | 要快、要短 |
| 答案 · 标准档 | 关 | 首字要快，事实靠联网和简历 |
| 答案 · 深度档 | **开** | STAR 结构、取舍分析、数据组织需要推理 |
| 评分报告 | **开（默认已开）** | 报告允许慢一点，思考有助于维度点评 |

对比开关：`.env` 里 `JUDGE_THINKING=1` 可开意图识别思考（默认 0，不建议）。

---

## 仿真面试

### 两种面试模式共用同一套意图识别与答案链路

真实面试和仿真面试**不是两套实现**，意图识别、追问/反问判定、答案生成、异步复核、问题重写全部复用同一个 `Pipeline` 实例：

```
真实面试：
  WASAPI 环回/麦克风 → 流式 ASR → session.emit_final()
仿真面试：
  题库/简历生成题目 → TTS 播放 → ASR 识别 → session.emit_final()
  （TTS 不可用时降级：直接 session.emit_final(文字)）
        │
        ▼
  同一个 session.pipeline.feed_final()
        ├─ 意图识别（judge.classify，thinking=False）
        ├─ 追问/反问链、话题树
        ├─ 答案生成（极简/标准/深度，档位规则完全一致）
        ├─ 异步复核 / 问题重写
        └─ 评分报告（含话题链评分）
```

| 环节 | 真实面试 | 仿真面试 | 是否同一份代码 |
|---|---|---|---|
| 音频采集 | WASAPI 环回 + 麦克风 | TTS 音频注入 `feed_audio` | 采集不同，**后续相同** |
| ASR 识别 | 同一引擎（auto/local/volc） | 同一引擎 | ✅ |
| 定稿处理（碎片合并） | `session.emit_final` | `session.emit_final` | ✅ |
| 意图识别 | `pipeline.feed_final → judge.classify` | 同左 | ✅ |
| 追问/反问/话题链 | `pipeline._judge` | 同左 | ✅ |
| 答案生成（三版/档位/联网/思考） | `pipeline._produce_answer` | 同左 | ✅ |
| 异步复核 / 问题重写 | `pipeline._post_verify/_post_rewrite` | 同左 | ✅ |
| 评分报告 | `pipeline.finish → report.build_report` | 同左 | ✅ |

**唯一的模式差异**只在“题目从哪来”和“声音怎么进出”：
- 真实面试：题目来自面试官真实说话
- 仿真面试：题目来自题库/简历生成，再用 TTS 播出来走真实 ASR 链路
- 两者的 `SessionConfig` 可能不同（岗位/简历/引擎/档位），但进入 `Pipeline` 之后的所有处理逻辑一致



不需要真人、不需要开会。服务端把题库里的题 TTS 播出来（可选真的从扬声器发声），
走同一条 ASR → 意图识别 → 答案链路，再把答案播回麦克风通道，最多 20 轮（`SIM_QUESTION_LIMIT` 可调）。

真实面试时声音播放开关是锁死的——否则扬声器声音会被麦克风收回去造成回音和重复转写。

---

## 目录结构

```
app/
  __init__.py      包入口：第一次 import 就初始化日志
  __main__.py      入口：起 uvicorn；控制台隐藏规则、--console / --open-browser
  config.py        全部配置项与默认值（.env / 环境变量覆盖）
  server.py        FastAPI 路由 + WebSocket + 静态托管 frontend/build
  state.py         会话状态、双通道采集、VAD/端点定稿、持久化
  pipeline.py      意图识别流水线、三版答案、话题链、异步复核/重写
  judge.py         意图识别 / 答案 / 相关性（含启发式兜底与 warmup）
  llm.py           大模型统一调用层（JSON 模式、流式、thinking、联网搜索）
  bank.py          题库 + 混合召回（语义 bge-small-zh + BM25）
  embedding.py     本地 ONNX 向量模型
  prompts.py       8 条提示词仓库（可改、可恢复默认）
  positions.py     岗位管理
  profile.py       跨场记忆（薄弱项累积）
  memory.py        意图识别纠错样本（few-shot）
  report.py        结束评分（含话题链评分）
  simulator.py     服务端仿真面试（出题 → TTS → 回灌 ASR）
  interviews.py    面试记录 CRUD、ID 生成、超 2 小时自动中断
  resumes.py       简历库存储
  resume_parse.py  简历 PDF / Word → Markdown
  settings_store.py 运行时设置（设置页改的值，覆盖 .env）
  schemas.py       接口请求/响应模型
  llmlog.py        LLM 调用日志（进出参、耗时、token）
  logging_setup.py 统一日志（控制台 + 滚动文件）
  store.py         会话持久化
  tts.py           仿真 TTS（Windows SAPI，失败回退 edge-tts）
  asr/             语音识别：local(sherpa) / volc / cloud / deepseek / mock；correct.py 做术语纠正
  audio/           WASAPI 采集、重采样、VAD
frontend/
  src/             React 源码（改这里）
  build/           构建产物（pnpm build 生成，随仓库提供，Python 托管）
  package.json     前端依赖
  vite.config.ts   构建配置
requirements.txt   依赖清单（uv pip install -r）
run.ps1            启动器（进程异常退出自动重启）
.env.example       配置模板（复制成 .env 后填 Key）
tools/             模型下载、仿真脚本、自动化测试与评测、自检记录清理
```

---

## 部署到「只装 Python」的机器

**运行完全不需要 Node。** `frontend/build/` 是已编译好的静态文件（html 543B + js 243KB + css 75KB，共约 320KB），
随仓库提供，Python 直接托管。

客户机要求：**Windows 10/11 + Python 3.12 及以上**（依赖里有 `Requires-Python: >=3.12` 的包，3.10/3.11 装不上）。

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env      # 填 LLM_API_KEY
python -m app
```

不需要 uv。`uv run python -m app` 与上面最后一条等价，但**前提是 `.venv` 里已经装好依赖**——`uv run` 不会替你装 `requirements.txt`。

需要 Node 的只有一种情况：**改了前端源码后重新出产物**

```powershell
corepack enable pnpm
cd frontend
pnpm install
pnpm build                  # 产物写到 frontend/build，连同源码一起提交
```

忘了重新 build 也能发现：后端启动时会比对 `frontend/src` 下 `.ts/.tsx/.css` 与 `build/index.html` 的修改时间，产物过期会打印警告；完全没有 build 目录时会显示「前端还没构建」占位页。

### 模型文件

- 用火山云识别（`ASR_PROVIDER=volc`）：**不需要**任何本地模型
- 用本地离线识别（`local`）：需要 `models/sherpa-onnx-...`（约 230MB）
- 题库语义召回：需要 `models/bge-small-zh-v1.5`（约 23MB）；没有也能跑，会退回关键词召回

模型目录不在 git 里，交付时单独拷 `models/` 目录，或让客户机执行
`python tools/download_models.py --semantic`（同时下语音 + 语义模型；只要语义模型就用 `--only-semantic`，省掉 226MB）。

---

## 测试

### 统一开始入口 + 仿真输入开关

**仿真入口已统一**：后端只有一个控制器 `_apply_simulation` 和主接口 `POST /api/session/sim`。

```json
{ "enabled": true,              // 开/关仿真
  "audible": true,              // 可选：改声音播放
  "mode": "resume" }           // 可选：改提问来源 bank/resume
```

- 已在跑时改 `audible` / `mode` 只更新参数，不重启、不换题
- 前端只保留一个 `setSim({...})` 方法
- 旧入口已删除：`/api/session/simulate`、`/simulate/mode`、`/simulate/audio` 不再提供；新建面试只支持真实模式，开始后再用仿真开关
- 继续面试恢复仿真仍走同一个控制器

**仿真默认 20 题**：开启仿真输入后最多自动问 20 道，问卷满会自动提示「可以点结束并评分」，避免无限出题浪费 token。
可在设置页「仿真」分组调整 `sim_question_limit`，或 `.env` 的 `SIM_QUESTION_LIMIT`（默认 20）。

- 面试列表只有一个「开始面试」按钮，不再区分真实/仿真两个入口
- 面试进行中，提词器底部有「仿真输入」开关：
  - 开：自动出题 + TTS 把面试官问题播进系统 + 把参考答案当作你的回答播回，问题来源可选题库/简历+追问
  - 关：采集真实会议声音和你的麦克风
- 两种状态下意图识别、追问反问、三版话术、联网/思考、评分规则完全一致，唯一区别就是这个开关
- 对应接口：`POST /api/session/sim`，记录里会写 `mode` / `sim_enabled`

### 真实面试模式冒烟（无需真人说话）

**前置条件：先 `uv run python -m app` 把服务起起来**（脚本连的是写死的 `127.0.0.1:8765`，不会自己起服务），**并且当前不能有进行中的面试**，否则脚本直接退出码 1。

```bash
uv run python tools/smoke_real_mode.py
```

- 用真实面试链路起一场（打开 WASAPI 环回 + 麦克风采集、本地引擎、真实设备）
- 通过 `/api/session/inject` 注入一句面试官提问，验证：出卡 → 检索改写 → 三版话术 → 自动联网/深度思考 → 停止 → 评分 → 回放
- 再起一场仿真面试对照：TTS → ASR → 出卡 → 话术 → 停止 → 评分
- 自动清理测试记录；`--keep` 保留记录，页面里可看

**它不能替代真人试音**：真实声音进环回/麦克风这条路径（WASAPI 采集线程 → ASR）只有真人说话才能验证。

### 一键汇总（按资源分档）

```bash
uv run python tools/test_all.py            # 默认 core：约 20 秒，不调用大模型、不占音频设备
uv run python tools/test_all.py --models   # 追加：意图识别 48 用例 + 检索改写 12 用例（按量计费，需 LLM_API_KEY）
uv run python tools/test_all.py --smoke    # 追加：真实/仿真冒烟（占音频设备，需服务已在跑）
uv run python tools/test_all.py --full     # 全部：core + models + smoke
```

分档原则（避免重复跑同一条链路）：

| 档位 | 内容 | 耗时 | 资源 |
|---|---|---|---|
| core（默认） | 接口面自检（`full_test --fast`，10 项）、面试接口自检（`check_interview_api`）、面试状态机（`check_interviews`）、真实/仿真一致性（`check_mode_parity`）、题库检索评测（`eval_retrieval`） | 约 20 秒 | 不调用大模型；题库检索会跑本地 bge 向量 |
| models | 意图识别评测（48 用例，`eval_judge`）、检索改写评测（12 用例 × 1 轮，`eval_search_queries`） | 视模型速度，约 1 分钟 | 大模型 token（需要 `LLM_API_KEY`） |
| smoke | 真实面试冒烟（启动采集→注入→出卡→三版话术→停止→评分）+ 仿真输入开关开/关验证 | 约 1~2 分钟 | 音频设备 + 大模型 + 服务已启动 |

说明：
- 默认不再包含模型链路和音频冒烟，日常改代码只跑 core 即可
- `full_test --fast` 跳过 `t_session_flow`/`t_manual_ask`：前者与 smoke 的真实链路基本重合；**后者（手工提问 `/api/answer/generate`）目前没有自动覆盖，改了要手动验**
- 检索改写评测在汇总里跑 1 轮省资源；手工调优用 `uv run python tools/eval_search_queries.py --repeat 3`
- 本地服务未启动时，需要服务的步骤自动标 `SKIP`，不会被误判为失败
- 退出码：0 = 执行项全部通过；1 = 有失败项并打印原因

### 单独跑某一项

```bash
uv run python tools/full_test.py --fast        # 接口面（快）
uv run python tools/full_test.py --slow        # 接口面 + 实时会话/手工提问 + 仿真链路（真实模型调用，花 token）
uv run python tools/eval_judge.py              # 意图识别评测
uv run python tools/eval_search_queries.py --repeat 3   # 检索改写评测（取平均）
uv run python tools/eval_retrieval.py          # 题库召回（词法 vs 混合）
uv run python tools/smoke_real_mode.py         # 真实/仿真冒烟
uv run python tools/check_mode_parity.py       # 真实/仿真链路一致性
```

### 真实/仿真模式一致性（自动化）

```bash
uv run python tools/check_mode_parity.py
```

检查两种面试模式（真实 / 仿真）的意图识别与答案生成是否共用同一套链路：

- 静态结构（AST）：真实路径 `_run_channel → Session.emit_final`；仿真路径 TTS 注入 `feed_audio`、TTS 降级 `emit_final`；Simulator 不得调用 `judge.classify` / `judge.compose_answer` / `pipeline._produce_answer` / `pipeline._judge`（防止自己再实现一套）。注意这些是「文件里是否出现该调用」级别的检查，不区分具体分支
- 服务端：真实面试创建调用 `session.start`；仿真只走 `_apply_simulation` 起 Simulator，但复用同一个全局 session 单例（脚本实际校验的是 create→start，以及旧仿真入口确实已删除）
- 运行时：`emit_final` 确实把定稿喂给 `pipeline.feed_final`；候选人碎片合并逻辑共用

退出码 0 = 一致，1 = 发现偏差并打印具体失败项。


### 真实面试前自检（按顺序跑一遍）

```bash
# 前置：先 uv run python -m app 起动服务；除 check_judge_loop 外都要占音频设备
uv run python tools/smoke_real_mode.py   # 统一入口 + 仿真开关 + 真实/仿真链路冒烟（推荐先跑）
uv run python tools/check_loopback.py    # 物理环回：真的放一段声音，验证采集→转写→意图识别
uv run python tools/check_interrupt.py   # 打断：答案生成到一半来下一题，检查半截答案是否保留
uv run python tools/check_modes.py       # 仿真开关 / 中断后继续 / 超 2 小时自动中断（会写真实 data/interview.db）
uv run python tools/check_judge_loop.py  # 意图识别闭环（judge_start → judge_result），不占音频但会真调一次模型
uv run python tools/cleanup_selftest_records.py  # 清掉上面产生的「自检」记录（可加 --dry-run 先看）
```

这些都通过，说明「采集 → 识别 → 意图识别 → 三版话术 → 存储/评分 → 继续/删除」整条链路是通的。
真实面试前再手动确认三件事：会议软件输出到哪个扬声器、新建面试里就选那个「扬声器环回」、
点「测扬声器 / 测麦克风」看到电平有动静再点开始。

> `cleanup_selftest_records.py` 是按**岗位名关键字**（自检 / 测试 / 浸泡 / 打断 / 环回）匹配删除的；`smoke_real_mode.py --keep` 留下的记录用的是真实岗位名，清不掉，需要在页面里手动删。

当前基线（本机 Windows + DeepSeek-flash，2026-11 实测；仓库不存评测产物，换模型或换机器结果会变，仅供参考）：

| 指标 | 结果 |
|---|---|
| 意图识别是否需要回答（48 用例） | 100% |
| 意图识别细分类型 | 97.9% |
| 误报 / 漏报 | 0 / 0 |
| 问题语义相似度 | ~91% |
| 追问 / 反问识别 | 100% / 100% |
| 题库召回 Recall@1（12 条改写问法） | 67% |
| 题库召回 Recall@3 | 83% → 多路检索 89% |

---

## 日志与排查

关键操作都会打日志：接口调用、会话启停、采集/识别、每句转写、意图识别（含 trace、类型、置信度、
耗时、跳过原因）、每次模型请求（模型、trace、token、首字延迟）、检索召回、评分。

- **文件**：`data/logs/app.log`，单个 5MB 滚动，保留 5 个备份（`app.log.1` … `app.log.5`，连当前文件共 6 个）。
  root logger 固定 DEBUG，所以文件里最全；控制台只显示 `LOG_LEVEL`（默认 INFO）；uvicorn 与第三方库的日志会压到 WARNING 并同样进这个文件。
- **控制台**：`python -m app` 的窗口里同步输出，uvicorn 的访问日志也并进同一条流。

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | INFO | 控制台级别：DEBUG / INFO / WARNING / ERROR |
| `LOG_FILE` | 1 | 0 = 只输出到控制台 |
| `LOG_DIR` | `data/logs` | 日志目录 |

一次提问在日志里长这样（`trace` 和卡片 `id` 可以把整条链路串起来）：

```
意图识别开始 trace=9744b583 原话='什么是 RAG？他的工作流程是什么' 上下文=1句 去抖=900ms 超时=20s
模型返回(chat_json): model=deepseek-flash trace=9744b583 用时=1127ms tokens=591/64 字数=150
意图识别结论 trace=9744b583 类型=question 置信度=0.97 用时=1128ms 超时兜底=False 出卡问题='…'
出卡 id=2f0c7a61 问题='…' 追问=False 聚焦=['RAG 定义', '工作流程'] 原话='…'
题库召回: 查询='…' 候选=12条 语义=开 命中=2条 用时=452ms
流式首包: model=deepseek-flash trace=2f0c7a61 首字=690ms
流式结束: model=deepseek-flash trace=2f0c7a61 状态=ok 首字=690ms 总用时=4200ms …
参考答案完成: id=2f0c7a61 字数=118 相关度=1.00 降级=False 思考=False 思考字数=0
三版齐全: id=2f0c7a61 {'standard': 118, 'brief': 46, 'deep': 263}
```

高频问题的日志特征：

| 现象 | 日志里应该看到 | 说明 |
|---|---|---|
| 意图识别一直转圈 | 只有 `意图识别开始`，没有 `意图识别结论` | 意图识别任务异常退出，搜 `意图识别流程异常退出` 找到 traceback |
| 意图识别慢 | `模型返回` 的用时 / 意图识别超时警告 | 接口慢或网络问题，超时会自动用规则兜底 |
| 听不到声音 | `采集启动` 后 20 秒的「没收到任何音频帧」警告 | 环回选错扬声器或设备被独占 |
| 一直没识别 | `识别引擎就绪 … 类型=mock` | 掉到演示脚本了，真实语音不会被识别 |
| 出卡但没答案 | `参考答案为空，用模板兜底` + `模型调用最终失败` | 看错误原文（HTTP 状态、超时） |

一个秒级出结论的自检（不启动服务、不占用正在跑的会话，但会真调一次意图识别模型）：

```powershell
uv run python tools\check_judge_loop.py
```

> 日志里有转写原文（部分字段会截断到 100~120 字）、岗位背景、检索到的题库原文和完整提示词，发给别人前请先脱敏。

---

## 已知限制

- **必须在面试所在的 Windows 机器上跑**：WASAPI 环回是 Windows-only
- 环回设备要选会议软件实际使用的扬声器；蓝牙耳机休眠后需要重开
- 本地识别（sherpa）不出标点，火山识别有标点
- 题库召回剩余漏检需要 cross-encoder 重排才能解决
- 历史面试会一直累积，暂未做自动清理

---

## 安全

- `.env` 已在 `.gitignore` 中，不要提交（`git add -f` 也别用）
- 调用日志（`/logs` 页面、`/api/llm/calls`）与 `data/logs` 会记录提示词、简历与岗位背景；请求正文虽截断到 20000 字左右，但已经很敏感，分享截图前先脱敏
- 换过 Key 之后记得作废旧的
