from typing import Literal
from .domain import Model,Scenario,AgentResult

State=Literal['queued','running','completed','failed','cancelled']
class RunCreated(Model):
    id:str
    state:State
    scenario_id:str
    version:int
    events_url:str
class RunSummary(RunCreated):
    stage:str
    attempt:int
    error:str|None
    created_at:float
    completed_at:float|None
    adapter_revision:str
class RunDetail(RunSummary):
    result:AgentResult|None
class Version(Model):
    scenario_id:str
    version:int
    scenario:Scenario
    sha256:str
    parent_run_id:str|None
class PresetOut(Model):
    id:str
    revision:int
    scenario:Scenario
class ScenarioSummary(Model):
    id:str
    name:str
    latest_version:int
    created_at:float
class VersionSummary(Model):
    version:int
    sha256:str
    created_at:float
