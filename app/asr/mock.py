"""演示用识别引擎：不依赖麦克风和模型，按脚本"边听边说"地产出文本。

用途：在没有音频输入的环境里验证 意图识别 → 话术 → 上屏 的完整链路，
以及在浏览器里做「演示模式」。它按收到的音频字节数推进脚本，所以
真人对着麦克风说话时，语速会带动脚本推进，观感接近真实。
"""

from __future__ import annotations


INTERVIEWER_SCRIPT = [
    "你好，我是今天的面试官，先简单聊几句。",
    "我看你简历上写做过订单系统，能说说你在里面负责哪一块吗？",
    "你说你负责了下单链路，那如果流量突然涨十倍，你觉得瓶颈会先出现在哪里？",
    "你刚才提到用了缓存，缓存和数据库的一致性你们是怎么保证的？",
    "嗯嗯，明白了，那你在团队里一般怎么推进比较有争议的技术方案？",
    "好的，我今天的问题差不多了，你还有什么想问我的吗？",
]

CANDIDATE_SCRIPT = [
    "好的，我先做个自我介绍。",
    "我在上一个项目里主要负责订单系统的下单链路和库存扣减模块。",
    "当时我们用的是本地缓存加异步刷新的方案，主要是为了扛住大促的读流量。",
]


class MockEngine:
    """按"音频时长"推进脚本：1 秒音频约吐 4.5 个字，接近真人语速。"""

    def __init__(self, script: list[str], chars_per_second: float = 4.5) -> None:
        self.name = "mock"
        self._script = script
        self._index = 0
        self._seconds = 0.0
        self._chars_per_second = chars_per_second

    def push(self, pcm: bytes) -> tuple[str, bool]:
        # 16kHz / 16bit / 单声道 => 每秒 32000 字节
        self._seconds += len(pcm) / 32000
        sentence = self._script[self._index % len(self._script)]
        target = int(self._seconds * self._chars_per_second)
        text = sentence[: min(len(sentence), target)]

        if target >= len(sentence):
            self._index += 1
            self._seconds = 0.0
            return sentence, True
        return text, False

    def reset(self) -> None:
        self._seconds = 0.0

    def close(self) -> None:
        pass
