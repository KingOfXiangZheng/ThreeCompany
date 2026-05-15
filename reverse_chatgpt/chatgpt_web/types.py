from typing import Optional, Dict
from dataclasses import dataclass


@dataclass
class ChatGPTWebAuth:
    access_token: str
    cookie: str
    user_agent: str


@dataclass
class ChatGPTWebClientOptions:
    access_token: str
    cookie: Optional[str] = None
    user_agent: Optional[str] = None


@dataclass
class ModelDefinitionConfig:
    id: str
    name: str
    api: str
    reasoning: bool
    input: list
    cost: Dict[str, float]
    context_window: int
    max_tokens: int
