"""Example usage:

python predicators/main.py --approach oracle --env pybullet_domino \
--seed 0 --num_test_tasks 1 --use_gui --debug --num_train_tasks 0 \
--sesame_max_skeletons_optimized 1  --make_failure_videos --video_fps 20 \
--pybullet_camera_height 900 --pybullet_camera_width 900 --debug \
--sesame_check_expected_atoms False --horizon 60 \
--video_not_break_on_exception --pybullet_ik_validate False
"""
import time
import logging
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pybullet as p

from predicators import utils
from predicators.envs.pybullet_env import PyBulletEnv, create_pybullet_block
from predicators.pybullet_helpers.geometry import Pose3D, Quaternion
from predicators.pybullet_helpers.objects import create_object, update_object
from predicators.pybullet_helpers.robots import SingleArmPyBulletRobot
from predicators.settings import CFG
from predicators.structs import Action, EnvironmentTask, GroundAtom, Object, \
    Predicate, State, Type


class PyBulletDominoEnv(PyBulletEnv):
    """A simple PyBullet environment involving M dominoes and N targets.

    Each target is considered 'toppled' if it is significantly tilted
    from its upright orientation. The overall goal is to topple all
    targets.
    """

    _out_of_view_xy: ClassVar[Sequence[float]] = [10.0, 10.0]

    # Table / workspace config
    table_height: ClassVar[float] = 0.4
    table_pos: ClassVar[Pose3D] = (0.75, 1.35, table_height / 2)
    table_orn: ClassVar[Quaternion] = p.getQuaternionFromEuler(
        [0., 0., np.pi / 2])

    x_lb: ClassVar[float] = 0.4
    x_ub: ClassVar[float] = 1.1
    y_lb: ClassVar[float] = 1.1
    y_ub: ClassVar[float] = 1.6
    z_lb: ClassVar[float] = table_height
    z_ub: ClassVar[float] = 0.75 + table_height / 2

    # Domino shape
    domino_width: ClassVar[float] = 0.07
    domino_depth: ClassVar[float] = 0.02
    domino_height: ClassVar[float] = 0.15
    domino_mass: ClassVar[float] = 0.3
    light_green: ClassVar[Tuple[float, float, float,
                                float]] = (0.56, 0.93, 0.56, 1.)
    domino_color: ClassVar[Tuple[float, float, float,
                                 float]] = (0.6, 0.8, 1.0, 1.0)
    start_domino_x: ClassVar[float] = x_lb + domino_width
    start_domino_y: ClassVar[float] = y_lb + domino_width

    target_height: ClassVar[float] = 0.2
    pivot_width: ClassVar[float] = 0.2

    # For deciding if a target is toppled: if absolute tilt in x or y
    # is bigger than some threshold (e.g. 0.4 rad ~ 23 deg), treat as toppled.
    topple_angle_threshold: ClassVar[float] = 0.4

    # Camera defaults, optional
    _camera_distance: ClassVar[float] = 1.3
    _camera_yaw: ClassVar[float] = 70
    _camera_pitch: ClassVar[float] = -40
    _camera_target: ClassVar[Pose3D] = (0.75, 1.25, 0.42)

    robot_init_x: ClassVar[float] = (x_lb + x_ub) * 0.5
    robot_init_y: ClassVar[float] = (y_lb + y_ub) * 0.5
    robot_init_z: ClassVar[float] = z_ub
    robot_base_pos: ClassVar[Pose3D] = (0.75, 0.72, 0.0)
    robot_base_orn: ClassVar[Quaternion] = p.getQuaternionFromEuler(
        [0.0, 0.0, np.pi / 2])
    robot_init_tilt: ClassVar[float] = np.pi / 2
    robot_init_wrist: ClassVar[float] = -np.pi / 2

    num_dominos = 9
    num_targets = 2
    num_pivots = 1

    _robot_type = Type("robot", ["x", "y", "z", "fingers", "tilt", "wrist"])
    _domino_type = Type(
        "domino",
        ["x", "y", "z", "rot", "start_block", "is_held"],
    )
    _target_type = Type("target", ["x", "y", "z", "rot"],
                        sim_features=["id", "joint_id"])
    _pivot_type = Type("pivot", ["x", "y", "z", "rot"],
                       sim_features=["id", "joint_id"])

    def __init__(self, use_gui: bool = True) -> None:
        # Create 'dummy' Objects (they'll be assigned IDs on reset)
        self._robot = Object("robot", self._robot_type)
        # We'll hold references to all domino and target objects in lists
        # after we create them in tasks.
        self.dominos: List[Object] = []
        for i in range(self.num_dominos):
            name = f"domino_{i}"
            obj_type = self._domino_type
            obj = Object(name, obj_type)
            self.dominos.append(obj)
        self.targets: List[Object] = []
        for i in range(self.num_targets):
            name = f"target_{i}"
            obj_type = self._target_type
            obj = Object(name, obj_type)
            self.targets.append(obj)
        self.pivots: List[Object] = []
        for i in range(self.num_pivots):
            name = f"pivot_{i}"
            obj_type = self._pivot_type
            obj = Object(name, obj_type)
            self.pivots.append(obj)

        super().__init__(use_gui)

        # Define Predicates
        self._Toppled = Predicate("Toppled", [self._target_type],
                                  self._Toppled_holds)
        self._StartBlock = Predicate("StartBlock", [self._domino_type],
                                     self._StartBlock_holds)
        self._HandEmpty = Predicate("HandEmpty", [self._robot_type],
                                    self._HandEmpty_holds)
        self._Holding = Predicate("Holding",
                                  [self._robot_type, self._domino_type],
                                  self._Holding_holds)

    @classmethod
    def get_name(cls) -> str:
        return "pybullet_domino"

    @property
    def predicates(self) -> Set[Predicate]:
        return {
            self._Toppled, self._StartBlock, self._HandEmpty, self._Holding
        }

    @property
    def goal_predicates(self) -> Set[Predicate]:
        # The goal is always to topple all targets
        return {self._Toppled}

    @property
    def types(self) -> Set[Type]:
        return {
            self._robot_type, self._domino_type, self._target_type,
            self._pivot_type
        }

    # -------------------------------------------------------------------------
    # Environment Setup

    @classmethod
    def initialize_pybullet(
            cls, using_gui: bool
    ) -> Tuple[int, SingleArmPyBulletRobot, Dict[str, Any]]:
        # Reuse parent method to create a robot and get a physics client
        physics_client_id, pybullet_robot, bodies = super(
        ).initialize_pybullet(using_gui)

        # (Optional) Add a simple table
        table_id = create_object(asset_path="urdf/table.urdf",
                                 position=cls.table_pos,
                                 orientation=cls.table_orn,
                                 scale=1.0,
                                 use_fixed_base=True,
                                 physics_client_id=physics_client_id)
        bodies["table_id"] = table_id

        # Create a fixed number of dominoes and targets here
        domino_ids = []
        target_ids = []
        for i in range(cls.num_dominos):  # e.g. 3 dominoes
            domino_id = create_pybullet_block(
                color=cls.light_green if i == 0 else cls.domino_color,
                half_extents=(cls.domino_width / 2, cls.domino_depth / 2,
                              cls.domino_height / 2),
                mass=cls.domino_mass,
                friction=0.5,
                orientation=[0.0, 0.0, 0.0],
                physics_client_id=physics_client_id,
            )
            domino_ids.append(domino_id)
        for _ in range(cls.num_targets):  # e.g. 2 targets
            tid = create_object("urdf/domino_target.urdf",
                                position=(cls.x_lb, cls.y_lb, cls.z_lb),
                                orientation=p.getQuaternionFromEuler(
                                    [0.0, 0.0, 0.0]),
                                scale=1.0,
                                use_fixed_base=True,
                                physics_client_id=physics_client_id)
            target_ids.append(tid)
        pivot_ids = []
        for _ in range(cls.num_pivots):
            pid = create_object("urdf/domino_pivot.urdf",
                                position=(cls.x_lb, cls.y_lb, cls.z_lb),
                                orientation=p.getQuaternionFromEuler(
                                    [0.0, 0.0, 0.0]),
                                scale=1.0,
                                use_fixed_base=True,
                                physics_client_id=physics_client_id)
            pivot_ids.append(pid)
        bodies["pivot_ids"] = pivot_ids
        bodies["domino_ids"] = domino_ids
        bodies["target_ids"] = target_ids

        return physics_client_id, pybullet_robot, bodies

    @staticmethod
    def _get_joint_id(obj_id: int, joint_name: str) -> int:
        num_joints = p.getNumJoints(obj_id)
        for j in range(num_joints):
            info = p.getJointInfo(obj_id, j)
            if info[1].decode("utf-8") == joint_name:
                return j
        return -1

    def _store_pybullet_bodies(self, pybullet_bodies: Dict[str, Any]) -> None:
        # We don't have a single known ID for dominoes or targets, so we'll store
        # them all at runtime. For now, we just keep a reference to the dict.
        for domini, id in zip(self.dominos, pybullet_bodies["domino_ids"]):
            domini.id = id
        for target, id in zip(self.targets, pybullet_bodies["target_ids"]):
            target.id = id
            target.joint_id = self._get_joint_id(id, "flap_hinge_joint")
        for pivot, pid in zip(self.pivots, pybullet_bodies["pivot_ids"]):
            pivot.id = pid
            pivot.joint_id = self._get_joint_id(pid, "flap_hinge_joint")

    # -------------------------------------------------------------------------
    # State Management

    def _get_object_ids_for_held_check(self) -> List[int]:
        return []

    def _create_task_specific_objects(self, state):
        pass

    def _extract_feature(self, obj: Object, feature: str) -> float:
        """Extract features for creating the State object."""
        if obj.type == self._domino_type:
            if feature == "start_block":
                return 1.0 if obj.name == "domino_0" else 0.0

        raise ValueError(f"Unknown feature {feature} for object {obj}")

    def _reset_custom_env_state(self, state: State) -> None:
        """Reset the custom environment state to match the given state."""
        domino_objs = state.get_objects(self._domino_type)
        oov_x, oov_y = self._out_of_view_xy
        for i in range(len(domino_objs), len(self.dominos)):
            oov_x += 0.1
            oov_y += 0.1
            update_object(self.dominos[i].id,
                          position=(oov_x, oov_y, self.domino_height/2),
                          physics_client_id=self._physics_client_id)

        target_objs = state.get_objects(self._target_type)
        for target_obj in target_objs:
            self._set_flat_rotation(target_obj, 0.0)
        for i in range(len(target_objs), len(self.targets)):
            oov_x += 0.1
            oov_y += 0.1
            update_object(self.targets[i].id,
                          position=(oov_x, oov_y, self.domino_height/2),
                          physics_client_id=self._physics_client_id)

        pivot_objs = state.get_objects(self._pivot_type)
        for pivot_obj in pivot_objs:
            self._set_flat_rotation(pivot_obj, 0.0)
        for i in range(len(pivot_objs), len(self.pivots)):
            oov_x += 0.1
            oov_y += 0.1
            update_object(self.pivots[i].id,
                          position=(oov_x, oov_y, self.domino_height/2),
                          physics_client_id=self._physics_client_id)

    def _get_flat_rotation(self, flap_obj: Object) -> float:
        j_pos, _, _, _ = p.getJointState(flap_obj.id, flap_obj.joint_id)
        return j_pos
    
    def _set_flat_rotation(self, flap_obj: Object, rot: float = 0.0) -> None:
        p.resetJointState(flap_obj.id, flap_obj.joint_id, rot)
        return

    def step(self, action: Action, render_obs: bool = False) -> State:
        """In this domain, stepping might be trivial (we won't do anything
        special aside from the usual robot step)."""
        next_state = super().step(action, render_obs=render_obs)

        final_state = self._get_state()
        self._current_observation = final_state
        return final_state

    # -------------------------------------------------------------------------
    # Predicates

    @classmethod
    def _Toppled_holds(cls, state: State, objects: Sequence[Object]) -> bool:
        """Target is toppled if it’s significantly tilted from upright in pitch
        or roll. We measure this from the actual PyBullet orientation (read
        from .step()), but for demonstration we can say that if the object was
        set (or has become) rotated in x/y beyond a threshold, it’s toppled.

        Here, we only have `rot` about z in the state, but in a real
        implementation, you might store or compute orientation around
        x,y, etc. We'll demonstrate a simpler check for a large rotation
        from upright.
        """
        return False
        # If we had orientation around x or y in the state, we'd read that here.
        # Suppose we treat a large difference in z-rotation from the "initial"
        # upright as toppled (just a simplified approach).
        # In a real scenario, you'd measure pitch/roll or object bounding box.
        target, = objects
        rot_z = state.get(target, "rot")
        # If the target has spun more than e.g. ±0.8 rad from upright, call it
        # toppled.
        # This is an arbitrary threshold for demonstration.
        if abs(utils.wrap_angle(rot_z)) < 0.8:
            return True
        return False

    @classmethod
    def _StartBlock_holds(cls, state: State,
                          objects: Sequence[Object]) -> bool:
        domino, = objects
        return state.get(domino, "start_block") == 1.0

    @classmethod
    def _HandEmpty_holds(cls, state: State, objects: Sequence[Object]) -> bool:
        robot, = objects
        return state.get(robot, "fingers") > 0.02

    @classmethod
    def _Holding_holds(cls, state: State, objects: Sequence[Object]) -> bool:
        _, domino = objects
        return state.get(domino, "is_held") > 0.5

    # -------------------------------------------------------------------------
    # Task Generation

    def _generate_train_tasks(self) -> List[EnvironmentTask]:
        return self._make_tasks(num_tasks=CFG.num_train_tasks,
                                rng=self._train_rng)

    def _generate_test_tasks(self) -> List[EnvironmentTask]:
        return self._make_tasks(num_tasks=CFG.num_test_tasks,
                                rng=self._test_rng)

    def _make_tasks(self, num_tasks: int,
                    rng: np.random.Generator) -> List[EnvironmentTask]:
        tasks = []
        # Suppose we want to create M = 3 dominoes, N = 2 targets for each task

        for _ in range(num_tasks):
            # 1) Robot initial
            robot_dict = {
                "x": self.robot_init_x,
                "y": self.robot_init_y,
                "z": self.robot_init_z,
                "fingers": self.open_fingers,
                "tilt": self.robot_init_tilt,
                "wrist": self.robot_init_wrist,
            }

            # 2) Dominoes
            init_dict = {self._robot: robot_dict}
            # Place dominoes (D) and targets (T) in order: D D T D T
            # at fixed positions along the x-axis
            gap = self.domino_width * 1.3
            if CFG.domino_debug_layout:
                domino_dict = {}
                rot = np.pi / 2  # e.g. initial orientation
                x = self.start_domino_x
                y = self.start_domino_y
                domino_dict[self.dominos[0]] = {
                    "x": x,
                    "y": y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot,
                    "start_block": 1.0,
                    "is_held": 0.0,
                }
                x += gap
                domino_dict[self.dominos[1]] = {
                    "x": x,
                    "y": self.start_domino_y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }
                x += gap
                domino_dict[self.dominos[2]] = {
                    "x": x,
                    "y": self.start_domino_y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }
                x += gap
                domino_dict[self.targets[0]] = {
                    "x": x,
                    "y": self.start_domino_y,
                    "z": self.z_lb,
                    "rot": rot,
                }
                x += gap
                domino_dict[self.dominos[3]] = {
                    "x": x,
                    "y": self.start_domino_y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }
                x += gap
                domino_dict[self.dominos[4]] = {
                    "x": x,
                    "y": self.start_domino_y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }

                # 180-degree Turn with pivot
                x += gap / 3
                y = self.start_domino_y + self.pivot_width / 2
                domino_dict[self.pivots[0]] = {
                    "x": x,
                    "y": y,
                    "z": self.z_lb,
                    "rot": rot,
                }
                x -= gap / 3
                y += self.pivot_width / 2
                domino_dict[self.dominos[5]] = {
                    "x": x,
                    "y": y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }
                x -= gap
                domino_dict[self.dominos[6]] = {
                    "x": x,
                    "y": y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }
                # 90-degree Turn
                x -= gap * 1 / 4
                y += gap / 2
                domino_dict[self.dominos[7]] = {
                    "x": x,
                    "y": y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": rot - np.pi / 4,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }
                x -= gap / 3
                y += gap * 3 / 4
                domino_dict[self.dominos[8]] = {
                    "x": x,
                    "y": y,
                    "z": self.z_lb + self.domino_height / 2,
                    "rot": 0,
                    "start_block": 0.0,
                    "is_held": 0.0,
                }
                y += gap
                domino_dict[self.targets[1]] = {
                    "x": x,
                    "y": y,
                    "z": self.z_lb,
                    "rot": 0,
                }
            else:

                def _in_bounds(nx: float, ny: float) -> bool:
                    """Check if (nx, ny) is within table boundaries."""
                    return self.x_lb < nx < self.x_ub and \
                        self.y_lb < ny < self.y_ub

                n_dominos = rng.integers(low=5, high=len(self.dominos) + 1)
                n_dominos = len(self.dominos) - 1
                n_targets = rng.integers(low=1,
                                         high=min(3, len(self.targets)) + 1)
                # n_targets = 2
                # "n_pivots" means how many times we *attempt* a 180° pivot
                n_pivots = rng.integers(low=0,
                                        high=min(2, len(self.pivots)) + 1)
                # n_pivots = 1

                while True:
                    print("\nSample again:")
                    domino_dict = {}
                    domino_count = 0
                    target_count = 0
                    pivot_count = 0
                    just_placed_target = False
                    just_turned_90 = False
                    success = True  # Track whether we placed everything successfully

                    # Initial domino
                    x = rng.uniform(self.x_lb, self.x_ub)
                    y = rng.uniform(self.y_lb + self.domino_width ,
                                    self.y_ub - 3 * self.domino_width )
                                    # self.y_lb + self.domino_width * 2)
                    rot = rng.uniform(-np.pi/2, np.pi/2)
                    rot = rng.choice([0, np.pi/2, -np.pi/2])
                    gap = self.domino_width * 1.3

                    # Place first domino
                    domino_dict[
                        self.dominos[domino_count]] = self._place_domino(
                            domino_count, x, y, rot, start_block=1.0)
                    domino_count += 1

                    turn_choices = [
                        "straight",
                        "turn90",
                        "pivot180"
                    ]
                    if pivot_count == n_pivots:
                        turn_choices.remove("pivot180")

                    """
                    
                    """
                    # Try placing dominos/targets
                    while domino_count < n_dominos or target_count < n_targets:
                        can_place_target = (domino_count >= 2
                                            and target_count < n_targets
                                            and not just_placed_target)
                        must_place_domino = not can_place_target

                        if must_place_domino or rng.random() > 0.5:
                            # If just placed a target, enforce a "straight" choice
                            choices = turn_choices.copy()
                            if just_turned_90:
                                choices.remove("turn90")
                            if just_placed_target:
                                choices = ["straight"]
                            choice = rng.choice(choices)
                            print(f"Choice: {choice}")

                            if choice == "straight":
                                dy = gap * np.cos(rot)
                                dx = gap * np.sin(rot)
                                nx, ny = x + dx, y + dy
                                if not _in_bounds(nx, ny):
                                    success = False
                                    break
                                x, y = nx, ny
                                domino_dict[self.dominos[
                                    domino_count]] = self._place_domino(
                                        domino_count,
                                        x,
                                        y,
                                        rot,
                                        start_block=0.0)
                                domino_count += 1
                                just_turned_90 = False

                            elif choice == "turn90":
                                # Check we have enough dominos left
                                if domino_count + 1 >= n_dominos:
                                    # Fallback to straight
                                    dy = gap * np.cos(rot)
                                    dx = gap * np.sin(rot)
                                    nx, ny = x + dx, y + dy
                                    if not _in_bounds(nx, ny):
                                        success = False
                                        break
                                    x, y = nx, ny
                                    domino_dict[self.dominos[
                                        domino_count]] = self._place_domino(
                                            domino_count,
                                            x,
                                            y,
                                            rot,
                                            start_block=0.0)
                                    domino_count += 1
                                else:
                                    # Turn 45° twice
                                    turn_dir = rng.choice(
                                        [-1, 1])
                                    half_turn = np.pi / 4 * turn_dir

                                    # First 45°
                                    side_offset = 0 #(self.domino_width / 4)
                                    rot += half_turn
                                    dx = gap * np.sin(rot)
                                    dy = gap * np.cos(rot)

                                    dx -= turn_dir * side_offset * np.cos(rot)
                                    dy += turn_dir * side_offset * np.sin(rot)
                                    nx, ny = x + dx, y + dy
                                    if not _in_bounds(nx, ny):
                                        success = False
                                        break
                                    x, y = nx, ny
                                    domino_dict[self.dominos[
                                        domino_count]] = self._place_domino(
                                            domino_count,
                                            x,
                                            y,
                                            rot + np.pi / 2,
                                            start_block=0.0)
                                    domino_count += 1

                                    # Second 45°
                                    side_offset = (self.domino_width / 2)
                                    rot += half_turn
                                    dx = gap * np.sin(rot)
                                    dy = gap * np.cos(rot)

                                    dx -= turn_dir * side_offset * np.cos(rot)
                                    dy += turn_dir * side_offset * np.sin(rot)
                                    nx, ny = x + dx, y + dy
                                    if not _in_bounds(nx, ny):
                                        success = False
                                        break
                                    x, y = nx, ny
                                    domino_dict[self.dominos[
                                        domino_count]] = self._place_domino(
                                            domino_count,
                                            x,
                                            y,
                                            rot,
                                            start_block=0.0)
                                    domino_count += 1
                                just_turned_90 = True

                            elif choice == "pivot180" and pivot_count < n_pivots:

                                pivot_dir = rng.choice(
                                    [-1, 1])  # pick left or right offset
                                side_offset = (self.pivot_width / 2)

                                # Parallel movement along orientation rot:
                                #   (cos(rot), sin(rot)) is the unit vector in direction 'rot'
                                pivot_x = x + gap / 2 * np.sin(rot)  # 0.03
                                pivot_y = y + gap / 2 * np.cos(rot)

                                # Optional sideways shift:
                                pivot_x -= pivot_dir * side_offset * np.cos(rot)  # 0
                                pivot_y -= pivot_dir * side_offset * np.sin(rot)  # -0.1

                                if not _in_bounds(pivot_x, pivot_y):
                                    success = False
                                    break

                                domino_dict[self.pivots[
                                    pivot_count]] = self._place_pivot_or_target(
                                        pivot_x, pivot_y, rot)
                                pivot_count += 1

                                # Flip orientation
                                # big +y, small -x
                                back_x = pivot_x - (gap / 2) * np.sin(rot)
                                back_y = pivot_y - (gap / 2) * np.cos(rot)

                                # Optionally keep the same sideways offset so
                                # it's "same side"
                                back_x -= pivot_dir * side_offset * np.cos(rot)
                                back_y += pivot_dir * side_offset * -np.sin(rot)
                                if not _in_bounds(back_x, back_y):
                                    success = False
                                    break
                                x, y = back_x, back_y
                                rot += np.pi  # 180° flip

                                # Place next domino at this new position
                                domino_dict[self.dominos[
                                    domino_count]] = self._place_domino(
                                        domino_count,
                                        x,
                                        y,
                                        rot,
                                        start_block=0.0)
                                domino_count += 1
                                just_turned_90 = False
                            else:
                                # fallback
                                dy = gap * np.cos(rot)
                                dx = gap * np.sin(rot)
                                nx, ny = x + dx, y + dy
                                if not _in_bounds(nx, ny):
                                    success = False
                                    break
                                x, y = nx, ny
                                domino_dict[self.dominos[
                                    domino_count]] = self._place_domino(
                                        domino_count,
                                        x,
                                        y,
                                        rot,
                                        start_block=0.0)
                                domino_count += 1
                                just_turned_90 = False
                            just_placed_target = False

                        else:
                            print("Placing target")
                            # Place a target
                            dy = gap * np.cos(rot)
                            dx = gap * np.sin(rot)
                            nx, ny = x + dx, y + dy
                            if not _in_bounds(nx, ny):
                                success = False
                                break
                            x, y = nx, ny
                            domino_dict[self.targets[
                                target_count]] = self._place_pivot_or_target(
                                    x, y, rot)
                            target_count += 1
                            just_placed_target = True
                            just_turned_90 = False

                        if not success:
                            break

                    if success and domino_count == n_dominos and target_count == n_targets and pivot_count == n_pivots:
                        print("Found satisfying a task")
                        break
                    else:
                        # Retry
                        continue
            print(f"Found a task")
            init_dict.update(domino_dict)
            init_state = utils.create_state_from_dict(init_dict)

            # The goal: topple all targets
            # goal_atoms = {
            #     GroundAtom(self._Toppled, [t_obj]) for t_obj in self.targets
            # }
            # Simp
            goal_atoms = {GroundAtom(self._Toppled, [self.targets[0]])}

            tasks.append(EnvironmentTask(init_state, goal_atoms))

        return self._add_pybullet_state_to_tasks(tasks)

    # A small helper to set up dictionary entries:
    def _place_domino(self,
                      d_idx: int,
                      x: float,
                      y: float,
                      rot: float,
                      start_block: float = 0.0) -> Dict:
        return {
            "x": x,
            "y": y,
            "z": self.z_lb + self.domino_height / 2,
            "rot": rot,
            "start_block": start_block,
            "is_held": 0.0,
        }

    # Same for pivot or target (note pivot/target is on z_lb):
    def _place_pivot_or_target(self,
                               x: float,
                               y: float,
                               rot: float = 0.0) -> Dict:
        return {
            "x": x,
            "y": y,
            "z": self.z_lb,
            "rot": rot,
        }


if __name__ == "__main__":

    CFG.seed = 1
    CFG.env = "pybullet_domino"
    env = PyBulletDominoEnv(use_gui=True)
    tasks = env._make_tasks(10, env._train_rng)
    for task in tasks:
        env._reset_state(task.init)

        for i in range(100):
            action = Action(np.array(env._pybullet_robot.initial_joint_positions))
            env.step(action)
            time.sleep(0.01)
