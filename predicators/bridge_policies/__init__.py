"""Handle creation of bridge policies."""

from typing import Set

from predicators import utils
from predicators.bridge_policies.base_bridge_policy import BaseBridgePolicy, \
    BridgePolicyDone
from predicators.structs import NSRT, Predicate

__all__ = ["BaseBridgePolicy", "BridgePolicyDone", "create_bridge_policy"]

# Find the subclasses.
utils.import_submodules(__path__, __name__)


def create_bridge_policy(name: str, predicates: Set[Predicate],
                         nsrts: Set[NSRT]) -> BaseBridgePolicy:
    """Create a bridge policy given its name."""
    for cls in utils.get_all_subclasses(BaseBridgePolicy):
        if not cls.__abstractmethods__ and cls.get_name() == name:
            bridge_policy = cls(predicates, nsrts)
            break
    else:
        raise NotImplementedError(f"Unknown bridge policy: {name}")
    return bridge_policy
