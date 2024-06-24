"""Generic controllers for the robots."""
import logging
from typing import Callable, Dict, Sequence, Set, Tuple, cast

import numpy as np
from gym.spaces import Box

from predicators import utils
from predicators.pybullet_helpers.geometry import Pose
from predicators.pybullet_helpers.inverse_kinematics import \
    InverseKinematicsError
from predicators.pybullet_helpers.robots.single_arm import \
    SingleArmPyBulletRobot
from predicators.structs import Action, Array, Object, ParameterizedOption, \
    State, Type
from predicators.pybullet_helpers.motion_planning import run_motion_planning

_SUPPORTED_ROBOTS: Set[str] = {"fetch", "panda"}


class MotionPlanController:
    def __init__(self, bodies, physics_client_id):
        self.motion_plan = []
        self.bodies = bodies
        self.physics_client_id = physics_client_id
        self.planned = False

    def _create_motion_plan(self, initial_joint_positions, final_joint_positions,
                            pybullet_robot: SingleArmPyBulletRobot):
        body_ids = []
        # logging.info(bodies)
        for body in self.bodies:
            if type(self.bodies[body]) is list:
                for b in self.bodies[body]:
                    body_ids.append(b)
            else:
                body_ids.append(self.bodies[body])
        assert pybullet_robot is not None
        assert initial_joint_positions is not None
        assert final_joint_positions is not None
        self.motion_plan = run_motion_planning(pybullet_robot,
                                               initial_joint_positions,
                                               final_joint_positions,
                                               collision_bodies=body_ids,
                                               seed=0,
                                               physics_client_id=self.physics_client_id)[1:]

    def get_next_state_in_plan(self):
        if len(self.motion_plan) > 0:
            return self.motion_plan.pop(0)
        return None


    def create_move_end_effector_to_pose_option(
            self,
            robot: SingleArmPyBulletRobot,
            name: str,
            types: Sequence[Type],
            params_space: Box,
            get_current_and_target_pose_and_finger_status: Callable[
                [State, Sequence[Object], Array], Tuple[Pose, Pose, str]],
            move_to_pose_tol: float,
            max_vel_norm: float,
            finger_action_nudge_magnitude: float,
            physics_client_id,
            bodies,
    ) -> ParameterizedOption:
        """A generic utility that creates a ParameterizedOption for moving the end
        effector to a target pose, given a function that takes in the current
        state, objects, and parameters, and returns the current pose and target
        pose of the end effector, and the finger status."""

        robot_name = robot.get_name()
        assert robot_name in _SUPPORTED_ROBOTS, (
                "Move end effector to pose option " +
                f"not implemented for robot {robot_name}.")

        def _policy(state: State, memory: Dict, objects: Sequence[Object],
                    params: Array) -> Action:
            del memory  # unused
            # Sync the joints.
            assert isinstance(state, utils.PyBulletState)
            robot.set_joints(state.joint_positions)
            # First handle the main arm joints.
            current_pose, target_pose, finger_status = \
                get_current_and_target_pose_and_finger_status(
                    state, objects, params)

            # This option currently assumes a fixed end effector orientation.

            # assert np.allclose(current_pose.orientation, target_pose.orientation)

            # orn = current_pose.orientation
            # current = current_pose.position
            #
            # target = target_pose.position
            # target_orn = target_pose.orientation  # Added by Quincy

            # # Run IK to determine the target joint positions.
            # ee_delta = np.subtract(target, current)
            # # Reduce the target to conform to the max velocity constraint.
            # ee_norm = np.linalg.norm(ee_delta)
            # if ee_norm > max_vel_norm:
            #     ee_delta = ee_delta * max_vel_norm / ee_norm
            # dx, dy, dz = np.add(current, ee_delta)
            # ee_action = Pose((dx, dy, dz), target_orn)

            # Keep validate as False because validate=True would update the
            # state of the robot during simulation, which overrides physics.

            try:
                # For the panda, always set the joints after running IK because
                # IKFast is very sensitive to initialization, and it's easier to
                # find good solutions on subsequent calls if we are already near
                # a solution from the previous call. The fetch robot does not
                # use IKFast, and in fact gets screwed up if we set joints here.
                initial_joint_positions = robot.inverse_kinematics(current_pose,
                                                                   validate=False,
                                                                   set_joints=True)

                target_joint_positions = robot.inverse_kinematics(target_pose,
                                                                  validate=False,
                                                                  set_joints=False)
                if len(self.motion_plan) == 0:
                    self._create_motion_plan(initial_joint_positions, target_joint_positions, robot)
                    logging.info("creating motion plan")
                logging.info(self.motion_plan)

                # logging.info(f'initial_joint_positions: {initial_joint_positions}')
                #logging.info(f'state: {state}')

            except InverseKinematicsError:
                self.planned = False
                self.motion_plan = []
                logging.info("IK FAIL DURING MOTION PLAN")
                raise utils.OptionExecutionFailure("Inverse kinematics failed.")


            # Handle the fingers. Fingers drift if left alone.
            # When the fingers are not explicitly being opened or closed, we
            # nudge the fingers toward being open or closed according to the
            # finger status.

            joint_positions = self.get_next_state_in_plan()
            robot.set_joints(joint_positions)

            if finger_status == "open":
                finger_delta = finger_action_nudge_magnitude
            else:
                assert finger_status == "closed"
                finger_delta = -finger_action_nudge_magnitude
            # Extract the current finger state.
            state = cast(utils.PyBulletState, state)
            finger_position = state.joint_positions[robot.left_finger_joint_idx]
            # The finger action is an absolute joint position for the fingers.
            f_action = finger_position + finger_delta
            # Override the meaningless finger values in joint_action.
            joint_positions[robot.left_finger_joint_idx] = f_action
            joint_positions[robot.right_finger_joint_idx] = f_action
            action_arr = np.array(joint_positions, dtype=np.float32)
            # This clipping is needed sometimes for the joint limits.
            action_arr = np.clip(action_arr, robot.action_space.low,
                                 robot.action_space.high)
            assert robot.action_space.contains(action_arr)
            return Action(action_arr)

        def _terminal(state: State, memory: Dict, objects: Sequence[Object],
                      params: Array) -> bool:
            del memory  # unused
            current_pose, target_pose, _ = \
                get_current_and_target_pose_and_finger_status(
                    state, objects, params)
            # This option currently assumes a fixed end effector orientation.
            assert np.allclose(current_pose.orientation, target_pose.orientation)
            current = current_pose.position
            target = target_pose.position
            squared_dist = np.sum(np.square(np.subtract(current, target)))
            if squared_dist < move_to_pose_tol:
                self.planned = False
                self.motion_plan = []
            if squared_dist < move_to_pose_tol:
                logging.info("Motion plan complete")
            return squared_dist < move_to_pose_tol

        return ParameterizedOption(name,
                                   types=types,
                                   params_space=params_space,
                                   policy=_policy,
                                   initiable=lambda _1, _2, _3, _4: True,
                                   terminal=_terminal)

