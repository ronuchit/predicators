from typing import Optional

from predicators.src import utils
from predicators.src.pybullet_helpers.ikfast import IKFastInfo
from predicators.src.pybullet_helpers.robots.single_arm import SingleArmPyBulletRobot


class PandaPyBulletRobot(SingleArmPyBulletRobot):
    """Franka Emika Panda which we assume is fixed on some base."""

    @classmethod
    def get_name(cls) -> str:
        return "panda"

    @classmethod
    def urdf_path(cls) -> str:
        return utils.get_env_asset_path(
            "urdf/franka_description/robots/panda_arm_hand.urdf"
        )

    @property
    def end_effector_name(self) -> str:
        # TODO explain or change this
        return "tool_joint"

    @property
    def tool_link_name(self) -> str:
        return "tool_link"

    @property
    def left_finger_joint_name(self) -> str:
        return "panda_finger_joint1"

    @property
    def right_finger_joint_name(self) -> str:
        return "panda_finger_joint2"

    @property
    def open_fingers(self) -> float:
        return 0.04

    @property
    def closed_fingers(self) -> float:
        return 0.03

    @classmethod
    def ikfast_info(cls) -> Optional[IKFastInfo]:
        return IKFastInfo(
            module_dir="panda_arm",
            module_name="ikfast_panda_arm",
            base_link="panda_link0",
            ee_link="panda_link8",
            free_joints=["panda_joint7"],
        )
