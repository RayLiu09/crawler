from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ApiScope(StrEnum):
    COLLECTION_WRITE = "collection:write"
    COLLECTION_READ = "collection:read"
    COLLECTION_CONTROL = "collection:control"


ALL_API_SCOPES: tuple[ApiScope, ...] = tuple(ApiScope)


class SourceCode(StrEnum):
    """Source identifiers supported by the production protocol."""

    ZHAOPIN = "zhaopin"


class ApiModel(BaseModel):
    """Base model for strict camelCase HTTP contracts."""

    model_config = ConfigDict(
        alias_generator=lambda value: "".join(
            word if index == 0 else word.capitalize() for index, word in enumerate(value.split("_"))
        ),
        populate_by_name=True,
        extra="forbid",
    )
