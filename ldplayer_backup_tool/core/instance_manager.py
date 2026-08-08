"""
Filters and selects LDPlayer instances for backup/restore operations.
"""
from typing import List, Optional

from core.ldconsole import InstanceInfo
from utils.helpers import parse_index_range
from utils.logger import get_logger

logger = get_logger(__name__)


def filter_instances(
    instances: List[InstanceInfo],
    index_range: Optional[str] = None,
    name_filter: Optional[str] = None,
) -> List[InstanceInfo]:
    """
    Filter the instance list.

    Args:
        instances:    Full list of InstanceInfo from ldconsole list.
        index_range:  Optional range string like "0-100" or "0,5,10-20".
        name_filter:  Optional comma-separated name substrings. Instance is
                      included if its name contains ANY of the substrings.

    Returns:
        Filtered, sorted list of InstanceInfo.
    """
    result = list(instances)

    if index_range and index_range.strip():
        try:
            allowed_indices = set(parse_index_range(index_range))
            result = [i for i in result if i.index in allowed_indices]
            logger.debug("After range filter '%s': %d instances", index_range, len(result))
        except ValueError as e:
            logger.error("Bad index range '%s': %s", index_range, e)

    if name_filter and name_filter.strip():
        parts = [p.strip().lower() for p in name_filter.split(",") if p.strip()]
        result = [i for i in result if any(p in i.name.lower() for p in parts)]
        logger.debug("After name filter '%s': %d instances", name_filter, len(result))

    result.sort(key=lambda i: i.index)
    return result
