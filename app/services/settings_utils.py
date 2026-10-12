from typing import Any, Callable, TypeVar

T = TypeVar("T")


def parse_comma_list(value: Any, *, transform: Callable[[str], T], default: list[T]) -> list[T]:
    """
    Shared `field_validator(mode="before")` body for comma-separated env-var
    list fields (e.g. API_KEYS, AZURE_RETRY_STATUS_CODES). Must be paired
    with `Annotated[list[T], NoDecode]` on the field, or pydantic-settings
    tries to json.loads() the raw .env string before this ever runs.
    """
    if value is None or value == "" or value == []:
        return default
    if isinstance(value, str):
        return [transform(part.strip()) for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [transform(str(v).strip()) for v in value if str(v).strip()]
    return default
