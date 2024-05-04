"""Ground-truth NSRTs for the blocks environment."""

from typing import Dict, Set

from predicators.ground_truth_models import GroundTruthNSRTFactory
from predicators.structs import NSRT, ParameterizedOption, Predicate, Type


class CoffeeBrewingGroundTruthNSRTFactory(GroundTruthNSRTFactory):
    """TODO"""

    @classmethod
    def get_env_names(cls) -> Set[str]:
        return {"coffee_brewing"}

    @staticmethod
    def get_nsrts(
        env_name: str, types: Dict[str, Type], predicates: Dict[str,
                                                                Predicate],
        options: Dict[str,
                      ParameterizedOption]) -> Set[NSRT]:  # pragma: no cover
        # For now, there are just no NSRTs
        return set()
