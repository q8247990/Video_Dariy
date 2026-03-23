from pydantic import BaseModel


class UserLogin(BaseModel):
    username: str
    password: str


class UserInit(BaseModel):
    username: str
    password: str


class UserChangePassword(BaseModel):
    old_password: str
    new_password: str


class TokenData(BaseModel):
    sub: str | None = None
