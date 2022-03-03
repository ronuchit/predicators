"""A PyBullet version of Blocks."""

from typing import Sequence, Tuple, Optional
from gym.spaces import Box
import numpy as np
import pybullet as p
from predicators.src.envs.blocks import BlocksEnv
from predicators.src.structs import State, Action
from predicators.src import utils
from predicators.src.pybullet_utils import get_kinematic_chain, \
    inverse_kinematics
from predicators.src.settings import CFG


class PyBulletBlocksEnv(BlocksEnv):
    """PyBullet Blocks domain."""
    # Parameters that aren't important enough to need to clog up settings.py

    # Fetch robot parameters.
    _base_position: Sequence[float] = [0.75, 0.7441, 0.0]
    _base_orientation: Sequence[float] = [0., 0., 0., 1.]
    _ee_orientation: Sequence[float] = [1., 0., -1., 0.]
    _ee_initial_position: Sequence[float] = [1., 0.7, 0.5]

    # Table parameters.
    _table_position: Sequence[float] = [1.35, 0.75, 0.0]
    _table_orientation: Sequence[float] = [0., 0., 0., 1.]

    # Block parameters.
    _block_orientation: Sequence[float] = [0., 0., 0., 1.]
    _block_mass = 0.04
    _block_friction = 1.2
    _block_colors: Sequence[Tuple[float, float, float, float]] = [
        (0.95, 0.05, 0.1, 1.),
        (0.05, 0.95, 0.1, 1.),
        (0.1, 0.05, 0.95, 1.),
        (0.4, 0.05, 0.6, 1.),
        (0.6, 0.4, 0.05, 1.),
        (0.05, 0.04, 0.6, 1.),
        (0.95, 0.95, 0.1, 1.),
        (0.95, 0.05, 0.95, 1.),
        (0.05, 0.95, 0.95, 1.),
    ]
    _out_of_view_xy: Sequence[float] = [10.0, 10.0]

    # Camera parameters.
    _camera_distance: float = 1.5
    _yaw: float = 90.0
    _pitch: float = -24
    _camera_target: Sequence[float] = [1.65, 0.75, 0.42]

    def __init__(self) -> None:
        super().__init__()

        # One-time initialization of pybullet assets. Note that this happens
        # in __init__ because many class attributes are created.
        if CFG.pybullet_use_gui:
            self._physics_client_id = p.connect(p.GUI)
            p.resetDebugVisualizerCamera(
                self._camera_distance,
                self._yaw,
                self._pitch,
                self._camera_target,
                physicsClientId=self._physics_client_id)
        else:
            self._physics_client_id = p.connect(p.DIRECT)

        p.resetSimulation(physicsClientId=self._physics_client_id)

        # Load plane.
        p.loadURDF(utils.get_env_asset_path("urdf/plane.urdf"), [0, 0, -1],
                   useFixedBase=True,
                   physicsClientId=self._physics_client_id)

        # Load Fetch robot.
        self._fetch_id = p.loadURDF(
            utils.get_env_asset_path("urdf/robots/fetch.urdf"),
            useFixedBase=True,
            physicsClientId=self._physics_client_id)
        p.resetBasePositionAndOrientation(
            self._fetch_id,
            self._base_position,
            self._base_orientation,
            physicsClientId=self._physics_client_id)

        # Extract IDs for individual robot links and joints.
        joint_names = [
            p.getJointInfo(
                self._fetch_id, i,
                physicsClientId=self._physics_client_id)[1].decode("utf-8")
            for i in range(
                p.getNumJoints(self._fetch_id,
                               physicsClientId=self._physics_client_id))
        ]
        self._ee_id = joint_names.index('gripper_axis')
        self._arm_joints = get_kinematic_chain(
            self._fetch_id,
            self._ee_id,
            physics_client_id=self._physics_client_id)
        self._left_finger_id = joint_names.index("l_gripper_finger_joint")
        self._right_finger_id = joint_names.index("r_gripper_finger_joint")
        self._arm_joints.append(self._left_finger_id)
        self._arm_joints.append(self._right_finger_id)

        # Load table.
        self._table_id = p.loadURDF(
            utils.get_env_asset_path("urdf/table.urdf"),
            useFixedBase=True,
            physicsClientId=self._physics_client_id)
        p.resetBasePositionAndOrientation(
            self._table_id,
            self._table_position,
            self._table_orientation,
            physicsClientId=self._physics_client_id)

        # Draw the workspace on the table for clarity.
        p.addUserDebugLine([self.x_lb, self.y_lb, self.table_height],
                           [self.x_ub, self.y_lb, self.table_height],
                           [1.0, 0.0, 0.0],
                           lineWidth=5.0)
        p.addUserDebugLine([self.x_lb, self.y_ub, self.table_height],
                           [self.x_ub, self.y_ub, self.table_height],
                           [1.0, 0.0, 0.0],
                           lineWidth=5.0)
        p.addUserDebugLine([self.x_lb, self.y_lb, self.table_height],
                           [self.x_lb, self.y_ub, self.table_height],
                           [1.0, 0.0, 0.0],
                           lineWidth=5.0)
        p.addUserDebugLine([self.x_ub, self.y_lb, self.table_height],
                           [self.x_ub, self.y_ub, self.table_height],
                           [1.0, 0.0, 0.0],
                           lineWidth=5.0)

        # Draw coordinate frame labels for reference.
        p.addUserDebugText("x", [0.25, 0, 0], [0.0, 0.0, 0.0])
        p.addUserDebugText("y", [0, 0.25, 0], [0.0, 0.0, 0.0])
        p.addUserDebugText("z", [0, 0, 0.25], [0.0, 0.0, 0.0])

        # Set gravity.
        p.setGravity(0., 0., -10., physicsClientId=self._physics_client_id)

        # Determine good initial joint values.
        self._initial_joint_values = inverse_kinematics(
            self._fetch_id,
            self._ee_id,
            self._ee_initial_position,
            self._ee_orientation,
            self._arm_joints,
            physics_client_id=self._physics_client_id)

        # Create blocks. Note that we create the maximum number once, and then
        # remove blocks from view based on the number involved in the state.
        num_blocks = max(max(self.num_blocks_train), max(self.num_blocks_test))
        self._block_ids = [self._create_block(i) for i in range(num_blocks)]

        # When a block is held, a constraint is created to prevent slippage.
        self._held_constraint_id: Optional[int] = None

    @property
    def action_space(self) -> Box:
        # dimensions: [dx, dy, dz, fingers]
        return Box(low=-1, high=1, shape=(4, ), dtype=np.float32)

    def reset(self, train_or_test: str, task_idx: int) -> State:
        # Resets current_state and current_task.
        state = super().reset(train_or_test, task_idx)

        # Tear down the old PyBullet scene.
        if self._held_constraint_id is not None:
            p.removeConstraint(self._held_constraint_id,
                               physicsClientId=self._physics_client_id)
            self._held_constraint_id = None

        p.resetBasePositionAndOrientation(
            self._fetch_id,
            self._base_position,
            self._base_orientation,
            physicsClientId=self._physics_client_id)

        for joint_idx, joint_val in zip(self._arm_joints,
                                        self._initial_joint_values):
            p.resetJointState(self._fetch_id,
                              joint_idx,
                              joint_val,
                              physicsClientId=self._physics_client_id)

        # Prevent collisions between robot and blocks during scene init.
        up_action = Action(np.array([-0.5, -0.5, 0.5, 0.0], dtype=np.float32))
        for _ in range(10):
            self.step(up_action)

        # Reset block positions based on the state.
        block_objs = list(o for o in state if o.type == self._block_type)
        for i, block_obj in enumerate(block_objs):
            block_id = self._block_ids[i]
            x, y, z, held = state[block_obj]
            assert held < 1.0  # not holding in the initial state
            p.resetBasePositionAndOrientation(
                block_id, [x, y, z],
                self._block_orientation,
                physicsClientId=self._physics_client_id)

        # For any blocks not involved, put them out of view.
        h = self.block_size
        oov_x, oov_y = self._out_of_view_xy
        for i in range(len(block_objs), len(self._block_ids)):
            block_id = self._block_ids[i]
            p.resetBasePositionAndOrientation(
                block_id, [oov_x, oov_y, i * h],
                self._block_orientation,
                physicsClientId=self._physics_client_id)

        while True:
            p.stepSimulation(physicsClientId=self._physics_client_id)

        return state

    def _create_block(self, block_num: int) -> int:
        """Returns the body ID."""
        color = self._block_colors[block_num % len(self._block_colors)]

        # The positions here are not important because they are overwritten by
        # the state values when a task is reset. By default, we just stack all
        # the blocks into one pile at the center of the table so we can see.
        h = self.block_size
        x = (self.x_lb + self.x_ub) / 2
        y = (self.y_lb + self.y_ub) / 2
        z = self.table_height + (0.5 * h) + (h * block_num)
        position = [x, y, z]

        # Create the collision shape.
        half_extents = [self.block_size / 2.] * 3
        collision_id = p.createCollisionShape(
            p.GEOM_BOX,
            halfExtents=half_extents,
            physicsClientId=self._physics_client_id)

        # Create the visual_shape.
        visual_id = p.createVisualShape(
            p.GEOM_BOX,
            halfExtents=half_extents,
            rgbaColor=color,
            physicsClientId=self._physics_client_id)

        # Create the body.
        block_id = p.createMultiBody(baseMass=self._block_mass,
                                     baseCollisionShapeIndex=collision_id,
                                     baseVisualShapeIndex=visual_id,
                                     basePosition=position,
                                     baseOrientation=self._block_orientation,
                                     physicsClientId=self._physics_client_id)
        p.changeDynamics(block_id,
                         -1,
                         lateralFriction=self._block_friction,
                         physicsClientId=self._physics_client_id)

        return block_id