#
# def create_move_end_effector_to_pose_option(
#         self,
#         robot: SingleArmPyBulletRobot,
#         name: str,
#         types: Sequence[Type],
#         params_space: Box,
#         get_current_and_target_pose_and_finger_status: Callable[
#             [State, Sequence[Object], Array], Tuple[Pose, Pose, str]],
#         move_to_pose_tol: float,
#         max_vel_norm: float,
#         finger_action_nudge_magnitude: float,
#         physics_client_id,
#         bodies,
# ) -> ParameterizedOption:
#     """A generic utility that creates a ParameterizedOption for moving the end
#     effector to a target pose, given a function that takes in the current
#     state, objects, and parameters, and returns the current pose and target
#     pose of the end effector, and the finger status."""
#
#     robot_name = robot.get_name()
#     assert robot_name in _SUPPORTED_ROBOTS, (
#             "Move end effector to pose option " +
#             f"not implemented for robot {robot_name}.")
#
#     def _policy(state: State, memory: Dict, objects: Sequence[Object],
#                 params: Array) -> Action:
#         del memory  # unused
#         # Sync the joints.
#
#         assert isinstance(state, utils.PyBulletState)
#         robot.set_joints(state.joint_positions)
#         # First handle the main arm joints.
#         current_pose, target_pose, finger_status = \
#             get_current_and_target_pose_and_finger_status(
#                 state, objects, params)
#
#         # This option currently assumes a fixed end effector orientation.
#
#         # assert np.allclose(current_pose.orientation, target_pose.orientation)
#
#         # orn = current_pose.orientation
#         # current = current_pose.position
#         #
#         # target = target_pose.position
#         # target_orn = target_pose.orientation  # Added by Quincy
#
#         # # Run IK to determine the target joint positions.
#         # ee_delta = np.subtract(target, current)
#         # # Reduce the target to conform to the max velocity constraint.
#         # ee_norm = np.linalg.norm(ee_delta)
#         # if ee_norm > max_vel_norm:
#         #     ee_delta = ee_delta * max_vel_norm / ee_norm
#         # dx, dy, dz = np.add(current, ee_delta)
#         # ee_action = Pose((dx, dy, dz), target_orn)
#
#         # Keep validate as False because validate=True would update the
#         # state of the robot during simulation, which overrides physics.
#
#         try:
#             # For the panda, always set the joints after running IK because
#             # IKFast is very sensitive to initialization, and it's easier to
#             # find good solutions on subsequent calls if we are already near
#             # a solution from the previous call. The fetch robot does not
#             # use IKFast, and in fact gets screwed up if we set joints here.
#             initial_joint_positions = robot.inverse_kinematics(current_pose,
#                                                                validate=False,
#                                                                set_joints=False)
#
#             target_joint_positions = robot.inverse_kinematics(target_pose,
#                                                               validate=False,
#                                                               set_joints=False)
#
#             if not self.motion_plan_created:
#                 self._create_motion_plan(initial_joint_positions, target_joint_positions, robot)
#
#             # logging.info(f'initial_joint_positions: {initial_joint_positions}')
#             # logging.info(f'motion_plan: {motion_plan}')
#
#         except InverseKinematicsError:
#             raise utils.OptionExecutionFailure("Inverse kinematics failed.")
#
#         # Handle the fingers. Fingers drift if left alone.
#         # When the fingers are not explicitly being opened or closed, we
#         # nudge the fingers toward being open or closed according to the
#         # finger status.
#
#         joint_positions = self.get_next_state_in_plan()
#         robot.set_joints(joint_positions)
#
#         if finger_status == "open":
#             finger_delta = finger_action_nudge_magnitude
#         else:
#             assert finger_status == "closed"
#             finger_delta = -finger_action_nudge_magnitude
#         # Extract the current finger state.
#         state = cast(utils.PyBulletState, state)
#         finger_position = state.joint_positions[robot.left_finger_joint_idx]
#         # The finger action is an absolute joint position for the fingers.
#         f_action = finger_position + finger_delta
#         # Override the meaningless finger values in joint_action.
#         joint_positions[robot.left_finger_joint_idx] = f_action
#         joint_positions[robot.right_finger_joint_idx] = f_action
#         action_arr = np.array(joint_positions, dtype=np.float32)
#         # This clipping is needed sometimes for the joint limits.
#         action_arr = np.clip(action_arr, robot.action_space.low,
#                              robot.action_space.high)
#         assert robot.action_space.contains(action_arr)
#         return Action(action_arr)
#
#     def _terminal(state: State, memory: Dict, objects: Sequence[Object],
#                   params: Array) -> bool:
#         del memory  # unused
#         current_pose, target_pose, _ = \
#             get_current_and_target_pose_and_finger_status(
#                 state, objects, params)
#         # This option currently assumes a fixed end effector orientation.
#         assert np.allclose(current_pose.orientation, target_pose.orientation)
#         current = current_pose.position
#         target = target_pose.position
#         squared_dist = np.sum(np.square(np.subtract(current, target)))
#         return squared_dist < move_to_pose_tol
#
#     return ParameterizedOption(name,
#                                types=types,
#                                params_space=params_space,
#                                policy=_policy,
#                                initiable=lambda _1, _2, _3, _4: True,
#                                terminal=_terminal)


