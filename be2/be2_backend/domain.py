"""BE2 validates the transport contract; BE1 owns domain calculations."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator, field_validator

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)

class Scenario(Model):
    # Raw input strings (CSV cells included) must survive validation unchanged.
    model_config = ConfigDict(str_strip_whitespace=False)
    name: str = Field(min_length=1,max_length=100)
    inputs: dict[str,JsonValue]

    @field_validator('name', mode='before')
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

class ScenarioCreate(Model):
    scenario: Scenario

class VersionCreate(ScenarioCreate):
    expected_version: int = Field(ge=1,le=2147483646)

class AnalysisCreate(Model):
    scenario_id: str = Field(min_length=1,max_length=36)
    version: int = Field(ge=1,le=2147483647)

class Progress(Model):
    stage: str = Field(min_length=1,max_length=24,pattern=r'^[a-z_]+$')
    message: str = Field(min_length=1,max_length=2000)

class AgentMessage(Model):
    text: str = Field(min_length=1,max_length=4000)

class ReportBlock(Model):
    id: str = Field(min_length=1,max_length=100)
    text: str = Field(min_length=1,max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list,max_length=100)

class Report(Model):
    title: str = Field(min_length=1,max_length=200)
    blocks: list[ReportBlock] = Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def unique(self):
        if len({b.id for b in self.blocks})!=len(self.blocks):
            raise ValueError('report block id must be unique')
        return self

class Recommendation(Model):
    id: str = Field(min_length=1,max_length=100)
    title: str = Field(min_length=1,max_length=200)
    details: dict[str,JsonValue] = Field(default_factory=dict)

class AgentResult(Model):
    schema_version: Literal['be2.v1'] = 'be2.v1'
    provider: str = Field(min_length=1,max_length=100)
    is_mock: bool
    summary: str = Field(min_length=1,max_length=8000)
    metrics: dict[str,str|None] = Field(default_factory=dict)
    recommendations: list[Recommendation] = Field(default_factory=list,max_length=100)
    report: Report
    metadata: dict[str,JsonValue] = Field(default_factory=dict)
