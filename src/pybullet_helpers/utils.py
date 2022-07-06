from typing import TYPE_CHECKING, Collection, Iterator, List, Optional, \
    Sequence, Tuple

import numpy as np
import pybullet as p

from predicators.src import utils
from predicators.src.settings import CFG
from predicators.src.structs import Array, JointsState, Pose3D

_BASE_LINK = -1


def matrix_from_quat(quat: Sequence[float], physics_client_id: int) -> Array:
    return np.array(
        p.getMatrixFromQuaternion(quat,
                                  physicsClientId=physics_client_id)).reshape(
                                      3, 3)


def get_pose(body: int,
             physics_client_id: int) -> Tuple[Pose3D, Sequence[float]]:
    return p.getBasePositionAndOrientation(body,
                                           physicsClientId=physics_client_id)


def get_link_from_name(body: int, name: str, physics_client_id: int) -> int:
    """Get the link ID from the name of the link."""
    base_info = p.getBodyInfo(body, physicsClientId=physics_client_id)
    base_name = base_info[0].decode(encoding="UTF-8")
    if name == base_name:
        return -1  # base link
    for link in range(p.getNumJoints(body, physicsClientId=physics_client_id)):
        joint_info = p.getJointInfo(body,
                                    link,
                                    physicsClientId=physics_client_id)
        joint_name = joint_info[12].decode("UTF-8")
        if joint_name == name:
            return link
    raise ValueError(f"Body {body} has no link with name {name}.")


def get_link_pose(body: int, link: int,
                  physics_client_id: int) -> Tuple[Pose3D, Sequence[float]]:
    """Get the position and orientation for a link."""
    if link == _BASE_LINK:
        return get_pose(body, physics_client_id)
    link_state = p.getLinkState(body, link, physicsClientId=physics_client_id)
    return link_state[0], link_state[1]


def get_relative_link_pose(
        body: int, link1: int, link2: int,
        physics_client_id: int) -> Tuple[Pose3D, Sequence[float]]:
    """Get the pose of one link relative to another link on the same body."""
    # X_WL1
    world_from_link1 = get_link_pose(body, link1, physics_client_id)
    # X_WL2
    world_from_link2 = get_link_pose(body, link2, physics_client_id)
    # X_L2L1 = (X_WL2)^-1 * (X_WL1)
    link2_from_link1 = p.multiplyTransforms(
        *p.invertTransform(*world_from_link2), *world_from_link1)
    return link2_from_link1


def get_kinematic_chain(robot: int, end_effector: int,
                        physics_client_id: int) -> List[int]:
    """Get all of the free joints from robot base to end effector.

    Includes the end effector.
    """
    kinematic_chain = []
    while end_effector > -1:
        joint_info = p.getJointInfo(robot,
                                    end_effector,
                                    physicsClientId=physics_client_id)
        if joint_info[3] > -1:
            kinematic_chain.append(end_effector)
        end_effector = joint_info[-1]
    return kinematic_chain


