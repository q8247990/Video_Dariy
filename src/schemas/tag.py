from typing import Optional

from pydantic import BaseModel, ConfigDict


class TagBase(BaseModel):
    tag_name: str
    tag_type: str
    description: Optional[str] = None
    enabled: bool = True


class TagResponse(TagBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
