"""
Example usage:
python predicators/main.py --approach oracle --env pybullet_domino --seed 1 \
--num_test_tasks 1 --use_gui --debug --num_train_tasks 0 \
--pybullet_camera_height 900 --pybullet_camera_width 900
"""

import logging
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pybullet as p

from predicators import utils
from predicators.envs.pybullet_env import PyBulletEnv, create_pybullet_block
from predicators.pybullet_helpers.geometry import Pose3D, Quaternion
from predicators.pybullet_helpers.objects import (create_object,
                                                  update_object)
from predicators.pybullet_helpers.robots import SingleArmPyBulletRobot
from predicators.settings import CFG
from predicators.structs import Action, EnvironmentTask, GroundAtom, \
    Object, Predicate, State, Type


class PyBulletDominoEnv(PyBulletEnv):
    """A simple PyBullet environment involving M dominoes and N targets.
    Each target is considered 'toppled' if it is significantly tilted from its
    upright orientation. The overall goal is to topple all targets.
    """

    # Bounds for placing dominoes and targets (you can adjust as needed).
    x_lb: ClassVar[float] = 0.4
    x_ub: ClassVar[float] = 1.1
    y_lb: ClassVar[float] = 1.1
    y_ub: ClassVar[float] = 1.6
    z_lb: ClassVar[float] = 0.2
    z_ub: ClassVar[float] = 0.75

    table_pos: ClassVar[Pose3D] = (0.75, 1.35, 0.0)
    table_orn: ClassVar[Quaternion] = p.getQuaternionFromEuler(
        [0., 0., np.pi/2])

    # Domino shape
    domino_width: ClassVar[float] = 0.07
    domino_depth: ClassVar[float] = 0.03
    domino_height: ClassVar[float] = 0.15
    domino_color: ClassVar[Tuple[float, float, float, float]] = (
        0.6, 0.8, 1.0, 1.0)
    
    target_height: ClassVar[float] = 0.2

    # For deciding if a target is toppled: if absolute tilt in x or y
    # is bigger than some threshold (e.g. 0.4 rad ~ 23 deg), treat as toppled.
    topple_angle_threshold: ClassVar[float] = 0.4

    # Camera defaults, optional
    _camera_distance: ClassVar[float] = 1.3
    _camera_yaw: ClassVar[float] = 70
    _camera_pitch: ClassVar[float] = -30
    _camera_target: ClassVar[Pose3D] = (0.75, 1.25, 0.42)

    # We won't manipulate the dominoes with the robot's gripper in this example,
    # but we still define a robot if we want to maintain a consistent interface.
    robot_init_x: ClassVar[float] = (x_lb + x_ub) * 0.5
    robot_init_y: ClassVar[float] = (y_lb + y_ub) * 0.5
    robot_init_z: ClassVar[float] = z_ub
    robot_base_pos: ClassVar[Pose3D] = (0.75, 0.72, 0.0)
    robot_base_orn: ClassVar[Quaternion] = p.getQuaternionFromEuler(
        [0.0, 0.0, np.pi / 2])
    robot_init_tilt: ClassVar[float] = np.pi / 2
    robot_init_wrist: ClassVar[float] = -np.pi / 2

    _robot_type = Type("robot", ["x", "y", "z", "fingers", "tilt", "wrist"])
    _domino_type = Type("domino", ["x", "y", "z", "rot"])
    _target_type = Type("target", ["x", "y", "z", "rot"])

    def __init__(self, use_gui: bool = True) -> None:
        # Define Types

        # Create 'dummy' Objects (they'll be assigned IDs on reset)
        self._robot = Object("robot", self._robot_type)
        # We'll hold references to all domino and target objects in lists
        # after we create them in tasks.

        super().__init__(use_gui)

        # Define Predicates
        self._Toppled = Predicate("Toppled", [self._target_type],
                                  self._Toppled_holds)

    @classmethod
    def get_name(cls) -> str:
        return "pybullet_domino"

    @property
    def predicates(self) -> Set[Predicate]:
        return {self._Toppled}

    @property
    def goal_predicates(self) -> Set[Predicate]:
        # The goal is always to topple all targets
        return {self._Toppled}

    @property
    def types(self) -> Set[Type]:
        return {self._robot_type, self._domino_type, self._target_type}

    # -------------------------------------------------------------------------
    # Environment Setup

    @classmethod
    def initialize_pybullet(
        cls, using_gui: bool
    ) -> Tuple[int, SingleArmPyBulletRobot, Dict[str, Any]]:
        # Reuse parent method to create a robot and get a physics client
        physics_client_id, pybullet_robot, bodies = super().initialize_pybullet(
            using_gui)

        # (Optional) Add a simple table
        table_id = create_object(
            asset_path="urdf/table.urdf",
            position=cls.table_pos,
            orientation=cls.table_orn,
            scale=1.0,
            use_fixed_base=True,
            physics_client_id=physics_client_id
        )
        bodies["table_id"] = table_id

        # We won't create the dominoes and targets until we generate tasks.
        # Just store them in bodies for now:
        bodies["domino_ids"] = []
        bodies["target_ids"] = []

        return physics_client_id, pybullet_robot, bodies

    def _store_pybullet_bodies(self, pybullet_bodies: Dict[str, Any]) -> None:
        # We don't have a single known ID for dominoes or targets, so we'll store
        # them all at runtime. For now, we just keep a reference to the dict.
        self._domino_ids: List[int] = pybullet_bodies["domino_ids"]
        self._target_ids: List[int] = pybullet_bodies["target_ids"]

    # -------------------------------------------------------------------------
    # State Management

    def _get_object_ids_for_held_check(self) -> List[int]:
        # In this domain, we don't pick anything up with the robot, so we
        # can return an empty list.
        return []

    def _get_state(self) -> State:
        """Construct a State from the current PyBullet simulation."""
        state_dict: Dict[Object, Dict[str, float]] = {}
        # 1) Robot
        rx, ry, rz, qx, qy, qz, qw, rf = self._pybullet_robot.get_state()
        _, tilt, wrist = p.getEulerFromQuaternion([qx, qy, qz, qw])
        state_dict[self._robot] = {
            "x": rx,
            "y": ry,
            "z": rz,
            "fingers": self._fingers_joint_to_state(self._pybullet_robot, rf),
            "tilt": tilt,
            "wrist": wrist
        }

        # 2) Dominoes
        for domino_obj in self._objects:
            if domino_obj.type == self._domino_type:
                (dx, dy, dz), orn = p.getBasePositionAndOrientation(
                    domino_obj.id, physicsClientId=self._physics_client_id)
                yaw = p.getEulerFromQuaternion(orn)[2]
                state_dict[domino_obj] = {
                    "x": dx,
                    "y": dy,
                    "z": dz,
                    "rot": utils.wrap_angle(yaw),
                }

        # 3) Targets
        for target_obj in self._objects:
            if target_obj.type == self._target_type:
                (tx, ty, tz), orn = p.getBasePositionAndOrientation(
                    target_obj.id, physicsClientId=self._physics_client_id)
                yaw = p.getEulerFromQuaternion(orn)[2]
                state_dict[target_obj] = {
                    "x": tx,
                    "y": ty,
                    "z": tz,
                    "rot": utils.wrap_angle(yaw),
                }

        # Convert to a State
        state = utils.create_state_from_dict(state_dict)
        joint_positions = self._pybullet_robot.get_joints()
        pyb_state = utils.PyBulletState(
            state.data, simulator_state={"joint_positions": joint_positions})
        return pyb_state

    def _reset_state(self, state: State) -> None:
        """Reset the PyBullet world to match the given state."""
        super()._reset_state(state)
        # We'll reconstruct the same set of self._objects that we used in
        # creating the tasks.

        # For each domino and target, update PyBullet position/orientation.
        for obj in self._objects:
            if obj.type not in [self._domino_type, self._target_type]:
                continue
            x = state.get(obj, "x")
            y = state.get(obj, "y")
            z = state.get(obj, "z")
            rot = state.get(obj, "rot")
            update_object(obj.id,
                          position=(x, y, z),
                          orientation=p.getQuaternionFromEuler([0.0, 0.0, rot]),
                          physics_client_id=self._physics_client_id)

        # Check reconstruction
        reconstructed_state = self._get_state()
        if not reconstructed_state.allclose(state):
            logging.warning("Could not reconstruct state exactly!")

    def step(self, action: Action, render_obs: bool = False) -> State:
        """In this domain, stepping might be trivial (we won't do anything
        special aside from the usual robot step)."""
        next_state = super().step(action, render_obs=render_obs)

        # In an advanced version, you might check collisions or toppling here
        # and update states accordingly. For simplicity, we let PyBullet
        # automatically handle physics updates, so the orientation is captured
        # by reading from PyBullet each time _get_state() is called.

        final_state = self._get_state()
        self._current_observation = final_state
        return final_state

    # -------------------------------------------------------------------------
    # Predicates

    @classmethod
    def _Toppled_holds(cls, state: State, objects: Sequence[Object]) -> bool:
        """Target is toppled if it’s significantly tilted from upright
        in pitch or roll. We measure this from the actual PyBullet orientation
        (read from .step()), but for demonstration we can say that if the object
        was set (or has become) rotated in x/y beyond a threshold, it’s toppled.

        Here, we only have `rot` about z in the state, but in a real
        implementation, you might store or compute orientation around x,y, etc.
        We'll demonstrate a simpler check for a large rotation from upright.
        """
        # If we had orientation around x or y in the state, we'd read that here.
        # Suppose we treat a large difference in z-rotation from the "initial"
        # upright as toppled (just a simplified approach).
        # In a real scenario, you'd measure pitch/roll or object bounding box.
        target, = objects
        rot_z = state.get(target, "rot")
        # If the target has spun more than e.g. ±0.8 rad from upright, call it toppled.
        # This is an arbitrary threshold for demonstration.
        if abs(utils.wrap_angle(rot_z)) > 0.8:
            return True
        return False

    # -------------------------------------------------------------------------
    # Task Generation

    def _generate_train_tasks(self) -> List[EnvironmentTask]:
        return self._make_tasks(num_tasks=CFG.num_train_tasks, rng=self._train_rng)

    def _generate_test_tasks(self) -> List[EnvironmentTask]:
        return self._make_tasks(num_tasks=CFG.num_test_tasks, rng=self._test_rng)

    def _make_tasks(self, num_tasks: int,
                    rng: np.random.Generator) -> List[EnvironmentTask]:
        tasks = []
        # Suppose we want to create M = 3 dominoes, N = 2 targets for each task
        # (configurable).
        M = 3
        N = 2

        for _ in range(num_tasks):
            # Create new objects for this task
            domino_objs: List[Object] = []
            target_objs: List[Object] = []

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
            for i in range(M):
                # Randomly place each domino
                name = f"domino_{i}"
                obj = Object(name, self._domino_type)
                # We create the block in PyBullet
                # Random yaw
                yaw = rng.uniform(-np.pi, np.pi)
                x = rng.uniform(self.x_lb, self.x_ub)
                y = rng.uniform(self.y_lb, self.y_ub)
                # Create the block in the simulator
                dom_id = create_pybullet_block(
                    color=self.domino_color,
                    half_extents=(self.domino_width / 2,
                                  self.domino_depth / 2,
                                  self.domino_height / 2),
                    mass=0.5,
                    friction=0.5,
                    orientation=[0.0, 0.0, yaw],
                    physics_client_id=self._physics_client_id,
                )
                update_object(dom_id, 
                        position=(x, y, self.z_lb + self.domino_height/2),
                        orientation=p.getQuaternionFromEuler([0.0, 0.0, yaw]),
                        physics_client_id=self._physics_client_id)
                obj.id = dom_id
                domino_objs.append(obj)

            # 3) Targets
            for j in range(N):
                name = f"target_{j}"
                obj = Object(name, self._target_type)
                # Random yaw
                yaw = rng.uniform(-np.pi, np.pi)
                x = rng.uniform(self.x_lb, self.x_ub)
                y = rng.uniform(self.y_lb, self.y_ub)
                # Create target from URDF
                t_id = create_object("urdf/domino_target.urdf",
                                     position=(x, y, self.z_lb + 0.05),
                                     orientation=p.getQuaternionFromEuler(
                                         [0.0, 0.0, yaw]),
                                     scale=1.0,
                                     physics_client_id=self._physics_client_id)
                update_object(t_id,
                        position=(x, y, self.z_lb + self.target_height/2),
                        orientation=p.getQuaternionFromEuler([0.0, 0.0, yaw]),
                        physics_client_id=self._physics_client_id)
                obj.id = t_id
                target_objs.append(obj)

            # Combine into self._objects for the environment
            # (Include the robot, though it’s not used for manipulation here.)
            self._objects = [self._robot] + domino_objs + target_objs

            # Build the initial State dictionary
            init_dict = {
                self._robot: robot_dict,
            }
            for d_obj in domino_objs:
                d_x, d_y, d_z = p.getBasePositionAndOrientation(
                    d_obj.id, physicsClientId=self._physics_client_id
                )[0]
                yaw = rng.uniform(-np.pi, np.pi)  # or read from PyBullet if desired
                init_dict[d_obj] = {
                    "x": d_x,
                    "y": d_y,
                    "z": d_z,
                    "rot": yaw,
                }

            for t_obj in target_objs:
                t_x, t_y, t_z = p.getBasePositionAndOrientation(
                    t_obj.id, physicsClientId=self._physics_client_id
                )[0]
                yaw = rng.uniform(-np.pi, np.pi)
                init_dict[t_obj] = {
                    "x": t_x,
                    "y": t_y,
                    "z": t_z,
                    "rot": yaw,
                }

            init_state = utils.create_state_from_dict(init_dict)

            # The goal: topple all targets
            goal_atoms = {
                GroundAtom(self._Toppled, [t_obj]) for t_obj in target_objs
            }

            tasks.append(EnvironmentTask(init_state, goal_atoms))

        return self._add_pybullet_state_to_tasks(tasks)


if __name__ == "__main__":
    import time

    CFG.seed = 0
    CFG.pybullet_sim_steps_per_action = 1
    env = PyBulletDominoEnv(use_gui=True)
    task = env._make_tasks(1, np.random.default_rng(0))[0]
    env._reset_state(task.init)

    while True:
        action = Action(np.array(env._pybullet_robot.initial_joint_positions))
        env.step(action)
        time.sleep(0.01)