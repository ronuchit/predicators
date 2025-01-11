"""A PyBullet version of Blocks."""

import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Set, Tuple, \
    Union

import numpy as np
import pybullet as p
from PIL import Image

from predicators import utils
from predicators.envs.balance import BalanceEnv
from predicators.envs.pybullet_env import PyBulletEnv, create_pybullet_block
from predicators.pybullet_helpers.geometry import Pose, Pose3D, Quaternion
from predicators.pybullet_helpers.robots import SingleArmPyBulletRobot
from predicators.settings import CFG
from predicators.structs import Action, Array, EnvironmentTask, NSPredicate, \
    Object, Predicate, State, Type
from predicators.utils import RawState, VLMQuery


class PyBulletBalanceEnv(PyBulletEnv, BalanceEnv):
    """PyBullet Balance domain."""
    # Parameters that aren't important enough to need to clog up settings.py

    # Table parameters.
    table_height: ClassVar[float] = 0.4
    _table2_pose: ClassVar[Pose3D] = (1.35, 0.75, table_height/2)
    _table_orientation: ClassVar[Quaternion] = (0., 0., 0., 1.)
    _camera_target: ClassVar[Pose3D] = (1.65, 0.75, 0.52)
    
    _obj_mass: ClassVar[float] = 0.05

    def __init__(self, use_gui: bool = True) -> None:
        super().__init__(use_gui)

        # Types
        bbox_features = ["bbox_left", "bbox_right", "bbox_upper", "bbox_lower"]
        self._block_type = Type("block", [
            "pose_x", "pose_y", "pose_z", "held", "color_r", "color_g",
            "color_b"
        ] + (bbox_features if CFG.env_include_bbox_features else []))
        self._robot_type = Type(
            "robot", ["pose_x", "pose_y", "pose_z", "fingers"] +
            (bbox_features if CFG.env_include_bbox_features else []))
        self._plate_type = Type(
            "plate", (bbox_features if CFG.env_include_bbox_features else []))

        # Predicates
        self._DirectlyOn_NSP = NSPredicate(
            "DirectlyOn", [self._block_type, self._block_type],
            self._DirectlyOn_NSP_holds)
        self._DirectlyOnPlate_NSP = NSPredicate(
            "DirectlyOnPlate", [self._block_type],
            self._DirectlyOnPlate_NSP_holds)
        self._Holding_NSP = NSPredicate("Holding", [self._block_type],
                                        self._Holding_NSP_holds)
        self._GripperOpen_NSP = NSPredicate("GripperOpen", [self._robot_type],
                                            self._GripperOpen_NSP_holds)
        self._Clear_NSP = NSPredicate("Clear", [self._block_type],
                                      self._Clear_NSP_holds)

        # We track the correspondence between PyBullet object IDs and Object
        # instances for blocks. This correspondence changes with the task.
        self._block_id_to_block: Dict[int, Object] = {}

        self.ns_to_sym_predicates: Dict[Tuple[str], Predicate] = {
            ("GripperOpen"): self._GripperOpen,
            ("Holding"): self._Holding,
            ("Clear"): self._Clear,
        }

    @property
    def ns_predicates(self) -> Set[NSPredicate]:
        return {
            # self._DirectlyOn_NSP,
            # self._DirectlyOnPlate_NSP,
            # self._GripperOpen_NSP,
            # self._Holding_NSP,
            # self._Clear_NSP,
            self._Balanced,
        }

    def _Clear_NSP_holds(self, state: RawState, objects: Sequence[Object]) -> \
            Union[bool, VLMQuery]:
        """Is there no block on top of the block."""
        block, = objects
        for other_block in state:
            if other_block.type != self._block_type:
                continue
            if self._DirectlyOn_holds(state, [other_block, block]):
                return False
        return True

    def _Holding_NSP_holds(self, state: RawState, objects: Sequence[Object]) ->\
            bool:
        """Is the robot holding the block."""
        block, = objects

        # The block can't be held if the robot's hand is open.
        # We know there is only one robot in this environment.
        robot = state.get_objects(self._robot_type)[0]
        if self._GripperOpen_NSP_holds(state, [robot]):
            return False

        # Using simple heuristics to check if they have overlap
        block_bbox = state.get_obj_bbox(block)
        robot_bbox = state.get_obj_bbox(robot)
        if block_bbox.right < robot_bbox.left or \
            block_bbox.left > robot_bbox.right or\
            block_bbox.upper < robot_bbox.lower or\
            block_bbox.lower > robot_bbox.upper:
            return False

        block_name = block.id_name
        attention_image = state.crop_to_objects([block, robot])
        return state.evaluate_simple_assertion(
            f"{block_name} is held by the robot", attention_image)

    def _GripperOpen_NSP_holds(self, state: RawState, objects: Sequence[Object]) ->\
            bool:
        """Is the robots gripper open."""
        robot, = objects
        finger_state = state.get(robot, "fingers")
        assert finger_state in (0.0, 1.0)
        return finger_state == 1.0


    def _DirectlyOnPlate_NSP_holds(state: RawState, objects:Sequence[Object]) ->\
            bool:
        """Determine if the block in objects is directly resting on the table's
        surface in the scene image."""
        block, = objects
        block_name = block.id_name

        # We know there is only one table in this environment.
        plate = state.get_objects(self._plate_type)[0]
        plate_name = plate.id_name
        # Crop the image to the smallest bounding box that include both objects.
        attention_image = state.crop_to_objects([block, plate])

        return state.evaluate_simple_assertion(
            f"{block_name} is directly resting on {plate_name}'s surface.",
            attention_image)

    def _DirectlyOn_NSP_holds(state: RawState,
                              objects: Sequence[Object]) -> bool:
        """Determine if the first block in objects is directly on top of the
        second block with no blocks in between in the scene image, by using a
        combination of rules and VLMs."""

        block1, block2 = objects
        block1_name, block2_name = block1.id_name, block2.id_name

        # We know a block can't be on top of itself.
        if block1_name == block2_name:
            return False

        # Situations where we're certain that block1 won't be above block2
        if state.get(block1, "bbox_lower") < state.get(block2, "bbox_lower") or\
           state.get(block1, "bbox_left") > state.get(block2, "bbox_right") or\
           state.get(block1, "bbox_right") < state.get(block2, "bbox_left") or\
           state.get(block1, "bbox_upper") < state.get(block2, "bbox_upper") or\
           state.get(block1, "pose_z") < state.get(block2, "pose_z"):
            return False

        # Use a VLM query to handle to reminder cases
        # Crop the scene image to the smallest bounding box that include both
        # objects.
        attention_image = state.crop_to_objects([block1, block2])
        return state.evaluate_simple_assertion(
            f"{block1_name} is directly on top of {block2_name} with no " +
            "blocks in between.", attention_image)

    @classmethod
    def initialize_pybullet(
            cls, using_gui: bool
    ) -> Tuple[int, SingleArmPyBulletRobot, Dict[str, Any]]:
        """Run super(), then handle blocks-specific initialization."""
        physics_client_id, pybullet_robot, bodies = super(
        ).initialize_pybullet(using_gui)

        table2_id = create_pybullet_block(
            (.9, .9, .9, 1),
            cls._table_mid_half_extents,
            0.0,  # mass
            1.0,  # friction
            cls._table2_pose,
            cls._table_orientation,
            physics_client_id,
        )

        plate3_id = create_pybullet_block(
            (.9, .9, .9, 1),
            cls._plate_half_extents,
            0.0,
            1.0,
            cls._plate3_pose,
            cls._table_orientation,
            physics_client_id,
        )

        plate1_id = create_pybullet_block(
            (.9, .9, .9, 1),
            cls._plate_half_extents,
            0.0,
            1.0,
            cls._plate1_pose,
            cls._table_orientation,
            physics_client_id,
        )
        bodies["table_ids"] = [plate1_id, plate3_id]

        beam1_id = create_pybullet_block(
            (0.9, 0.9, 0.9, 1),
            cls._beam_half_extents,
            0.0,
            1.0,
            cls._beam1_pose,
            cls._table_orientation,
            physics_client_id,
        )
        beam2_id = create_pybullet_block(
            (0.9, 0.9, 0.9, 1),
            cls._beam_half_extents,
            0.0,
            1.0,
            cls._beam2_pose,
            cls._table_orientation,
            physics_client_id,
        )
        bodies["beam_ids"] = [beam1_id, beam2_id]

        button_id = create_pybullet_block(
            cls._button_color_off,
            [cls._button_radius]*3,
            0.0,
            1.0,
            (cls.button_x, cls.button_y, cls.button_z),
            cls._table_orientation,
            physics_client_id,
        )
        bodies["button_id"] = button_id

        # Create blocks. Note that we create the maximum number once, and then
        # later on, in reset_state(), we will remove blocks from the workspace
        # (teleporting them far away) based on which ones are in the state.
        num_blocks = max(max(CFG.blocks_num_blocks_train),
                         max(CFG.blocks_num_blocks_test))
        block_ids = []
        block_size = CFG.blocks_block_size
        for i in range(num_blocks):
            color = cls._obj_colors[i % len(cls._obj_colors)]
            half_extents = (block_size / 2.0, block_size / 2.0,
                            block_size / 2.0)
            block_ids.append(
                create_pybullet_block(color, half_extents, cls._obj_mass,
                                      cls._obj_friction, 
                                      physics_client_id=physics_client_id))
        bodies["block_ids"] = block_ids

        return physics_client_id, pybullet_robot, bodies

    def _store_pybullet_bodies(self, pybullet_bodies: Dict[str, Any]) -> None:
        self._plate1.id = pybullet_bodies["table_ids"][0]
        self._plate3.id = pybullet_bodies["table_ids"][1]
        self._machine.id = pybullet_bodies["button_id"]
        self._robot.id = self._pybullet_robot.robot_id
        self._block_ids = pybullet_bodies["block_ids"]
        self._beam_ids = pybullet_bodies["beam_ids"]

    @classmethod
    def get_name(cls) -> str:
        return "pybullet_balance"

    def _reset_state(self, state: State) -> None:
        """Run super(), then handle blocks-specific resetting."""
        super()._reset_state(state)

        # Reset blocks based on the state.
        block_objs = state.get_objects(self._block_type)
        self._block_id_to_block = {}
        self._objects = [
            self._robot, self._plate1, self._plate3, self._machine
        ]
        for i, block_obj in enumerate(block_objs):
            block_id = self._block_ids[i]
            self._block_id_to_block[block_id] = block_obj
            block_obj.id = block_id
            self._objects.append(block_obj)
            bx = state.get(block_obj, "pose_x")
            by = state.get(block_obj, "pose_y")
            bz = state.get(block_obj, "pose_z")
            p.resetBasePositionAndOrientation(
                block_id, [bx, by, bz],
                self._default_orn,
                physicsClientId=self._physics_client_id)
            # Update the block color. RGB values are between 0 and 1.
            r = state.get(block_obj, "color_r")
            g = state.get(block_obj, "color_g")
            b = state.get(block_obj, "color_b")
            color = (r, g, b, 1.0)  # alpha = 1.0
            p.changeVisualShape(block_id,
                                linkIndex=-1,
                                rgbaColor=color,
                                physicsClientId=self._physics_client_id)

        # Check if we're holding some block.
        held_block = self._get_held_block(state)
        if held_block is not None:
            self._force_grasp_object(held_block)

        # For any blocks not involved, put them out of view.
        h = self._block_size
        oov_x, oov_y = self._out_of_view_xy
        for i in range(len(block_objs), len(self._block_ids)):
            block_id = self._block_ids[i]
            assert block_id not in self._block_id_to_block
            p.resetBasePositionAndOrientation(
                block_id, [oov_x, oov_y, i * h],
                self._default_orn,
                physicsClientId=self._physics_client_id)

        # Update the button color
        if self._MachineOn_holds(state, [self._machine, self._robot]):
            button_color = self._button_color_on
        else:
            button_color = self._button_color_off
        p.changeVisualShape(self._machine.id,
                            -1,
                            rgbaColor=button_color,
                            physicsClientId=self._physics_client_id)

        # Reset the difference to zero on environment reset
        self._prev_diff = 0
        # Also do one beam update to make sure the initial positions match
        self._update_balance_beam(state)

        # Assert that the state was properly reconstructed.
        reconstructed_state = self._get_state()
        if not reconstructed_state.allclose(state):
            logging.debug("Desired state:")
            logging.debug(state.pretty_str())
            logging.debug("Reconstructed state:")
            logging.debug(reconstructed_state.pretty_str())
            raise ValueError("Could not reconstruct state.")

    def _update_balance_beam(self, state: State) -> None:
        """Shift the plates/beams' z-values to simulate a balance, only if counts have changed."""
        # Count how many blocks are on each plate.
        # You can define “on left plate” or “on right plate” in multiple ways:
        #   (1) If block x < some threshold => on left; else => on right
        #   (2) If block is physically over plate bounding box
        #   (3) If block is within some radius of the plate
        # For simplicity, below we’ll just check x position relative to the midpoint.

        # Example: Suppose x=1.1 is the approximate midpoint between plate1 & plate3
        left_count = 0
        right_count = 0
        midpoint_x = 1.1

        block_objs = state.get_objects(self._block_type)
        for block_obj in block_objs:
            # If block is "out of view," skip it
            bz = state.get(block_obj, "pose_z")
            if bz < 0:
                continue
            bx = state.get(block_obj, "pose_x")
            if bx < midpoint_x:
                left_count += 1
            else:
                right_count += 1

        diff = left_count - right_count
        if diff == self._prev_diff:
            return  # No change in distribution, no need to reset positions

        # If the difference changed, recalculate plate/beam z shifts
        # For example, let’s do a small shift per block difference.
        # Positive diff => left heavier => left side goes down, right goes up.

        shift_per_block = 0.01
        shift_amount = diff * shift_per_block

        # Plate1/beam1 go downward if diff>0, upward if diff<0
        # Plate3/beam2 do the opposite
        new_plate1_z = self._plate1_pose[2] - shift_amount
        new_beam1_z = self._beam1_pose[2] - shift_amount
        new_plate3_z = self._plate1_pose[2] + shift_amount
        new_beam2_z = self._beam2_pose[2] + shift_amount

        # Reset the base positions of each accordingly
        # (We keep the same x, y as their initial poses, but swap z.)
        plate1_id = self._plate1.id
        beam1_id = self._beam_ids[0]
        plate3_id = self._plate3.id
        beam2_id = self._beam_ids[1]

        # plate1: same x,y, updated z
        plate1_pos, plate1_orn = p.getBasePositionAndOrientation(
            plate1_id, physicsClientId=self._physics_client_id)
        new_plate1_pos = [plate1_pos[0], plate1_pos[1], new_plate1_z]
        p.resetBasePositionAndOrientation(
            plate1_id,
            new_plate1_pos,
            plate1_orn,
            physicsClientId=self._physics_client_id)

        # beam1
        beam1_pos, beam1_orn = p.getBasePositionAndOrientation(
            beam1_id, physicsClientId=self._physics_client_id)
        new_beam1_pos = [beam1_pos[0], beam1_pos[1], new_beam1_z]
        p.resetBasePositionAndOrientation(
            beam1_id,
            new_beam1_pos,
            beam1_orn,
            physicsClientId=self._physics_client_id)

        # plate3
        plate3_pos, plate3_orn = p.getBasePositionAndOrientation(
            plate3_id, physicsClientId=self._physics_client_id)
        new_plate3_pos = [plate3_pos[0], plate3_pos[1], new_plate3_z]
        p.resetBasePositionAndOrientation(
            plate3_id,
            new_plate3_pos,
            plate3_orn,
            physicsClientId=self._physics_client_id)

        # beam2
        beam2_pos, beam2_orn = p.getBasePositionAndOrientation(
            beam2_id, physicsClientId=self._physics_client_id)
        new_beam2_pos = [beam2_pos[0], beam2_pos[1], new_beam2_z]
        p.resetBasePositionAndOrientation(
            beam2_id,
            new_beam2_pos,
            beam2_orn,
            physicsClientId=self._physics_client_id)

        # Finally, record the new difference
        self._prev_diff = diff

    def _get_state(self) -> State:
        """Create a State based on the current PyBullet state.

        Note that in addition to the state inside PyBullet itself, this
        uses self._block_id_to_block and self._held_obj_id. As long as
        the PyBullet internal state is only modified through reset() and
        step(), these all should remain in sync.
        """
        state_dict = {}

        # Get robot state.
        rx, ry, rz, _, _, _, _, rf = self._pybullet_robot.get_state()
        fingers = self._fingers_joint_to_state(self._pybullet_robot, rf)
        state_dict[self._robot] = np.array([rx, ry, rz, fingers],
                                           dtype=np.float32)
        joint_positions = self._pybullet_robot.get_joints()

        # Get block states.
        for block_id, block in self._block_id_to_block.items():
            (bx, by, bz), _ = p.getBasePositionAndOrientation(
                block_id, physicsClientId=self._physics_client_id)
            held = (block_id == self._held_obj_id)
            visual_data = p.getVisualShapeData(
                block_id, physicsClientId=self._physics_client_id)[0]
            r, g, b, _ = visual_data[7]
            # pose_x, pose_y, pose_z, held
            state_dict[block] = np.array([bx, by, bz, held, r, g, b],
                                         dtype=np.float32)

        # Get machine state.
        button_color = p.getVisualShapeData(
            self._machine.id, physicsClientId=self._physics_client_id)[0][-1]
        button_color_on_dist = sum(
            np.subtract(button_color, self._button_color_on)**2)
        button_color_off_dist = sum(
            np.subtract(button_color, self._button_color_off)**2)
        machine_on = float(button_color_on_dist < button_color_off_dist)
        state_dict[self._machine] = np.array([machine_on], dtype=np.float32)

        # Get table state.
        state_dict[self._plate1] = np.array([], dtype=np.float32)
        # state_dict[self._table2] = np.array([], dtype=np.float32)
        state_dict[self._plate3] = np.array([], dtype=np.float32)

        state = utils.PyBulletState(state_dict,
                                    simulator_state=joint_positions)
        assert set(state) == set(self._current_state), \
            (f"Reconstructed state has objects {set(state)}, but "
             f"self._current_state has objects {set(self._current_state)}.")

        return state

    def step(self, action: Action, render_obs: bool = False) -> State:
        state = super().step(action, render_obs=render_obs)

        self._update_balance_beam(state)

        # Turn machine on
        if self._PressingButton_holds(state, [self._robot, self._machine]):
            if self._Balanced_holds(state, [self._plate1, self._plate3]):
                p.changeVisualShape(self._machine.id,
                                    -1,
                                    rgbaColor=self._button_color_on,
                                    physicsClientId=self._physics_client_id)
            self._current_observation = self._get_state()
            state = self._current_observation.copy()

        return state

    def _make_tasks(self, num_tasks: int, possible_num_blocks: List[int],
                   rng: np.random.Generator) -> List[EnvironmentTask]:
        tasks = super()._make_tasks(num_tasks, possible_num_blocks, rng)
        return self._add_pybullet_state_to_tasks(tasks)

    def _load_task_from_json(self, json_file: Path) -> EnvironmentTask:
        task = super()._load_task_from_json(json_file)
        return self._add_pybullet_state_to_tasks([task])[0]

    def _get_object_ids_for_held_check(self) -> List[int]:
        return sorted(self._block_id_to_block)

    def _force_grasp_object(self, block: Object) -> None:
        block_to_block_id = {b: i for i, b in self._block_id_to_block.items()}
        block_id = block_to_block_id[block]
        # The block should already be held. Otherwise, the position of the
        # block was wrong in the state.
        held_obj_id = self._detect_held_object()
        assert block_id == held_obj_id
        # Create the grasp constraint.
        self._held_obj_id = block_id
        self._create_grasp_constraint()

if __name__ == "__main__":
    """Run a simple simulation to test the environment."""
    import time

    # Make a task
    CFG.seed = 0
    CFG.num_train_tasks = 0
    CFG.num_test_tasks = 1
    env = PyBulletBalanceEnv(use_gui=True)
    task = env._generate_test_tasks()[0]
    env._reset_state(task.init)

    while True:
        # Robot does nothing
        action = Action(np.array(env._pybullet_robot.get_joints()))

        env.step(action)
        time.sleep(0.01)