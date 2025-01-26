"""Ground-truth options for the coffee environment."""

import logging
from functools import lru_cache
from typing import Callable, ClassVar, Dict, List, Sequence, Set, Tuple
from typing import Type as TypingType

import numpy as np
import pybullet as p
from gym.spaces import Box

from predicators import utils
from predicators.envs.pybullet_domino import PyBulletDominoEnv
from predicators.envs.pybullet_env import PyBulletEnv
from predicators.ground_truth_models import GroundTruthOptionFactory
from predicators.pybullet_helpers.controllers import \
    create_change_fingers_option, create_move_end_effector_to_pose_option
from predicators.pybullet_helpers.geometry import Pose
from predicators.pybullet_helpers.robots import SingleArmPyBulletRobot
from predicators.settings import CFG
from predicators.structs import Action, Array, Object, ParameterizedOption, \
    ParameterizedPolicy, Predicate, State, Type


@lru_cache
def _get_pybullet_robot() -> SingleArmPyBulletRobot:
    _, pybullet_robot, _ = \
        PyBulletDominoEnv.initialize_pybullet(using_gui=False)
    return pybullet_robot


class PyBulletDominoGroundTruthOptionFactory(GroundTruthOptionFactory):
    """Ground-truth options for the grow environment."""

    env_cls: ClassVar[TypingType[PyBulletDominoEnv]] = PyBulletDominoEnv
    _move_to_pose_tol: ClassVar[float] = 1e-4
    _finger_action_nudge_magnitude: ClassVar[float] = 1e-3
    _transport_z: ClassVar[float] = env_cls.z_ub - 0.3
    _offset_x: ClassVar[float] = 0.05
    _offset_z: ClassVar[float] = 0.1

    @classmethod
    def get_env_names(cls) -> Set[str]:
        return {"pybullet_domino"}

    @classmethod
    def get_options(cls, env_name: str, types: Dict[str, Type],
                    predicates: Dict[str, Predicate],
                    action_space: Box) -> Set[ParameterizedOption]:
        """Get the ground-truth options for the grow environment."""
        del env_name, predicates, action_space  # unused

        _, pybullet_robot, _ = \
            PyBulletDominoEnv.initialize_pybullet(using_gui=False)

        # Types
        robot_type = types["robot"]
        domino_type = types["domino"]

        def get_current_fingers(state: State) -> float:
            robot, = state.get_objects(robot_type)
            return PyBulletDominoEnv._fingers_state_to_joint(
                pybullet_robot, state.get(robot, "fingers"))

        def open_fingers_func(state: State, objects: Sequence[Object],
                              params: Array) -> Tuple[float, float]:
            del objects, params  # unused
            current = get_current_fingers(state)
            target = pybullet_robot.open_fingers
            return current, target

        def close_fingers_func(state: State, objects: Sequence[Object],
                               params: Array) -> Tuple[float, float]:
            del objects, params  # unused
            current = get_current_fingers(state)
            target = pybullet_robot.closed_fingers
            return current, target

        options = set()
        # Push
        option_type = [robot_type, domino_type]
        params_space = Box(0, 1, (0, ))
        Push = utils.LinearChainParameterizedOption(
            "Push",
            [
                create_change_fingers_option(
                    pybullet_robot, "CloseFingers", option_type, params_space,
                    close_fingers_func, CFG.pybullet_max_vel_norm,
                    PyBulletEnv.grasp_tol_small),
                cls._create_domino_move_to_push_domino_option(
                    "MoveToAboveDomino", 
                    lambda x, rot: x - np.sin(rot) * cls._offset_x,
                    lambda y, rot: y - np.cos(rot) * cls._offset_x,
                    lambda _: cls._transport_z, "closed", option_type,
                    params_space),
                cls._create_domino_move_to_push_domino_option(
                    "MoveToBehindDomino", 
                    lambda x, rot: x - np.sin(rot) * cls._offset_x,
                    lambda y, rot: y - np.cos(rot) * cls._offset_x,
                    lambda z: z + cls._offset_z, "closed", option_type,
                    params_space),
                cls._create_domino_move_to_push_domino_option(
                    "PushDomino",
                    lambda x, rot: x + np.sin(rot) * cls._offset_x/4,
                    lambda y, rot: y + np.cos(rot) * cls._offset_x/4,
                    lambda z: z + cls._offset_z, "closed", option_type,
                    params_space),
                cls._create_domino_move_to_push_domino_option(
                    "BackUp",
                    lambda _1, _2: cls.env_cls.robot_init_x,
                    lambda _1, _2: cls.env_cls.robot_init_y,
                    lambda _: cls.env_cls.robot_init_z, 
                    "closed", option_type,
                    params_space),
                create_change_fingers_option(
                    pybullet_robot, "OpenFingers", option_type, params_space,
                    open_fingers_func, CFG.pybullet_max_vel_norm,
                    PyBulletEnv.grasp_tol_small),
                # cls._create_domino_move_to_push_domino_option(
                #     "MoveToBehindDomino",
                #     lambda _: cls.env_cls.start_domino_x - cls._offset_x,
                #     lambda z: z + cls._offset_z,
                #     "closed",
                #     option_type, params_space),
            ])
        options.add(Push)

        return options

    @classmethod
    def _create_domino_move_to_push_domino_option(
            cls, name: str, x_func: Callable[[float], float], 
                            y_func: Callable[[float], float],
                            z_func: Callable[[float], float],
            finger_status: str, option_types: List[Type],
            params_space: Box) -> ParameterizedOption:
        """Create a move-to-pose option for the domino environment."""

        def _get_current_and_target_pose_and_finger_status(
                state: State, objects: Sequence[Object], params: Array) -> \
                Tuple[Pose, Pose, str]:
            assert not params
            robot, domino = objects
            current_position = (state.get(robot, "x"), state.get(robot, "y"),
                                state.get(robot, "z"))
            ee_orn = p.getQuaternionFromEuler(
                [0, state.get(robot, "tilt"),
                 state.get(robot, "wrist")])
            current_pose = Pose(current_position, ee_orn)
            dx = state.get(domino, "x")
            dy = state.get(domino, "y")
            dz = state.get(domino, "z")
            drot = state.get(domino, "rot")
            target_position = (x_func(dx, drot), 
                               y_func(dy, drot),
                               z_func(dz))
            target_orn = p.getQuaternionFromEuler(
                [0, cls.env_cls.robot_init_tilt, drot + np.pi/2])
            target_pose = Pose(target_position, target_orn)
            return current_pose, target_pose, finger_status

        return create_move_end_effector_to_pose_option(
            _get_pybullet_robot(),
            name,
            option_types,
            params_space,
            _get_current_and_target_pose_and_finger_status,
            cls._move_to_pose_tol,
            CFG.pybullet_max_vel_norm,
            cls._finger_action_nudge_magnitude,
            validate=CFG.pybullet_ik_validate)
