from pydantic import Field
from pydantic_settings import BaseSettings,SettingsConfigDict

class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_prefix='BE2_',env_file='.env',extra='ignore')
    database_url: str='sqlite:///./be2.db'
    inline_worker: bool=True
    poll_seconds: float=Field(default=.2,ge=.02,le=10)
    lease_seconds: float=Field(default=30,ge=2,le=600)
    job_timeout_seconds: float=Field(default=120,ge=1,le=900)
    max_attempts: int=Field(default=3,ge=1,le=10)
    agent_factory: str='be2_backend.agent:MockAgent'
    agent_revision: str=Field(default='mock-v1',min_length=1,max_length=120)
    cors_origins: str=''
