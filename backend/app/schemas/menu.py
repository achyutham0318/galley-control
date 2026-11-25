import uuid

from pydantic import BaseModel, ConfigDict, Field


class MenuItemResponse(BaseModel):
    id: uuid.UUID = Field(description="Menu item UUID")
    restaurant_id: uuid.UUID = Field(description="Owning restaurant UUID")
    name: str = Field(description="Menu item name")
    description: str = Field(description="Menu item description")
    category: str = Field(default="General", description="Menu item category")
    price_paise: int = Field(description="Base unit price in integer paise")
    effective_price_paise: int = Field(
        description="Effective unit price in integer paise after active surplus discount"
    )
    active_discount_percent: int = Field(
        default=0, description="Active surplus discount percentage (0-90)"
    )
    is_surplus_active: bool = Field(
        default=False, description="True when surplus deal is active and unexpired"
    )
    surplus_note: str | None = Field(
        default=None, description="Optional manager surplus deal note"
    )
    surplus_expires_at: str | None = Field(
        default=None, description="Surplus deal expiry in UTC Z"
    )
    surplus_expires_at_ist: str | None = Field(
        default=None, description="Surplus deal expiry in IST"
    )
    stock_count: int = Field(description="Available stock units")
    is_bulk_excess: bool = Field(
        default=False, description="True when stock_count >= 15"
    )
    is_available: bool = Field(description="Whether the item is currently orderable")
    is_archived: bool = Field(default=False, description="True when soft-archived")
    current_wait_minutes: int = Field(
        description="Estimated kitchen wait time in minutes"
    )


class MenuItemCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=300)
    category: str = Field(default="General", min_length=1, max_length=60)
    price_paise: int = Field(gt=0)
    stock_count: int = Field(ge=0)
    estimated_prep_minutes: int = Field(ge=1, le=120)
    is_available: bool = True


class MenuItemUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, min_length=1, max_length=60)
    price_paise: int | None = Field(default=None, gt=0)
    stock_count: int | None = Field(default=None, ge=0)
    estimated_prep_minutes: int | None = Field(default=None, ge=1, le=120)
    is_available: bool | None = None


class SurplusDealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_surplus_deal: bool
    discount_percent: int = Field(default=0, ge=0, le=90)
    surplus_note: str | None = Field(default=None, max_length=160)
    expires_in_minutes: int | None = Field(default=None, ge=1, le=1440)
