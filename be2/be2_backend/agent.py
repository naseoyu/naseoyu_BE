"""The only integration seam BE1 must implement. No LLM SDK or cost engine here."""
import asyncio
import importlib
import inspect
from typing import Protocol,Callable,Awaitable
from .domain import Scenario,AgentResult,Report,ReportBlock

Emit=Callable[[str,dict],Awaitable[None]]

class Agent(Protocol):
    async def analyze(self,scenario:Scenario,emit:Emit)->AgentResult: ...

class MockAgent:
    supported_input_schemas = ('be2.csv.raw.v1',)

    async def analyze(self,scenario:Scenario,emit:Emit)->AgentResult:
        await emit('progress',{'stage':'analyzing','message':'모의 Agent가 입력 수신을 확인했습니다'})
        await asyncio.sleep(.05)
        await emit('agent_message',{'text':'BE 1 연결 전 파이프라인 테스트입니다. 비용 계산은 수행하지 않습니다.'})
        return AgentResult(provider='mock',is_mock=True,summary='모의 분석 완료: 실제 비용·추천 결과가 아닙니다.',
            metrics={'monthly_cost_krw':None,'saving_krw':None},
            report=Report(title='BE 2 연결 테스트',blocks=[ReportBlock(id='mock-notice',
                text=f'{scenario.name} 입력 저장, 작업 실행, 이벤트 전달을 확인했습니다. 실제 Agent를 연결해야 비용 분석이 가능합니다.')]))

def load_agent(settings)->Agent:
    module,separator,name=settings.agent_factory.partition(':')
    if not separator:
        raise ValueError('BE2_AGENT_FACTORY must be module:factory')
    agent=getattr(importlib.import_module(module),name)()
    if not inspect.iscoroutinefunction(getattr(agent,'analyze',None)):
        raise ValueError('Agent must expose async analyze(scenario, emit)')
    return agent
