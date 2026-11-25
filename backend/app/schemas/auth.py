from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.base import UserRole


class StudentRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(description="Student email address")
    name: str = Field(min_length=1, max_length=120, description="Student display name")
    password: str = Field(
        min_length=8,
        max_length=128,
        description="Account password (minimum 8 characters)",
    )


class StudentLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(description="Student email address")
    password: str = Field(min_length=1, max_length=128, description="Account password")


class StaffPinLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = Field(
        default=None,
        description="Staff or manager email address",
    )
    staff_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Staff or manager identifier",
    )
    pin: str = Field(
        pattern=r"^\d{6}$",
        description="Six-digit numeric PIN",
    )

    @model_validator(mode="after")
    def validate_identifier_present(self) -> "StaffPinLoginRequest":
        if not self.email and not self.staff_id:
            raise ValueError("Either email or staff_id must be provided")
        return self


class UserSummary(BaseModel):
    id: uuid.UUID = Field(description="User UUID")
    name: str = Field(description="User display name")
    role: UserRole = Field(description="User role")
    email: str | None = Field(default=None, description="User email when present")
    staff_id: str | None = Field(default=None, description="Staff ID when present")
    restaurant_id: uuid.UUID | None = Field(
        default=None, description="Assigned restaurant UUID for staff/manager"
    )
    restaurant_name: str | None = Field(
        default=None, description="Assigned restaurant name for staff/manager"
    )


class StaffDirectoryEntry(BaseModel):
    staff_id: str | None = None
    name: str
    role: UserRole
    restaurant_id: uuid.UUID | None = None
    restaurant_name: str | None = None


class StaffDirectoryResponse(BaseModel):
    restaurants: list[dict[str, str | None]]
    staff: list[StaffDirectoryEntry]


class AuthTokenResponse(BaseModel):
    access_token: str = Field(description="Signed HS256 JWT access token")
    token_type: Literal["bearer"] = Field(
        default="bearer",
        description="OAuth2 bearer token type",
    )
    expires_in_seconds: int = Field(description="Token lifetime in seconds")
    user: UserSummary = Field(description="Authenticated user profile summary")


class LogoutResponse(BaseModel):
    logged_out: bool = Field(default=True, description="Logout confirmation flag")
