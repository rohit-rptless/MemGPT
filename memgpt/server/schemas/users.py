from typing import List, Optional, TYPE_CHECKING
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from uuid import UUID

class GetAllUsersResponse(BaseModel):
    cursor: Optional["UUID"] = Field(None, description="Cursor for the next page in the response.")
    user_list: List[dict] = Field(..., description="A list of users.")


class CreateUserRequest(BaseModel):
    user_id: Optional["UUID"] = Field(None, description="Identifier of the user (optional, generated automatically if null).")
    api_key_name: Optional[str] = Field(None, description="Name for API key autogenerated on user creation (optional).")


class CreateUserResponse(BaseModel):
    user_id: "UUID" = Field(..., description="Identifier of the user (UUID).")
    api_key: str = Field(..., description="New API key generated for user.")


class CreateAPIKeyRequest(BaseModel):
    user_id: "UUID" = Field(..., description="Identifier of the user (UUID).")
    name: Optional[str] = Field(None, description="Name for the API key (optional).")


class CreateAPIKeyResponse(BaseModel):
    api_key: str = Field(..., description="New API key generated.")


class GetAPIKeysResponse(BaseModel):
    api_key_list: List[str] = Field(..., description="Identifier of the user (UUID).")


class DeleteAPIKeyResponse(BaseModel):
    message: str
    api_key_deleted: str


class DeleteUserResponse(BaseModel):
    message: str
    user_id_deleted: "UUID"
