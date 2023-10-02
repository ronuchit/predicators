"""Ground-truth options for the sticky table environment."""

from typing import Dict, Sequence, Set

import numpy as np
from gym.spaces import Box

from predicators import utils
from predicators.envs.sticky_table import StickyTableEnv
from predicators.ground_truth_models import GroundTruthOptionFactory
from predicators.structs import Action, Array, Object, ParameterizedOption, \
    ParameterizedPolicy, Predicate, State, Type


class StickyTableGroundTruthOptionFactory(GroundTruthOptionFactory):
    """Ground-truth options for the sticky table environment."""

    @classmethod
    def get_env_names(cls) -> Set[str]:
        return {
            "sticky_table", "sticky_table_tricky_floor",
            "sticky_table_with_nav"
        }

    @classmethod
    def get_options(cls, env_name: str, types: Dict[str, Type],
                    predicates: Dict[str, Predicate],
                    action_space: Box) -> Set[ParameterizedOption]:

        cube_type = types["cube"]
        table_type = types["table"]
        # Parameters are move_or_pickplace, absolute x, y actions.
        params_space = Box(
            np.array([0.0, StickyTableEnv.x_lb, StickyTableEnv.y_lb]),
            np.array([1.0, StickyTableEnv.x_ub, StickyTableEnv.y_ub]))
        robot_type = types["robot"]

        PickFromTable = utils.SingletonParameterizedOption(
            # variables: [robot, cube, table]
            "PickFromTable",
            cls._create_pass_through_policy(action_space),
            params_space=params_space,
            types=[robot_type, cube_type, table_type])

        PickFromFloor = utils.SingletonParameterizedOption(
            # variables: [robot, cube]
            "PickFromFloor",
            cls._create_pass_through_policy(action_space),
            # Parameters are absolute x, y actions.
            params_space=params_space,
            types=[robot_type, cube_type])

        PlaceOnTable = utils.SingletonParameterizedOption(
            # variables: [robot, cube, table]
            "PlaceOnTable",
            cls._create_pass_through_policy(action_space),
            # Parameters are absolute x, y actions.
            params_space=params_space,
            types=[robot_type, cube_type, table_type])

        PlaceOnFloor = utils.SingletonParameterizedOption(
            # variables: [robot, cube]
            "PlaceOnFloor",
            cls._create_pass_through_policy(action_space),
            # Parameters are absolute x, y actions.
            params_space=params_space,
            types=[robot_type, cube_type])

        NavigateToCube = utils.SingletonParameterizedOption(
            # variables: [robot, cube]
            "NavigateToCube",
            cls._create_pass_through_policy(action_space),
            # Parameters are absolute x, y actions.
            params_space=params_space,
            types=[robot_type, cube_type])
        
        NavigateToTable = utils.SingletonParameterizedOption(
            # variables: [robot, table]
            "NavigateToTable",
            cls._create_pass_through_policy(action_space),
            # Parameters are absolute x, y actions.
            params_space=params_space,
            types=[robot_type, table_type])

        return {
            PickFromTable, PickFromFloor, PlaceOnTable, PlaceOnFloor,
            NavigateToCube, NavigateToTable
        }

    @classmethod
    def _create_pass_through_policy(cls,
                                    action_space: Box) -> ParameterizedPolicy:

        def policy(state: State, memory: Dict, objects: Sequence[Object],
                   params: Array) -> Action:
            del state, memory, objects  # unused
            arr = np.array(params, dtype=np.float32)
            arr = np.clip(arr, action_space.low, action_space.high)
            return Action(arr)

        return policy
