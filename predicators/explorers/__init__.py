"""Handle creation of explorers."""

from typing import Callable, List, Optional, Set

from gym.spaces import Box

from predicators import utils
from predicators.explorers.base_explorer import BaseExplorer
from predicators.explorers.bilevel_planning_explorer import \
    BilevelPlanningExplorer
from predicators.option_model import _OptionModelBase
from predicators.structs import NSRT, GroundAtom, ParameterizedOption, \
    Predicate, State, Task, Type

__all__ = ["BaseExplorer"]

# Find the subclasses.
utils.import_submodules(__path__, __name__)


def create_explorer(
    name: str,
    initial_predicates: Set[Predicate],
    initial_options: Set[ParameterizedOption],
    types: Set[Type],
    action_space: Box,
    train_tasks: List[Task],
    nsrts: Optional[Set[NSRT]] = None,
    option_model: Optional[_OptionModelBase] = None,
    babble_predicates: Optional[Set[Predicate]] = None,
    atom_score_fn: Optional[Callable[[Set[GroundAtom]], float]] = None,
    state_score_fn: Optional[Callable[[Set[GroundAtom], State], float]] = None,
) -> BaseExplorer:
    """Create an explorer given its name."""
    for cls in utils.get_all_subclasses(BaseExplorer):
        if not cls.__abstractmethods__ and cls.get_name() == name:
            # Special case GLIB because it uses babble predicates and an atom
            # score function.
            if name == "glib":
                assert nsrts is not None
                assert option_model is not None
                assert babble_predicates is not None
                assert atom_score_fn is not None
                explorer = cls(initial_predicates, initial_options, types,
                               action_space, train_tasks, nsrts, option_model,
                               babble_predicates, atom_score_fn)
            # Special case greedy lookahead because it uses a state score
            # function.
            elif name == "greedy_lookahead":
                assert nsrts is not None
                assert option_model is not None
                assert state_score_fn is not None
                explorer = cls(initial_predicates, initial_options, types,
                               action_space, train_tasks, nsrts, option_model,
                               state_score_fn)
            # Bilevel planning approaches use NSRTs and an option model.
            elif issubclass(cls, BilevelPlanningExplorer):
                assert nsrts is not None
                assert option_model is not None
                explorer = cls(initial_predicates, initial_options, types,
                               action_space, train_tasks, nsrts, option_model)
            # Random NSRTs explorer uses NSRTs, but not an option model
            elif name == "random_nsrts":
                assert nsrts is not None
                explorer = cls(initial_predicates, initial_options, types,
                               action_space, train_tasks, nsrts)
            else:
                explorer = cls(initial_predicates, initial_options, types,
                               action_space, train_tasks)
            break
    else:
        raise NotImplementedError(f"Unrecognized explorer: {name}")
    return explorer
