"""A bilevel planning approach that learns NSRTs.

Learns operators and samplers. Does not attempt to learn new predicates
or options.
"""

import logging
from typing import Set, List, Optional, Callable
import dill as pkl
from gym.spaces import Box
from predicators.src.approaches import BilevelPlanningApproach2
from predicators.src.structs import Dataset, NSRT, ParameterizedOption, \
    Predicate, Type, Task, LowLevelTrajectory, State, Action
from predicators.src.nsrt_learning.nsrt_learning_main import \
    learn_nsrts_from_data
from predicators.src.settings import CFG
from predicators.src import utils
import abc
from typing import Callable, Set, List
from gym.spaces import Box
from predicators.src.approaches import BaseApproach, ApproachFailure
from predicators.src.planning import sesame_plan
from predicators.src.structs import State, Action, Task, NSRT, \
    Predicate, ParameterizedOption, Type, _Option
from predicators.src.option_model import create_option_model
from predicators.src.settings import CFG
from predicators.src import utils


class NSRTLearningApproach(BilevelPlanningApproach2):
    """A bilevel planning approach that learns NSRTs."""

    def __init__(self, initial_predicates: Set[Predicate],
                 initial_options: Set[ParameterizedOption], types: Set[Type],
                 action_space: Box, train_tasks: List[Task]) -> None:
        super().__init__(initial_predicates, initial_options, types,
                         action_space, train_tasks)
        self._nsrts: Set[NSRT] = set()

    @property
    def is_learning_based(self) -> bool:
        return True

    def _get_current_nsrts(self) -> Set[NSRT]:
        return self._nsrts

    def learn_from_offline_dataset(self, dataset: Dataset) -> None:
        # The only thing we need to do here is learn NSRTs,
        # which we split off into a different function in case
        # subclasses want to make use of it.
        self._learn_nsrts(dataset.trajectories, online_learning_cycle=None)

    def _learn_nsrts(self, trajectories: List[LowLevelTrajectory],
                     online_learning_cycle: Optional[int]) -> None:
        print("Size of trajectories: ", len(trajectories))
        self._nsrts = learn_nsrts_from_data(
            trajectories,
            self._train_tasks,
            self._get_current_predicates(),
            sampler_learner=CFG.sampler_learner)
        save_path = utils.get_approach_save_path_str()
        with open(f"{save_path}_{online_learning_cycle}.NSRTs", "wb") as f:
            pkl.dump(self._nsrts, f)

    def load(self, online_learning_cycle: Optional[int]) -> None:
        save_path = utils.get_approach_save_path_str()
        with open(f"{save_path}_{online_learning_cycle}.NSRTs", "rb") as f:
            self._nsrts = pkl.load(f)
        if CFG.pretty_print_when_loading:
            preds, _ = utils.extract_preds_and_types(self._nsrts)
            name_map = {}
            logging.info("Invented predicates:")
            for idx, pred in enumerate(
                    sorted(set(preds.values()) - self._initial_predicates)):
                vars_str, body_str = pred.pretty_str()
                logging.info(f"\tP{idx+1}({vars_str}) ≜ {body_str}")
                name_map[body_str] = f"P{idx+1}"
        logging.info("\n\nLoaded NSRTs:")
        for nsrt in sorted(self._nsrts):
            if CFG.pretty_print_when_loading:
                logging.info(nsrt.pretty_str(name_map))
            else:
                logging.info(nsrt)
        logging.info("")
        # Seed the option parameter spaces after loading.
        for nsrt in self._nsrts:
            nsrt.option.params_space.seed(CFG.seed)
