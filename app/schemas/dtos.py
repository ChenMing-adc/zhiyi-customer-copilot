from pydantic import BaseModel


class AiMessage(BaseModel):
    role: str
    content: str
