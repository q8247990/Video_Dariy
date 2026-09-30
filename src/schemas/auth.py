from pydantic import BaseModel, Field


class UserLogin(BaseModel):
    username: str
    password: str


class UserInit(BaseModel):
    username: str
    password: str


class UserChangePassword(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


class TokenData(BaseModel):
    sub: str | None = None
