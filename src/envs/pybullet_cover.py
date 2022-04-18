"""A PyBullet version of Cover."""

import logging
from typing import Callable, ClassVar, Dict, List, Sequence, Tuple

import numpy as np
import pybullet as p
from gym.spaces import Box

from predicators.src import utils
from predicators.src.envs.cover import CoverEnv
from predicators.src.envs.pybullet_env import PyBulletEnv, create_pybullet_block
from predicators.src.envs.pybullet_robots import _SingleArmPyBulletRobot, \
    create_single_arm_pybullet_robot
from predicators.src.settings import CFG
from predicators.src.structs import Array, Object, ParameterizedOption, \
    Pose3D, State, Action


class PyBulletCoverEnv(PyBulletEnv, CoverEnv):
    """PyBullet Cover domain."""
    # Parameters that aren't important enough to need to clog up settings.py

    # Option parameters.
    _open_fingers: ClassVar[float] = 0.04
    _closed_fingers: ClassVar[float] = 0.01
    _move_to_pose_tol: ClassVar[float] = 1e-7

    # Table parameters.
    _table_pose: ClassVar[Pose3D] = (1.35, 0.75, 0.0)
    _table_orientation: ClassVar[Sequence[float]] = [0., 0., 0., 1.]

    # Object parameters.
    _obj_len_hgt = 0.045
    _max_obj_width = 0.07  # highest width is normalized to this value

    # Dimension and workspace parameters.
    _table_height: ClassVar[float] = 0.2
    _y_lb: ClassVar[float] = 0.4
    _y_ub: ClassVar[float] = 1.1
    _robot_init_y: ClassVar[float] = (_y_lb + _y_ub) / 2
    _pickplace_z: ClassVar[float] = _table_height + _obj_len_hgt * 0.5 + 0.01
    _target_height: ClassVar[float] = 0.0001

    def __init__(self) -> None:
        super().__init__()

        # Override PickPlace option
        types = self._PickPlace.types
        params_space = self._PickPlace.params_space
        toggle_fingers_func = lambda s, _1, _2: (
            (self._open_fingers, self._closed_fingers)
            if self._HandEmpty_holds(s, []) else
            (self._closed_fingers, self._open_fingers))
        self._PickPlace: ParameterizedOption = \
            utils.LinearChainParameterizedOption(
                "PickPlace",
                [
                    # Move to far above the location we will pick/place at.
                    self._create_cover_move_option(
                        name="MoveEndEffectorToPrePose",
                        target_z=self._workspace_z),
                    # Move down to pick/place.
                    self._create_cover_move_option(
                        name="MoveEndEffectorToPose",
                        target_z=self._pickplace_z),
                    # Toggle fingers.
                    self._pybullet_robot.create_change_fingers_option(
                        "ToggleFingers", types, params_space,
                        toggle_fingers_func),
                    # Move back up.
                    self._create_cover_move_option(
                        name="MoveEndEffectorBackUp",
                        target_z=self._workspace_z)
                ])
        self._block_id_to_block: Dict[int, Object] = {}
        self._target_id_to_target: Dict[int, Object] = {}

    def _initialize_pybullet(self) -> None:
        """Run super(), then handle cover-specific initialization."""
        super()._initialize_pybullet()

        # Load table.
        self._table_id = p.loadURDF(
            utils.get_env_asset_path("urdf/table.urdf"),
            useFixedBase=True,
            physicsClientId=self._physics_client_id)
        p.resetBasePositionAndOrientation(
            self._table_id,
            self._table_pose,
            self._table_orientation,
            physicsClientId=self._physics_client_id)

        # Skip test coverage because GUI is too expensive to use in unit tests
        # and cannot be used in headless mode.
        if CFG.pybullet_draw_debug:  # pragma: no cover
            # TODO: draw hand regions
            pass

        max_width = max(max(CFG.cover_block_widths),
                        max(CFG.cover_target_widths))
        self._block_ids = []
        for i in range(CFG.cover_num_blocks):
            color = self._obj_colors[i % len(self._obj_colors)]
            width = CFG.cover_block_widths[i] / max_width * self._max_obj_width
            half_extents = (self._obj_len_hgt / 2.0, width / 2.0,
                            self._obj_len_hgt / 2.0)
            orientation = [0.0, 0.0, 0.0, 1.0]  # default
            self._block_ids.append(
                create_pybullet_block(color, half_extents, self._obj_mass,
                                      self._obj_friction, orientation,
                                      self._physics_client_id))
        self._target_ids = []
        for i in range(CFG.cover_num_targets):
            color = self._obj_colors[i % len(self._obj_colors)]
            color = (color[0], color[1], color[2], 0.5)  # slightly transparent
            width = CFG.cover_target_widths[i] / max_width * self._max_obj_width
            half_extents = [self._obj_len_hgt / 2.0, width / 2.0,
                            self._target_height / 2.0]
            orientation = [0.0, 0.0, 0.0, 1.0]  # default
            self._target_ids.append(
                create_pybullet_block(color, half_extents, self._obj_mass,
                                      self._obj_friction, orientation,
                                      self._physics_client_id))

    def _create_pybullet_robot(self) -> _SingleArmPyBulletRobot:
        ee_home = (self._workspace_x, self._robot_init_y, self._workspace_z)
        ee_orn = p.getQuaternionFromEuler([np.pi / 2, np.pi / 2, -np.pi])
        return create_single_arm_pybullet_robot(CFG.pybullet_robot, ee_home,
                                                ee_orn,
                                                self._open_fingers,
                                                self._closed_fingers,
                                                self._move_to_pose_tol,
                                                self._max_vel_norm,
                                                self._grasp_tol,
                                                self._physics_client_id)

    def _extract_robot_state(self, state: State) -> Array:
        if self._HandEmpty_holds(state, []):
            fingers = self._open_fingers
        else:
            fingers = self._closed_fingers
        y_norm = state.get(self._robot, "hand")
        # De-normalize robot y to actual coordinates.
        ry = self._y_lb + (self._y_ub - self._y_lb) * y_norm
        rx = state.get(self._robot, "pose_x")
        rz = state.get(self._robot, "pose_z")
        return np.array([rx, ry, rz, fingers], dtype=np.float32)

    def _reset_state(self, state: State) -> None:
        """Run super(), then handle cover-specific resetting."""
        super()._reset_state(state)

        # Reset blocks based on the state.
        # Assume not holding in the initial state
        assert self._HandEmpty_holds(state, [])
        block_objs = state.get_objects(self._block_type)
        self._block_id_to_block = {}
        for i, block_obj in enumerate(block_objs):
            block_id = self._block_ids[i]
            self._block_id_to_block[block_id] = block_obj
            bx = self._workspace_x
            # De-normalize block y to actual coordinates.
            y_norm = state.get(block_obj, "pose")
            by = self._y_lb + (self._y_ub - self._y_lb) * y_norm
            height = p.getVisualShapeData(block_id)[0][3][-1]
            bz = self._table_height + height * 0.5
            p.resetBasePositionAndOrientation(
                block_id, [bx, by, bz], [0.0, 0.0, 0.0, 1.0],
                physicsClientId=self._physics_client_id)

        target_objs = state.get_objects(self._target_type)
        self._target_id_to_target = {}
        for i, target_obj in enumerate(target_objs):
            target_id = self._target_ids[i]
            self._target_id_to_target[target_id] = target_obj
            tx = self._workspace_x
            # De-normalize target y to actual coordinates.
            y_norm = state.get(target_obj, "pose")
            ty = self._y_lb + (self._y_ub - self._y_lb) * y_norm
            height = p.getVisualShapeData(target_id)[0][3][-1]
            tz = self._table_height + height * 0.5
            p.resetBasePositionAndOrientation(
                target_id, [tx, ty, tz], [0.0, 0.0, 0.0, 1.0],
                physicsClientId=self._physics_client_id)

        # Assert that the state was properly reconstructed.
        reconstructed_state = self._get_state()
        if not reconstructed_state.allclose(state):
            logging.debug("Desired state:")
            logging.debug(state.pretty_str())
            logging.debug("Reconstructed state:")
            logging.debug(reconstructed_state.pretty_str())
            raise ValueError("Could not reconstruct state.")

    def step(self, action: Action) -> State:
        # In the cover environment, we need to first check the hand region
        # constraint before we can call PyBullet.
        # TODO: figuring out the pose from the action (joints) requires FK
        # hand_regions = self._get_hand_regions(self._current_state)
        # If we're not in any hand region, no-op.
        # if not any(hand_lb <= pose <= hand_rb
        #            for hand_lb, hand_rb in hand_regions):
        #     return self._current_state.copy()
        return super().step(action)

    def _get_state(self) -> State:
        state_dict = {}
        max_width = max(max(CFG.cover_block_widths),
                        max(CFG.cover_target_widths))

        # Get robot state.
        rx, ry, rz, _ = self._pybullet_robot.get_state()
        hand = (ry - self._y_lb) / (self._y_ub - self._y_lb)
        state_dict[self._robot] = np.array([hand, rx, rz], dtype=np.float32)
        joint_state = self._pybullet_robot.get_joints()

        # Get block states.
        for block_id, block in self._block_id_to_block.items():
            width_unnorm = p.getVisualShapeData(block_id)[0][3][1]
            width = width_unnorm / self._max_obj_width * max_width
            (_, by, _), _ = p.getBasePositionAndOrientation(
                block_id, physicsClientId=self._physics_client_id)
            pose = (by - self._y_lb) / (self._y_ub - self._y_lb)
            held = (block_id == self._held_obj_id)
            if held:
                grasp_unnorm = p.getConstraintInfo(
                    self._held_constraint_id, self._physics_client_id)[7][1]
                # Normalize grasp.
                grasp = grasp_unnorm / (self._y_ub - self._y_lb)
            else:
                grasp = -1
            state_dict[block] = np.array([1.0, 0.0, width, pose, grasp],
                                         dtype=np.float32)

        # Get target states.
        for target_id, target in self._target_id_to_target.items():
            width_unnorm = p.getVisualShapeData(target_id)[0][3][1]
            width = width_unnorm / self._max_obj_width * max_width
            (_, ty, _), _ = p.getBasePositionAndOrientation(
                target_id, physicsClientId=self._physics_client_id)
            pose = (ty - self._y_lb) / (self._y_ub - self._y_lb)
            state_dict[target] = np.array([0.0, 1.0, width, pose],
                                          dtype=np.float32)

        state = utils.PyBulletState(state_dict, simulator_state=joint_state)
        assert set(state) == set(self._current_state), \
            (f"Reconstructed state has objects {set(state)}, but "
             f"self._current_state has objects {set(self._current_state)}.")

        return state

    def _get_object_ids_for_held_check(self) -> List[int]:
        return sorted(self._block_id_to_block)

    @classmethod
    def get_name(cls) -> str:
        return "pybullet_cover"

    def _create_cover_move_option(self, name: str, target_z: float
                                  ) -> ParameterizedOption:
        """Creates a ParameterizedOption for moving to a pose in Cover."""
        types = []
        params_space = Box(0, 1, (1, ))

        def _get_current_and_target_pose_and_finger_status(
                state: State, objects: Sequence[Object],
                params: Array) -> Tuple[Pose3D, Pose3D, str]:
            assert not objects
            hand = state.get(self._robot, "hand")
            # De-normalize hand feature to actual table coordinates.
            current_y = self._y_lb + (self._y_ub - self._y_lb) * hand
            current_pose = (state.get(self._robot, "pose_x"), current_y,
                            state.get(self._robot, "pose_z"))
            y_norm, = params
            # De-normalize parameter to actual table coordinates.
            target_y = self._y_lb + (self._y_ub - self._y_lb) * y_norm
            target_pose = (self._workspace_x, target_y, target_z)
            if self._HandEmpty_holds(state, []):
                finger_status = "open"
            else:
                finger_status = "closed"
            return current_pose, target_pose, finger_status

        return self._pybullet_robot.create_move_end_effector_to_pose_option(
            name, types, params_space,
            _get_current_and_target_pose_and_finger_status)
