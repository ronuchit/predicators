"""
The goal is to turn the lightbulb on.
In the simplest case, the lightbulb is automatically turned on when the light
is connected to both the positive and negative terminals of the battery.
The lightbulb and the battery are fixed, the wire is moveable.
"""

import logging
from typing import Any, ClassVar, Dict, List, Sequence, Set, Tuple

import numpy as np
import pybullet as p

from predicators import utils
from predicators.envs.pybullet_env import PyBulletEnv
from predicators.pybullet_helpers.geometry import Pose3D, Quaternion
from predicators.pybullet_helpers.objects import (create_object, update_object,
                                                    create_connector_with_bulb)
from predicators.pybullet_helpers.robots import SingleArmPyBulletRobot
from predicators.settings import CFG
from predicators.structs import Action, EnvironmentTask, GroundAtom, \
    Object, Predicate, State, Type


class PyBulletCircuitEnv(PyBulletEnv):
    """A PyBullet environment involving a battery, a light bulb socket,
    and two wire connectors. When the battery is connected to the socket
    via connectors (the 'Connected' predicate is true), the bulb color
    changes to yellow, indicating it's 'on'.
    """

    # Workspace / table bounds (adjust as you wish).
    x_lb: ClassVar[float] = 0.4
    x_ub: ClassVar[float] = 1.1
    y_lb: ClassVar[float] = 1.1
    y_ub: ClassVar[float] = 1.6
    z_lb: ClassVar[float] = 0.2
    z_ub: ClassVar[float] = 0.75
    init_padding = 0.05

    # Table config
    table_pos: ClassVar[Pose3D] = (0.75, 1.35, 0.0)
    table_orn: ClassVar[Quaternion] = p.getQuaternionFromEuler([0., 0., 
                                                                np.pi / 2])

    # Robot config
    robot_init_x: ClassVar[float] = (x_lb + x_ub) * 0.5
    robot_init_y: ClassVar[float] = (y_lb + y_ub) * 0.5
    robot_init_z: ClassVar[float] = z_ub - 0.1
    robot_base_pos: ClassVar[Pose3D] = (0.75, 0.72, 0.0)
    robot_base_orn: ClassVar[Quaternion] = p.getQuaternionFromEuler(
        [0.0, 0.0, np.pi / 2])
    robot_init_tilt: ClassVar[float] = np.pi / 2
    robot_init_wrist: ClassVar[float] = -np.pi / 2

    # Hard-coded finger states for open/close
    open_fingers: ClassVar[float] = 0.4
    closed_fingers: ClassVar[float] = 0.1

    # Some helpful color specs
    _bulb_on_color: ClassVar[Tuple[float, float, float, float]] = (1.0, 1.0, 0.0, 1.0)  # yellow
    _bulb_off_color: ClassVar[Tuple[float, float, float, float]] = (1.0, 1.0, 1.0, 1.0)  # white

    connector_width: ClassVar[float] = 0.05
    connector_height: ClassVar[float] = 0.05
    connector4_length: ClassVar[float] = 0.4
    battery_radius: ClassVar[float] = 0.05
    bulb_height: ClassVar[float] = 0.12
    bulb_radius: ClassVar[float] = 0.05
    box_wall_thickness: ClassVar[float] = 0.01
    # Camera parameters
    _camera_distance: ClassVar[float] = 1.3
    _camera_yaw: ClassVar[float] = 70
    _camera_pitch: ClassVar[float] = -38
    _camera_target: ClassVar[Pose3D] = (0.75, 1.25, 0.42)

    def __init__(self, use_gui: bool = True) -> None:
        # 1) Define Types
        # You can store as many features as you want. For brevity, we store
        # just x, y, z, color, and an is_on for the bulb.
        self._robot_type = Type("robot", ["x", "y", "z", "fingers", "tilt", "wrist"])
        self._wire_type = Type("wire", ["x", "y", "z", "rot"])
        self._battery_type = Type("battery", ["x", "y", "z"])
        # Light has an 'on' feature to track whether it’s lit, plus color as needed.
        self._light_type = Type("light_socket", ["x", "y", "z", "is_on"])

        # 2) Create placeholder objects. We'll fill them in tasks.
        self._robot = Object("robot", self._robot_type)
        self._wire1 = Object("wire1", self._wire_type)
        self._wire2 = Object("wire2", self._wire_type)
        self._battery = Object("battery", self._battery_type)
        self._light = Object("light", self._light_type)

        super().__init__(use_gui)

        # 3) Define Predicates
        # We'll define a placeholder Connected predicate, and a LightOn predicate.
        # The "connected" logic can be domain-specific; here we just
        # say we don't implement it. If it returns True, we set color to yellow.
        self._Connected = Predicate("Connected", 
                                    [self._light_type, self._battery_type],
                                    self._Connected_holds)
        self._LightOn = Predicate("LightOn", [self._light_type],
                                  self._LightOn_holds)

    @classmethod
    def get_name(cls) -> str:
        return "pybullet_circuit"

    @property
    def predicates(self) -> Set[Predicate]:
        return {
            self._Connected,
            self._LightOn,
        }

    @property
    def goal_predicates(self) -> Set[Predicate]:
        # In many tasks, you might want the goal to be that the light is on.
        return {self._LightOn}

    @property
    def types(self) -> Set[Type]:
        return {
            self._robot_type,
            self._wire_type,
            self._battery_type,
            self._light_type,
        }

    # -------------------------------------------------------------------------
    # PyBullet Initialization
    @classmethod
    def initialize_pybullet(
        cls, using_gui: bool
    ) -> Tuple[int, SingleArmPyBulletRobot, Dict[str, Any]]:
        physics_client_id, pybullet_robot, bodies = super().initialize_pybullet(using_gui)

        # Add table
        table_id = create_object(
            asset_path="urdf/table.urdf",
            position=cls.table_pos,
            orientation=cls.table_orn,
            scale=1.0,
            use_fixed_base=True,
            physics_client_id=physics_client_id,
        )
        bodies["table_id"] = table_id

        # Create the battery
        # battery_id = create_connector_with_bulb(scale=1)
        battery_id = create_object(
            asset_path="urdf/battery_box_snap.urdf",
            physics_client_id=physics_client_id,
            scale=1
        )
        bodies["battery_id"] = battery_id

        # Create the light socket (with a bulb)
        light_id = create_object(
            asset_path="urdf/bulb_box_snap.urdf",
            physics_client_id=physics_client_id,
            scale=1
        )
        bodies["light_id"] = light_id

        # Create two wire connectors
        wire_ids = []
        for _ in range(2):
            wire_id = create_object(
                asset_path="urdf/snap_connector4.urdf",
                physics_client_id=physics_client_id,
                scale=1
            )
            wire_ids.append(wire_id)
        bodies["wire_ids"] = wire_ids

        return physics_client_id, pybullet_robot, bodies

    def _store_pybullet_bodies(self, pybullet_bodies: Dict[str, Any]) -> None:
        """Store references to PyBullet IDs for environment assets."""
        self._battery.id = pybullet_bodies["battery_id"]
        self._light.id = pybullet_bodies["light_id"]
        self._wire1.id = pybullet_bodies["wire_ids"][0]
        self._wire2.id = pybullet_bodies["wire_ids"][1]

    # -------------------------------------------------------------------------
    # State Management
    def _get_object_ids_for_held_check(self) -> List[int]:
        """Return IDs of wires (assuming the robot can pick them up)."""
        return [self._wire1.id, self._wire2.id]

    def _get_state(self) -> State:
        """Construct a State from the current PyBullet simulation."""
        state_dict: Dict[Object, Dict[str, float]] = {}

        # Robot
        rx, ry, rz, qx, qy, qz, qw, rf = self._pybullet_robot.get_state()
        _, tilt, wrist = p.getEulerFromQuaternion([qx, qy, qz, qw])
        state_dict[self._robot] = {
            "x": rx,
            "y": ry,
            "z": rz,
            "fingers": self._fingers_joint_to_state(self._pybullet_robot, rf),
            "tilt": tilt,
            "wrist": wrist,
        }

        # Battery
        (bx, by, bz), _ = p.getBasePositionAndOrientation(
            self._battery.id, physicsClientId=self._physics_client_id
        )
        state_dict[self._battery] = {"x": bx, "y": by, "z": bz}

        # Light (with bulb)
        (lx, ly, lz), _ = p.getBasePositionAndOrientation(
            self._light.id, physicsClientId=self._physics_client_id
        )
        # We store an is_on feature: 1.0 means on, 0.0 means off
        # If you want to get fancy, you could check the PyBullet color shape, etc.
        # but let's assume we keep track of it in the environment logic.
        # We'll store it in self._light_on if needed. For now, store 0.0 as default.
        # We'll update it if we detect it in step().
        # This is an example; you'll need your own approach to color changes.
        # For brevity, we skip reading color back from PyBullet. 
        state_dict[self._light] = {
            "x": lx,
            "y": ly,
            "z": lz,
            "is_on": 0.0,
        }

        # Wires
        for wire_obj in [
            self._wire1, self._wire2]:
            (wx, wy, wz), orn = p.getBasePositionAndOrientation(
                wire_obj.id, physicsClientId=self._physics_client_id
            )
            state_dict[wire_obj] = {
                "x": wx,
                "y": wy,
                "z": wz,
                "rot": p.getEulerFromQuaternion(orn)[2],
            }

        # Convert dictionary to a PyBulletState
        state = utils.create_state_from_dict(state_dict)
        joint_positions = self._pybullet_robot.get_joints()
        pyb_state = utils.PyBulletState(
            state.data, simulator_state={"joint_positions": joint_positions}
        )
        return pyb_state

    def _reset_state(self, state: State) -> None:
        """Reset from a given state."""
        super()._reset_state(state)  # Clears constraints, resets robot
        self._objects = [self._robot, self._wire1, self._wire2, self._battery, self._light]

        # Update battery
        bx = state.get(self._battery, "x")
        by = state.get(self._battery, "y")
        bz = state.get(self._battery, "z")
        update_object(
            self._battery.id,
            position=(bx, by, bz),
            orientation=p.getQuaternionFromEuler([0, 0, np.pi / 2]),
            physics_client_id=self._physics_client_id
        )

        # Update light socket
        lx = state.get(self._light, "x")
        ly = state.get(self._light, "y")
        lz = state.get(self._light, "z")
        update_object(
            self._light.id,
            position=(lx, ly, lz),
            orientation=p.getQuaternionFromEuler([0, 0, -np.pi / 2]),
            physics_client_id=self._physics_client_id
        )
        # Optionally set color here if you want to reflect the on/off state visually.

        # Update wires
        for wire_obj in [
                         self._wire1, self._wire2]:
            wx = state.get(wire_obj, "x")
            wy = state.get(wire_obj, "y")
            wz = state.get(wire_obj, "z")
            rot = state.get(wire_obj, "rot")
            update_object(
                wire_obj.id,
                position=(wx, wy, wz),
                orientation=p.getQuaternionFromEuler([0, 0, rot]),
                physics_client_id=self._physics_client_id
            )

        # Check if re-creation matches
        reconstructed_state = self._get_state()
        if not reconstructed_state.allclose(state):
            logging.warning("Could not reconstruct state exactly!")

    def step(self, action: Action, render_obs: bool = False) -> State:
        """Process a single action step. If the battery is connected
        to the light, turn the bulb on."""
        next_state = super().step(action, render_obs=render_obs)

        # Check if the Connected predicate is satisfied => turn the light on
        # (Here we don't implement the actual connection logic. We just do
        # a placeholder check for demonstration.)
        if self._Connected_holds(next_state, [self._light, self._battery]):
            self._turn_bulb_on()
        else:
            self._turn_bulb_off()

        final_state = self._get_state()
        self._current_observation = final_state
        return final_state

    # -------------------------------------------------------------------------
    # Predicates
    @staticmethod
    def _Connected_holds(state: State, objects: Sequence[Object]) -> bool:
        """Placeholder. True if the light is connected to the battery
        (via wires). Actual logic not shown."""
        # Could do a real geometric check, or a symbolic approach.
        # For now, just return False unconditionally.
        return False

    @staticmethod
    def _LightOn_holds(state: State, objects: Sequence[Object]) -> bool:
        (light,) = objects
        return state.get(light, "is_on") > 0.5

    # -------------------------------------------------------------------------
    # Turning the bulb on/off visually
    def _turn_bulb_on(self) -> None:
        if self._light.id is not None:
            p.changeVisualShape(
                self._light.id,
                -1,  # all link indices
                rgbaColor=self._bulb_on_color,
                physicsClientId=self._physics_client_id
            )

    def _turn_bulb_off(self) -> None:
        if self._light.id is not None:
            p.changeVisualShape(
                self._light.id,
                -1,  # all link indices
                rgbaColor=self._bulb_off_color,
                physicsClientId=self._physics_client_id
            )

    # -------------------------------------------------------------------------
    # Task Generation
    def _generate_train_tasks(self) -> List[EnvironmentTask]:
        return self._make_tasks(num_tasks=CFG.num_train_tasks, rng=self._train_rng)

    def _generate_test_tasks(self) -> List[EnvironmentTask]:
        return self._make_tasks(num_tasks=CFG.num_test_tasks, rng=self._test_rng)

    def _make_tasks(
        self, num_tasks: int, rng: np.random.Generator
    ) -> List[EnvironmentTask]:
        tasks = []
        for _ in range(num_tasks):
            # Robot at center
            robot_dict = {
                "x": self.robot_init_x,
                "y": self.robot_init_y,
                "z": self.robot_init_z,
                "fingers": self.open_fingers,
                "tilt": self.robot_init_tilt,
                "wrist": self.robot_init_wrist,
            }

            # Battery near the lower region
            battery_x = self.x_lb + 2 * self.init_padding
            battery_dict = {
                "x": battery_x,
                "y": 1.35,
                "z": self.z_lb + self.connector_height / 2,
            }


            # Wires on the sides short wires
            wire1_dict = {
                "x": 0.75,
                "y": 1.15 ,  # lower region
                "z": self.z_lb + self.connector_height / 2,
                "rot": 0,
            }
            wire2_dict = {
                "x": 0.75,
                "y": 1.55,  # upper region
                "z": self.z_lb + self.connector_height / 2,
                "rot": 0,
            }

            # Light near upper region
            bulb_x = battery_x + self.connector4_length - self.connector_width
            light_dict = {
                "x": bulb_x,
                "y": 1.35,
                "z": self.z_lb + self.connector_height / 2,
                "is_on": 0.0,
            }

            init_dict = {
                self._robot: robot_dict,
                self._battery: battery_dict,
                self._light: light_dict,
                self._wire1: wire1_dict,
                self._wire2: wire2_dict,
            }
            init_state = utils.create_state_from_dict(init_dict)

            # The goal can be that the light is on.
            goal_atoms = {
                GroundAtom(self._LightOn, [self._light]),
            }
            tasks.append(EnvironmentTask(init_state, goal_atoms))

        return self._add_pybullet_state_to_tasks(tasks)