def pybullet_inverse_kinematics(
    robot: int,
    end_effector: int,
    target_position: Pose3D,
    target_orientation: Sequence[float],
    joints: Sequence[int],
    physics_client_id: int,
    validate: bool = True,
) -> JointsState:
    """Runs IK and returns a joints state for the given (free) joints.

    If validate is True, the PyBullet IK solver is called multiple
    times, resetting the robot state each time, until the target
    position is reached. If the target position is not reached after a
    maximum number of iters, an exception is raised.
    """
    # Figure out which joint each dimension of the return of IK corresponds to.
    free_joints = []
    num_joints = p.getNumJoints(robot, physicsClientId=physics_client_id)
    for idx in range(num_joints):
        joint_info = p.getJointInfo(robot,
                                    idx,
                                    physicsClientId=physics_client_id)
        if joint_info[3] > -1:
            free_joints.append(idx)
    assert set(joints).issubset(set(free_joints))

    # Record the initial state of the joints so that we can reset them after.
    if validate:
        initial_joints_states = p.getJointStates(
            robot, free_joints, physicsClientId=physics_client_id)
        assert len(initial_joints_states) == len(free_joints)

    # Running IK once is often insufficient, so we run it multiple times until
    # convergence. If it does not converge, an error is raised.
    convergence_tol = CFG.pybullet_ik_tol
    for _ in range(CFG.pybullet_max_ik_iters):
        free_joint_vals = p.calculateInverseKinematics(
            robot,
            end_effector,
            target_position,
            targetOrientation=target_orientation,
            physicsClientId=physics_client_id,
        )
        assert len(free_joints) == len(free_joint_vals)
        if not validate:
            break
        # Update the robot state and check if the desired position and
        # orientation are reached.
        for joint, joint_val in zip(free_joints, free_joint_vals):
            p.resetJointState(robot,
                              joint,
                              targetValue=joint_val,
                              physicsClientId=physics_client_id)
        # TODO can this be replaced with get_link_pose?
        ee_link_state = p.getLinkState(
            robot,
            end_effector,
            computeForwardKinematics=True,
            physicsClientId=physics_client_id,
        )
        position = ee_link_state[4]
        # Note: we are checking positions only for convergence.
        if np.allclose(position, target_position, atol=convergence_tol):
            break
    else:
        raise Exception("Inverse kinematics failed to converge.")

    # Reset the joint states to their initial values to avoid modifying the
    # PyBullet internal state.
    if validate:
        for joint, (pos, vel, _, _) in zip(free_joints, initial_joints_states):
            p.resetJointState(
                robot,
                joint,
                targetValue=pos,
                targetVelocity=vel,
                physicsClientId=physics_client_id,
            )
    # Order the found free_joint_vals based on the requested joints.
    joint_vals = []
    for joint in joints:
        free_joint_idx = free_joints.index(joint)
        joint_val = free_joint_vals[free_joint_idx]
        joint_vals.append(joint_val)

    return joint_vals


def run_motion_planning(
    robot: "SingleArmPyBulletRobot",
    initial_state: JointsState,
    target_state: JointsState,
    collision_bodies: Collection[int],
    seed: int,
    physics_client_id: int,
) -> Optional[Sequence[JointsState]]:
    """Run BiRRT to find a collision-free sequence of joint states.

    Note that this function changes the state of the robot.
    """
    rng = np.random.default_rng(seed)
    joint_space = robot.action_space
    joint_space.seed(seed)
    _sample_fn = lambda _: joint_space.sample()
    num_interp = CFG.pybullet_birrt_extend_num_interp

    def _extend_fn(pt1: JointsState,
                   pt2: JointsState) -> Iterator[JointsState]:
        pt1_arr = np.array(pt1)
        pt2_arr = np.array(pt2)
        num = int(np.ceil(max(abs(pt1_arr - pt2_arr)))) * num_interp
        if num == 0:
            yield pt2
        for i in range(1, num + 1):
            yield list(pt1_arr * (1 - i / num) + pt2_arr * i / num)

    def _collision_fn(pt: JointsState) -> bool:
        robot.set_joints(pt)
        p.performCollisionDetection(physicsClientId=physics_client_id)
        for body in collision_bodies:
            if p.getContactPoints(robot.robot_id,
                                  body,
                                  physicsClientId=physics_client_id):
                return True
        return False

    def _distance_fn(from_pt: JointsState, to_pt: JointsState) -> float:
        from_ee = robot.forward_kinematics(from_pt)
        to_ee = robot.forward_kinematics(to_pt)
        return sum(np.subtract(from_ee, to_ee)**2)

    birrt = utils.BiRRT(
        _sample_fn,
        _extend_fn,
        _collision_fn,
        _distance_fn,
        rng,
        num_attempts=CFG.pybullet_birrt_num_attempts,
        num_iters=CFG.pybullet_birrt_num_iters,
        smooth_amt=CFG.pybullet_birrt_smooth_amt,
    )

    return birrt.query(initial_state, target_state)
