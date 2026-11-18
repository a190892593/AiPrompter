"""提示词仓库：所有 LLM 调用的提示词都在这里，可在界面里动态修改。

- 默认值写死在 DEFAULTS，首次使用时展示；
- 改过的存 SQLite，运行时优先用改过的版本；
- 支持一键恢复默认；
- 渲染时容错：占位符缺失或写错不会让调用崩掉，会退回默认模板。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from string import Formatter

from .config import settings

log = logging.getLogger("prompts")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


# ------------------------------------------------------------------ 默认提示词

DEFAULTS: dict[str, str] = {
    "judge.system": (
        "你是面试现场的实时意图识别器。任务有两个：① 判断候选人此刻是否需要开口回答；"
        "② 如果需要，结合上下文把面试官真正想问的问题还原成一句自包含、可直接回答的规范问题"
        "（处理口头禅、碎片句、自我修正、指代、术语误识）。只输出严格 JSON，不要任何解释文字。"
    ),
    "judge.user": """【判定规则】（这些规则是固定的，先读）
1. 只有 question / instruction 需要候选人开口；statement、chitchat、aside、incomplete 一律不出话术。
2. 看最近若干句，不只看最后一句；问题被拆成几句时合并，spans 写从最近第几句开始拼（1~3）。
3. 接电话、离场、和旁人说话、网络设备问题（"喂喂""听得到吗""稍等""信号不好"）判 aside，不打断原问题；
   最后一句是旁白时，即使前面有提问也判 aside（那个问题上一轮已处理，重复出卡就是错题）。
4. 引子式反问判 statement："你们之前不是做过一个订单系统嘛？""对吧？""是吧？"——等面试官往下说。
5. 候选人上一句正在回答同一个问题时，不要重复判提问；短附和（"嗯嗯""好的""行""收到"）判 chitchat。
6. 最后一句明显没说完（逗号/"那个"/"我是说"/"如果"结尾且无谓语）判 incomplete，不要硬凑问题。
7. 转写通常无标点，不要仅因为句末没有"？"就判 statement/incomplete。
8. 明确要求候选人做动作（请/先/讲一下/介绍一下/手写/举个例子/设计一下/发我）判 instruction。

【问题还原规则】（question / instruction 的 question 字段必须满足）
1. 自包含：看不到上下文的人也能回答；"那个/这个/刚才说的/它/这块"必须换成上文里的具体对象。
2. 自纠：出现"不是、我是说、准确说、说错了"时，以最后一次修正为准。
3. 补全：省略的主语/对象/场景从候选人上一句回答或前文补齐。
4. 去噪：去掉"嗯、啊、那个、就是说、其实、我想问一下"等口头禅。
5. 保留约束：数字、量级、场景（"量涨十倍""单机""百万级""大促"）不能丢。
6. 必须写成能直接回答的规范问题（≤40 字），禁止输出"说说吧/详细讲讲/具体谈谈"这类空泛题。
7. 尽量沿用面试官原话里的关键名词（"订单系统""主从切换""缓存击穿"），不要替换成同义词。
8. 英文术语明显是语音误识时纠正（launch→LangChain、v act→vLLM、蓝链→LangChain、蓝格拉夫→LangGraph），
   并在 reason 末尾写"术语纠正:误识→正确"。
9. 如果原话里有明显的英文术语误识，把纠正后的**整句原话**填到 corrected；除术语外一个字都不要改。
   没有误识时 corrected 给空字符串。
10. 检索改写：另外给 1~3 条 search_queries，只保留「术语 + 对象」的中文关键词短语，
    用于题库/资料检索（例如 ["召回率 评测 口径", "RAG 召回 指标"]）；不要和 question 完全重复，
    没有合适关键词就给空数组。

【追问/反问规则】
1. followup：最后一句出现承接词（"那/那如果/那这个/刚才你说/你刚说的/除了…还有/为什么要/具体多少/再详细点/然后呢/还有呢"），
   或以"那…呢""那…怎么…""那你们…"开头，或直接针对候选人刚回答的内容继续问 → followup=true；开新话题 → false。
   追问≠重复：带新增信息就要出卡；只有几乎原样重复才跳过。
2. challenge：面试官用质疑、验证、抬杠的方式反问——"你确定吗""真的吗""凭什么""有数据吗""这能行吗""怎么证明"
   "为什么不 用X""那按你说的岂不是……"——判 question，challenge=true；question 要还原成他真正想验证的点。
3. followup 与 challenge 可同时为 true。

【输出】只输出 JSON，不要任何解释：
{{"type": "question|instruction|statement|chitchat|aside|incomplete",
  "question": "面试官真正想问的问题，≤40 字；不需要回答时给空字符串",
  "focus": ["考察点 1-3 个，每个 ≤8 字"],
  "spans": 1,
  "followup": false,
  "challenge": false,
  "corrected": "",
  "search_queries": ["检索关键词短语 1", "检索关键词短语 2"],
  "confidence": 0.0 到 1.0,
  "reason": "判定依据，≤30 字"}}

