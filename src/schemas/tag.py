from typing import Optional

from pydantic import BaseModel, ConfigDict


class TagBase(BaseModel):
    tag_name: str
    tag_type: str
    description: Optional[str] = None
    enabled: bool = True


class TagCreate(TagBase):
    pass


class TagUpdate(BaseModel):
    tag_name: Optional[str] = None
    tag_type: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


class TagResponse(TagBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
