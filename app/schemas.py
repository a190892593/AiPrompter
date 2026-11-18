"""接口数据模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["junior", "mid", "senior"]
EngineKind = Literal["auto", "local", "cloud", "volc", "deepseek", "mock"]
AnswerLevel = Literal["brief", "standard", "deep"]


class SessionConfig(BaseModel):
    role: str = Field(default="后端开发工程师", max_length=60)
    level: Level = "mid"
    language: Literal["zh", "en"] = "zh"
    background: str = Field(default="", max_length=8000)
    # 个人简历：新建面试必填，会随问题一起作为上下文给模型
    resume: str = Field(default="", max_length=8000)
    engine: EngineKind = "auto"
    answer_level: AnswerLevel = "standard"
    output_device: str = ""
    input_device: str = ""


class InterviewIn(BaseModel):
    """新建面试：岗位 + 个人简历是必填项。"""

    role: str = Field(min_length=1, max_length=60)
    resume: str = Field(min_length=1, max_length=8000)
    position_id: int | None = None
    position_name: str = ""
    level: Level = "mid"
    engine: EngineKind = "auto"
    answer_level: AnswerLevel = "standard"
    output_device: str = ""
    input_device: str = ""
    mode: Literal["real", "simulate"] = "real"
    # 岗位 JD / 技能要求（从岗位管理带出来，可选）
    background: str = Field(default="", max_length=8000)

    def to_config(self) -> SessionConfig:
        return SessionConfig(
            role=self.role,
            level=self.level,
            background=self.background,
            resume=self.resume,
            engine=self.engine,
            answer_level=self.answer_level,
            output_device=self.output_device,
            input_device=self.input_device,
        )


class FeedbackIn(BaseModel):
    last_text: str
    context: str = ""
    predicted_type: str = ""
    correct_type: str
    corrected_question: str = ""
    note: str = ""


class InjectIn(BaseModel):
    channel: Literal["interviewer", "candidate"] = "interviewer"
    text: str