【示例】
1. 碎片+自纠：「那个如果、我是说如果哈」+「量突然上去十倍，你们那个架构还扛得住吗？」→ question：流量涨到 10 倍时如何保证架构扛住？spans=2
2. 指代追问：候选人「我们用 Redis 缓存热点数据」→「那这个一下子失效了呢？」→ question：Redis 缓存失效或击穿时怎么处理？followup=true
3. 自纠：「你们 QPS 多少？不是，我是说峰值 QPS 大概多少？」→ question：系统峰值 QPS 大概是多少？
4. 术语纠错：「你们那个 launch 里 agent 是怎么编排的」→ question：LangChain 里的 Agent 是如何编排的？
5. 引子反问：「你们之前不是做过一个订单系统嘛？」→ statement，不需要回答
6. 旁白：「喂喂，我这边网卡了，你刚说什么？」→ aside，不打断原问题
7. 指令：「你先说一下检索链路整体设计，然后举一个线上踩坑的例子」→ instruction，合并成一句
8. 反问：「真的吗？这个 81% 是怎么测的，有数据能证明吗？」→ question：请说明 81% 召回率的评测方法和数据依据，followup=true, challenge=true

【个人简历】{resume}
【岗位要求】{jd}
【领域术语】（转写里的英文术语可能是语音误识，按标准写法纠正；拿不准不改）
{terms}

【最近对话】（从早到晚，最后一条是最新的；[时:分:秒] 是时间，括号里的"停顿"可用于判断是否同一句话延续）
{lines}

{examples}只输出 JSON。""",
    "answer.system": (
        "你是候选人的面试参考答案生成器。根据岗位、简历、面试官问题和题库里的参考问答，"
        "生成一份可以直接用来回答的参考答案：口语化、能一口气说完、内容准确完整，"
        "不书面化、不罗列提纲、不写成通用套话。"
    ),
    "answer.user": """【岗位】{role}
【岗位要求】{background}
【历史记忆】（你过往面试反复暴露的问题，回答时主动避开）
{memory}
【面试官真正想问的】{question}
【考察点】{focus}
【上文链条】（追问/反问链，没有则为空；按“第1问 → 追问/反问”顺序给出，必须承接链里已说过的结论/数字，不要重复整段）
{prev_qa}
【提问方式】{mode_note}
【候选人准备过的参考答案】（可能为空；有就尽量沿用它的口径和事实，别自相矛盾）
{reference}
【前文上下文】{context}

生成一份参考答案：
1. {length_rule}
2. 必须紧扣上面这个问题作答，禁止答成通用套话、禁止答成另一个话题
3. 第一人称口语，像真人在说话；不要堆砌"首先/其次/最后"，不要 Markdown 标题
4. 先给结论，再给 1-2 个具体支撑，最后一句收尾
5. 数据必须写具体：优先沿用【我的简历】和【候选人准备过的参考答案】里的真实数字；没有就写明确的量级或范围（如"百万级""80~120 毫秒""70%~80%"）。禁止出现 XX、XX 万、N、若干、【】这类占位符，也不要写"一些万条""一些毫秒"这种含糊说法；参考里如果带 XX 或【】，同样不要照抄。例如不要写"延迟【X】毫秒"或"吞吐翻【X】倍"，要写"延迟 80 毫秒""吞吐翻 3 倍"
6. 如果是追问/反问链（【上文链条】有内容）：先用半句话承接链条里已经说过的结论或数字，再回答这次新增的部分，不要从头再讲一遍
7. 如果面试官在质疑/验证（【提问方式】里写明）：先正面回应质疑——给数据、线上结果、对比实验或明确边界与取舍，再展开细节；禁止只喊"没问题/绝对可以"
8. 不要编造公司名；只输出回答正文，不要任何前缀或解释""",
    "suggest.system": "你是面试现场的意图纠错助手，只输出 JSON。",
    "suggest.user": """面试官原话：「{raw}」
系统理解成：「{current}」
候选人认为这个理解不对。请给出 5 个面试官更可能想问的问题，每句不超过 30 字，覆盖不同角度（换考察点、追问细节、假设场景等）。
只输出 JSON：{{"questions": ["问题1", "问题2", "问题3", "问题4", "问题5"]}}""",
    "report.system": "你是面试复盘教练，只输出严格 JSON，不要解释。",
    "report.user": """【岗位】{role}
【岗位要求】{jd}
【面试记录】（问题 → 候选人实际回答 → 系统给的参考答案）
{rounds}