def create_change_fingers_option(
        robot: SingleArmPyBulletRobot,
        name: str,
        types: Sequence[Type],
        params_space: Box,
        get_current_and_target_val: Callable[[State, Sequence[Object], Array],
        Tuple[float, float]],
        max_vel_norm: float,
        grasp_tol: float,
) -> ParameterizedOption:
    """A generic utility that creates a ParameterizedOption for changing the
    robot fingers, given a function that takes in the current state, objects,
    and parameters, and returns the current and target finger joint values."""

    assert robot.get_name() in _SUPPORTED_ROBOTS, (
            "Change fingers option not " +
            f"implemented for robot {robot.get_name()}.")

    def _policy(state: State, memory: Dict, objects: Sequence[Object],
                params: Array) -> Action:
        del memory  # unused
        current_val, target_val = get_current_and_target_val(
            state, objects, params)
        f_delta = target_val - current_val
        f_delta = np.clip(f_delta, -max_vel_norm, max_vel_norm)
        f_action = current_val + f_delta
        # Don't change the rest of the joints.
        state = cast(utils.PyBulletState, state)
        target = np.array(state.joint_positions, dtype=np.float32)
        target[robot.left_finger_joint_idx] = f_action
        target[robot.right_finger_joint_idx] = f_action
        # This clipping is needed sometimes for the joint limits.
        target = np.clip(target, robot.action_space.low,
                         robot.action_space.high)
        assert robot.action_space.contains(target)
        return Action(target)

    def _terminal(state: State, memory: Dict, objects: Sequence[Object],
                  params: Array) -> bool:
        del memory  # unused
        current_val, target_val = get_current_and_target_val(
            state, objects, params)
        squared_dist = (target_val - current_val) ** 2
        return squared_dist < grasp_tol

    return ParameterizedOption(name,
                               types=types,
                               params_space=params_space,
                               policy=_policy,
                               initiable=lambda _1, _2, _3, _4: True,
                               terminal=_terminal)
