from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from crawler_api.application.tenant_allowlist import TenantAllowlist
from pydantic import ValidationError
from yaml.constructor import ConstructorError  # type: ignore[import-untyped]

MAX_ALLOWLIST_BYTES = 1024 * 1024


class TenantAllowlistLoadError(ValueError):
    pass


class UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, Any]:
    loader.flatten_mapping(node)
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "allowlist mapping keys must be strings",
                key_node.start_mark,
            )
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate mapping key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_tenant_allowlist(path: Path) -> TenantAllowlist:
    try:
        raw_content = path.read_bytes()
        if len(raw_content) > MAX_ALLOWLIST_BYTES:
            raise TenantAllowlistLoadError("tenant allowlist exceeds the 1 MiB size limit")
        content = raw_content.decode("utf-8")
        raw_config = yaml.load(content, Loader=UniqueKeySafeLoader)
        return TenantAllowlist.model_validate(raw_config)
    except TenantAllowlistLoadError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, TypeError) as error:
        raise TenantAllowlistLoadError("tenant allowlist is missing or invalid") from error
