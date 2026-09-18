"""API 请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TextIn(BaseModel):
    text: str = Field(default="", description="批量录入文本")


class ConfigIn(BaseModel):
    transfer_minutes: int = Field(gt=0, description="换乘有效间隔（分钟）")
    transfer_discount: float = Field(ge=0, description="换乘抵扣额（元）")
    daily_cap: float = Field(ge=0, description="每卡每运营日封顶额（元）")
    day_split_hour: int = Field(ge=0, le=23, description="运营日分界时刻")