请评估候选人的表现，输出 JSON：
{{"overall_score": 0-100 整数,
  "verdict": "一句话结论，不超过 30 字",
  "chains": [{{"root_question": "这条话题链的根问题",
              "depth": 链层数,
              "score": 0-100 链整体分,
              "comment": "这条链整体答得怎么样，≤40 字",
              "weakest_layer": 答得最虚的层号}}],
  "dimensions": [{{"name": "维度", "score": 0-100, "comment": "一句话点评"}}],
  "per_question": [{{"index": 1, "score": 0-100, "comment": "这题答得怎么样，不超过 40 字"}}],
  "strengths": ["最多 3 条"],
  "gaps": ["最多 3 条"],
  "action_plan": ["接下来 2-4 周能做的具体动作，3 条"]}}

维度用：问题理解、回答结构、技术深度、表达清晰、岗位匹配。
per_question 的 index 与上面轮次序号一致。评分要有区分度，不要一律给高分；
候选人没答或答跑题的，要明确扣分并说明原因。

话题链评分规则（重点，chains 字段必须输出，每个话题链一条，缺失视为评分无效）：
1. 先按链打分（chains），再按单题打分（per_question），链分是这条话题整体是否被问穿；
2. 链层数越深（追问/反问越多）而候选人越答越虚，链分越低；追问 3 层以上还能给出具体数据和细节，链分应偏高；
3. weakest_layer 指向这条链里最虚的那一层（从 1 开始），并写清为什么虚（绕开、没有数据、重复之前的话）；
4. 链里出现反问/质疑（"你确定吗""怎么证明"）时，重点看候选人有没有正面回应、有没有证据。""",
}

# 每个提示词的中文名与可用占位符，界面用
META: dict[str, dict] = {
    "judge.system": {"label": "意图识别 · 系统提示", "placeholders": []},
    "judge.user": {"label": "意图识别 · 用户提示", "placeholders": ["role", "jd", "resume", "terms", "lines", "examples"]},
    "answer.system": {"label": "参考答案 · 系统提示", "placeholders": []},
    "answer.user": {
        "label": "参考答案 · 用户提示",
        "placeholders": ["role", "background", "memory", "question", "focus", "prev_qa", "mode_note", "reference", "context", "length_rule"],
    },
    "suggest.system": {"label": "候选问题 · 系统提示", "placeholders": []},
    "suggest.user": {"label": "候选问题 · 用户提示", "placeholders": ["raw", "current"]},
    "report.system": {"label": "评分 · 系统提示", "placeholders": []},
    "report.user": {"label": "评分 · 用户提示", "placeholders": ["role", "jd", "rounds"]},
}


# ------------------------------------------------------------------ 存储


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS prompts (
                   key TEXT PRIMARY KEY,
                   content TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        _conn.commit()
    return _conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(key: str) -> str:
    if key not in DEFAULTS:
        raise KeyError(key)
    with _lock:
        row = _db().execute("SELECT content FROM prompts WHERE key = ?", (key,)).fetchone()
    return row[0] if row else DEFAULTS[key]


def set_prompt(key: str, content: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    content = content.strip()
    if not content:
        raise ValueError("内容不能为空")
    with _lock:
        db = _db()
        db.execute(
            """INSERT INTO prompts (key, content, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (key, content, _now()),
        )
        db.commit()


def reset(key: str) -> str:
    if key not in DEFAULTS:
        raise KeyError(key)
    with _lock:
        db = _db()
        db.execute("DELETE FROM prompts WHERE key = ?", (key,))
        db.commit()
    return DEFAULTS[key]


def all_prompts() -> list[dict]:
    with _lock:
        rows = dict(_db().execute("SELECT key, content FROM prompts").fetchall())
    items = []
    for key, default in DEFAULTS.items():
        items.append(
            {
                "key": key,
                "label": META[key]["label"],
                "placeholders": META[key]["placeholders"],
                "content": rows.get(key, default),
                "default": default,
                "modified": key in rows,
            }
        )
    return items


def render(key: str, **kwargs: object) -> str:
    """渲染模板；占位符写错或缺失时退回默认模板，绝不让调用崩掉。"""
    for template, is_default in ((get(key), False), (DEFAULTS[key], True)):
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            if not is_default:
                log.warning("提示词 %s 渲染失败(%s)，改用默认模板", key, exc)
    return DEFAULTS[key]


def check_placeholders(key: str, content: str) -> list[str]:
    """界面用：找出模板里写了但不在 META 里的占位符。"""
    allowed = set(META.get(key, {}).get("placeholders", []))
    found = set()
    try:
        for _, field, _, _ in Formatter().parse(content):
            if field:
                found.add(field.split(".")[0].split("[")[0])
    except ValueError:
        return []
    return sorted(found - allowed)
